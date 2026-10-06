from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (ID_PREFIX, STATES, is_measure, measure_blockers,
                    reinspection_state, validate_transition)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        self._migrate()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS close_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    item_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    blockers TEXT NOT NULL DEFAULT '[]',
                    checkpoint INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_by TEXT,
                    confirmed_at TEXT,
                    closed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_close_batches_active
                    ON close_batches(item_id)
                    WHERE status IN ('pending','blocked','awaiting_sign','signed');
            """)

    def _migrate(self) -> None:
        """为旧库补充复验字段与批次字段；缺字段的措施在查询时升级为待补核。"""
        with self._lock, self.conn:
            record_cols = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(records)").fetchall()
            }
            for col, ddl in (
                ("executor", "ALTER TABLE records ADD COLUMN executor TEXT"),
                ("reinspected_by", "ALTER TABLE records ADD COLUMN reinspected_by TEXT"),
                ("reinspected_at", "ALTER TABLE records ADD COLUMN reinspected_at TEXT"),
                ("reinspection_ref", "ALTER TABLE records ADD COLUMN reinspection_ref TEXT"),
            ):
                if col not in record_cols:
                    self.conn.execute(ddl)
            audit_cols = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(audit_events)").fetchall()
            }
            for col, ddl in (
                ("batch_id", "ALTER TABLE audit_events ADD COLUMN batch_id INTEGER"),
                ("op_number", "ALTER TABLE audit_events ADD COLUMN op_number INTEGER"),
            ):
                if col not in audit_cols:
                    self.conn.execute(ddl)
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_audit_batch_op "
                "ON audit_events(batch_id, op_number)"
            )

    @contextmanager
    def _immediate(self):
        """可重入的写事务：BEGIN IMMEDIATE 保证先落账者生效。"""
        self._lock.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._lock.release()

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["is_measure"] = is_measure(item.get("kind"))
        item["reinspection_state"] = reinspection_state(item)
        return item

    def _batch(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["snapshot"] = json.loads(item["snapshot"])
        item["blockers"] = json.loads(item["blockers"])
        return item

    def _snapshot(self, item_id: int) -> Dict[str, Any]:
        return {
            "item": self.get_item(item_id),
            "records": self.list_records(item_id),
            "snapshotted_at": utc_now(),
        }

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def _transition_item_tx(self, conn: sqlite3.Connection, item_id: int, target: str,
                            expected_version: int, actor: str) -> None:
        now = utc_now()
        cur = conn.execute(
            """UPDATE items SET status=?, version=version+1, updated_at=?
               WHERE id=? AND version=?""",
            (target, now, item_id, expected_version),
        )
        if cur.rowcount == 0:
            raise ConflictError("版本冲突，请刷新后重试")

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   executor: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at, executor) VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now, executor),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        # 措施变更会让已冻结的快照失效
        if is_measure(kind):
            self.invalidate_active_batch(item_id, "measure_added", actor)
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return self._record(row)

    def get_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("记录不存在")
        return self._record(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._record(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def update_record_reinspection(self, record_id: int, reinspected_by: str,
                                   reinspection_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFoundError("记录不存在")
            self.conn.execute(
                """UPDATE records SET reinspected_by=?, reinspected_at=?, reinspection_ref=?
                   WHERE id=?""",
                (reinspected_by, now, reinspection_ref, record_id),
            )
            item_id = row["item_id"]
        self.invalidate_active_batch(item_id, "reinspection_changed", actor)
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return self._record(row)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at, batch_id, op_number)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"],
                 None, None),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += """ WHERE entity_id=? OR (entity_type='close_batch' AND entity_id IN (
                       SELECT id FROM close_batches WHERE item_id=?))"""
            params = (entity_id, entity_id)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 关闭批次 ----

    def create_close_batch(self, item_id: int, expected_version: int,
                           actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._immediate():
            item = self.get_item(item_id)
            active = self.conn.execute(
                """SELECT * FROM close_batches WHERE item_id=?
                   AND status IN ('pending','blocked','awaiting_sign','signed')
                   ORDER BY id DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
            if active:
                # 恢复：同一调查员、同一冻结版本的批次视为上次审计写入失败后的重试
                if active["item_version"] == expected_version and active["created_by"] == actor:
                    return self._batch(active)
                raise ConflictError("已有进行中的关闭批次")
            if item["version"] != expected_version:
                raise ConflictError("版本冲突，请刷新后重试")
            snapshot = self._snapshot(item_id)
            try:
                cur = self.conn.execute(
                    """INSERT INTO close_batches(item_id, item_version, status, snapshot,
                       blockers, checkpoint, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, expected_version, "pending",
                     json.dumps(snapshot, ensure_ascii=False), "[]", 0, actor, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("已有进行中的关闭批次") from exc
            batch_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE items SET version=version+1, updated_at=? WHERE id=? AND version=?",
                (now, item_id, expected_version),
            )
        return self.get_close_batch(batch_id)

    def get_close_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM close_batches WHERE id=?", (batch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("关闭批次不存在")
        return self._batch(row)

    def list_close_batches(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM close_batches WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._batch(row) for row in rows]

    def invalidate_active_batch(self, item_id: int, reason: str, actor: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                """SELECT id FROM close_batches WHERE item_id=?
                   AND status IN ('pending','blocked','awaiting_sign','signed')
                   ORDER BY id DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
        if row is None:
            return False
        batch_id = int(row["id"])
        self._apply_op(batch_id, "close_batch_invalidated", actor,
                       self._invalidate_effect(batch_id, reason))
        return True

    def _invalidate_effect(self, batch_id: int, reason: str):
        def effect(conn: sqlite3.Connection, batch: Dict[str, Any], actor: str) -> Dict[str, Any]:
            conn.execute(
                "UPDATE close_batches SET status='invalidated' WHERE id=?", (batch_id,)
            )
            return {"reason": reason}
        return effect

    def _check_effect(self, conn: sqlite3.Connection, batch: Dict[str, Any],
                      actor: str) -> Dict[str, Any]:
        records = self.list_records(batch["item_id"])
        blockers = measure_blockers(records)
        status = "blocked" if blockers else "awaiting_sign"
        conn.execute(
            "UPDATE close_batches SET status=?, blockers=? WHERE id=?",
            (status, json.dumps(blockers, ensure_ascii=False), batch["id"]),
        )
        return {"blockers": blockers, "result": status}

    def _sign_effect(self, conn: sqlite3.Connection, batch: Dict[str, Any],
                     actor: str) -> Dict[str, Any]:
        if batch["status"] != "awaiting_sign":
            raise ConflictError("当前批次状态不允许签字")
        conn.execute(
            """UPDATE close_batches SET status='signed', confirmed_by=?, confirmed_at=?
               WHERE id=?""",
            (actor, utc_now(), batch["id"]),
        )
        return {"confirmed_by": actor}

    def _close_effect(self, conn: sqlite3.Connection, batch: Dict[str, Any],
                      actor: str) -> Dict[str, Any]:
        if batch["status"] != "signed":
            raise ConflictError("当前批次状态不允许关闭")
        item = self.get_item(batch["item_id"])
        validate_transition(item["status"], "closed")
        if item["status"] != "closed":
            self._transition_item_tx(
                conn, batch["item_id"], "closed", item["version"], actor
            )
        conn.execute(
            "UPDATE close_batches SET status='closed', closed_at=? WHERE id=?",
            (utc_now(), batch["id"]),
        )
        return {"closed_by": actor}

    def _resubmit_effect(self, conn: sqlite3.Connection, batch: Dict[str, Any],
                          actor: str) -> Dict[str, Any]:
        item = self.get_item(batch["item_id"])
        snapshot = self._snapshot(batch["item_id"])
        conn.execute(
            """UPDATE close_batches SET snapshot=?, item_version=?, status='pending',
               blockers='[]' WHERE id=?""",
            (json.dumps(snapshot, ensure_ascii=False), item["version"], batch["id"]),
        )
        return {"item_version": item["version"]}

    def _apply_op(self, batch_id: int, action: str, actor: str,
                  side_effect) -> Dict[str, Any]:
        """原子地执行一个操作：副作用 + 追加审计 + 推进检查点。

        若该操作号已追加过审计事件则跳过，保证按操作号接着办且不重复追加。
        """
        with self._immediate():
            batch = self.get_close_batch(batch_id)
            op = int(batch["checkpoint"]) + 1
            existing = self.conn.execute(
                "SELECT id FROM audit_events WHERE batch_id=? AND op_number=?",
                (batch_id, op),
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE close_batches SET checkpoint=? WHERE id=?", (op, batch_id)
                )
                return self.get_close_batch(batch_id)
            detail = side_effect(self.conn, batch, actor) if side_effect else {}
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, "close_batch", batch_id, actor, detail, previous)
            self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at, batch_id, op_number)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (event["action"], "close_batch", batch_id, actor,
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"],
                 batch_id, op),
            )
            self.conn.execute(
                "UPDATE close_batches SET checkpoint=? WHERE id=?", (op, batch_id)
            )
        return self.get_close_batch(batch_id)

    def ensure_batch_progress(self, batch_id: int, actor: str,
                              steps: List[tuple]) -> Dict[str, Any]:
        for action, effect in steps:
            self._apply_op(batch_id, action, actor, effect)
        return self.get_close_batch(batch_id)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
