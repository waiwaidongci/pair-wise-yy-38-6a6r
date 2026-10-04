"""对账子系统仓储：原始上报、规范值、指令、关闭确认、补传游标与测点锁。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .audit import utc_now
from .domain import ConflictError, NotFoundError
from .recon_domain import CANON_PROVISIONAL, INBOX_PENDING, METRICS


class ReconRepository:
    def __init__(self, conn: sqlite3.Connection, lock):
        self.conn = conn
        self._lock = lock
        self._create_schema()

    def _create_schema(self) -> None:
        metrics = ",".join("'" + m + "'" for m in METRICS)
        with self._lock, self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS recon_points (
                    point TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    open_threshold REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recon_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    point TEXT NOT NULL,
                    metric TEXT NOT NULL CHECK(metric IN ({metrics})),
                    field_time TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('device','manual')),
                    value REAL NOT NULL,
                    unit TEXT,
                    anomaly_note TEXT,
                    state TEXT NOT NULL,
                    batch_ref TEXT,
                    source_seq INTEGER NOT NULL DEFAULT 0,
                    replaced_by_id INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                -- 批次补传幂等：同批同测点同来源同序列只落一次
                CREATE UNIQUE INDEX IF NOT EXISTS ux_recon_obs_batch_seq
                    ON recon_observations(point, metric, source, source_seq,
                                          COALESCE(batch_ref, ''))
                    WHERE batch_ref IS NOT NULL;
                -- 无批次（手报/即时回执）幂等：完全相同上报(含值)才算重试，
                -- 同现场时间不同值视为修正/冲突，进入仲裁而非重复丢弃
                CREATE UNIQUE INDEX IF NOT EXISTS ux_recon_obs_manual_dedupe
                    ON recon_observations(point, metric, source, field_time, value)
                    WHERE batch_ref IS NULL;
                CREATE INDEX IF NOT EXISTS ix_recon_obs_lookup
                    ON recon_observations(point, metric, field_time);
                CREATE TABLE IF NOT EXISTS recon_canonical (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    point TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    field_time TEXT NOT NULL,
                    observation_id INTEGER NOT NULL
                        REFERENCES recon_observations(id),
                    source TEXT NOT NULL,
                    value REAL NOT NULL,
                    anomaly_note TEXT,
                    confirm_state TEXT NOT NULL DEFAULT '{CANON_PROVISIONAL}',
                    confirmed_by TEXT,
                    confirmed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(point, metric, field_time)
                );
                CREATE TABLE IF NOT EXISTS recon_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    point TEXT NOT NULL,
                    command TEXT NOT NULL CHECK(command IN ('open','close')),
                    status TEXT NOT NULL CHECK(status IN ('pending','invalidated','executed')),
                    basis_observation_id INTEGER,
                    basis_value REAL,
                    basis_threshold REAL,
                    basis_field_time TEXT,
                    recalc_of_id INTEGER,
                    recalc_source TEXT,
                    executed_at TEXT,
                    executed_by TEXT,
                    close_qualification TEXT,
                    close_reasons TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_recon_cmd_point ON recon_commands(point, status);
                CREATE TABLE IF NOT EXISTS recon_closures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    point TEXT NOT NULL,
                    command_id INTEGER REFERENCES recon_commands(id),
                    position_value REAL NOT NULL,
                    flow_value REAL NOT NULL,
                    field_time TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(point, command_id)
                );
                CREATE TABLE IF NOT EXISTS recon_batches (
                    batch_ref TEXT PRIMARY KEY,
                    point TEXT NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    pending INTEGER NOT NULL DEFAULT 0,
                    duplicate INTEGER NOT NULL DEFAULT 0,
                    last_seq INTEGER,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    # ------------------------------------------------------------------ points
    def register_point(self, point: str, title: str, threshold: float,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    """INSERT INTO recon_points(point, title, open_threshold,
                       created_by, created_at) VALUES(?,?,?,?,?)""",
                    (point, title, threshold, actor, now))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("测点已存在") from exc
        return self.get_point(point)

    def get_point(self, point: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recon_points WHERE point=?", (point,)).fetchone()
        if row is None:
            raise NotFoundError("测点不存在")
        return dict(row)

    def list_points(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM recon_points ORDER BY point").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- locks
    # 同一测点人工并发的互斥在服务层 threading.Lock 完成（临界区内所有写入
    # 各自使用规范事务）；持久层不再维护锁表，避免裸 SQL 的隐式事务与
    # 显式 with conn 事务嵌套。


    # ------------------------------------------------------------- observations
    def insert_observation(self, obs: Dict[str, Any], state: str,
                           batch_ref: Optional[str], actor: str) -> Tuple[int, bool]:
        """返回(observation_id, inserted)；幂等重命中原样返回不重复追加。"""
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO recon_observations(point, metric, field_time, source,
                       value, unit, anomaly_note, state, batch_ref, source_seq,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (obs["point"], obs["metric"], obs["field_time"], obs["source"],
                     obs["value"], obs.get("unit"), obs.get("anomaly_note"), state,
                     batch_ref, obs["source_seq"], actor, now))
                return int(cur.lastrowid), True
        except sqlite3.IntegrityError:
            if batch_ref is not None:
                row = self.conn.execute(
                    """SELECT id FROM recon_observations
                       WHERE point=? AND metric=? AND source=? AND source_seq=?
                         AND batch_ref=?""",
                    (obs["point"], obs["metric"], obs["source"], obs["source_seq"],
                     batch_ref)).fetchone()
            else:
                row = self.conn.execute(
                    """SELECT id FROM recon_observations
                       WHERE point=? AND metric=? AND source=? AND field_time=?
                         AND value=? AND batch_ref IS NULL""",
                    (obs["point"], obs["metric"], obs["source"], obs["field_time"],
                     obs["value"])).fetchone()
            if row is None:
                raise ConflictError("上报与已有数据冲突且无法去重")
            return int(row["id"]), False

    def mark_observation(self, obs_id: int, state: str,
                         replaced_by_id: Optional[int] = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE recon_observations SET state=?, replaced_by_id=? WHERE id=?",
                (state, replaced_by_id, obs_id))

    def get_canonical(self, point: str, metric: str,
                      field_time: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recon_canonical WHERE point=? AND metric=? AND field_time=?",
                (point, metric, field_time)).fetchone()
        return dict(row) if row else None

    def upsert_canonical(self, point: str, metric: str, field_time: str,
                         obs: Dict[str, Any]) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO recon_canonical(point, metric, field_time, observation_id,
                   source, value, anomaly_note, confirm_state, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(point, metric, field_time) DO UPDATE SET
                     observation_id=excluded.observation_id, source=excluded.source,
                     value=excluded.value, anomaly_note=excluded.anomaly_note,
                     updated_at=excluded.updated_at""",
                (point, metric, field_time, obs["id"], obs["source"], obs["value"],
                 obs.get("anomaly_note"), CANON_PROVISIONAL, now, now))

    def update_canonical_value(self, canon_id: int, obs: Dict[str, Any]) -> None:
        """被设备值取代时更新值，但确认状态与人工异常原因另行保留处理。"""
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE recon_canonical SET observation_id=?, source=?, value=?,
                   updated_at=? WHERE id=? AND confirm_state!=?""",
                (obs["id"], obs["source"], obs["value"], utc_now(),
                 canon_id, "confirmed"))

    def merge_canonical_anomaly(self, canon_id: int, note: Optional[str]) -> None:
        if not note:
            return
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT anomaly_note FROM recon_canonical WHERE id=?", (canon_id,)).fetchone()
            existing = row["anomaly_note"] if row else None
            notes = [n for n in (existing, note) if n]
            merged: List[str] = []
            for n in notes:
                if n not in merged:
                    merged.append(n)
            self.conn.execute(
                "UPDATE recon_canonical SET anomaly_note=? WHERE id=?",
                (" | ".join(merged), canon_id))

    def confirm_canonical(self, canon_id: int, actor: str) -> None:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE recon_canonical SET confirm_state='confirmed',
                   confirmed_by=?, confirmed_at=? WHERE id=?""",
                (actor, utc_now(), canon_id))
            if cur.rowcount == 0:
                raise NotFoundError("规范值不存在")

    def latest_canonical(self, point: str, metric: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM recon_canonical WHERE point=? AND metric=?
                   ORDER BY field_time DESC LIMIT 1""",
                (point, metric)).fetchone()
        return dict(row) if row else None

    def list_canonical(self, point: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM recon_canonical"
        params: tuple = ()
        if point:
            sql += " WHERE point=?"
            params = (point,)
        sql += " ORDER BY point, metric, field_time"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def list_observations(self, point: Optional[str] = None,
                          state: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM recon_observations WHERE 1=1"
        params: List[Any] = []
        if point:
            sql += " AND point=?"; params.append(point)
        if state:
            sql += " AND state=?"; params.append(state)
        sql += " ORDER BY point, metric, field_time, id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- commands
    def insert_command(self, point: str, command: str, basis: Optional[Dict[str, Any]],
                       recalc_of_id: Optional[int], recalc_source: Optional[str]) -> int:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO recon_commands(point, command, status, basis_observation_id,
                   basis_value, basis_threshold, basis_field_time, recalc_of_id,
                   recalc_source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (point, command, "pending",
                 basis.get("basis_observation_id") if basis else None,
                 basis.get("basis_value") if basis else None,
                 basis.get("basis_threshold") if basis else None,
                 basis.get("basis_field_time") if basis else None,
                 recalc_of_id, recalc_source, now))
            return int(cur.lastrowid)

    def invalidate_command(self, command_id: int, reason: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE recon_commands SET status='invalidated', recalc_source=? WHERE id=?",
                (reason, command_id))

    def pending_command(self, point: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recon_commands WHERE point=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (point,)).fetchone()
        return dict(row) if row else None

    def get_command(self, command_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recon_commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            raise NotFoundError("指令不存在")
        return dict(row)

    def mark_command_executed(self, command_id: int, actor: str) -> None:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE recon_commands SET status='executed', executed_at=?,
                   executed_by=? WHERE id=? AND status='pending'""",
                (utc_now(), actor, command_id))
            if cur.rowcount == 0:
                raise ConflictError("指令不存在、已执行或已失效")

    def list_commands(self, point: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM recon_commands"
        params: tuple = ()
        if point:
            sql += " WHERE point=?"; params = (point,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def set_close_qualification(self, command_id: int, qualification: str,
                                reasons: List[str]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE recon_commands SET close_qualification=?, close_reasons=? WHERE id=?",
                (qualification, json.dumps(reasons, ensure_ascii=False), command_id))

    # ----------------------------------------------------------------- closure
    def add_closure(self, point: str, command_id: Optional[int], position_value: float,
                    flow_value: float, field_time: str, source: str,
                    actor: str) -> int:
        now = utc_now()
        with self._lock, self.conn:
            try:
                cur = self.conn.execute(
                    """INSERT INTO recon_closures(point, command_id, position_value,
                       flow_value, field_time, source, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (point, command_id, position_value, flow_value, field_time,
                     source, actor, now))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该指令已有关闭确认") from exc
            return int(cur.lastrowid)

    def latest_closure(self, point: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recon_closures WHERE point=? ORDER BY id DESC LIMIT 1",
                (point,)).fetchone()
        return dict(row) if row else None

    def list_closures(self, point: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM recon_closures"
        params: tuple = ()
        if point:
            sql += " WHERE point=?"; params = (point,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ batches
    def upsert_batch(self, batch_ref: str, point: str, total: int,
                     counters: Dict[str, int], last_seq: Optional[int],
                     status: str) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO recon_batches(batch_ref, point, total, accepted, pending,
                   duplicate, last_seq, status, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_ref) DO UPDATE SET
                     accepted=excluded.accepted, pending=excluded.pending,
                     duplicate=excluded.duplicate, last_seq=excluded.last_seq,
                     status=excluded.status, updated_at=excluded.updated_at""",
                (batch_ref, point, total, counters.get("accepted", 0),
                 counters.get("pending", 0), counters.get("duplicate", 0),
                 last_seq, status, now, now))

    def get_batch(self, batch_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recon_batches WHERE batch_ref=?", (batch_ref,)).fetchone()
        return dict(row) if row else None

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM recon_batches ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    def confirmed_seq(self, point: str) -> int:
        """已确认（采纳）到该测点的最大设备序列，作为补传续传起点。"""
        with self._lock:
            row = self.conn.execute(
                """SELECT COALESCE(MAX(source_seq), -1) AS s FROM recon_observations
                   WHERE point=? AND source='device' AND state!=?""",
                (point, INBOX_PENDING)).fetchone()
        return int(row["s"])
