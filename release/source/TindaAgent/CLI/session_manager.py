"""会话管理：复用 SessionStore 提供 CLI 友好接口。"""

from __future__ import annotations

import uuid
from typing import Any

from TindaAgent.Web.session_store import SessionStore
from TindaAgent.Process.Architecture.paths import get_sessions_root, get_legacy_sessions_root


class SessionManager:
    def __init__(self) -> None:
        root = get_sessions_root()
        legacy = get_legacy_sessions_root()
        self._store = SessionStore(root, legacy_root_dir=legacy)

    @property
    def store(self) -> SessionStore:
        return self._store

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._store.list_sessions(limit=limit).get("sessions", [])

    def create_session(self, title: str = "新对话") -> str:
        sid = f"s_{uuid.uuid4().hex[:12]}"
        row = self._store.create_session(sid, title=title)
        return str(row.get("id", sid))

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._store.get_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        return self._store.delete_session(session_id)

    def ensure_session(self, session_id: str) -> dict[str, Any]:
        return self._store.ensure_session(session_id)

    def get_messages(self, session_id: str) -> dict[str, Any]:
        return self._store.load_messages(session_id)

    def append_messages(self, session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self._store.append_messages(session_id, messages)

    def set_session_title(self, session_id: str, title: str) -> dict[str, Any]:
        return self._store.set_session_title(session_id, title)
