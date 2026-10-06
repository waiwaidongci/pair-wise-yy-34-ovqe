from __future__ import annotations
import hashlib
import json
from .domain import ConflictError, ValidationError
TITLE='工伤事故调查与纠正措施'; ENTITY='事故'; ID_PREFIX='OI'
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; TRANSITIONS={'reported': ['investigating'], 'investigating': ['corrective_action'], 'corrective_action': ['verification'], 'verification': ['closed'], 'closed': []}; TRANSITION_ROLES={'investigating': ['investigator'], 'corrective_action': ['investigator'], 'verification': ['safety_manager'], 'closed': ['safety_manager']}
CREATE_ROLES=set(['reporter', 'investigator']); RECORD_ROLES=set(['investigator', 'safety_manager']); AUDIT_ROLES=set(['safety_manager', 'viewer']); VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'viewer'])
# 措施相关角色：调查员登记/执行措施，安全经理组织独立复验，双方都可查看。
MEASURE_ROLES=set(['investigator', 'safety_manager', 'reporter', 'viewer'])
BATCH_SUBMIT_ROLES=set(['investigator', 'safety_manager'])
BATCH_CONFIRM_ROLES=set(['safety_manager'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'serious': 6.0, 'fatal': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'serious': 8, 'fatal': 4}; TERMINAL_STATES=set(['closed'])

# 纠正措施记录的kind取值；其他kind仍为普通记录，普通查询照旧。
MEASURE_KINDS=('measure', 'corrective_action', 'action')
# 措施复验生命周期：
#   pending_verify    新措施，等待独立复验
#   pending_supplement 历史数据缺复验字段，升级为待补核
#   verified          已由独立复验人复验通过
VERIFY_PENDING='pending_verify'; VERIFY_SUPPLEMENT='pending_supplement'; VERIFY_OK='verified'
VERIFY_STATUSES=(VERIFY_PENDING, VERIFY_SUPPLEMENT, VERIFY_OK)
# 关闭批次状态：pending待安全经理确认；closed已关闭并保留快照；voided依据改动后作废。
BATCH_PENDING='pending'; BATCH_CLOSED='closed'; BATCH_VOIDED='voided'
BATCH_STATUSES=(BATCH_PENDING, BATCH_CLOSED, BATCH_VOIDED)

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

def is_measure(record) -> bool:
    """判断一条记录是否为纠正措施：显式is_measure或kind属于措施类型。"""
    if not isinstance(record, dict):
        return False
    if record.get('is_measure') in (1, True, '1'):
        return True
    kind = (record.get('kind') or '').strip().lower()
    return kind in MEASURE_KINDS

def effective_verify_status(record):
    """措施的复验状态；历史行缺复验字段时升级为待补核，普通记录返回None。"""
    if not is_measure(record):
        return None
    status = record.get('verify_status')
    if status in VERIFY_STATUSES:
        return status
    # 已有数据里的措施缺复验字段：升级为待补核（不落库改写，仅在读侧呈现）。
    if status:
        return status
    if record.get('verified_by') or record.get('verify_ref'):
        return VERIFY_OK
    return VERIFY_SUPPLEMENT

def measure_blockers(record) -> list:
    """返回单条措施阻止关闭的原因列表；为空表示可进入关闭批次。"""
    if not is_measure(record):
        return []
    blockers = []
    if record.get('status') != 'closed':
        blockers.append(f"措施#{record.get('id')}尚未关闭")
    status = effective_verify_status(record)
    if status == VERIFY_SUPPLEMENT:
        blockers.append(f"措施#{record.get('id')}缺少复验字段，待补核")
    elif status != VERIFY_OK:
        blockers.append(f"措施#{record.get('id')}尚未完成独立复验")
    executor = (record.get('executed_by') or '').strip() if record.get('executed_by') else ''
    verifier = (record.get('verified_by') or '').strip() if record.get('verified_by') else ''
    if status == VERIFY_OK:
        if not executor:
            blockers.append(f"措施#{record.get('id')}缺少执行人")
        if not verifier:
            blockers.append(f"措施#{record.get('id')}缺少复验人")
        if executor and verifier and executor == verifier:
            blockers.append(f"措施#{record.get('id')}复验人与执行人相同，复验不独立")
    return blockers

def collect_closure_blockers(records) -> list:
    blockers = []
    for record in records:
        blockers.extend(measure_blockers(record))
    return blockers

def measure_basis(record) -> tuple:
    """批次快照中每条措施所冻结的依据；改动任一字段都会使批次作废。"""
    return (
        int(record['id']),
        record.get('status'),
        record.get('executed_by'),
        record.get('verify_status') or effective_verify_status(record),
        record.get('verified_by'),
        record.get('verify_ref'),
        record.get('verify_detail'),
    )

def snapshot_measures(records) -> list:
    return [list(measure_basis(r)) for r in records if is_measure(r)]

def calculate_basis_hash(item_version: int, measures_snapshot: list) -> str:
    payload = json.dumps(
        {'item_version': item_version, 'measures': measures_snapshot},
        ensure_ascii=False, sort_keys=True, default=str,
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()

def basis_changed(batch, current_version: int, current_records) -> bool:
    """批次依据（事故版本或任一措施依据）在确认前是否被改动。"""
    if int(batch.get('frozen_item_version') or 0) != current_version:
        return True
    current = snapshot_measures(current_records)
    frozen = batch.get('snapshot')
    if isinstance(frozen, str):
        frozen = json.loads(frozen or '[]')
    return calculate_basis_hash(current_version, current) != batch.get('basis_hash')
