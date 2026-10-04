from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BACKFILL_ROLES, CONFIRM_ROLES, CREATE_ROLES, ENTITY,
                    READING_ROLES, RECORD_ROLES, TITLE, VIEW_ROLES,
                    closure_eligibility, completion_blockers, effective_reading,
                    escalation_required, priority_score, recalc_snapshot,
                    response_deadline_hours, role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ---- 调度指令 ----

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        point = payload.get("point")
        if point is not None:
            point = require_text(point, "point", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, point)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "point": point,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        # 回执更新：未执行指令失效重算，已执行记录保留并重新核对关闭资格
        item = self.repository.get_item(item_id)
        self._apply_basis_change(item.get("point"), actor, "receipt", record["id"],
                                 item_id=item_id, new_value=None)
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        # 关闭前重新核对关闭资格：已执行记录保留，仅核对是否具备关闭条件
        if target == "closed":
            eligible, reason = closure_eligibility(
                item, self.repository.list_readings(item.get("point")))
            self.repository.append_audit("closure_recheck", ENTITY, item_id, actor, {
                "eligible": eligible, "reason": reason,
            })
            if not eligible:
                raise ConflictError(f"不具备关闭资格：{reason}")
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        # 授权后依据重新生效，清除失效标记
        if target == "authorized":
            updated = self.repository.set_item_invalidated(item_id, False)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        if item.get("status") == "executed":
            eligible, reason = closure_eligibility(
                item, self.repository.list_readings(item.get("point")))
            result["closure_eligible"] = eligible
            result["closure_reason"] = reason
        return result

    # ---- 水情读数与对账 ----

    def submit_reading(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, READING_ROLES)
        actor = require_text(actor, "actor", 100)
        point = require_text(payload.get("point"), "point", 100)
        source = payload.get("source")
        if source not in ("device", "manual"):
            raise ValidationError("source必须是device或manual")
        observed_at = require_text(payload.get("observed_at"), "observed_at", 40)
        kind = payload.get("kind", "water_level")
        if kind is not None:
            kind = require_text(kind, "kind", 100)
        value = payload.get("value")
        if value is not None:
            value = require_number(value, "value")
        text_value = payload.get("text_value")
        if text_value is not None:
            text_value = require_text(text_value, "text_value", 100)
        unit = payload.get("unit")
        if unit is not None:
            unit = require_text(unit, "unit", 40)
        reason = payload.get("reason")
        if reason is not None:
            reason = require_text(reason, "reason", 500)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        batch_ref = payload.get("batch_ref")
        if batch_ref is not None:
            batch_ref = require_text(batch_ref, "batch_ref", 100)
        reading, created = self.repository.submit_reading(
            point, source, observed_at, kind, value, text_value, unit, reason,
            external_ref, batch_ref, actor)
        if created:
            self.repository.append_audit("reading_submitted", "reading", reading["id"], actor, {
                "point": point, "source": source, "observed_at": observed_at,
                "status": reading["status"], "value": value, "text_value": text_value,
                "reason": reason, "batch_ref": batch_ref,
            })
            # 新依据生效：未执行指令失效重算，已执行记录重新核对关闭资格
            if reading["status"] in ("confirmed", "pending"):
                self._apply_basis_change(point, actor, "reading", reading["id"],
                                         item_id=None, new_value=value)
        return reading

    def confirm_reading(self, reading_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        reading = self.repository.confirm_reading(reading_id, actor)
        self.repository.append_audit("reading_confirmed", "reading", reading["id"], actor, {
            "point": reading["point"], "observed_at": reading["observed_at"],
            "source": reading["source"], "reason": reading.get("reason"),
        })
        # 确认后依据锁定，触发重算/关闭资格核对
        self._apply_basis_change(reading["point"], actor, "reading", reading["id"],
                                 item_id=None, new_value=reading.get("value"))
        return reading

    def list_readings(self, role: str, point: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_readings(point)

    def list_reconciliation(self, role: str, point: Optional[str] = None) -> list:
        """对账视图：按测点+现场时间汇总设备值与人工值，展示差异与异常原因。"""
        self._view(role)
        readings = self.repository.list_readings(point)
        groups: Dict[tuple, Dict[str, Any]] = {}
        for r in readings:
            key = (r["point"], r["observed_at"])
            g = groups.setdefault(key, {
                "point": r["point"], "observed_at": r["observed_at"],
                "device": None, "manual": None, "held": [], "reasons": [],
            })
            if r["source"] == "device" and r["status"] != "held":
                g["device"] = r
            elif r["source"] == "manual" and r["status"] != "held":
                g["manual"] = r
            if r["status"] == "held":
                g["held"].append({"id": r["id"], "source": r["source"],
                                  "value": r.get("value"), "text_value": r.get("text_value"),
                                  "reason": r.get("reason"), "status": r["status"]})
            if r.get("reason"):
                g["reasons"].append(r["reason"])
        result = []
        for g in groups.values():
            key = (g["point"], g["observed_at"])
            devs = sorted([r for r in readings
                           if r["point"] == key[0] and r["observed_at"] == key[1]
                           and r["source"] == "device"],
                          key=lambda r: r["id"], reverse=True)
            mans = sorted([r for r in readings
                           if r["point"] == key[0] and r["observed_at"] == key[1]
                           and r["source"] == "manual"],
                          key=lambda r: r["id"], reverse=True)
            dev = next((d for d in devs if d["status"] != "held"), None) or (devs[0] if devs else None)
            man = next((m for m in mans if m["status"] != "held"), None) or (mans[0] if mans else None)
            match = None
            if dev and man:
                match = (dev.get("value") == man.get("value")
                         and dev.get("text_value") == man.get("text_value"))
            eff = effective_reading([r for r in readings
                                    if r["point"] == g["point"] and r["observed_at"] == g["observed_at"]])
            result.append({
                "point": g["point"],
                "observed_at": g["observed_at"],
                "device_value": dev.get("value") if dev else None,
                "device_text": dev.get("text_value") if dev else None,
                "manual_value": man.get("value") if man else None,
                "manual_text": man.get("text_value") if man else None,
                "match": match,
                "reasons": g["reasons"],
                "held_count": len(g["held"]),
                "held": g["held"],
                "effective_status": eff["status"] if eff else None,
                "effective_source": eff["source"] if eff else None,
            })
        result.sort(key=lambda x: (x["observed_at"], x["point"]), reverse=True)
        return result

    # ---- 补传（可续传，重试不重复追加审计） ----

    def submit_backfill(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BACKFILL_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_ref = require_text(payload.get("batch_ref"), "batch_ref", 100)
        raw_readings = payload.get("readings")
        if not isinstance(raw_readings, list) or not raw_readings:
            raise ValidationError("readings必须是非空数组")
        for rd in raw_readings:
            if not isinstance(rd, dict):
                raise ValidationError("每笔读数必须是对象")
        try:
            batch = self.repository.create_backfill(batch_ref, len(raw_readings), actor)
            self.repository.append_audit("backfill_started", "backfill", batch["id"], actor, {
                "batch_ref": batch_ref, "total": len(raw_readings),
            })
        except ConflictError:
            batch = self.repository.get_backfill(batch_ref)
            if batch["status"] == "completed":
                # 已完成批次重试：幂等返回，不重复追加审计
                return batch
            self.repository.append_audit("backfill_resumed", "backfill", batch["id"], actor, {
                "batch_ref": batch_ref, "from_index": batch["resume_index"],
                "processed": batch["processed"],
            })
        processed = batch["processed"]
        resume_index = batch["resume_index"]
        last_point = batch["last_point"]
        newly = 0
        try:
            for i, rd in enumerate(raw_readings):
                if i < resume_index:
                    continue  # 已处理测点，续传不重复处理
                external_ref = rd.get("external_ref")
                reading, created = self.repository.submit_reading(
                    require_text(rd.get("point"), "point", 100),
                    "device",
                    require_text(rd.get("observed_at"), "observed_at", 40),
                    rd.get("kind", "water_level"),
                    require_number(rd["value"], "value") if rd.get("value") is not None else None,
                    rd.get("text_value"),
                    rd.get("unit"),
                    rd.get("reason"),
                    external_ref,
                    batch_ref,
                    actor)
                if created:
                    newly += 1
                    self.repository.append_audit("reading_submitted", "reading", reading["id"], actor, {
                        "point": reading["point"], "source": "device",
                        "observed_at": reading["observed_at"], "status": reading["status"],
                        "value": reading.get("value"), "reason": reading.get("reason"),
                        "batch_ref": batch_ref, "backfill": True,
                    })
                    if reading["status"] in ("confirmed", "pending"):
                        self._apply_basis_change(reading["point"], actor, "reading",
                                                 reading["id"], item_id=None,
                                                 new_value=reading.get("value"))
                processed += 1
                resume_index = i + 1
                last_point = reading["point"]
                self.repository.update_backfill_progress(
                    batch_ref, processed, resume_index, last_point)
        except Exception:
            # 失败：从已确认测点继续，记录续传进度，重试不重复追加审计
            self.repository.update_backfill_progress(
                batch_ref, processed, resume_index, last_point, status="failed")
            self.repository.append_audit("backfill_progress", "backfill", batch["id"], actor, {
                "batch_ref": batch_ref, "failed_at_index": resume_index,
                "processed": processed, "last_point": last_point,
            })
            raise
        batch = self.repository.update_backfill_progress(
            batch_ref, processed, resume_index, last_point, status="completed")
        self.repository.append_audit("backfill_completed", "backfill", batch["id"], actor, {
            "batch_ref": batch_ref, "processed": processed, "total": len(raw_readings),
            "newly_confirmed": newly,
        })
        return batch

    def list_backfills(self, role: str) -> list:
        self._view(role)
        return self.repository.list_backfills()

    # ---- 依据/回执更新后的失效重算与关闭资格核对 ----

    def _apply_basis_change(self, point: Optional[str], actor: str, trigger: str,
                            source_id: Any, item_id: Optional[int],
                            new_value: Optional[float]) -> List[Dict[str, Any]]:
        if not point:
            return []
        if item_id is not None:
            items = [self.repository.get_item(item_id)]
        else:
            items = self.repository.list_items_by_point(point)
        results = []
        for item in items:
            if item["status"] in ("draft", "checked", "authorized"):
                old_q = item["quantity"]
                snapshot = recalc_snapshot(item, new_value)
                updated = self.repository.recalc_item(
                    item["id"], snapshot["quantity"], invalidated=True)
                self.repository.append_audit("recalculated", ENTITY, item["id"], actor, {
                    "trigger": trigger, "source_id": source_id, "point": point,
                    "old_quantity": old_q, "new_quantity": snapshot["quantity"],
                    "priority": snapshot["priority"],
                    "deadline_hours": snapshot["deadline_hours"],
                    "escalation_required": snapshot["escalation_required"],
                })
                results.append({"item_id": item["id"], "recalculated": True,
                                "invalidated": updated["invalidated"]})
            elif item["status"] == "executed":
                eligible, reason = closure_eligibility(
                    item, self.repository.list_readings(point))
                self.repository.append_audit("closure_recheck", ENTITY, item["id"], actor, {
                    "trigger": trigger, "source_id": source_id, "point": point,
                    "eligible": eligible, "reason": reason,
                })
                results.append({"item_id": item["id"], "closure_recheck": True,
                                "eligible": eligible, "reason": reason})
        return results
