from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (AUDIT_ROLES, CLOSE_BATCH_ROLES, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, REINSPECT_ROLES, SIGN_ROLES, TITLE, VIEW_ROLES,
                    close_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition, validate_transition)


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
        executor = payload.get("executor")
        if executor is not None:
            executor = require_text(executor, "executor", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor, executor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        records = self.repository.list_records(item_id)
        blockers = close_blockers(target, records)
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
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 关闭批次 ----

    def submit_close(self, item_id: int, expected_version: int,
                     actor: str, role: str) -> Dict[str, Any]:
        """提交关闭：冻结事故版本与全部措施依据，并拦住未关闭/未独立复验的措施。"""
        ensure_role(role, CLOSE_BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        batch = self.repository.create_close_batch(item_id, expected_version, actor)
        steps = [
            ("close_batch_submitted",
             lambda c, b, a: {"item_version": b["item_version"]}),
            ("close_batch_checked", self.repository._check_effect),
        ]
        return self.repository.ensure_batch_progress(batch["id"], actor, steps)

    def sign_close(self, batch_id: int, actor: str, role: str) -> Dict[str, Any]:
        """安全经理签字：签字后接着关闭，失败可从检查点恢复。"""
        ensure_role(role, SIGN_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_close_batch(batch_id)
        if batch["status"] == "invalidated":
            from .domain import ConflictError
            raise ConflictError("批次已作废，请重新确认后再签字")
        if batch["status"] == "blocked":
            from .domain import ConflictError
            raise ConflictError("批次存在未解决的拦住项，不能签字")
        if batch["status"] == "closed":
            return batch
        steps = [
            ("close_batch_signed", self.repository._sign_effect),
            ("close_batch_closed", self.repository._close_effect),
        ]
        return self.repository.ensure_batch_progress(batch_id, actor, steps)

    def reconfirm_batch(self, batch_id: int, actor: str, role: str) -> Dict[str, Any]:
        """作废后重新确认：重新冻结快照并重新检查。"""
        ensure_role(role, CLOSE_BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_close_batch(batch_id)
        if batch["status"] not in ("invalidated", "blocked"):
            from .domain import ConflictError
            raise ConflictError("当前批次状态不需要重新确认")
        steps = [
            ("close_batch_resubmitted", self.repository._resubmit_effect),
            ("close_batch_rechecked", self.repository._check_effect),
        ]
        return self.repository.ensure_batch_progress(batch_id, actor, steps)

    def list_close_batches(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_close_batches(item_id)

    def resume_batch(self, batch_id: int, actor: str, role: str) -> Dict[str, Any]:
        """从检查点恢复未完成的批次，按操作号接着办且不重复追加。"""
        ensure_role(role, CLOSE_BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_close_batch(batch_id)
        if batch["status"] == "pending":
            steps = [
                ("close_batch_submitted",
                 lambda c, b, a: {"item_version": b["item_version"]}),
                ("close_batch_checked", self.repository._check_effect),
            ]
        elif batch["status"] == "awaiting_sign":
            steps = [
                ("close_batch_signed", self.repository._sign_effect),
                ("close_batch_closed", self.repository._close_effect),
            ]
        elif batch["status"] == "signed":
            steps = [("close_batch_closed", self.repository._close_effect)]
        elif batch["status"] == "blocked":
            from .domain import ConflictError
            raise ConflictError("批次存在未解决的拦住项，不能恢复")
        elif batch["status"] == "invalidated":
            from .domain import ConflictError
            raise ConflictError("批次已作废，请重新确认")
        elif batch["status"] == "closed":
            return batch
        else:
            from .domain import ConflictError
            raise ConflictError("未知批次状态")
        return self.repository.ensure_batch_progress(batch_id, actor, steps)

    # ---- 复验 ----

    def reinspect_record(self, record_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        """对措施做复验：复验人不得与执行人相同，否则拦住关闭。"""
        ensure_role(role, REINSPECT_ROLES)
        actor = require_text(actor, "actor", 100)
        reinspected_by = require_text(payload.get("reinspected_by"), "reinspected_by", 100)
        reinspection_ref = payload.get("reinspection_ref")
        if reinspection_ref is not None:
            reinspection_ref = require_text(reinspection_ref, "reinspection_ref", 100)
        record = self.repository.update_record_reinspection(
            record_id, reinspected_by, reinspection_ref, actor)
        self.repository.append_audit("reinspect", ENTITY, record["item_id"], actor, {
            "record_id": record_id, "reinspected_by": reinspected_by,
            "reinspection_ref": reinspection_ref,
        })
        return record

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
