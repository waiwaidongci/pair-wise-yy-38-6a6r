from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='水库防汛调度与操作确认'; ENTITY='调度指令'; ID_PREFIX='RF'
SEVERITIES=['routine', 'attention', 'urgent', 'emergency']; STATES=['draft', 'checked', 'authorized', 'executed', 'closed']; TRANSITIONS={'draft': ['checked'], 'checked': ['authorized'], 'authorized': ['executed'], 'executed': ['closed'], 'closed': []}; TRANSITION_ROLES={'checked': ['duty_officer'], 'authorized': ['chief_engineer'], 'executed': ['dispatcher'], 'closed': ['chief_engineer']}
CREATE_ROLES=set(['duty_officer']); RECORD_ROLES=set(['duty_officer', 'dispatcher']); AUDIT_ROLES=set(['chief_engineer', 'viewer']); VIEW_ROLES=set(['duty_officer', 'chief_engineer', 'dispatcher', 'viewer'])
# 读数相关角色：值班员可提交/确认手报，首席工程师可确认锁定
READING_ROLES=set(['duty_officer', 'dispatcher']); CONFIRM_ROLES=set(['chief_engineer']); BACKFILL_ROLES=set(['duty_officer', 'dispatcher'])
SEVERITY_WEIGHT={'routine': 1.0, 'attention': 3.0, 'urgent': 6.0, 'emergency': 9.0}; DEADLINE_HOURS={'routine': 72, 'attention': 24, 'urgent': 8, 'emergency': 4}; TERMINAL_STATES=set(['closed'])
# 读数来源与状态（与 domain 中常量保持一致，供规则层独立校验）
READING_SOURCES=['device', 'manual']; READING_STATUSES=['pending', 'confirmed', 'held']
# 关闭资格：水情依据中表明闸门到位/关闭的读数类型
CLOSURE_KINDS=set(['closure', 'gate_position']); CLOSURE_TEXT_VALUES=set(['closed', '到位'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

def validate_reading_source(source):
    if source not in READING_SOURCES: raise ValidationError("source必须是device或manual")
    return source

def reading_conflict_outcome(has_confirmed, has_pending, new_source):
    """同一测点同一现场时间冲突裁决。

    返回 (new_status, supersedes_pending)：
      - 已存在锁定值(confirmed)：新件一律留待核(held)，已确认值不被后到数据盖掉。
      - 无锁定值时设备(device)优先：新件确认(confirmed)，原待核(pending)件留待核。
      - 无锁定值且新件为人工(manual)：先到生效——已有待核件则新件留待核，否则新件待核。
    人工标明的异常原因(reason)由调用方持久化保留，本规则不丢弃任何记录。
    """
    if new_source not in READING_SOURCES:
        raise ValidationError("source必须是device或manual")
    if has_confirmed:
        return ('held', False)
    if new_source == 'device':
        return ('confirmed', True)
    if has_pending:
        return ('held', False)
    return ('pending', False)

def effective_reading(readings):
    """取该测点当前用于重算的依据：最新已确认值，否则最新待核生效值；留待核件不参与。"""
    confirmed=[r for r in readings if r.get('status')=='confirmed']
    if confirmed:
        return max(confirmed, key=lambda r: r.get('observed_at',''))
    pending=[r for r in readings if r.get('status')=='pending']
    if pending:
        return max(pending, key=lambda r: r.get('observed_at',''))
    return None

def closure_eligibility(item, readings):
    """重新核对已执行指令的关闭资格。

    已执行记录保留，仅依据最新水情依据判断是否具备关闭条件：
      - 存在已确认的关闭/到位读数；或
      - 最新有效水位低于汛限(threshold)。
    无测点或无读数时不设卡（返回 eligible=True），保持对历史数据兼容。
    返回 (eligible, reason)。
    """
    point=item.get('point')
    if not point:
        return (True, '无测点，免核对')
    eff=effective_reading(readings)
    if eff is None:
        return (False, '无有效水情依据')
    if eff.get('status')!='confirmed':
        return (False, '水情依据尚未确认')
    kind=eff.get('kind')
    if kind in CLOSURE_KINDS:
        text=(eff.get('text_value') or '').strip()
        if text in CLOSURE_TEXT_VALUES:
            return (True, '闸门已到位/关闭')
    if kind=='water_level' and eff.get('value') is not None:
        threshold=item.get('threshold',1.0)
        if float(eff['value']) < float(threshold):
            return (True, '水位已低于汛限')
    return (False, '关闭条件不满足')

def recalc_snapshot(item, new_value=None):
    """依据新结果重算未执行指令的优先级/期限/紧迫度。"""
    quantity=float(new_value) if new_value is not None else float(item.get('quantity',0.0))
    threshold=float(item.get('threshold',1.0))
    severity=item.get('severity')
    return {
        'quantity': quantity,
        'priority': priority_score(severity, quantity, threshold),
        'deadline_hours': response_deadline_hours(severity, quantity, threshold),
        'escalation_required': escalation_required(severity, quantity, threshold),
    }
