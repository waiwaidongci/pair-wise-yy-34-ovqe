from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, RecoverableError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_CONFIRM_ROLES, BATCH_PENDING,
                    BATCH_SUBMIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    VIEW_ROLES, calculate_basis_hash, collect_closure_blockers,
                    effective_verify_status, is_measure, priority_score,
                    response_deadline_hours, escalation_required,
                    role_for_transition, snapshot_measures, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

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
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
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
        # 纠正措施：可显式标记is_measure；登记执行人时即记录执行依据。
        measure_flag = bool(payload.get("is_measure", False))
        executed_by = payload.get("executed_by")
        if executed_by is not None:
            executed_by = require_text(executed_by, "executed_by", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor,
                                            is_measure=measure_flag,
                                            executed_by=executed_by)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
            "is_measure": is_measure(record),
        })
        if measure_flag:
            # 新增措施若使未完成批次作废，作废事件已入检查点，立即尝试续办。
            self._flush_silently()
        return self._present_record(record)

    # ---- 措施执行与独立复验 -------------------------------------------------

    def execute_measure(self, record_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ("investigator",))
        actor = require_text(actor, "actor", 100)
        executor = require_text(payload.get("executed_by", actor), "executed_by", 100)
        record = self.repository.get_record(record_id)
        if not is_measure(record):
            # 普通记录按措施执行无意义，明确拒绝以免误操作。
            raise ConflictError("该记录不是纠正措施")
        updated = self.repository.mark_measure_executed(record_id, executor)
        self.repository.append_audit("measure_executed", ENTITY,
                                     updated["item_id"], actor, {
                                         "record_id": record_id,
                                         "executed_by": executor})
        # 作废事件随业务事务已进入检查点，立刻尝试重放（失败也可后续恢复）。
        self._flush_silently()
        return self._present_record(updated)

    def verify_measure(self, record_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ("safety_manager", "investigator"))
        actor = require_text(actor, "actor", 100)
        verifier = require_text(payload.get("verified_by", actor), "verified_by", 100)
        verify_ref = require_text(payload.get("verify_ref"), "verify_ref", 100)
        verify_detail = payload.get("verify_detail")
        if verify_detail is not None:
            verify_detail = require_text(verify_detail, "verify_detail", 2000)
        record = self.repository.get_record(record_id)
        if not is_measure(record):
            raise ConflictError("该记录不是纠正措施")
        # 独立复验硬门槛：复验人不得与执行人相同（安全经理签字前发现也为时不晚）。
        executor = (record.get("executed_by") or "").strip()
        if executor and executor == verifier.strip():
            raise ConflictError("复验人与执行人相同，复验必须独立")
        updated = self.repository.verify_measure(record_id, verifier, verify_ref,
                                                 verify_detail)
        voided = updated.pop("_voided_batches", 0)
        # 复验依据改动后，未完成批次作废：作废事件已在同事务进入检查点。
        replay = self.repository.flush_pending_ops()
        self.repository.append_audit("measure_verified", ENTITY,
                                     record["item_id"], actor, {
                                         "record_id": record_id,
                                         "verified_by": verifier,
                                         "verify_ref": verify_ref,
                                         "voided_batches": voided})
        result = self._present_record(updated)
        result["voided_batches"] = voided
        result["replay"] = replay
        return result

    # ---- 可恢复的关闭批次 ---------------------------------------------------

    def submit_closure_batch(self, item_id: int, payload: Dict[str, Any],
                             actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("事故已关闭，原关闭快照保留")
        if item["status"] != "verification":
            raise ConflictError(f"事故当前为{item['status']}，需先进入verification才能提交关闭批次")
        records = self.repository.list_records(item_id)
        # 未关闭或未独立复验（含待补核）的措施拦住推进；不允许拿旧措施直接推进。
        blockers = collect_closure_blockers(records)
        if blockers:
            self.repository.append_audit("closure_blocked", ENTITY, item_id, actor, {
                "blockers": blockers, "expected_version": expected_version})
            raise ConflictError("关闭被拦截：" + "；".join(blockers),
                                {"blockers": blockers})
        measures = snapshot_measures(records)
        # 冻结事故版本与全部措施依据：提交占用新版本号frozen=expected+1，
        # 后到提交方因expected_version对不上而失败，并从错误中拿到新版本。
        basis_hash = calculate_basis_hash(expected_version + 1, measures)
        batch = self.repository.create_closure_batch(
            item_id, expected_version, actor, measures, basis_hash,
            {"measures_count": len(measures), "blockers": []})
        # 业务已落账；审计按操作号进入检查点，写入失败后仍可恢复且不重复追加。
        try:
            replay = self.repository.flush_pending_ops(batch["id"])
        except Exception as exc:  # pragma: no cover - 防御：flush内部已吞掉单次失败
            raise RecoverableError(
                "批次已提交，审计追加失败，可调用恢复接口按操作号续办",
                {"batch_id": batch["id"], "recover": f"/api/closure-batches/{batch['id']}/recover"}
            ) from exc
        result = self._present_batch(batch, item)
        result["replay"] = replay
        return result

    def confirm_closure_batch(self, batch_id: int, payload: Dict[str, Any],
                              actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_closure_batch(batch_id)
        item = self.repository.get_item(batch["item_id"])
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        if batch["status"] == "voided":
            raise ConflictError("批次已作废：复验依据改动后需重新提交",
                                {"void_reason": batch.get("void_reason")})
        if expected_version != batch["frozen_item_version"]:
            raise ConflictError("事故版本与批次冻结版本不一致，后到提交请使用新版本",
                                {"current_version": item["version"],
                                 "frozen_version": batch["frozen_item_version"]})
        records = self.repository.list_records(batch["item_id"])
        current_hash = calculate_basis_hash(item["version"], snapshot_measures(records))
        if current_hash != batch["basis_hash"]:
            raise ConflictError("措施依据已改动，未完成批次作废并需重新确认")
        blockers = collect_closure_blockers(records)
        if blockers:
            raise ConflictError("关闭被拦截：" + "；".join(blockers),
                                {"blockers": blockers})
        updated = self.repository.confirm_closure_batch(
            batch_id, expected_version, actor)
        try:
            replay = self.repository.flush_pending_ops(batch_id)
        except Exception as exc:  # pragma: no cover
            raise RecoverableError(
                "事故已关闭落账，审计追加失败，可从检查点恢复",
                {"batch_id": batch_id, "recover": f"/api/closure-batches/{batch_id}/recover"}
            ) from exc
        closed_item = self.repository.get_item(batch["item_id"])
        result = self._present_batch(updated, closed_item)
        result["replay"] = replay
        return result

    def recover_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        """检查点恢复：按操作号(seq)续办；已追加的审计跳过，绝不重复。"""
        ensure_role(role, AUDIT_ROLES)
        batch = self.repository.get_closure_batch(batch_id)
        replay = self.repository.flush_pending_ops(batch_id)
        pending = self.repository.pending_ops(batch_id)
        return {
            "batch_id": batch_id,
            "batch_status": batch["status"],
            "resumed_from_seq": (pending[0]["seq"] if pending else None),
            "result": replay,
            "chain_ok": self.repository.verify_audit_chain(),
            "pending": [{"seq": op["seq"], "op_key": op["op_key"],
                         "action": op["action"]} for op in pending],
        }

    def recover_all(self, role: str) -> Dict[str, Any]:
        ensure_role(role, AUDIT_ROLES)
        replay = self.repository.flush_pending_ops()
        return {"result": replay, "chain_ok": self.repository.verify_audit_chain()}

    def list_closure_batches(self, item_id: Optional[int], role: str,
                             status: Optional[str] = None) -> list:
        self._view(role)
        batches = self.repository.list_closure_batches(item_id, status)
        result = []
        for batch in batches:
            item = self.repository.get_item(batch["item_id"])
            result.append(self._present_batch(batch, item))
        return result

    def get_closure_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        batch = self.repository.get_closure_batch(batch_id)
        item = self.repository.get_item(batch["item_id"])
        return self._present_batch(batch, item)

    # ---- 既有流程（保持兼容） -------------------------------------------------

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_or_measures_open(target, self.repository.list_records(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
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
        records = self.repository.list_records(item_id)
        return [self._present_record(r) for r in records]

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 呈现与组装 ----------------------------------------------------------

    @staticmethod
    def _present_record(record: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(record)
        measure = is_measure(record)
        result["is_measure"] = measure
        # 普通记录不附带复验语义；普通查询照旧。历史措施缺字段时升级为待补核。
        result["verify_status_effective"] = effective_verify_status(record) if measure else None
        return result

    @staticmethod
    def _present_batch(batch: Dict[str, Any], item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = dict(batch)
        result["pending"] = batch["status"] == BATCH_PENDING
        if item is not None:
            result["item_status"] = item["status"]
            result["current_item_version"] = item["version"]
        return result

    def _flush_silently(self) -> None:
        try:
            self.repository.flush_pending_ops()
        except Exception:
            pass

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result


def completion_or_measures_open(target: str, records: list) -> list:
    """旧状态机关闭不变量：未关闭事项计数；并叠加措施独立复验门槛。"""
    blockers = []
    if target == "closed":
        if any(r.get("status") != "closed" for r in records):
            blockers.append("仍有未关闭事项")
        blockers.extend(collect_closure_blockers(records))
    return blockers
