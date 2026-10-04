"""对账规则：冲突仲裁、指令重算、关闭资格核对。

规则均为纯函数，输入快照、输出裁决，便于测试与审计复现。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .recon_domain import (CANON_CONFIRMED, CLOSE_BLOCKED,
                           CLOSE_PENDING, CLOSE_QUALIFIED, CMD_CLOSE,
                           CMD_OPEN, FLOW_ZERO, GATE_FULLY_CLOSED,
                           METRIC_GATE_FLOW, METRIC_GATE_POSITION,
                           METRIC_WATER_LEVEL, SOURCE_DEVICE,
                           SOURCE_MANUAL)

# 同测点同时间冲突时的来源优先级：设备值优先。
SOURCE_PRIORITY = {SOURCE_DEVICE: 2, SOURCE_MANUAL: 1}


def should_replace(canonical: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
    """决定 incoming 是否取代当前规范值。

    已确认值锁定，后到数据（含设备补传）一律不得覆盖；
    暂定值只有在来源优先级更高（设备 > 人工）时才被取代，
    同优先级冲突进入待核（由 repository 的并发锁配合处理）。
    """
    if canonical["confirm_state"] == CANON_CONFIRMED:
        return False
    return SOURCE_PRIORITY[incoming["source"]] > SOURCE_PRIORITY[canonical["source"]]


def merge_anomaly(existing: Optional[str], incoming: Optional[str]) -> Optional[str]:
    """人工标明的异常原因必须保留：合并不重复，后到补充追加。"""
    notes = [n for n in (existing, incoming) if n]
    if not notes:
        return None
    merged: List[str] = []
    for note in notes:
        if note not in merged:
            merged.append(note)
    return " | ".join(merged)


def diff_entry(canonical: Dict[str, Any], incoming: Dict[str, Any],
               outcome: str) -> Dict[str, Any]:
    return {
        "point": canonical["point"],
        "metric": canonical["metric"],
        "field_time": canonical["field_time"],
        "canonical_value": canonical["value"],
        "canonical_source": canonical["source"],
        "incoming_value": incoming["value"],
        "incoming_source": incoming["source"],
        "incoming_seq": incoming["source_seq"],
        "outcome": outcome,   # replaced / locked / pending_review / same
        "anomaly_note": canonical.get("anomaly_note"),
    }


def reconcile(canonical: Optional[Dict[str, Any]],
              incoming: Dict[str, Any]) -> str:
    """对单条上报裁决：accept / replace / lock_pending / pending_review。"""
    if canonical is None:
        return "accept"
    if canonical["confirm_state"] == CANON_CONFIRMED:
        return "lock_pending"
    if incoming["source"] == canonical["source"] and incoming["value"] == canonical["value"]:
        return "accept"  # 幂等/重复
    prio_in = SOURCE_PRIORITY[incoming["source"]]
    prio_old = SOURCE_PRIORITY[canonical["source"]]
    if prio_in > prio_old:
        return "replace"
    if prio_in == prio_old:
        return "pending_review"  # 同优先级冲突（如两个手报）留待核
    return "pending_review"      # 低优先级不得覆盖高优先级


# ---------------------------------------------------------------------------
# 指令重算：水情依据或回执更新后，按测点最新规范值决定闸门应开/应关
# ---------------------------------------------------------------------------

def desired_command(point: str, canon: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """根据某测点的规范水情给出期望指令；依据不足时返回 None。"""
    level = canon.get((point, METRIC_WATER_LEVEL))
    if level is None:
        return None
    threshold = level.get("threshold")
    value = level["value"]
    if threshold is None:
        return None
    return {
        "point": point,
        "command": CMD_OPEN if value >= threshold else CMD_CLOSE,
        "basis_value": value,
        "basis_threshold": threshold,
        "basis_observation_id": level["observation_id"],
        "basis_field_time": level["field_time"],
    }


def recalc_decision(pending: Optional[Dict[str, Any]],
                    desired: Optional[Dict[str, Any]]) -> str:
    """未执行指令重算裁决。

    invalidate：新结果与待执行指令不一致，旧指令失效并产生新指令；
    reissue：旧指令已失效但新结果再次需要它，重新下发；
    unchanged：指令与新结果一致，保持；
    withdraw：不再需要且没有新期望（依据缺失），失效不再补。
    """
    if desired is None:
        return "withdraw" if pending is not None else "unchanged"
    if pending is None:
        return "issue"
    if pending["command"] == desired["command"] and pending["status"] == "pending":
        return "unchanged"
    if pending["command"] == desired["command"]:
        return "reissue"
    return "invalidate"


# ---------------------------------------------------------------------------
# 已执行记录的关闭资格核对
# ---------------------------------------------------------------------------

def closure_qualification(point: str, executed: Dict[str, Any],
                          receipts: Dict[str, Any],
                          closure: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """重新核对已执行闸门指令是否具备关闭资格。

    要求：关闭指令已执行；到位回执显示全关(GATE_FULLY_CLOSED)；
    过流回执显示断流(FLOW_ZERO)；且有关闭确认。
    任何一项缺失或与执行结果矛盾即 blocked/pending，并给出差异原因。
    """
    reasons: List[str] = []
    if executed["command"] != CMD_CLOSE:
        reasons.append("已执行记录非关闭指令")

    position = receipts.get((point, METRIC_GATE_POSITION))
    flow = receipts.get((point, METRIC_GATE_FLOW))

    if position is None:
        reasons.append("缺少到位回执")
    elif position["confirm_state"] != CANON_CONFIRMED and position.get("state") != "accepted":
        reasons.append("到位数据尚未确认")
    elif position["value"] > GATE_FULLY_CLOSED:
        reasons.append(f"尚未全关到位(开度{position['value']})")

    if flow is None:
        reasons.append("缺少过流回执")
    elif flow["value"] > FLOW_ZERO:
        reasons.append(f"仍有过流({flow['value']})")

    if closure is None:
        reasons.append("缺少关闭确认")
    else:
        # 关闭确认与到位/过流数据必须自洽，否则现场矛盾不予关闭
        if abs(closure.get("position_value", position["value"] if position else 0.0)
               - (position["value"] if position else 0.0)) > GATE_FULLY_CLOSED:
            reasons.append("关闭确认开度与到位回执矛盾")
        if closure.get("flow_value", flow["value"] if flow else 0.0) > FLOW_ZERO:
            reasons.append("关闭确认显示仍有过流")

    if not reasons:
        qualification = CLOSE_QUALIFIED
    elif any("矛盾" in r or "仍有过流" in r or "尚未全关" in r or "非关闭指令" in r
             for r in reasons):
        # 硬性冲突：现场数据与关闭结论对不上
        qualification = CLOSE_BLOCKED
    else:
        # 材料不全或尚未确认，可补齐后自动转为 qualified
        qualification = CLOSE_PENDING
    return {"point": point, "executed_command_id": executed["id"],
            "qualification": qualification, "reasons": reasons}
