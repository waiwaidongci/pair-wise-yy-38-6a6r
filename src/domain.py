from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['routine', 'attention', 'urgent', 'emergency']; STATES=['draft', 'checked', 'authorized', 'executed', 'closed']; ROLES=['duty_officer', 'chief_engineer', 'dispatcher', 'viewer']

# 水情读数：来源（遥测设备/人工手报）、测点、现场时间
READING_SOURCES=['device', 'manual']
# 读数状态：待核（人工首报生效中）、已确认（锁定，后到不覆盖）、留待核（冲突后到件）
READING_STATUSES=['pending', 'confirmed', 'held']
# 补传批次状态：进行中、已完成、失败（可续传）
BATCH_STATUSES=['in_progress', 'completed', 'failed']

@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; point:Optional[str]; invalidated:int; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class Reading:
    id:int; point:str; source:str; observed_at:str; kind:str; value:Optional[float]; text_value:Optional[str]; unit:Optional[str]; reason:Optional[str]; status:str; external_ref:Optional[str]; batch_ref:Optional[str]; created_by:str; created_at:str; confirmed_at:Optional[str]; confirmed_by:Optional[str]
@dataclass(frozen=True)
class BackfillBatch:
    id:int; batch_ref:str; status:str; total:int; processed:int; resume_index:int; last_point:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
