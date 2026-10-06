from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='工伤事故调查与纠正措施'; ENTITY='事故'; ID_PREFIX='OI'
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; TRANSITIONS={'reported': ['investigating'], 'investigating': ['corrective_action'], 'corrective_action': ['verification'], 'verification': ['closed'], 'closed': []}; TRANSITION_ROLES={'investigating': ['investigator'], 'corrective_action': ['investigator'], 'verification': ['safety_manager'], 'closed': ['safety_manager']}
CREATE_ROLES=set(['reporter', 'investigator']); RECORD_ROLES=set(['investigator', 'safety_manager']); AUDIT_ROLES=set(['safety_manager', 'viewer']); VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'viewer'])
# 关闭批次相关角色
CLOSE_BATCH_ROLES=frozenset(['investigator', 'safety_manager']); SIGN_ROLES=frozenset(['safety_manager']); REINSPECT_ROLES=frozenset(['investigator', 'safety_manager'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'serious': 6.0, 'fatal': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'serious': 8, 'fatal': 4}; TERMINAL_STATES=set(['closed'])
# 措施（纠正措施）记录需要独立复验；证据等记录不需要
MEASURE_KINDS=frozenset(['action'])
# 复验状态
REINSPECTION_INDEPENDENT='independent'; REINSPECTION_SAME_PERSON='same_person'; REINSPECTION_PENDING='pending_reinspection'; REINSPECTION_NOT_CLOSED='not_closed'; REINSPECTION_NOT_REQUIRED='not_required'
def is_measure(kind): return kind in MEASURE_KINDS
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
def reinspection_state(record):
    """返回复验状态。record 为 dict，需含 kind/status/executor/reinspected_by。"""
    if not is_measure(record.get('kind')): return REINSPECTION_NOT_REQUIRED
    if record.get('status')!='closed': return REINSPECTION_NOT_CLOSED
    executor=record.get('executor'); reinspected_by=record.get('reinspected_by')
    if not reinspected_by: return REINSPECTION_PENDING
    if executor and reinspected_by==executor: return REINSPECTION_SAME_PERSON
    return REINSPECTION_INDEPENDENT
def measure_blockers(records):
    """返回措施拦住项：未关闭、待补核、复验人同执行人。"""
    blockers=[]
    for record in records:
        if not is_measure(record.get('kind')): continue
        state=reinspection_state(record)
        if state==REINSPECTION_NOT_CLOSED: blockers.append(f"措施{record['id']}未关闭")
        elif state==REINSPECTION_PENDING: blockers.append(f"措施{record['id']}待补核")
        elif state==REINSPECTION_SAME_PERSON: blockers.append(f"措施{record['id']}复验人同执行人")
    return blockers
def close_blockers(target,records):
    """关闭前拦住项：未关闭记录 + 未独立复验的措施。"""
    if target not in TERMINAL_STATES: return []
    blockers=measure_blockers(records)
    for record in records:
        if not is_measure(record.get('kind')) and record.get('status')=='open':
            blockers.append(f"记录{record['id']}未关闭")
    return blockers
