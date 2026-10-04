from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES, reading_conflict_outcome


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
                    point TEXT,
                    invalidated INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    point TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('device','manual')),
                    observed_at TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'water_level',
                    value REAL,
                    text_value TEXT,
                    unit TEXT,
                    reason TEXT,
                    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','held')),
                    external_ref TEXT,
                    batch_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    confirmed_by TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_readings_external_ref
                    ON readings(external_ref) WHERE external_ref IS NOT NULL;
                CREATE INDEX IF NOT EXISTS ix_readings_point_time
                    ON readings(point, observed_at);
                CREATE TABLE IF NOT EXISTS backfill_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_ref TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('in_progress','completed','failed')),
                    total INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0,
                    resume_index INTEGER NOT NULL DEFAULT 0,
                    last_point TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
            """)
            self._migrate_items()

    def _migrate_items(self) -> None:
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        if "point" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN point TEXT")
        if "invalidated" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN invalidated INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, point: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, point, invalidated, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, point, 0, actor, now, now),
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

    def list_items_by_point(self, point: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM items WHERE point=? ORDER BY id DESC", (point,)
            ).fetchall()
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

    def recalc_item(self, item_id: int, quantity: float, invalidated: bool) -> Dict[str, Any]:
        """按新依据重算未执行指令的水量，并置失效标记。"""
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE items SET quantity=?, invalidated=?, updated_at=? WHERE id=?",
                (quantity, 1 if invalidated else 0, now, item_id),
            )
        return self.get_item(item_id)

    def set_item_invalidated(self, item_id: int, invalidated: bool) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE items SET invalidated=?, updated_at=? WHERE id=?",
                (1 if invalidated else 0, now, item_id),
            )
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
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

    # ---- 水情读数 ----

    def submit_reading(self, point: str, source: str, observed_at: str, kind: str,
                       value: Optional[float], text_value: Optional[str], unit: Optional[str],
                       reason: Optional[str], external_ref: Optional[str],
                       batch_ref: Optional[str], actor: str) -> tuple:
        """提交一笔读数，按测点+现场时间冲突裁决。返回 (reading, created)。

        人工标明的异常原因(reason)始终保留；已确认值不被后到数据盖掉。
        同一 external_ref 幂等返回，不重复落库（重试不重复追加审计）。
        """
        now = utc_now()
        with self._lock, self.conn:
            if external_ref is not None:
                existing = self.conn.execute(
                    "SELECT * FROM readings WHERE external_ref=?", (external_ref,)
                ).fetchone()
                if existing is not None:
                    return dict(existing), False
            rows = self.conn.execute(
                "SELECT * FROM readings WHERE point=? AND observed_at=?",
                (point, observed_at),
            ).fetchall()
            has_confirmed = any(r["status"] == "confirmed" for r in rows)
            has_pending = any(r["status"] == "pending" for r in rows)
            new_status, supersedes = reading_conflict_outcome(has_confirmed, has_pending, source)
            if supersedes:
                # 设备值优先：原待核件留待核，异常原因随记录保留
                self.conn.execute(
                    "UPDATE readings SET status='held' WHERE point=? AND observed_at=? AND status='pending'",
                    (point, observed_at),
                )
            cur = self.conn.execute(
                """INSERT INTO readings(point, source, observed_at, kind, value, text_value,
                   unit, reason, status, external_ref, batch_ref, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (point, source, observed_at, kind, value, text_value, unit, reason,
                 new_status, external_ref, batch_ref, actor, now),
            )
            reading_id = int(cur.lastrowid)
            row = self.conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        return dict(row), True

    def get_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        if row is None:
            raise NotFoundError("读数不存在")
        return dict(row)

    def list_readings(self, point: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM readings"
        params: tuple = ()
        if point:
            sql += " WHERE point=?"
            params = (point,)
        sql += " ORDER BY observed_at DESC, id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def confirm_reading(self, reading_id: int, actor: str) -> Dict[str, Any]:
        """锁定一笔待核读数为已确认；已确认值不被后到数据盖掉。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
            if row is None:
                raise NotFoundError("读数不存在")
            if row["status"] == "confirmed":
                return dict(row)
            conflict = self.conn.execute(
                "SELECT id FROM readings WHERE point=? AND observed_at=? AND status='confirmed' AND id!=?",
                (row["point"], row["observed_at"], reading_id),
            ).fetchone()
            if conflict is not None:
                raise ConflictError("同一测点同一时间已有已确认值")
            self.conn.execute(
                "UPDATE readings SET status='confirmed', confirmed_at=?, confirmed_by=? WHERE id=?",
                (now, actor, reading_id),
            )
            row = self.conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        return dict(row)

    # ---- 补传批次 ----

    def create_backfill(self, batch_ref: str, total: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    """INSERT INTO backfill_batches(batch_ref, status, total, processed,
                       resume_index, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (batch_ref, "in_progress", total, 0, 0, actor, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("批次已存在") from exc
        return self.get_backfill(batch_ref)

    def get_backfill(self, batch_ref: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM backfill_batches WHERE batch_ref=?", (batch_ref,)
            ).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return dict(row)

    def update_backfill_progress(self, batch_ref: str, processed: int, resume_index: int,
                                 last_point: Optional[str], status: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            if status is not None:
                self.conn.execute(
                    """UPDATE backfill_batches SET processed=?, resume_index=?, last_point=?,
                       status=?, updated_at=? WHERE batch_ref=?""",
                    (processed, resume_index, last_point, status, now, batch_ref),
                )
            else:
                self.conn.execute(
                    """UPDATE backfill_batches SET processed=?, resume_index=?, last_point=?,
                       updated_at=? WHERE batch_ref=?""",
                    (processed, resume_index, last_point, now, batch_ref),
                )
        return self.get_backfill(batch_ref)

    def list_backfills(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM backfill_batches ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    # ---- 审计 ----

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
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

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
