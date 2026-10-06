import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError
from src.repository import Repository
from src.service import Service


LEGACY_DDL = """
CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
 description TEXT NOT NULL, severity TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 0,
 threshold REAL NOT NULL DEFAULT 1, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 external_ref TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
 kind TEXT NOT NULL, detail TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 external_ref TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
 entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL,
 previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
"""


class LegacyDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "legacy.db")
        con = sqlite3.connect(self.path)
        con.executescript(LEGACY_DDL)
        con.execute(
            "INSERT INTO items VALUES(1,'旧事故','d','moderate',1,1,'verification',4,"
            "NULL,'u','t','t')")
        con.executemany(
            "INSERT INTO records(id,item_id,kind,detail,status,external_ref,created_by,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            [(1, 1, "measure", "历史措施缺复验", "closed", None, "u", "t"),
             (2, 1, "evidence", "普通证据", "closed", None, "u", "t"),
             (3, 1, "action", "旧整改缺复验", "open", None, "u", "t")])
        con.commit()
        con.close()
        self.repo = Repository(self.path)
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_legacy_measures_upgraded_to_pending_supplement(self):
        records = self.service.list_records(1, "viewer")
        by_id = {r["id"]: r for r in records}
        # 普通查询照旧：全部记录仍可读取，普通记录无复验语义
        self.assertEqual(set(by_id), {1, 2, 3})
        self.assertIsNone(by_id[2]["verify_status_effective"])
        # 历史措施不静默改写，仅读侧升级为待补核
        self.assertIsNone(by_id[1]["verify_status"])
        self.assertEqual(by_id[1]["verify_status_effective"], "pending_supplement")
        self.assertEqual(by_id[3]["verify_status_effective"], "pending_supplement")
        # 待补核拦住关闭
        with self.assertRaises(ConflictError) as ctx:
            self.service.submit_closure_batch(
                1, {"expected_version": 4}, "inv", "investigator")
        self.assertIn("待补核", ctx.exception.message)

        # 补核后可走批次关闭
        self.service.execute_measure(1, {"executed_by": "exe1"}, "inv", "investigator")
        self.service.verify_measure(1, {"verify_ref": "V1"}, "ver1", "safety_manager")
        self.service.execute_measure(3, {"executed_by": "exe2"}, "inv", "investigator")
        self.service.verify_measure(3, {"verify_ref": "V2"}, "ver2", "safety_manager")
        batch = self.service.submit_closure_batch(
            1, {"expected_version": 4}, "inv", "investigator")
        closed = self.service.confirm_closure_batch(
            batch["id"], {"expected_version": batch["frozen_item_version"]},
            "sm", "safety_manager")
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
