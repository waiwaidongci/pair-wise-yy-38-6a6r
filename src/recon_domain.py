"""汛期闸门遥测/手报对账领域模型。

每条数据都必须回答三个问题：来自哪里(source)、哪个测点(point)、
对应什么现场时间(field_time)。规范值(canonical)与原始上报(inbox)分离，
保证补传乱序、迟到手报不会篡改已经确认的事实。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .domain import ValidationError, require_number, require_text

# 数据来源
SOURCE_DEVICE = "device"      # 设备遥测（含恢复后的批量补传）
SOURCE_MANUAL = "manual"      # 值班员现场手报
SOURCES = (SOURCE_DEVICE, SOURCE_MANUAL)

# 指标：水情依据
METRIC_WATER_LEVEL = "water_level"   # 库位/水位
METRIC_INFLOW = "inflow"             # 入库流量
METRIC_DOWNSTREAM = "downstream"     # 下游流量/水位
# 指标：闸门操作回执 / 关闭确认
METRIC_GATE_FLOW = "gate_flow"       # 过流情况
METRIC_GATE_POSITION = "gate_position"  # 闸门到位情况
WATER_METRICS = (METRIC_WATER_LEVEL, METRIC_INFLOW, METRIC_DOWNSTREAM)
RECEIPT_METRICS = (METRIC_GATE_FLOW, METRIC_GATE_POSITION)
METRICS = WATER_METRICS + RECEIPT_METRICS

# 上报收件箱状态
INBOX_ACCEPTED = "accepted"          # 已采纳为当前规范值
INBOX_SUPERSEDED = "superseded"      # 曾采纳，被更高优先级数据取代（设备优先）
INBOX_PENDING = "pending"            # 留待核（并发后到 / 确认锁后到）
INBOX_DUPLICATE = "duplicate"        # 幂等重试，未重复落账
INBOX_REJECTED = "rejected"          # 待核裁决时被否决不采信
INBOX_STATES = (INBOX_ACCEPTED, INBOX_SUPERSEDED, INBOX_PENDING,
                INBOX_DUPLICATE, INBOX_REJECTED)

# 规范值确认状态
CANON_PROVISIONAL = "provisional"    # 暂定，可被设备值修正
CANON_CONFIRMED = "confirmed"        # 已确认（人工复核/关闭确认），锁定
CANON_REJECTED = "rejected"          # 待核裁决时被否决不采信

# 补传批次状态
BATCH_OPEN = "open"
BATCH_DONE = "done"
BATCH_FAILED = "failed"
BATCH_STATES = (BATCH_OPEN, BATCH_DONE, BATCH_FAILED)

# 闸门指令状态
CMD_OPEN = "open"
CMD_CLOSE = "close"
CMD_STATES = (CMD_OPEN, CMD_CLOSE)
CMD_PENDING = "pending"              # 未执行，可随新依据失效重算
CMD_INVALIDATED = "invalidated"      # 依据更新后失效
CMD_EXECUTED = "executed"            # 已执行，永久保留
CMD_LIFECYCLE = (CMD_PENDING, CMD_INVALIDATED, CMD_EXECUTED)

# 回执类型
RECEIPT_OPERATION = "operation"      # 操作回执（过流/到位）
RECEIPT_CLOSURE = "closure"          # 关闭确认

# 关闭资格（已执行指令重新核对）
CLOSE_QUALIFIED = "qualified"
CLOSE_PENDING = "pending"
CLOSE_BLOCKED = "blocked"

# 完全到位阈值（开度百分比，<= 视为全关到位）
GATE_FULLY_CLOSED = 0.5
# 过流断流阈值（m^3/s，<= 视为断流）
FLOW_ZERO = 0.05

POINT_MAX = 100
ACTOR_MAX = 100
ANOMALY_MAX = 500


@dataclass(frozen=True)
class Observation:
    id: int
    point: str
    metric: str
    field_time: str
    source: str
    value: float
    unit: Optional[str]
    anomaly_note: Optional[str]
    state: str
    batch_ref: Optional[str]
    source_seq: int
    replaced_by_id: Optional[int]
    created_by: str
    created_at: str


def normalize_source(value: Any) -> str:
    if value not in SOURCES:
        raise ValidationError("source必须是device或manual")
    return value


def normalize_point(value: Any) -> str:
    return require_text(value, "point", POINT_MAX)


def normalize_metric(value: Any, allowed=METRICS) -> str:
    if value not in allowed:
        raise ValidationError(f"metric必须是{ '/'.join(allowed) }之一")
    return value


def normalize_field_time(value: Any) -> str:
    text = require_text(value, "field_time", 64)
    # 必须形如 ISO 8601（2026-07-04T08:00:00 或带时区），用于排序乱序补传
    try:
        from datetime import datetime
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("field_time必须是ISO8601时间") from exc
    return text


def normalize_seq(value: Any) -> int:
    if isinstance(value, bool):
        raise ValidationError("seq必须是非负整数")
    try:
        seq = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("seq必须是非负整数") from exc
    if seq < 0:
        raise ValidationError("seq不能为负")
    return seq


def parse_observation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验单条水情上报。"""
    return {
        "point": normalize_point(payload.get("point")),
        "metric": normalize_metric(payload.get("metric"), WATER_METRICS),
        "field_time": normalize_field_time(payload.get("field_time")),
        "source": normalize_source(payload.get("source")),
        "value": require_number(payload.get("value"), "value"),
        "unit": (require_text(payload.get("unit"), "unit", 20)
                 if payload.get("unit") is not None else None),
        "anomaly_note": (require_text(payload.get("anomaly_note"), "anomaly_note", ANOMALY_MAX)
                         if payload.get("anomaly_note") else None),
        "source_seq": normalize_seq(payload.get("seq", 0)),
    }
