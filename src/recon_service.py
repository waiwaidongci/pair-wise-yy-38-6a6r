"""对账用例编排：上报仲裁、指令重算、关闭资格核对、补传续传与审计。"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .domain import ConflictError, ensure_role, require_text
from .recon_domain import (CANON_CONFIRMED, INBOX_ACCEPTED,
                           INBOX_DUPLICATE, INBOX_PENDING, INBOX_REJECTED,
                           INBOX_SUPERSEDED,
                           METRIC_GATE_FLOW, METRIC_GATE_POSITION,
                           METRIC_WATER_LEVEL,
                           RECEIPT_METRICS, SOURCE_DEVICE, SOURCE_MANUAL,
                           WATER_METRICS, normalize_field_time,
                           normalize_metric, normalize_point, normalize_seq,
                           normalize_source, parse_observation)
from .recon_repository import ReconRepository
from .recon_rules import (closure_qualification, desired_command,
                          recalc_decision, reconcile)
from .repository import Repository

# 复用既有角色矩阵
VIEW_ROLES = {"duty_officer", "chief_engineer", "dispatcher", "viewer"}
SUBMIT_ROLES = {"duty_officer", "dispatcher"}
CONFIRM_ROLES = {"chief_engineer", "duty_officer"}
AUDIT_ROLES = {"chief_engineer", "viewer"}

ENTITY_OBS = "recon_observation"
ENTITY_CMD = "recon_command"
ENTITY_POINT = "recon_point"
ENTITY_BATCH = "recon_batch"
ENTITY_CLOSURE = "recon_closure"


class ReconService:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.recon = ReconRepository(repository.conn, repository._lock)
        # 同一测点人工并发提交互斥；锁本身只做持久化占位与诊断
        self._point_locks: Dict[str, threading.Lock] = {}
        self._points_guard = threading.Lock()

    def _audit(self, action: str, entity_type: str, entity_id: int,
               actor: str, detail: Dict[str, Any]) -> None:
        self.repository.append_audit(action, entity_type, entity_id, actor, detail)

    # ------------------------------------------------------------------ points
    def register_point(self, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        point = normalize_point(payload.get("point"))
        title = require_text(payload.get("title"), "title", 200)
        from .domain import require_number
        threshold = require_number(payload.get("open_threshold"), "open_threshold")
        saved = self.recon.register_point(point, title, threshold, actor)
        self._audit("recon.point_register", ENTITY_POINT, 0, actor,
                    {"point": point, "open_threshold": threshold})
        return saved

    def list_points(self, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return self.recon.list_points()

    # -------------------------------------------------------------- ingest core
    def _apply_observation(self, obs: Dict[str, Any], batch_ref: Optional[str],
                           actor: str) -> Dict[str, Any]:
        """对一条已校验上报做仲裁落账，返回处理结果描述（不负责并发锁）。"""
        canonical = self.recon.get_canonical(
            obs["point"], obs["metric"], obs["field_time"])
        verdict = reconcile(canonical, obs) if canonical else "accept"

        if verdict == "accept" and canonical is not None:
            # 同来源同现场时间/序列：命中唯一约束即幂等重复，不重复落账
            obs_id, inserted = self.recon.insert_observation(
                obs, INBOX_ACCEPTED, batch_ref, actor)
            return {"outcome": "accept" if inserted else "duplicate",
                    "observation_id": obs_id}

        if verdict == "accept":
            obs_id, inserted = self.recon.insert_observation(
                obs, INBOX_ACCEPTED, batch_ref, actor)
            if not inserted:
                return {"outcome": "duplicate", "observation_id": obs_id}
            self.recon.upsert_canonical(obs["point"], obs["metric"],
                                        obs["field_time"], {"id": obs_id, **obs})
            self._audit("recon.observe", ENTITY_OBS, obs_id, actor, {
                "point": obs["point"], "metric": obs["metric"],
                "field_time": obs["field_time"], "source": obs["source"],
                "value": obs["value"], "outcome": "accept",
                "batch_ref": batch_ref})
            return {"outcome": "accept", "observation_id": obs_id}

        if verdict == "replace":
            # 设备值优先取代暂定人工值：人工异常原因保留
            new_id, inserted = self.recon.insert_observation(
                obs, INBOX_ACCEPTED, batch_ref, actor)
            if not inserted:
                return {"outcome": "duplicate", "observation_id": new_id}
            old_obs_id = canonical["observation_id"]
            self.recon.mark_observation(old_obs_id, INBOX_SUPERSEDED, new_id)
            self.recon.update_canonical_value(canonical["id"],
                                              {"id": new_id, **obs})
            self.recon.merge_canonical_anomaly(canonical["id"], obs.get("anomaly_note"))
            self._audit("recon.observe", ENTITY_OBS, new_id, actor, {
                "point": obs["point"], "metric": obs["metric"],
                "field_time": obs["field_time"], "source": obs["source"],
                "value": obs["value"], "outcome": "replace",
                "superseded_observation_id": old_obs_id,
                "preserved_anomaly": canonical.get("anomaly_note"),
                "batch_ref": batch_ref})
            return {"outcome": "replace", "observation_id": new_id,
                    "superseded": old_obs_id}

        # lock_pending（已确认锁定）或 pending_review（同级/低级冲突）→ 留待核
        outcome = "locked" if verdict == "lock_pending" else "pending_review"
        obs_id, inserted = self.recon.insert_observation(
            obs, INBOX_PENDING, batch_ref, actor)
        if not inserted:
            return {"outcome": "duplicate", "observation_id": obs_id}
        # 即便留待核，异常原因也不丢弃：合并到规范值备注并保留在上报行
        self.recon.merge_canonical_anomaly(canonical["id"], obs.get("anomaly_note"))
        self._audit("recon.observe", ENTITY_OBS, obs_id, actor, {
            "point": obs["point"], "metric": obs["metric"],
            "field_time": obs["field_time"], "source": obs["source"],
            "value": obs["value"], "outcome": outcome,
            "canonical_observation_id": canonical["observation_id"],
            "batch_ref": batch_ref})
        return {"outcome": outcome, "observation_id": obs_id}

    def ingest_observation(self, payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        obs = parse_observation(payload)
        self.recon.get_point(obs["point"])
        batch_ref = payload.get("batch_ref")
        if batch_ref is not None:
            batch_ref = require_text(batch_ref, "batch_ref", 100)

        result = self._serialized_manual(obs, batch_ref, actor) \
            if obs["source"] == SOURCE_MANUAL and batch_ref is None \
            else self._apply_observation(obs, batch_ref, actor)

        if obs["metric"] in WATER_METRICS:
            result["recalc"] = self._recalc_commands(obs["point"], actor,
                                                     trigger_obs_id=result.get("observation_id"))
        elif obs["metric"] in RECEIPT_METRICS:
            result["closure_check"] = self._recheck_executed(obs["point"], actor)
        return result

    def _point_lock(self, point: str) -> threading.Lock:
        with self._points_guard:
            lock = self._point_locks.get(point)
            if lock is None:
                lock = threading.Lock()
                self._point_locks[point] = lock
            return lock

    def _serialized_manual(self, obs: Dict[str, Any], batch_ref: Optional[str],
                           actor: str) -> Dict[str, Any]:
        """两个值班员同时提交同一测点：先到生效，后到留待核（409）。"""
        point = obs["point"]
        lock = self._point_lock(point)
        if not lock.acquire(blocking=False):
            # 后到：落为待核并提示，不覆盖先到结果
            obs_id, _ = self.recon.insert_observation(
                obs, INBOX_PENDING, batch_ref, actor)
            self._audit("recon.observe", ENTITY_OBS, obs_id, actor, {
                "point": point, "metric": obs["metric"],
                "field_time": obs["field_time"], "source": obs["source"],
                "value": obs["value"], "outcome": "concurrent_held"})
            raise ConflictError("该测点正在被其他值班员提交，本次上报已留待核")
        try:
            return self._apply_observation(obs, batch_ref, actor)
        finally:
            lock.release()

    # -------------------------------------------------------- device backfill
    def ingest_batch(self, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        """设备恢复后批量补传：乱序按现场时间仲裁，续传从已确认测点序列继续。

        同一 batch_ref 重试为幂等：已处理序列不重复落账、不重复追加审计。
        """
        ensure_role(role, SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_ref = require_text(payload.get("batch_ref"), "batch_ref", 100)
        point = normalize_point(payload.get("point"))
        self.recon.get_point(point)
        raw_items = payload.get("observations")
        if not isinstance(raw_items, list) or not raw_items:
            from .domain import ValidationError
            raise ValidationError("observations必须是非空数组")

        parsed: List[Dict[str, Any]] = []
        for item in raw_items:
            one = parse_observation(item)
            if one["point"] != point:
                from .domain import ValidationError
                raise ValidationError("批量补传必须属于同一测点")
            if one["source"] != SOURCE_DEVICE:
                from .domain import ValidationError
                raise ValidationError("批量补传仅用于device设备数据")
            parsed.append(one)

        resume_from = self.recon.confirmed_seq(point)
        counters = {"accepted": 0, "pending": 0, "duplicate": 0}
        outcomes: List[Dict[str, Any]] = []
        max_seq = resume_from
        # 乱序补传：先按现场时间排序再逐条仲裁
        for one in sorted(parsed, key=lambda o: (o["field_time"], o["source_seq"])):
            res = self._apply_observation(one, batch_ref, actor)
            outcomes.append({"seq": one["source_seq"], **res})
            if res["outcome"] == "duplicate":
                counters["duplicate"] += 1
            elif res["outcome"] in ("locked", "pending_review"):
                counters["pending"] += 1
            else:
                counters["accepted"] += 1
            max_seq = max(max_seq, one["source_seq"])

        done = counters["pending"] == 0
        self.recon.upsert_batch(
            batch_ref, point, len(parsed), counters, max_seq,
            "done" if done else "open")
        # 批次进度只记一条审计（重试更新同批，不逐条重复）
        if counters["duplicate"] < len(parsed):
            self._audit("recon.batch", ENTITY_BATCH, 0, actor, {
                "batch_ref": batch_ref, "point": point, "total": len(parsed),
                **counters, "resume_from_seq": resume_from,
                "last_seq": max_seq, "status": "done" if done else "open"})

        recalc = self._recalc_commands(point, actor, trigger_obs_id=None)
        return {"batch_ref": batch_ref, "point": point,
                "resume_from_seq": resume_from, "last_seq": max_seq,
                "counts": counters, "outcomes": outcomes,
                "recalc": recalc}

    # ---------------------------------------------------------------- receipts
    def submit_receipt(self, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        """闸门操作回执：过流 / 到位（可迟到，按同规则对账）。"""
        ensure_role(role, SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        point = normalize_point(payload.get("point"))
        self.recon.get_point(point)
        metric = normalize_metric(payload.get("metric"), RECEIPT_METRICS)
        source = normalize_source(payload.get("source", SOURCE_MANUAL))
        field_time = normalize_field_time(payload.get("field_time"))
        from .domain import require_number
        value = require_number(payload.get("value"), "value")
        seq = normalize_seq(payload.get("seq", 0))
        obs = {"point": point, "metric": metric, "field_time": field_time,
               "source": source, "value": value, "unit": payload.get("unit"),
               "anomaly_note": payload.get("anomaly_note"), "source_seq": seq}
        result = self._serialized_manual(obs, None, actor) \
            if source == SOURCE_MANUAL else self._apply_observation(obs, None, actor)
        result["closure_check"] = self._recheck_executed(point, actor)
        return result

    def submit_closure(self, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        """关闭确认：确认全关到位与断流，并重新核对关闭资格。"""
        ensure_role(role, CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        point = normalize_point(payload.get("point"))
        point_row = self.recon.get_point(point)
        field_time = normalize_field_time(payload.get("field_time"))
        from .domain import require_number
        position_value = require_number(payload.get("position_value"), "position_value")
        flow_value = require_number(payload.get("flow_value"), "flow_value")
        source = normalize_source(payload.get("source", SOURCE_MANUAL))

        executed = self._latest_executed_close(point)
        command_id = executed["id"] if executed else None
        closure_id = self.recon.add_closure(
            point, command_id, position_value, flow_value, field_time, source, actor)

        # 关闭确认不能自造到位/过流数据：只能锁定与之自洽的已有操作回执；
        # 回执缺失或数值对不上的情况由 close_qualification 判定为 pending/blocked。
        for metric, value in ((METRIC_GATE_POSITION, position_value),
                              (METRIC_GATE_FLOW, flow_value)):
            latest = self.recon.latest_canonical(point, metric)
            if latest is not None and abs(latest["value"] - value) <= (
                    0.5 if metric == METRIC_GATE_POSITION else 0.05):
                self.recon.confirm_canonical(latest["id"], actor)

        self._audit("recon.closure", ENTITY_CLOSURE, closure_id, actor, {
            "point": point, "command_id": command_id,
            "position_value": position_value, "flow_value": flow_value,
            "field_time": field_time})
        check = self._recheck_executed(point, actor)
        return {"closure_id": closure_id, "point": point,
                "command_id": command_id, "qualification": check}

    # --------------------------------------------------------------- commands
    def _recalc_commands(self, point: str, actor: str,
                         trigger_obs_id: Optional[int]) -> List[Dict[str, Any]]:
        """水情依据更新后：未执行指令按新结果失效重算；已执行记录不动。"""
        point_row = self.recon.get_point(point)
        latest = self.recon.latest_canonical(point, METRIC_WATER_LEVEL)
        desired: Optional[Dict[str, Any]] = None
        if latest is not None:
            canon = {(point, METRIC_WATER_LEVEL): {
                **latest, "threshold": point_row["open_threshold"]}}
            desired = desired_command(point, canon)

        pending = self.recon.pending_command(point)
        decision = recalc_decision(
            {"command": pending["command"], "status": pending["status"]} if pending else None,
            desired)

        changes: List[Dict[str, Any]] = []
        if decision == "unchanged":
            return changes
        if decision in ("invalidate", "withdraw") and pending is not None:
            reason = self._recalc_reason(decision, pending, desired, trigger_obs_id)
            self.recon.invalidate_command(pending["id"], reason)
            self._audit("recon.command_invalidate", ENTITY_CMD, pending["id"], actor, {
                "point": point, "command": pending["command"], "reason": reason,
                "trigger_observation_id": trigger_obs_id})
            changes.append({"command_id": pending["id"], "change": "invalidated",
                            "reason": reason})
        if decision in ("issue", "invalidate") and desired is not None:
            recalc_of = pending["id"] if decision == "invalidate" and pending else None
            new_id = self.recon.insert_command(
                point, desired["command"], desired, recalc_of,
                self._recalc_reason(decision, pending, desired, trigger_obs_id))
            self._audit("recon.command_issue", ENTITY_CMD, new_id, actor, {
                "point": point, "command": desired["command"],
                "basis_value": desired["basis_value"],
                "basis_threshold": desired["basis_threshold"],
                "basis_observation_id": desired["basis_observation_id"],
                "recalc_of_id": recalc_of,
                "trigger_observation_id": trigger_obs_id})
            changes.append({"command_id": new_id, "change": "issued",
                            "command": desired["command"], "recalc_of_id": recalc_of})
        return changes

    @staticmethod
    def _recalc_reason(decision: str, pending: Optional[Dict[str, Any]],
                       desired: Optional[Dict[str, Any]],
                       trigger_obs_id: Optional[int]) -> str:
        if desired is None:
            return f"依据缺失或不足，原{pending['command'] if pending else ''}指令撤回(obs={trigger_obs_id})"
        return (f"水情依据更新为{desired['basis_value']}/{desired['basis_threshold']}"
                f"@{desired['basis_field_time']}，应{desired['command']}"
                f"(obs={desired['basis_observation_id']},trigger={trigger_obs_id})")

    def execute_command(self, command_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, {"dispatcher", "duty_officer"})
        actor = require_text(actor, "actor", 100)
        command = self.recon.get_command(command_id)
        self.recon.mark_command_executed(command_id, actor)
        self._audit("recon.command_execute", ENTITY_CMD, command_id, actor, {
            "point": command["point"], "command": command["command"],
            "recalc_of_id": command["recalc_of_id"]})
        # 已执行：初始关闭资格核对（等待回执与关闭确认）
        check = self._recheck_executed(command["point"], actor)
        return {"command_id": command_id, "status": "executed",
                "qualification": check}

    def _latest_executed_close(self, point: str) -> Optional[Dict[str, Any]]:
        for cmd in reversed(self.recon.list_commands(point)):
            if cmd["status"] == "executed" and cmd["command"] == "close":
                return cmd
        return None

    def _recheck_executed(self, point: str, actor: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """已执行记录保留；回执/确认更新后重新核对关闭资格。"""
        executed = self._latest_executed_close(point)
        if executed is None:
            return None
        receipts: Dict[Any, Any] = {}
        for metric in (METRIC_GATE_POSITION, METRIC_GATE_FLOW):
            latest = self.recon.latest_canonical(point, metric)
            if latest is not None:
                receipts[(point, metric)] = latest
        closure = self.recon.latest_closure(point)
        result = closure_qualification(point, executed, receipts, closure)
        self.recon.set_close_qualification(
            executed["id"], result["qualification"], result["reasons"])
        if actor:
            self._audit("recon.close_recheck", ENTITY_CMD, executed["id"], actor, {
                "point": point, "qualification": result["qualification"],
                "reasons": result["reasons"]})
        return result

    # ------------------------------------------------------------- review queue
    def pending_review(self, role: str, point: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return self.recon.list_observations(point, INBOX_PENDING)

    def resolve_pending(self, observation_id: int, approve: bool,
                        actor: str, role: str) -> Dict[str, Any]:
        """裁决留待核上报：approve 采纳为规范值，reject 否决不采信。"""
        ensure_role(role, CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        rows = [o for o in self.recon.list_observations() if o["id"] == observation_id]
        if not rows:
            from .domain import NotFoundError
            raise NotFoundError("待核记录不存在")
        obs_row = rows[0]
        if obs_row["state"] != INBOX_PENDING:
            raise ConflictError("该记录已处理，无需重复裁决")
        if not approve:
            self.recon.mark_observation(observation_id, INBOX_REJECTED)
            self._audit("recon.review", ENTITY_OBS, observation_id, actor,
                        {"decision": "rejected", "point": obs_row["point"]})
            return {"observation_id": observation_id, "decision": "rejected"}

        obs = {"point": obs_row["point"], "metric": obs_row["metric"],
               "field_time": obs_row["field_time"], "source": obs_row["source"],
               "value": obs_row["value"], "unit": obs_row["unit"],
               "anomaly_note": obs_row["anomaly_note"],
               "source_seq": obs_row["source_seq"]}
        canonical = self.recon.get_canonical(
            obs_row["point"], obs_row["metric"], obs_row["field_time"])
        if canonical is not None and canonical["confirm_state"] != CANON_CONFIRMED:
            self.recon.mark_observation(canonical["observation_id"],
                                        INBOX_SUPERSEDED, observation_id)
        self.recon.mark_observation(observation_id, INBOX_ACCEPTED)
        self.recon.upsert_canonical(obs_row["point"], obs_row["metric"],
                                    obs_row["field_time"], {"id": observation_id, **obs})
        self._audit("recon.review", ENTITY_OBS, observation_id, actor,
                    {"decision": "approved", "point": obs_row["point"],
                     "value": obs_row["value"]})
        recalc = self._recalc_commands(
            obs_row["point"], actor, trigger_obs_id=observation_id) \
            if obs_row["metric"] in WATER_METRICS else []
        check = self._recheck_executed(obs_row["point"], actor) \
            if obs_row["metric"] in RECEIPT_METRICS else None
        return {"observation_id": observation_id, "decision": "approved",
                "recalc": recalc, "closure_check": check}

    # ---------------------------------------------------------------- views
    def list_commands(self, role: str, point: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return self.recon.list_commands(point)

    def list_closures(self, role: str, point: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return self.recon.list_closures(point)

    def reconciliation_view(self, role: str,
                            point: Optional[str] = None) -> Dict[str, Any]:
        """列表与审计共用的对账视图：差异、重算来源链、续传进度。"""
        ensure_role(role, VIEW_ROLES)
        canon = self.recon.list_canonical(point)
        observations = self.recon.list_observations(point)
        canon_key = {(c["point"], c["metric"], c["field_time"]): c for c in canon}

        diffs: List[Dict[str, Any]] = []
        for o in observations:
            if o["state"] in (INBOX_ACCEPTED, INBOX_DUPLICATE):
                continue
            c = canon_key.get((o["point"], o["metric"], o["field_time"]))
            diffs.append({
                "point": o["point"], "metric": o["metric"],
                "field_time": o["field_time"],
                "incoming_value": o["value"], "incoming_source": o["source"],
                "incoming_seq": o["source_seq"], "state": o["state"],
                "anomaly_note": o["anomaly_note"],
                "canonical_value": c["value"] if c else None,
                "canonical_source": c["source"] if c else None,
                "canonical_locked": bool(c and c["confirm_state"] == CANON_CONFIRMED),
                "preserved_anomaly": c["anomaly_note"] if c else None,
            })

        commands = self.recon.list_commands(point)
        recalc_chain = [{
            "command_id": c["id"], "point": c["point"], "command": c["command"],
            "status": c["status"], "recalc_of_id": c["recalc_of_id"],
            "recalc_source": c["recalc_source"],
            "basis": None if c["basis_value"] is None else {
                "value": c["basis_value"], "threshold": c["basis_threshold"],
                "field_time": c["basis_field_time"],
                "observation_id": c["basis_observation_id"]},
            "close_qualification": c["close_qualification"],
            "close_reasons": json_loads(c["close_reasons"]),
        } for c in commands]

        batches = self.recon.list_batches()
        if point:
            batches = [b for b in batches if b["point"] == point]
        resume_progress = [{
            "batch_ref": b["batch_ref"], "point": b["point"], "total": b["total"],
            "accepted": b["accepted"], "pending": b["pending"],
            "duplicate": b["duplicate"], "last_seq": b["last_seq"],
            "confirmed_seq": self.recon.confirmed_seq(b["point"]),
            "status": b["status"],
        } for b in batches]

        return {
            "canonical": canon,
            "diffs": diffs,
            "recalc_chain": recalc_chain,
            "resume_progress": resume_progress,
            "pending_review_count": len([d for d in diffs if d["state"] == INBOX_PENDING]),
        }


def json_loads(value):
    import json
    if not value:
        return []
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return []
