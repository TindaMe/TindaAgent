from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from TindaAgent.Process.Architecture.paths import get_legacy_sessions_root
from TindaAgent.Process.Observability import audit_event


class SessionStoreError(ValueError):
    pass


ROLE_SET = {"user", "assistant", "system"}
ENTRY_TYPES = {"chat", "notice", "tool_marker", "terminal", "attachment", "terminal_confirm", "terminal_exec"}
TERMINAL_KINDS = {"", "cmd", "out", "sep"}
TERMINAL_CLASSES = {"", "err", "info", "dim"}
MAX_MSG_CHARS = 64000
MAX_TITLE_LEN = 15
_THIS_FILE = str(Path(__file__).resolve())


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_session_id(session_id: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(session_id or "").strip())
    text = text[:80]
    return text


def _truncate_text(text: str, max_len: int) -> str:
    s = str(text or "").strip()
    return s[:max_len]


@dataclass
class SessionMeta:
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0
    reset_anchor_msg_id: str = ""
    summary_anchor_msg_id: str = ""
    latest_summary_message_id: str = ""
    max_context_tokens: int = 16000


class SessionStore:
    def __init__(self, root_dir: Path, *, legacy_root_dir: Path | None = None) -> None:
        self.root_dir = root_dir.resolve()
        self.legacy_root_dir = Path(legacy_root_dir).resolve() if legacy_root_dir is not None else None
        self.sessions_file = self.root_dir / "sessions.json"
        self.messages_dir = self.root_dir / "messages"
        self.exports_dir = self.root_dir / "exports"
        self.legacy_sessions_file = self.legacy_root_dir / "sessions.json" if self.legacy_root_dir else None
        self.legacy_messages_dir = self.legacy_root_dir / "messages" if self.legacy_root_dir else None
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.messages_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._sessions_cache: dict[str, dict] | None = None
        self._sessions_dirty = False
        self._meta_write_count = 0
        self._META_FLUSH_EVERY = 8
        self._msg_cache: dict[str, list[dict]] = {}
        self._msg_cache_order: list[str] = []
        self._MSG_CACHE_MAX = 50

        if not self.sessions_file.exists():
            self._write_sessions({"sessions": []})

    def clear_all(self) -> None:
        with self._lock:
            if self.root_dir.exists():
                for path in sorted(self.root_dir.rglob("*"), reverse=True):
                    try:
                        if path.is_file():
                            path.unlink(missing_ok=True)
                        elif path.is_dir():
                            path.rmdir()
                    except Exception:
                        continue
            self.root_dir.mkdir(parents=True, exist_ok=True)
            self.messages_dir.mkdir(parents=True, exist_ok=True)
            self.exports_dir.mkdir(parents=True, exist_ok=True)
            self._write_sessions({"sessions": []})
            self._session_locks.clear()
            audit_event(
                op_type="SYSTEM_WRITE",
                subsystem="session",
                func="SessionStore.clear_all",
                file_path=_THIS_FILE,
                content="session_store_clear_all",
                extra={"root_dir": str(self.root_dir)},
            )

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _read_sessions(self) -> dict[str, Any]:
        if self._sessions_cache is not None:
            return {"sessions": list(self._sessions_cache.values())}
        try:
            data = json.loads(self.sessions_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                cache: dict[str, dict] = {}
                for it in data["sessions"]:
                    cache[str(it.get("id", ""))] = it
                self._sessions_cache = cache
                return data
        except Exception:
            pass
        if self.legacy_sessions_file and self.legacy_sessions_file.exists():
            try:
                data = json.loads(self.legacy_sessions_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                    cache: dict[str, dict] = {}
                    for it in data["sessions"]:
                        cache[str(it.get("id", ""))] = it
                    self._sessions_cache = cache
                    audit_event(
                        op_type="SYSTEM_READ",
                        subsystem="storage_migration",
                        func="SessionStore._read_sessions",
                        file_path=_THIS_FILE,
                        content="legacy_fallback_read_sessions",
                        extra={"legacy_file": str(self.legacy_sessions_file)},
                    )
                    return data
            except Exception:
                pass
        self._sessions_cache = {}
        return {"sessions": []}

    def _write_sessions(self, payload: dict[str, Any]) -> None:
        self._sessions_dirty = False
        self._meta_write_count = 0
        temp = self.sessions_file.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.sessions_file)
        # sync cache
        cache: dict[str, dict] = {}
        for it in payload.get("sessions", []):
            cache[str(it.get("id", ""))] = it
        self._sessions_cache = cache

    def _maybe_flush_sessions(self) -> None:
        self._meta_write_count += 1
        if self._meta_write_count >= self._META_FLUSH_EVERY:
            self._sessions_dirty = True
        if self._sessions_dirty and self._sessions_cache is not None:
            with self._lock:
                if self._sessions_dirty and self._sessions_cache is not None:
                    payload = {"sessions": sorted(
                        list(self._sessions_cache.values()),
                        key=lambda x: str(x.get("updated_at", "")), reverse=True)}
                    self._write_sessions(payload)

    def _messages_path(self, session_id: str) -> Path:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id 非法")
        return self.messages_dir / f"{sid}.jsonl"

    def _touch_msg_cache(self, sid: str) -> None:
        with self._lock:
            if sid in self._msg_cache_order:
                self._msg_cache_order.remove(sid)
            self._msg_cache_order.append(sid)
            while len(self._msg_cache_order) > self._MSG_CACHE_MAX:
                evicted = self._msg_cache_order.pop(0)
                self._msg_cache.pop(evicted, None)

    def _load_messages(self, session_id: str) -> list[dict[str, Any]]:
        sid = _safe_session_id(session_id)
        # 热缓存命中
        cached = self._msg_cache.get(sid)
        if cached is not None:
            self._touch_msg_cache(sid)
            return list(cached)
        path = self._messages_path(sid)
        legacy_path = None
        if self.legacy_messages_dir is not None and sid:
            legacy_path = self.legacy_messages_dir / f"{sid}.jsonl"
        if (not path.exists()) and legacy_path is not None and legacy_path.exists():
            path = legacy_path
            audit_event(
                op_type="SYSTEM_READ",
                subsystem="storage_migration",
                func="SessionStore._load_messages",
                file_path=_THIS_FILE,
                content=f"legacy_fallback_read_messages session_id={sid}",
                extra={"legacy_file": str(legacy_path)},
            )
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
        self._msg_cache[sid] = list(rows)
        self._touch_msg_cache(sid)
        return rows

    def _write_messages(self, session_id: str, rows: list[dict[str, Any]]) -> None:
        path = self._messages_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(item, ensure_ascii=False) for item in rows]
        temp = path.with_suffix(".jsonl.tmp")
        temp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        temp.replace(path)
        sid = _safe_session_id(session_id)
        if sid:
            self._msg_cache[sid] = list(rows)
            self._touch_msg_cache(sid)

    def _touch_session_meta(
        self,
        session_id: str,
        *,
        title: str | None = None,
        reset_anchor_msg_id: str | None = None,
        summary_anchor_msg_id: str | None = None,
        latest_summary_message_id: str | None = None,
        message_count: int | None = None,
        max_context_tokens: int | None = None,
    ) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id 非法")

        now = _now_iso()
        cache = self._sessions_cache if self._sessions_cache is not None else {}
        if sid in cache:
            row = cache[sid]
        else:
            row = {
                "id": sid, "title": "新对话", "created_at": now,
                "updated_at": now, "message_count": 0,
                "reset_anchor_msg_id": "", "summary_anchor_msg_id": "",
                "latest_summary_message_id": "", "max_context_tokens": 16000,
            }
            cache[sid] = row

        row.setdefault("reset_anchor_msg_id", "")
        row.setdefault("summary_anchor_msg_id", "")
        row.setdefault("latest_summary_message_id", "")
        row.setdefault("max_context_tokens", 16000)
        row["updated_at"] = now
        if title is not None:
            t = str(title or "").strip().strip("\"'")
            row["title"] = _truncate_text(t, MAX_TITLE_LEN) or "新对话"
        if reset_anchor_msg_id is not None:
            row["reset_anchor_msg_id"] = str(reset_anchor_msg_id or "")
        if summary_anchor_msg_id is not None:
            row["summary_anchor_msg_id"] = str(summary_anchor_msg_id or "")
        if latest_summary_message_id is not None:
            row["latest_summary_message_id"] = str(latest_summary_message_id or "")
        if message_count is not None:
            row["message_count"] = max(0, int(message_count))
        if max_context_tokens is not None:
            row["max_context_tokens"] = max(100, min(10_000_000, int(max_context_tokens)))

        self._sessions_cache = cache
        self._maybe_flush_sessions()
        return row

    def create_session(self, session_id: str | None = None, title: str = "新对话") -> dict[str, Any]:
        sid = _safe_session_id(session_id or f"s_{uuid.uuid4().hex[:12]}")
        if not sid:
            sid = f"s_{uuid.uuid4().hex[:12]}"
        with self._lock:
            # 确保唯一
            payload = self._read_sessions()
            exists = {str(x.get("id", "")) for x in payload.get("sessions", [])}
            base = sid
            if sid in exists:
                sid = f"{base}_{uuid.uuid4().hex[:6]}"
            row = self._touch_session_meta(sid, title=title, message_count=0)
            audit_event(
                op_type="PUBLIC_WRITE",
                subsystem="session",
                func="SessionStore.create_session",
                file_path=_THIS_FILE,
                content=f"session_created session_id={sid}",
                extra={"session_id": sid, "title": str(row.get("title", ""))},
            )
            return row

    def ensure_session(self, session_id: str) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id 非法")
        with self._lock:
            row = self.get_session(sid)
            if row:
                audit_event(
                    op_type="PUBLIC_READ",
                    subsystem="session",
                    func="SessionStore.ensure_session",
                    file_path=_THIS_FILE,
                    content=f"session_exists session_id={sid}",
                    extra={"session_id": sid},
                )
                return row
            created = self.create_session(sid)
            audit_event(
                op_type="PUBLIC_WRITE",
                subsystem="session",
                func="SessionStore.ensure_session",
                file_path=_THIS_FILE,
                content=f"session_created_by_ensure session_id={sid}",
                extra={"session_id": sid},
            )
            return created

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        sid = _safe_session_id(session_id)
        if not sid:
            return None
        payload = self._read_sessions()
        for it in payload.get("sessions", []):
            if str(it.get("id", "")) == sid:
                return dict(it)
        return None

    def list_sessions(self, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        payload = self._read_sessions()
        rows = sorted(payload.get("sessions", []), key=lambda x: str(x.get("updated_at", "")), reverse=True)
        total = len(rows)
        paged = rows[offset:offset + limit]
        return {"sessions": paged, "total": total, "limit": limit, "offset": offset}

    def delete_session(self, session_id: str) -> bool:
        sid = _safe_session_id(session_id)
        if not sid:
            return False
        with self._lock:
            payload = self._read_sessions()
            old = payload.get("sessions", [])
            new_rows = [it for it in old if str(it.get("id", "")) != sid]
            if len(new_rows) == len(old):
                audit_event(
                    op_type="PUBLIC_WRITE",
                    subsystem="session",
                    func="SessionStore.delete_session",
                    file_path=_THIS_FILE,
                    content=f"session_delete_not_found session_id={sid}",
                    extra={"ok": False, "session_id": sid},
                )
                return False
            payload["sessions"] = new_rows
            self._write_sessions(payload)
            self._messages_path(sid).unlink(missing_ok=True)
            (self.exports_dir / f"{sid}.md").unlink(missing_ok=True)
            (self.exports_dir / f"{sid}.txt").unlink(missing_ok=True)
            self._session_locks.pop(sid, None)
            audit_event(
                op_type="PUBLIC_WRITE",
                subsystem="session",
                func="SessionStore.delete_session",
                file_path=_THIS_FILE,
                content=f"session_deleted session_id={sid}",
                extra={"ok": True, "session_id": sid},
            )
            return True

    def _normalize_message(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        role = str(raw.get("role", "")).strip()
        if role not in ROLE_SET:
            return None
        content = str(raw.get("content", ""))
        if not content.strip():
            return None
        msg_id = str(raw.get("id", "")).strip() or f"m_{uuid.uuid4().hex[:16]}"
        entry_type = str(raw.get("entry_type", "chat")).strip() or "chat"
        if entry_type not in ENTRY_TYPES:
            entry_type = "chat"
        terminal_kind = str(raw.get("terminal_kind", "")).strip()
        if terminal_kind not in TERMINAL_KINDS:
            terminal_kind = ""
        terminal_class = str(raw.get("terminal_class", raw.get("class", ""))).strip().lower()
        if terminal_class not in TERMINAL_CLASSES:
            terminal_class = ""
        turn_id = re.sub(r"[^A-Za-z0-9._:-]+", "_", str(raw.get("turn_id", "") or "").strip())[:80]
        is_summary = bool(raw.get("is_summary", False))
        created_at = str(raw.get("created_at", "")).strip() or _now_iso()
        out = {
            "id": msg_id,
            "role": role,
            "content": _truncate_text(content, MAX_MSG_CHARS),
            "entry_type": entry_type,
            "terminal_kind": terminal_kind,
            "terminal_class": terminal_class,
            "is_summary": is_summary,
            "created_at": created_at,
        }
        if turn_id:
            out["turn_id"] = turn_id
        return out

    def append_messages(self, session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id 非法")
        lock = self._get_session_lock(sid)
        with lock:
            self.ensure_session(sid)
            additions: list[dict[str, Any]] = []
            for item in messages:
                n = self._normalize_message(item)
                if n is not None:
                    additions.append(n)
            if not additions:
                return {"session_id": sid, "added": 0}

            # 增量追加：只写新行，不重写整个文件
            path = self._messages_path(sid)
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = [json.dumps(item, ensure_ascii=False) for item in additions]
            with path.open("a", encoding="utf-8") as fp:
                fp.write("\n".join(lines) + "\n")
            # 同步到热缓存
            cached = self._msg_cache.get(sid)
            if cached is not None:
                cached.extend(additions)
                self._touch_msg_cache(sid)
            else:
                self._load_messages(sid)

            # 只从缓存取计数，避免全量读取
            meta = self._sessions_cache.get(sid) if self._sessions_cache else None
            new_count = max(0, int(meta.get("message_count", 0) if meta else 0)) + len(additions)
            self._touch_session_meta(sid, message_count=new_count)
            return {"session_id": sid, "added": len(additions), "message_count": new_count}

    def set_session_title(self, session_id: str, title: str) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id 非法")
        with self._lock:
            row = self._touch_session_meta(sid, title=title)
            audit_event(
                op_type="PUBLIC_WRITE",
                subsystem="session",
                func="SessionStore.set_session_title",
                file_path=_THIS_FILE,
                content=f"session_title_updated session_id={sid}",
                extra={"session_id": sid, "title": str(row.get("title", ""))},
            )
            return row

    def mark_reset_anchor(self, session_id: str) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id 非法")
        lock = self._get_session_lock(sid)
        with lock:
            self.ensure_session(sid)
            rows = self._load_messages(sid)
            anchor_id = str(rows[-1].get("id", "")) if rows else ""
            meta = self._touch_session_meta(
                sid,
                reset_anchor_msg_id=anchor_id,
                # 重置后摘要边界也回到初始，避免老摘要锚点干扰新上下文。
                summary_anchor_msg_id="",
                latest_summary_message_id="",
                message_count=len(rows),
            )
            audit_event(
                op_type="PUBLIC_WRITE",
                subsystem="session",
                func="SessionStore.mark_reset_anchor",
                file_path=_THIS_FILE,
                content=f"session_reset_anchor_marked session_id={sid}",
                extra={
                    "session_id": sid,
                    "reset_anchor_msg_id": str(meta.get("reset_anchor_msg_id", "") or ""),
                    "message_count": len(rows),
                },
            )
            return {
                "session_id": sid,
                "reset_anchor_msg_id": str(meta.get("reset_anchor_msg_id", "") or ""),
                "message_count": len(rows),
            }

    def set_session_config(self, session_id: str, *, max_context_tokens: int | None = None) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id 非法")
        with self._get_session_lock(sid):
            self.ensure_session(sid)
            meta = self._touch_session_meta(sid, max_context_tokens=max_context_tokens)
            return {"session_id": sid, "max_context_tokens": int(meta.get("max_context_tokens", 16000))}

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        sid = _safe_session_id(session_id)
        if not sid:
            return []
        return self._load_messages(sid)

    def get_context_messages(self, session_id: str) -> list[dict[str, Any]]:
        sid = _safe_session_id(session_id)
        if not sid:
            return []
        meta = self.get_session(sid) or {}
        rows = [dict(x) for x in self._load_messages(sid)]
        if not rows:
            return []

        reset_anchor_id = str(meta.get("reset_anchor_msg_id", "") or "")
        if reset_anchor_id:
            reset_idx = -1
            for i, item in enumerate(rows):
                if str(item.get("id", "")) == reset_anchor_id:
                    reset_idx = i
                    break
            if reset_idx >= 0:
                rows = rows[reset_idx + 1:]
        if not rows:
            return []

        latest_summary_id = str(meta.get("latest_summary_message_id", "") or "")
        anchor_id = str(meta.get("summary_anchor_msg_id", "") or "")
        if not latest_summary_id or not anchor_id:
            return rows

        latest_summary = None
        anchor_idx = -1
        for i, item in enumerate(rows):
            mid = str(item.get("id", ""))
            if mid == latest_summary_id:
                latest_summary = dict(item)
            if mid == anchor_id:
                anchor_idx = i

        if not latest_summary or anchor_idx < 0:
            return rows

        tail = [
            dict(x)
            for i, x in enumerate(rows)
            if i >= anchor_idx and str(x.get("id", "")) != latest_summary_id
        ]
        out = [latest_summary] + tail
        out.sort(key=lambda x: str(x.get("created_at", "")))
        return out

    def _effective_context_rows(self, sid: str) -> list[dict[str, Any]]:
        """
        返回当前会话“真正参与模型上下文”的消息集合。
        """
        return self.get_context_messages(sid)

    def maybe_first_round_messages(self, session_id: str) -> tuple[str, str] | None:
        rows = self._load_messages(session_id)
        if not rows:
            return None
        pending_user = ""
        first_user = ""
        for item in rows:
            if str(item.get("entry_type", "")) != "chat":
                continue
            role = str(item.get("role", ""))
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            if role == "user":
                if not first_user:
                    first_user = content
                pending_user = content
                continue
            if role == "assistant" and pending_user:
                return pending_user, content
        # 仅有用户消息时也给标题生成留兜底输入（例如首轮是命令或工具链路异常）。
        if first_user:
            return first_user, ""
        return None

    def compress_context(self, session_id: str, summary_text: str) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id 非法")
        lock = self._get_session_lock(sid)
        with lock:
            all_rows = self._load_messages(sid)
            context_rows = self._effective_context_rows(sid)
            # “原始消息”定义：非摘要 + user/assistant + chat
            raw_rows = [
                x for x in context_rows
                if not bool(x.get("is_summary", False))
                and str(x.get("entry_type", "chat")) == "chat"
                and str(x.get("role", "")) in {"user", "assistant"}
            ]
            if len(raw_rows) < 6:
                audit_event(
                    op_type="PUBLIC_WRITE",
                    subsystem="session",
                    func="SessionStore.compress_context",
                    file_path=_THIS_FILE,
                    content=f"compress_context_insufficient_messages session_id={sid}",
                    extra={"session_id": sid, "raw_count": len(raw_rows), "ok": False},
                )
                raise SessionStoreError("消息数量不足，至少需要 6 条消息才能压缩")

            keep_tail = raw_rows[-4:]
            older = raw_rows[:-4]
            if not older:
                audit_event(
                    op_type="PUBLIC_WRITE",
                    subsystem="session",
                    func="SessionStore.compress_context",
                    file_path=_THIS_FILE,
                    content=f"compress_context_no_older_messages session_id={sid}",
                    extra={"session_id": sid, "ok": False},
                )
                raise SessionStoreError("消息数量不足，至少需要 6 条消息才能压缩")

            summary_msg_id = f"m_{uuid.uuid4().hex[:16]}"
            summary_msg = {
                "id": summary_msg_id,
                "role": "system",
                "content": _truncate_text(summary_text, MAX_MSG_CHARS),
                "entry_type": "notice",
                "terminal_kind": "",
                "is_summary": True,
                "created_at": _now_iso(),
            }
            all_rows.append(summary_msg)
            all_rows.sort(key=lambda x: str(x.get("created_at", "")))
            self._write_messages(sid, all_rows)
            anchor_id = str(keep_tail[0].get("id", ""))
            self._touch_session_meta(
                sid,
                message_count=len(all_rows),
                latest_summary_message_id=summary_msg_id,
                summary_anchor_msg_id=anchor_id,
            )
            self._render_exports_for_session(sid)
            audit_event(
                op_type="PUBLIC_WRITE",
                subsystem="session",
                func="SessionStore.compress_context",
                file_path=_THIS_FILE,
                content=f"compress_context_done session_id={sid}",
                extra={
                    "session_id": sid,
                    "compressed_count": len(older),
                    "summary_message_id": summary_msg_id,
                    "anchor_message_id": anchor_id,
                },
            )
            return {
                "session_id": sid,
                "compressed_count": len(older),
                "summary_message_id": summary_msg_id,
                "anchor_message_id": anchor_id,
                "visible_count": 1 + len(keep_tail),
            }

    def _render_exports_for_session(self, session_id: str) -> None:
        rows = self._load_messages(session_id)
        sid = _safe_session_id(session_id)
        meta = self.get_session(sid) or {}
        md_path = self.exports_dir / f"{sid}.md"
        txt_path = self.exports_dir / f"{sid}.txt"

        md_lines = [
            "# TindaAgent Session Export",
            "",
            f"- session_id: `{sid}`",
            f"- title: `{meta.get('title', '新对话')}`",
            f"- created_at: `{meta.get('created_at', '')}`",
            f"- updated_at: `{meta.get('updated_at', '')}`",
            f"- message_count: `{len(rows)}`",
            "",
            "---",
            "",
        ]
        txt_lines = [
            f"session_id={sid}",
            f"title={meta.get('title', '新对话')}",
            f"created_at={meta.get('created_at', '')}",
            f"updated_at={meta.get('updated_at', '')}",
            f"message_count={len(rows)}",
            "",
        ]
        for idx, item in enumerate(rows, start=1):
            role = str(item.get("role", "")).upper()
            et = str(item.get("entry_type", "chat")).upper()
            ts = str(item.get("created_at", ""))
            content = str(item.get("content", ""))
            is_summary = bool(item.get("is_summary", False))
            summary_tag = " [SUMMARY]" if is_summary else ""

            md_lines.append(f"## {idx}. {role} · {et}{summary_tag} · {ts}")
            md_lines.append("")
            md_lines.append("```text")
            md_lines.append(content)
            md_lines.append("```")
            md_lines.append("")

            txt_lines.append(f"[{idx}] role={role.lower()} entry_type={et.lower()} is_summary={int(is_summary)} ts={ts}")
            txt_lines.append(content)
            txt_lines.append("")

        md_path.write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")
        txt_path.write_text("\n".join(txt_lines).rstrip() + "\n", encoding="utf-8")
        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="session",
            func="SessionStore._render_exports_for_session",
            file_path=_THIS_FILE,
            content=f"render_session_exports session_id={sid}",
            extra={"session_id": sid, "message_count": len(rows)},
        )

    def get_tool_events_after(self, session_id: str, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        # 与 ToolRuntimeManager 组合使用；SessionStore 只提供会话存在兜底
        self.ensure_session(session_id)
        return {
            "session_id": _safe_session_id(session_id),
            "after_seq": max(0, int(after_seq)),
            "events": [],
            "next_seq": max(0, int(after_seq)),
            "total": 0,
        }


def cleanup_legacy_chat_records(chat_records_root: Path) -> None:
    root = chat_records_root.resolve()
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                path.rmdir()
        except Exception:
            continue
    try:
        root.rmdir()
    except Exception:
        pass
