from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import BATCH_PENDING, STATES, VERIFY_PENDING

RECORD_COLUMNS = [
    ("is_measure", "INTEGER NOT NULL DEFAULT 0"),
    ("executed_by", "TEXT"),
    ("executed_at", "TEXT"),
    ("verify_status", "TEXT"),
    ("verified_by", "TEXT"),
    ("verify_ref", "TEXT"),
    ("verify_detail", "TEXT"),
    ("verified_at", "TEXT"),
]


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        # 测试钩子：置真后下一次审计事件写入会失败（事务回滚，业务已落账）。
        self.fail_next_audit_write = False
        self._create_schema()

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
                CREATE TABLE IF NOT EXISTS closure_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT '{BATCH_PENDING}',
                    frozen_item_version INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    basis_hash TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    confirmed_by TEXT,
                    confirmed_at TEXT,
                    voided_by TEXT,
                    voided_at TEXT,
                    void_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_batch_pending
                    ON closure_batches(item_id) WHERE status = '{BATCH_PENDING}';
                CREATE TABLE IF NOT EXISTS closure_ops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES closure_batches(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    op_key TEXT NOT NULL UNIQUE,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','done')),
                    audit_event_id INTEGER,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(batch_id, seq)
                );
            """)
            self._migrate_columns()
            # 历史措施缺复验字段时不静默改写落库，由读侧(effective_verify_status)升级为待补核。

    def _migrate_columns(self) -> None:
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(records)")}
        for name, decl in RECORD_COLUMNS:
            if name not in existing:
                self.conn.execute(f"ALTER TABLE records ADD COLUMN {name} {decl}")
        audit_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(audit_events)")}
        if "op_key" not in audit_cols:
            self.conn.execute("ALTER TABLE audit_events ADD COLUMN op_key TEXT")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_audit_op_key "
            "ON audit_events(op_key) WHERE op_key IS NOT NULL"
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _batch(row: sqlite3.Row) -> Dict[str, Any]:
        batch = dict(row)
        batch["snapshot"] = json.loads(batch["snapshot"])
        return batch

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

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   is_measure: bool = False,
                   executed_by: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        verify_status = VERIFY_PENDING if is_measure else None
        executed_at = now if is_measure and executed_by else None
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at, is_measure, executed_by, executed_at,
                       verify_status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now,
                     1 if is_measure else 0, executed_by, executed_at, verify_status),
                )
                record_id = int(cur.lastrowid)
                # 新登记措施改变了措施依据：未完成批次立即作废并留检查点。
                if is_measure:
                    pending = self.conn.execute(
                        "SELECT id FROM closure_batches WHERE item_id=? AND status='pending'",
                        (item_id,),
                    ).fetchall()
                    for row in pending:
                        batch_id = int(row["id"])
                        seq = self._next_seq_in_tx(self.conn, batch_id)
                        self._insert_op_in_tx(
                            self.conn, batch_id, seq, "closure_batch_voided", "事故",
                            item_id, actor,
                            {"batch_id": batch_id, "record_id": record_id,
                             "reason": "新增措施，批次冻结快照失效"},
                        )
                    self.conn.execute(
                        """UPDATE closure_batches SET status='voided', voided_by=?, voided_at=?,
                           void_reason='新增措施，批次冻结快照失效'
                           WHERE item_id=? AND status='pending'""",
                        (actor, now, item_id),
                    )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        return self.get_record(record_id)

    def get_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("记录不存在")
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 措施执行与独立复验 -------------------------------------------------

    def mark_measure_executed(self, record_id: int, executor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            record = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                raise NotFoundError("记录不存在")
            item = self.conn.execute("SELECT status FROM items WHERE id=?", (record["item_id"],)).fetchone()
            if item["status"] == "closed":
                raise ConflictError("事故已关闭并保留快照，不能再改动措施依据")
            before = (record["status"], record["executed_by"])
            self.conn.execute(
                """UPDATE records SET status='closed', executed_by=?,
                   executed_at=COALESCE(executed_at,?) WHERE id=?""",
                (executor, now, record_id),
            )
            after = ("closed", executor)
            # 执行依据改动也会使冻结快照失效：未完成批次一并作废并留检查点。
            if before != after:
                pending = self.conn.execute(
                    "SELECT id FROM closure_batches WHERE item_id=? AND status='pending'",
                    (record["item_id"],),
                ).fetchall()
                for row in pending:
                    batch_id = int(row["id"])
                    seq = self._next_seq_in_tx(self.conn, batch_id)
                    self._insert_op_in_tx(
                        self.conn, batch_id, seq, "closure_batch_voided", "事故",
                        record["item_id"], executor,
                        {"batch_id": batch_id, "record_id": record_id,
                         "reason": "措施执行依据改动，批次冻结快照失效"},
                    )
                self.conn.execute(
                    """UPDATE closure_batches SET status='voided', voided_by=?, voided_at=?,
                       void_reason='措施执行依据改动，批次冻结快照失效'
                       WHERE item_id=? AND status='pending'""",
                    (executor, now, record["item_id"]),
                )
        return self.get_record(record_id)

    def verify_measure(self, record_id: int, verifier: str,
                       verify_ref: str, verify_detail: Optional[str]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            record = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                raise NotFoundError("记录不存在")
            item = self.conn.execute(
                "SELECT id, status, version FROM items WHERE id=?", (record["item_id"],)
            ).fetchone()
            if item["status"] == "closed":
                raise ConflictError("事故已关闭并保留快照，不能再改动复验依据")
            executor = (record["executed_by"] or "").strip()
            if not executor:
                raise ValidationError("措施尚未登记执行人，不能复验")
            if record["status"] != "closed":
                raise ValidationError("措施尚未执行完成（关闭），不能复验")
            if executor == verifier.strip():
                raise ConflictError("复验人与执行人不能相同，复验必须独立")
            self.conn.execute(
                """UPDATE records SET verify_status='verified', verified_by=?, verify_ref=?,
                   verify_detail=?, verified_at=? WHERE id=?""",
                (verifier, verify_ref, verify_detail, now, record_id),
            )
            # 复验依据改动：所有未完成批次立即作废，并在同一事务留下可恢复审计检查点。
            pending = self.conn.execute(
                "SELECT id FROM closure_batches WHERE item_id=? AND status='pending'",
                (record["item_id"],),
            ).fetchall()
            for row in pending:
                batch_id = int(row["id"])
                seq = self._next_seq_in_tx(self.conn, batch_id)
                self._insert_op_in_tx(
                    self.conn, batch_id, seq, "closure_batch_voided", "事故",
                    record["item_id"], verifier,
                    {"batch_id": batch_id, "record_id": record_id,
                     "reason": "复验依据改动，批次冻结快照失效"},
                )
            self.conn.execute(
                """UPDATE closure_batches SET status='voided', voided_by=?, voided_at=?,
                   void_reason='复验依据改动，批次冻结快照失效'
                   WHERE item_id=? AND status='pending'""",
                (verifier, now, record["item_id"]),
            )
            voided = len(pending)
        result = self.get_record(record_id)
        result["_voided_batches"] = voided
        return result

    # ---- 关闭批次 -----------------------------------------------------------

    def create_closure_batch(self, item_id: int, expected_version: int, actor: str,
                             snapshot: list, basis_hash: str,
                             submit_detail: dict) -> Dict[str, Any]:
        """提交关闭批次：冻结事故版本（提交占用一个新版本号）与全部措施依据，
        并在同一事务写入审计检查点。两名调查员并发提交时后到者在此撞版本。"""
        now = utc_now()
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock, self.conn:
            item = self.conn.execute(
                "SELECT id, version, status FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            if item["status"] == "closed":
                raise ConflictError("事故已关闭，原关闭快照保留，不能再次提交批次")
            if item["version"] != expected_version:
                raise ConflictError(
                    "事故已有新版本，请刷新后重新提交",
                    {"current_version": item["version"]},
                )
            pending = self.conn.execute(
                "SELECT id FROM closure_batches WHERE item_id=? AND status='pending'",
                (item_id,),
            ).fetchone()
            if pending is not None:
                raise ConflictError("该事故已有进行中的关闭批次，请先确认或作废")
            # 提交即落账并冻结：版本+1，让并发后到者凭新版本识别失败。
            self.conn.execute(
                "UPDATE items SET version=version+1, updated_at=? WHERE id=?",
                (now, item_id),
            )
            frozen_version = expected_version + 1
            try:
                cur = self.conn.execute(
                    """INSERT INTO closure_batches(item_id, status, frozen_item_version,
                       snapshot, basis_hash, submitted_by, submitted_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (item_id, BATCH_PENDING, frozen_version, payload, basis_hash,
                     actor, now),
                )
                batch_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该事故已有进行中的关闭批次，请先确认或作废") from exc
            self._insert_op_in_tx(
                self.conn, batch_id, 1, "closure_batch_submitted", "事故", item_id,
                actor, dict(submit_detail, batch_id=batch_id,
                            frozen_item_version=frozen_version, basis_hash=basis_hash),
            )
        return self.get_closure_batch(batch_id)

    @staticmethod
    def _next_seq_in_tx(conn, batch_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS n FROM closure_ops WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def _insert_op_in_tx(conn, batch_id: int, seq: int, action: str, entity_type: str,
                         entity_id: int, actor: str, detail: dict,
                         op_key: Optional[str] = None) -> int:
        now = utc_now()
        key = op_key or f"batch-{batch_id}-op-{seq}"
        body = json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
        cur = conn.execute(
            """INSERT INTO closure_ops(batch_id, seq, op_key, action, entity_type,
               entity_id, actor, detail, status, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, seq, key, action, entity_type, entity_id, actor, body,
             "pending", now),
        )
        return int(cur.lastrowid)

    def get_closure_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM closure_batches WHERE id=?", (batch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("关闭批次不存在")
        return self._batch(row)

    def list_closure_batches(self, item_id: Optional[int] = None,
                             status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM closure_batches WHERE 1=1"
        params: list = []
        if item_id is not None:
            sql += " AND item_id=?"
            params.append(item_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [self._batch(row) for row in rows]

    def confirm_closure_batch(self, batch_id: int, expected_version: int,
                              actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            batch = self.conn.execute(
                "SELECT * FROM closure_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("关闭批次不存在")
            if batch["status"] == "voided":
                raise ConflictError("关闭批次已作废，依据已改动，请重新确认并提交")
            if batch["status"] == "closed":
                raise ConflictError("关闭批次已完成，事故快照已保留")
            item = self.conn.execute(
                "SELECT version, status FROM items WHERE id=?", (batch["item_id"],)
            ).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            if item["status"] == "closed":
                raise ConflictError("事故已关闭，原快照保留")
            if batch["frozen_item_version"] != expected_version:
                raise ConflictError("批次依据的事故版本已变化，请重新确认",
                                    {"current_version": item["version"],
                                     "frozen_version": batch["frozen_item_version"]})
            cur = self.conn.execute(
                """UPDATE items SET status='closed', version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status!='closed'""",
                (now, batch["item_id"], expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请基于新版本重新提交",
                                    {"current_version": item["version"]})
            self.conn.execute(
                "UPDATE closure_batches SET status='closed', confirmed_by=?, confirmed_at=? WHERE id=?",
                (actor, now, batch_id),
            )
            seq = self._next_seq_in_tx(self.conn, batch_id)
            self._insert_op_in_tx(
                self.conn, batch_id, seq, "closure_batch_confirmed", "事故",
                batch["item_id"], actor,
                {"batch_id": batch_id, "from_version": expected_version},
            )
        return self.get_closure_batch(batch_id)

    # ---- 审计 outbox 与检查点恢复 -------------------------------------------

    def enqueue_op(self, batch_id: int, seq: int, action: str, entity_type: str,
                   entity_id: int, actor: str, detail: dict,
                   op_key: Optional[str] = None) -> Dict[str, Any]:
        """登记一笔待追加的审计操作（与业务事务同库，提交后即可靠落账）。"""
        now = utc_now()
        key = op_key or f"batch-{batch_id}-op-{seq}"
        body = json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT id FROM closure_ops WHERE op_key=?", (key,)
            ).fetchone()
            if existing is not None:
                row = self.conn.execute("SELECT * FROM closure_ops WHERE id=?",
                                        (existing["id"],)).fetchone()
                return dict(row)
            cur = self.conn.execute(
                """INSERT INTO closure_ops(batch_id, seq, op_key, action, entity_type,
                   entity_id, actor, detail, status, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, seq, key, action, entity_type, entity_id, actor, body,
                 "pending", now),
            )
            op_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute("SELECT * FROM closure_ops WHERE id=?", (op_id,)).fetchone()
        return dict(row)

    def enqueue_op_in_tx(self, conn, batch_id: int, seq: int, action: str,
                         entity_type: str, entity_id: int, actor: str, detail: dict,
                         op_key: Optional[str] = None) -> int:
        """在已有业务事务内登记操作，保证“业务落账即检查点存在”。"""
        now = utc_now()
        key = op_key or f"batch-{batch_id}-op-{seq}"
        body = json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
        cur = conn.execute(
            """INSERT INTO closure_ops(batch_id, seq, op_key, action, entity_type,
               entity_id, actor, detail, status, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, seq, key, action, entity_type, entity_id, actor, body,
             "pending", now),
        )
        return int(cur.lastrowid)

    def pending_ops(self, batch_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM closure_ops WHERE status='pending'"
        params: tuple = ()
        if batch_id is not None:
            sql += " AND batch_id=?"
            params = (batch_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        ops = []
        for row in rows:
            op = dict(row)
            op["detail"] = json.loads(op["detail"])
            ops.append(op)
        return ops

    def _append_event_locked(self, action: str, entity_type: str, entity_id: int,
                             actor: str, detail: dict, created_at: str,
                             op_key: Optional[str]) -> int:
        """单次审计追加；调用方持锁。成功返回事件ID，键重复返回负ID表示幂等命中。"""
        if op_key is not None:
            dup = self.conn.execute(
                "SELECT id FROM audit_events WHERE op_key=?", (op_key,)
            ).fetchone()
            if dup is not None:
                return -int(dup["id"])
        if self.fail_next_audit_write:
            self.fail_next_audit_write = False
            raise RuntimeError("模拟审计写入失败")
        tail = self.conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = tail["entry_hash"] if tail else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        event["created_at"] = created_at
        from .audit import calculate_hash
        payload = {k: event[k] for k in
                   ("action", "entity_type", "entity_id", "actor", "detail", "created_at")}
        entry_hash = calculate_hash(previous, payload)
        cur = self.conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at, op_key)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (action, entity_type, entity_id, actor,
             json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str),
             previous, entry_hash, created_at, op_key),
        )
        return int(cur.lastrowid)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict, op_key: Optional[str] = None,
                     retries: int = 50) -> Dict[str, Any]:
        """追加审计事件；并发下按链尾重试，op_key保证不重复追加。"""
        import time
        created_at = utc_now()
        with self._lock:
            for attempt in range(retries):
                try:
                    with self.conn:
                        event_id = self._append_event_locked(
                            action, entity_type, entity_id, actor, detail,
                            created_at, op_key)
                    if event_id < 0:
                        row = self.conn.execute(
                            "SELECT * FROM audit_events WHERE id=?", (-event_id,)
                        ).fetchone()
                        event = dict(row)
                        event["detail"] = json.loads(event["detail"])
                        return event
                    break
                except sqlite3.IntegrityError as exc:
                    if op_key is not None and self.conn.execute(
                            "SELECT 1 FROM audit_events WHERE op_key=?", (op_key,)).fetchone():
                        row = self.conn.execute(
                            "SELECT * FROM audit_events WHERE op_key=?", (op_key,)
                        ).fetchone()
                        event = dict(row)
                        event["detail"] = json.loads(event["detail"])
                        return event
                    if attempt == retries - 1:
                        raise
                    time.sleep(0.005)
                except sqlite3.OperationalError:
                    if attempt == retries - 1:
                        raise
                    time.sleep(0.005)
            row = self.conn.execute("SELECT * FROM audit_events WHERE id=?", (event_id,)).fetchone()
        event = dict(row)
        event["detail"] = json.loads(event["detail"])
        return event

    def flush_pending_ops(self, batch_id: Optional[int] = None) -> Dict[str, int]:
        """从检查点恢复：按操作号(seq)顺序补写审计，已完成的操作跳过、绝不重复。"""
        applied = 0
        skipped = 0
        ops = self.pending_ops(batch_id)
        for op in ops:
            try:
                with self._lock, self.conn:
                    event_id = self._append_event_locked(
                        op["action"], op["entity_type"], op["entity_id"], op["actor"],
                        op["detail"], op["created_at"], op["op_key"],
                    )
                    if event_id < 0:
                        event_id = -event_id
                        skipped += 1
                    else:
                        applied += 1
                    self.conn.execute(
                        """UPDATE closure_ops SET status='done', audit_event_id=?,
                           finished_at=?, last_error=NULL WHERE id=?""",
                        (event_id, utc_now(), op["id"]),
                    )
            except Exception as exc:  # 检查点保留pending，下次按seq继续。
                with self._lock, self.conn:
                    self.conn.execute(
                        "UPDATE closure_ops SET last_error=? WHERE id=?",
                        (str(exc)[:500], op["id"]),
                    )
                break
        return {"applied": applied, "skipped": skipped,
                "remaining": len(self.pending_ops(batch_id))}

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
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

    def close(self) -> None:
        with self._lock:
            self.conn.close()
