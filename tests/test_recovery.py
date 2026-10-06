import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError
from src.repository import Repository
from src.service import Service


class FailureRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "test.db")
        self.repo = Repository(self.path)
        self.service = Service(self.repo)
        item = self.service.create_item(
            {"title": "recovery", "description": "checkpoint", "severity": "moderate"},
            "creator", "reporter")
        self.item_id = item["id"]
        self.measure = self.service.add_record(
            self.item_id, {"kind": "measure", "detail": "fix", "is_measure": True},
            "recorder", "investigator")
        current = item
        for target, role in (("investigating", "investigator"),
                             ("corrective_action", "investigator"),
                             ("verification", "safety_manager")):
            current = self.service.transition(
                current["id"], target, current["version"], "u", role)
        self.service.execute_measure(self.measure["id"], {"executed_by": "exe"},
                                     "inv", "investigator")
        self.service.verify_measure(self.measure["id"], {"verify_ref": "VR-1"},
                                    "ver", "safety_manager")
        self.item = self.service.get_item(self.item_id, "viewer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_audit_failure_recovers_by_seq_without_duplicates(self):
        # 提交时审计写入失败：批次仍落账，操作停在检查点
        self.repo.fail_next_audit_write = True
        batch = self.service.submit_closure_batch(
            self.item_id, {"expected_version": self.item["version"]},
            "inv", "investigator")
        pending = self.repo.pending_ops(batch["id"])
        self.assertEqual([p["seq"] for p in pending], [1])
        # 恢复：按操作号续办
        rec = self.service.recover_batch(batch["id"], "safety_manager")
        self.assertEqual(rec["result"]["applied"], 1)
        self.assertTrue(rec["chain_ok"])
        # 再次恢复不重复追加
        again = self.service.recover_batch(batch["id"], "safety_manager")
        self.assertEqual(again["result"]["remaining"], 0)
        submitted = [e for e in self.service.audit("viewer")
                     if e["action"] == "closure_batch_submitted"]
        self.assertEqual(len(submitted), 1)

        # 确认阶段审计再失败：业务已关闭，检查点恢复
        self.repo.fail_next_audit_write = True
        confirmed = self.service.confirm_closure_batch(
            batch["id"], {"expected_version": batch["frozen_item_version"]},
            "sm", "safety_manager")
        self.assertEqual(confirmed["status"], "closed")
        self.assertEqual([p["seq"] for p in self.repo.pending_ops(batch["id"])], [2])
        rec2 = self.service.recover_batch(batch["id"], "safety_manager")
        self.assertEqual(rec2["result"]["applied"], 1)
        self.assertTrue(rec2["chain_ok"])
        rec3 = self.service.recover_batch(batch["id"], "safety_manager")
        self.assertEqual(rec3["result"], {"applied": 0, "skipped": 0, "remaining": 0})
        audit = self.service.audit("viewer")
        self.assertEqual(
            [e["action"] for e in audit if e["action"].startswith("closure_batch_")],
            ["closure_batch_submitted", "closure_batch_confirmed"])

    def test_concurrent_submit_first_commit_wins(self):
        repo2 = Repository(self.path)
        service2 = Service(repo2)
        results = {}
        barrier = threading.Barrier(2)

        def submit(name, svc, delay):
            barrier.wait()
            import time
            time.sleep(delay)
            try:
                b = svc.submit_closure_batch(
                    self.item_id, {"expected_version": self.item["version"]},
                    name, "investigator")
                results[name] = ("ok", b["id"])
            except ConflictError as exc:
                results[name] = ("conflict", exc.detail["current_version"])

        t1 = threading.Thread(target=submit, args=("invA", self.service, 0.0))
        t2 = threading.Thread(target=submit, args=("invB", service2, 0.01))
        t1.start(); t2.start(); t1.join(); t2.join()

        winners = [name for name, value in results.items() if value[0] == "ok"]
        self.assertEqual(len(winners), 1)
        loser = next(name for name, value in results.items() if value[0] == "conflict")
        # 后到者拿到新版本
        self.assertEqual(results[loser][1], self.item["version"] + 1)
        pending = self.service.list_closure_batches(self.item_id, "viewer", "pending")
        self.assertEqual(len(pending), 1)
        repo2.close()


if __name__ == "__main__":
    unittest.main()
