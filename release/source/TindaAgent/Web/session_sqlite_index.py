"""SQLite cache/index for session reads.

This module is intentionally non-authoritative: JSON session files remain the
source of truth, and the SQLite database may be deleted and rebuilt at any time.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from TindaAgent.Web import session_adapter as sa


class SessionSQLiteIndex:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    owner_uid TEXT,
                    title TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    message_count INTEGER DEFAULT 0,
                    indexed_updated_at TEXT,
                    indexed_at REAL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    msg_id TEXT,
                    role TEXT,
                    entry_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_seq
                    ON messages(session_id, seq);
                CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated
                    ON sessions(owner_uid, updated_at DESC);
                """
            )

    def is_session_current(self, sid: str, meta: dict[str, Any] | None) -> bool:
        sid = str(sid or "").strip()
        if not sid:
            return False
        expected_updated = str((meta or {}).get("updated_at", "") or "")
        expected_count = int((meta or {}).get("message_count") or 0)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT indexed_updated_at, message_count FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
        if not row:
            return False
        return str(row["indexed_updated_at"] or "") == expected_updated and int(row["message_count"] or 0) == expected_count

    def index_session(self, sid: str, meta: dict[str, Any] | None, store_dict: dict[str, Any]) -> None:
        sid = str(sid or "").strip()
        if not sid:
            return
        meta = dict(meta or {})
        keys = sorted((int(k) for k in store_dict if str(k).isdigit()), key=int)
        entries = sa.store_dict_to_frontend(store_dict)
        now = time.time()
        session_row = (
            sid,
            str(meta.get("owner_uid", "") or ""),
            str(meta.get("title", "") or "新对话"),
            str(meta.get("created_at", "") or ""),
            str(meta.get("updated_at", "") or ""),
            int(meta.get("message_count") or len(keys)),
            str(meta.get("updated_at", "") or ""),
            now,
        )
        message_rows: list[tuple[Any, ...]] = []
        for idx, seq in enumerate(keys):
            if idx >= len(entries):
                break
            entry = dict(entries[idx] or {})
            entry["seq"] = int(seq)
            message_rows.append(
                (
                    sid,
                    int(seq),
                    str(entry.get("id", "") or ""),
                    str(entry.get("role", "") or ""),
                    json.dumps(entry, ensure_ascii=False, separators=(",", ":")),
                )
            )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO sessions (
                    id, owner_uid, title, created_at, updated_at, message_count,
                    indexed_updated_at, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    owner_uid = excluded.owner_uid,
                    title = excluded.title,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    message_count = excluded.message_count,
                    indexed_updated_at = excluded.indexed_updated_at,
                    indexed_at = excluded.indexed_at
                """,
                session_row,
            )
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.executemany(
                "INSERT INTO messages(session_id, seq, msg_id, role, entry_json) VALUES (?, ?, ?, ?, ?)",
                message_rows,
            )
            conn.commit()

    def get_messages(self, sid: str, *, limit: int, before_seq: int = 0) -> dict[str, Any] | None:
        sid = str(sid or "").strip()
        limit = max(1, min(int(limit or 0), 500))
        before_seq = max(0, int(before_seq or 0))
        if not sid:
            return None
        where = "session_id = ?"
        params: list[Any] = [sid]
        if before_seq > 0:
            where += " AND seq < ?"
            params.append(before_seq)
        with self._connect() as conn:
            total_row = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (sid,)).fetchone()
            total = int(total_row["c"] if total_row else 0)
            if total <= 0:
                return None
            rows = conn.execute(
                f"""
                SELECT seq, entry_json
                FROM messages
                WHERE {where}
                ORDER BY seq DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        rows = list(reversed(rows))
        entries: list[dict[str, Any]] = []
        keys: list[int] = []
        for row in rows:
            seq = int(row["seq"])
            try:
                entry = json.loads(str(row["entry_json"] or "{}"))
            except Exception:
                entry = {}
            if isinstance(entry, dict):
                entry["seq"] = seq
                entries.append(entry)
                keys.append(seq)
        oldest_seq = int(keys[0]) if keys else 0
        newest_seq = int(keys[-1]) if keys else 0
        return {
            "ok": True,
            "session_id": sid,
            "entries": entries,
            "total": total,
            "oldest_seq": oldest_seq,
            "newest_seq": newest_seq,
            "has_more": bool(oldest_seq > 1),
            "limit": limit,
            "source": "sqlite_index",
        }

    def delete_session(self, sid: str) -> None:
        sid = str(sid or "").strip()
        if not sid:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
