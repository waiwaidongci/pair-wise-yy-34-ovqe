import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, RecoverableError
from src.repository import Repository
from src.service import Service


class ClosureBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        item = self.service.create_item(
            {"title": "closure", "description": "batch", "severity": "serious",
             "quantity": 5, "threshold": 10, "external_ref": "CLOSE-1"},
            "creator", "reporter")
        self.item_id = item["id"]
        self.measure = self.service.add_record(
            self.item_id,
            {"kind": "measure", "detail": "fix guard", "is_measure": True,
             "external_ref": "M-1"},
            "recorder", "investigator")
        current = item
        for target, role in (("investigating", "investigator"),
                             ("corrective_action", "investigator"),
                             ("verification", "safety_manager")):
            current = self.service.transition(
                current["id"], target, current["version"], "u", role)
        self.item = self.service.get_item(self.item_id, "viewer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _verify(self, verifier="verifier-a", ref="VR-1", executor="executor-a"):
        self.service.execute_measure(self.measure["id"],
                                     {"executed_by": executor}, "inv", "investigator")
        self.service.verify_measure(self.measure["id"], {"verify_ref": ref},
                                    verifier, "safety_manager")

    def test_open_or_non_independent_measure_blocks_closure(self):
        # 措施未关闭/未复验：拦截推进
        with self.assertRaises(ConflictError) as ctx:
            self.service.submit_closure_batch(
                self.item_id, {"expected_version": self.item["version"]},
                "inv", "investigator")
        self.assertIn("措施#", ctx.exception.message)
        # 复验人与执行人相同：复验直接被拒绝，签字前即可发现
        self.service.execute_measure(self.measure["id"],
                                     {"executed_by": "same-person"}, "inv", "investigator")
        with self.assertRaises(ConflictError):
            self.service.verify_measure(
                self.measure["id"], {"verify_ref": "VR-X"},
                "same-person", "safety_manager")

    def test_batch_freezes_snapshot_and_closes(self):
        self._verify()
        current = self.service.get_item(self.item_id, "viewer")
        batch = self.service.submit_closure_batch(
            current["id"], {"expected_version": current["version"]},
            "inv", "investigator")
        self.assertEqual(batch["status"], "pending")
        self.assertEqual(batch["frozen_item_version"], current["version"] + 1)
        self.assertEqual(len(batch["snapshot"]), 1)
        # 只有安全经理能确认关闭
        with self.assertRaises(PermissionDenied):
            self.service.confirm_closure_batch(
                batch["id"], {"expected_version": batch["frozen_item_version"]},
                "inv", "investigator")
        closed = self.service.confirm_closure_batch(
            batch["id"], {"expected_version": batch["frozen_item_version"]},
            "sm", "safety_manager")
        self.assertEqual(closed["status"], "closed")
        item = self.service.get_item(self.item_id, "viewer")
        self.assertEqual(item["status"], "closed")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_reverify_change_voids_pending_batch(self):
        self._verify()
        current = self.service.get_item(self.item_id, "viewer")
        batch = self.service.submit_closure_batch(
            current["id"], {"expected_version": current["version"]},
            "inv", "investigator")
        # 复验依据改动：未完成批次作废
        res = self.service.verify_measure(
            self.measure["id"], {"verify_ref": "VR-2", "verify_detail": "recheck"},
            "verifier-b", "safety_manager")
        self.assertEqual(res["voided_batches"], 1)
        with self.assertRaises(ConflictError):
            self.service.confirm_closure_batch(
                batch["id"], {"expected_version": batch["frozen_item_version"]},
                "sm", "safety_manager")
        # 重新提交并关闭；已关闭批次保留原快照
        fresh = self.service.get_item(self.item_id, "viewer")
        batch2 = self.service.submit_closure_batch(
            fresh["id"], {"expected_version": fresh["version"]},
            "inv", "investigator")
        closed = self.service.confirm_closure_batch(
            batch2["id"], {"expected_version": batch2["frozen_item_version"]},
            "sm", "safety_manager")
        self.assertEqual(closed["status"], "closed")
        batches = self.service.list_closure_batches(self.item_id, "viewer")
        self.assertEqual([b["status"] for b in batches], ["voided", "closed"])
        # 已关闭事故不能再改动复验依据，快照保留
        with self.assertRaises(ConflictError):
            self.service.verify_measure(
                self.measure["id"], {"verify_ref": "VR-3"},
                "verifier-c", "safety_manager")


if __name__ == "__main__":
    unittest.main()
