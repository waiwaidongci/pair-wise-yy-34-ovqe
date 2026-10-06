import threading
import unittest
from unittest import mock

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.rules import TRANSITION_ROLES
from src.service import Service
import tempfile
from pathlib import Path


class CloseBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "close batch item", "description": "close batch flow",
             "severity": "serious", "quantity": 5, "threshold": 10,
             "external_ref": "CB-1"},
            "creator", "reporter")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _advance_to_verification(self, item):
        for target in ["investigating", "corrective_action", "verification"]:
            item = self.service.transition(
                item["id"], target, item["version"],
                "reviewer", TRANSITION_ROLES[target][0])
        return item

    def _add_measure(self, item_id, executor="exec1", status="closed"):
        return self.service.add_record(
            item_id,
            {"kind": "action", "detail": "corrective measure",
             "status": status, "executor": executor},
            "recorder", "investigator")

    def _reinspect(self, record_id, by="inspector1", ref=None):
        payload = {"reinspected_by": by}
        if ref is not None:
            payload["reinspection_ref"] = ref
        return self.service.reinspect_record(
            record_id, payload, "recorder", "investigator")

    # ---- 快照冻结 ----

    def test_snapshot_freezes_version_and_records(self):
        self._add_measure(self.item["id"])
        self._reinspect(self.item["id"] and self.service.list_records(
            self.item["id"], "viewer")[0]["id"])
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        self.assertEqual(batch["item_version"], item["version"])
        self.assertEqual(batch["snapshot"]["item"]["version"], item["version"])
        self.assertEqual(len(batch["snapshot"]["records"]), 1)
        # 提交后事故版本递增，快照保留提交时的版本
        updated = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(updated["version"], item["version"] + 1)

    # ---- 拦住项 ----

    def test_pending_reinspection_blocks(self):
        # 措施已关闭但未复验 -> 待补核 -> 拦住
        self._add_measure(self.item["id"])
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        self.assertEqual(batch["status"], "blocked")
        with self.assertRaises(ConflictError):
            self.service.sign_close(batch["id"], "safety", "safety_manager")

    def test_open_measure_blocks(self):
        self._add_measure(self.item["id"], status="open")
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        self.assertEqual(batch["status"], "blocked")

    def test_same_person_reinspection_blocks(self):
        rec = self._add_measure(self.item["id"], executor="exec1")
        self._reinspect(rec["id"], by="exec1")  # 复验人同执行人
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        self.assertEqual(batch["status"], "blocked")

    def test_independent_reinspection_allows_and_closes(self):
        rec = self._add_measure(self.item["id"], executor="exec1")
        self._reinspect(rec["id"], by="inspector1")
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        self.assertEqual(batch["status"], "awaiting_sign")
        batch = self.service.sign_close(batch["id"], "safety", "safety_manager")
        self.assertEqual(batch["status"], "closed")
        closed = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(closed["status"], "closed")

    def test_evidence_does_not_require_reinspection(self):
        self.service.add_record(
            self.item["id"],
            {"kind": "evidence", "detail": "photo", "status": "closed"},
            "recorder", "investigator")
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        self.assertEqual(batch["status"], "awaiting_sign")

    # ---- 作废与重新确认 ----

    def test_basis_change_invalidates_and_reconfirm(self):
        rec = self._add_measure(self.item["id"], executor="exec1")
        self._reinspect(rec["id"], by="inspector1")
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        self.assertEqual(batch["status"], "awaiting_sign")
        # 改动复验依据 -> 未完成批次作废
        self._reinspect(rec["id"], by="inspector2", ref="REF-2")
        batch = self.repo.get_close_batch(batch["id"])
        self.assertEqual(batch["status"], "invalidated")
        # 重新确认后可签字关闭
        batch = self.service.reconfirm_batch(
            batch["id"], "investigator", "investigator")
        self.assertEqual(batch["status"], "awaiting_sign")
        batch = self.service.sign_close(batch["id"], "safety", "safety_manager")
        self.assertEqual(batch["status"], "closed")

    def test_closed_accident_keeps_snapshot(self):
        rec = self._add_measure(self.item["id"], executor="exec1")
        self._reinspect(rec["id"], by="inspector1")
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        batch = self.service.sign_close(batch["id"], "safety", "safety_manager")
        self.assertEqual(batch["status"], "closed")
        snapshot_before = batch["snapshot"]
        # 关闭后再改依据 -> 已关闭事故保留原快照
        self._reinspect(rec["id"], by="inspector2")
        batch = self.repo.get_close_batch(batch["id"])
        self.assertEqual(batch["status"], "closed")
        self.assertEqual(batch["snapshot"], snapshot_before)

    # ---- 并发提交 ----

    def test_concurrent_submit_first_wins(self):
        item = self.service.get_item(self.item["id"], "viewer")
        results = []
        errors = []

        def do_submit(actor):
            try:
                results.append(self.service.submit_close(
                    self.item["id"], item["version"],
                    actor, "investigator"))
            except Exception as exc:
                errors.append(exc)

        # 两名调查员同时提交同一事故
        t1 = threading.Thread(target=do_submit, args=("investigator1",))
        t2 = threading.Thread(target=do_submit, args=("investigator2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        self.assertEqual(results[0]["status"], "awaiting_sign")

    # ---- 从检查点恢复 ----

    def test_sign_recovers_from_audit_failure(self):
        rec = self._add_measure(self.item["id"], executor="exec1")
        self._reinspect(rec["id"], by="inspector1")
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        self.assertEqual(batch["checkpoint"], 2)
        # 签字时审计写入失败
        with mock.patch("src.repository.make_entry",
                        side_effect=RuntimeError("audit write failed")):
            with self.assertRaises(RuntimeError):
                self.service.sign_close(batch["id"], "safety", "safety_manager")
        # 检查点仍在 2，状态未变
        batch = self.repo.get_close_batch(batch["id"])
        self.assertEqual(batch["checkpoint"], 2)
        self.assertEqual(batch["status"], "awaiting_sign")
        # 重试：从检查点接着办，不重复追加
        batch = self.service.sign_close(batch["id"], "safety", "safety_manager")
        self.assertEqual(batch["status"], "closed")
        self.assertEqual(batch["checkpoint"], 4)
        events = self.service.audit("viewer", self.item["id"])
        batch_events = [e for e in events
                        if e["entity_type"] == "close_batch"
                        and e["entity_id"] == batch["id"]]
        op_numbers = [e["op_number"] for e in batch_events]
        self.assertEqual(op_numbers, [1, 2, 3, 4])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_submit_recovers_from_audit_failure(self):
        item = self.service.get_item(self.item["id"], "viewer")
        # 提交时审计写入失败
        with mock.patch("src.repository.make_entry",
                        side_effect=RuntimeError("audit write failed")):
            with self.assertRaises(RuntimeError):
                self.service.submit_close(
                    self.item["id"], item["version"],
                    "investigator", "investigator")
        # 批次已落账，检查点为 0
        batches = self.service.list_close_batches(self.item["id"], "viewer")
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["checkpoint"], 0)
        # 重试提交：恢复已有批次，接着办
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        self.assertEqual(batch["status"], "awaiting_sign")
        self.assertEqual(batch["checkpoint"], 2)

    def test_resume_batch(self):
        rec = self._add_measure(self.item["id"], executor="exec1")
        self._reinspect(rec["id"], by="inspector1")
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        with mock.patch("src.repository.make_entry",
                        side_effect=RuntimeError("audit write failed")):
            with self.assertRaises(RuntimeError):
                self.service.sign_close(batch["id"], "safety", "safety_manager")
        # 通过 resume_batch 从检查点恢复
        batch = self.service.resume_batch(
            batch["id"], "safety", "safety_manager")
        self.assertEqual(batch["status"], "closed")
        self.assertEqual(batch["checkpoint"], 4)

    # ---- 权限 ----

    def test_sign_requires_safety_manager(self):
        item = self._advance_to_verification(
            self.service.get_item(self.item["id"], "viewer"))
        batch = self.service.submit_close(
            self.item["id"], item["version"], "investigator", "investigator")
        with self.assertRaises(PermissionDenied):
            self.service.sign_close(batch["id"], "investigator", "investigator")

    # ---- 旧数据迁移 ----

    def test_legacy_record_pending_reinspection(self):
        # 缺复验字段的措施升级为待补核，普通查询照旧
        rec = self.service.add_record(
            self.item["id"],
            {"kind": "action", "detail": "old measure", "status": "closed"},
            "recorder", "investigator")
        self.assertEqual(rec["reinspection_state"], "pending_reinspection")
        records = self.service.list_records(self.item["id"], "viewer")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reinspection_state"], "pending_reinspection")
        self.assertTrue(records[0]["is_measure"])

    # ---- 直接转换也拦住复验 ----

    def test_transition_requires_reinspection(self):
        self._add_measure(self.item["id"], executor="exec1")
        item = self.service.get_item(self.item["id"], "viewer")
        for target in ["investigating", "corrective_action", "verification"]:
            item = self.service.transition(
                item["id"], target, item["version"],
                "reviewer", TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self.service.transition(
                item["id"], "closed", item["version"],
                "safety", "safety_manager")


if __name__ == "__main__":
    unittest.main()
