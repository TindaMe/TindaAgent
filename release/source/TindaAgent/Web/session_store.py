"""Session store — JSON dict format, one .json file per session."""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from TindaAgent.Process.Architecture.paths import get_legacy_sessions_root
from TindaAgent.Process.Observability import audit_event
from TindaAgent.Web import session_adapter as sa

_THIS_FILE = str(Path(__file__).resolve())

ROLE_SET = {"user", "assistant", "system"}
MAX_TITLE_LEN = 15


class SessionStoreError(ValueError):
    pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_session_id(session_id: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(session_id or "").strip())[:80]
    return text


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


def _normalize_entry(raw: dict) -> dict | None:
    """Normalize old or new format entry to new format."""
    role = str(raw.get("role", "")).strip()
    if role not in ROLE_SET:
        return None
    content = raw.get("content", "")
    msg_id = raw.get("id", "") or sa.make_message_id()

    # Already new format?
    if isinstance(content, dict) and not content.get("user") and not content.get("text"):
        # Might be sub-steps format — check for numeric keys
        has_num_keys = any(k.isdigit() for k in content)
        if has_num_keys:
            consolidated = "".join(
                str(ss.get("text", "")) for ss in content.values()
                if isinstance(ss, dict)
            )
            return {"role": role, "id": str(msg_id), "content": consolidated}

    # Old format: entry_type-based
    et = str(raw.get("entry_type", "chat")).strip() or "chat"
    if isinstance(content, dict):
        text = str(content.get("user") or content.get("text") or "")
        if not text.strip():
            text = "".join(str(v.get("text", "")) for v in content.values() if isinstance(v, dict))
        if not text.strip():
            text = str(content)
    else:
        text = str(content or "")

    if et == "notice" or role == "system":
        return sa.build_system_message(text)
    elif et == "tool_marker":
        return None  # tool markers embedded in assistant content
    elif et == "terminal":
        return None  # terminal output merged into tool_marker
    elif role == "user":
        is_raw = content.get("user") if isinstance(content, dict) else False
        return sa.build_user_message(text, raw=bool(is_raw))
    elif role == "assistant":
        substeps = [{"kind": "text", "content": text}]
        reasoning = raw.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            substeps.insert(0, {"kind": "thinking", "content": reasoning})
        return sa.build_assistant_message(substeps)
    return None


class SessionStore:
    def __init__(self, root_dir: Path, *, legacy_root_dir: Path | None = None) -> None:
        self.root_dir = root_dir.resolve()
        self.legacy_root_dir = Path(legacy_root_dir).resolve() if legacy_root_dir else None
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
        if not self.sessions_file.exists():
            self._write_sessions({"sessions": []})

    # ── Meta persistence (unchanged) ──

    def _read_sessions(self) -> dict[str, Any]:
        try:
            data = json.loads(self.sessions_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                return data
        except Exception:
            pass
        if self.legacy_sessions_file and self.legacy_sessions_file.exists():
            try:
                data = json.loads(self.legacy_sessions_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                    return data
            except Exception:
                pass
        return {"sessions": []}

    def _write_sessions(self, payload: dict[str, Any]) -> None:
        temp = self.sessions_file.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.sessions_file)

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    # ── Messages path (now .json) ──

    def _messages_path(self, session_id: str) -> Path:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        return self.messages_dir / f"{sid}.json"

    # ── Messages I/O ──

    def _load_messages_raw(self, session_id: str) -> dict[str, Any]:
        path = self._messages_path(session_id)
        if not path.exists():
            # Try legacy JSONL fallback
            legacy = self.legacy_messages_dir / f"{_safe_session_id(session_id)}.jsonl" if self.legacy_messages_dir else None
            if legacy and legacy.exists():
                return _migrate_jsonl_to_dict(legacy)
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {k: v for k, v in data.items() if isinstance(v, dict)}
        except Exception:
            return {}

    def _write_messages(self, session_id: str, data: dict[str, Any]) -> None:
        path = self._messages_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def load_messages(self, session_id: str) -> dict[str, Any]:
        """Return full session dict: {"1": {...}, "2": {...}}"""
        return self._load_messages_raw(session_id)

    def append_to_last_assistant(self, session_id: str,
                                  substeps: list[dict[str, Any]]) -> bool:
        """Atomically append sub-steps to the last assistant message. Returns True if updated."""
        sid = _safe_session_id(session_id)
        if not sid:
            return False
        lock = self._get_session_lock(sid)
        with lock:
            data = self._load_messages_raw(sid)
            keys = sorted((int(k) for k in data if k.isdigit()), reverse=True)
            for k in keys:
                entry = data[str(k)]
                if not isinstance(entry, dict):
                    continue
                if entry.get("role") == "assistant":
                    content = entry.get("content", {})
                    if not isinstance(content, dict):
                        content = {}
                    max_sub = max((int(sk) for sk in content if sk.isdigit()), default=0)
                    for ss in substeps:
                        max_sub += 1
                        content[str(max_sub)] = ss
                    entry["content"] = content
                    data[str(k)] = entry
                    self._write_messages(sid, data)
                    self._touch_session_meta(sid, message_count=len(data))
                    self._render_exports_for_session(sid)
                    return True
            return False

    def append_messages(self, session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        lock = self._get_session_lock(sid)
        with lock:
            self.ensure_session(sid)
            data = self._load_messages_raw(sid)
            max_key = max((int(k) for k in data if k.isdigit()), default=0)
            for msg in messages:
                normalized = _normalize_entry(msg)
                if normalized is None:
                    continue
                max_key += 1
                data[str(max_key)] = normalized
            self._write_messages(sid, data)
            self._touch_session_meta(sid, message_count=len(data))
            self._render_exports_for_session(sid)
            return {"session_id": sid, "added": len(messages), "message_count": len(data)}

    def get_context_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return LLM-ready message rows (delegates to session_adapter)."""
        sid = _safe_session_id(session_id)
        if not sid:
            return []
        data = self._load_messages_raw(sid)
        meta = self.get_session(sid) or {}
        rows, _ = sa.store_dict_to_agent_messages(data, meta)
        return rows

    # ── Session lifecycle ──

    def _touch_session_meta(self, session_id: str, *, title: str | None = None,
                            reset_anchor_msg_id: str | None = None,
                            summary_anchor_msg_id: str | None = None,
                            latest_summary_message_id: str | None = None,
                            message_count: int | None = None) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        payload = self._read_sessions()
        sessions = payload.get("sessions", [])
        now = _now_iso()
        idx = next((i for i, it in enumerate(sessions) if it.get("id") == sid), -1)
        if idx < 0:
            item = {"id": sid, "title": "新对话", "created_at": now, "updated_at": now,
                    "message_count": 0, "reset_anchor_msg_id": "", "summary_anchor_msg_id": "",
                    "latest_summary_message_id": ""}
            sessions.append(item)
            idx = len(sessions) - 1
        row = dict(sessions[idx])
        row.setdefault("reset_anchor_msg_id", "")
        row.setdefault("summary_anchor_msg_id", "")
        row.setdefault("latest_summary_message_id", "")
        row["updated_at"] = now
        if title is not None:
            t = str(title or "").strip().strip("\"'")
            row["title"] = t[:MAX_TITLE_LEN] or "新对话"
        if reset_anchor_msg_id is not None:
            row["reset_anchor_msg_id"] = str(reset_anchor_msg_id or "")
        if summary_anchor_msg_id is not None:
            row["summary_anchor_msg_id"] = str(summary_anchor_msg_id or "")
        if latest_summary_message_id is not None:
            row["latest_summary_message_id"] = str(latest_summary_message_id or "")
        if message_count is not None:
            row["message_count"] = max(0, int(message_count))
        sessions[idx] = row
        sessions.sort(key=lambda x: str(x.get("updated_at", "")), reverse=True)
        payload["sessions"] = sessions
        self._write_sessions(payload)
        return row

    def create_session(self, session_id: str | None = None, title: str = "新对话") -> dict[str, Any]:
        sid = _safe_session_id(session_id or f"s_{uuid.uuid4().hex[:12]}")
        if not sid:
            sid = f"s_{uuid.uuid4().hex[:12]}"
        with self._lock:
            payload = self._read_sessions()
            exists = {str(x.get("id", "")) for x in payload.get("sessions", [])}
            if sid in exists:
                sid = f"{sid}_{uuid.uuid4().hex[:6]}"
            row = self._touch_session_meta(sid, title=title, message_count=0)
            return row

    def ensure_session(self, session_id: str) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        with self._lock:
            row = self.get_session(sid)
            return row if row else self.create_session(sid)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        sid = _safe_session_id(session_id)
        if not sid:
            return None
        for it in self._read_sessions().get("sessions", []):
            if it.get("id") == sid:
                return dict(it)
        return None

    def list_sessions(self, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        rows = sorted(self._read_sessions().get("sessions", []),
                      key=lambda x: str(x.get("updated_at", "")), reverse=True)
        total = len(rows)
        return {"sessions": rows[offset:offset + limit], "total": total, "limit": limit, "offset": offset}

    def delete_session(self, session_id: str) -> bool:
        sid = _safe_session_id(session_id)
        if not sid:
            return False
        with self._lock:
            payload = self._read_sessions()
            old = payload.get("sessions", [])
            new_rows = [it for it in old if it.get("id") != sid]
            if len(new_rows) == len(old):
                return False
            payload["sessions"] = new_rows
            self._write_sessions(payload)
            self._messages_path(sid).unlink(missing_ok=True)
            (self.exports_dir / f"{sid}.md").unlink(missing_ok=True)
            (self.exports_dir / f"{sid}.txt").unlink(missing_ok=True)
            self._session_locks.pop(sid, None)
            return True

    def cleanup_empty_sessions(self, protect_session_id: str | None = None) -> int:
        payload = self._read_sessions()
        old = payload.get("sessions", [])
        pid = (protect_session_id or "").strip()
        empty = [it for it in old if int(it.get("message_count", 0)) <= 0
                 and (not pid or it.get("id") != pid)]
        if not empty:
            return 0
        payload["sessions"] = [it for it in old if int(it.get("message_count", 0)) > 0]
        self._write_sessions(payload)
        for it in empty:
            sid = str(it.get("id", ""))
            if sid:
                self._messages_path(sid).unlink(missing_ok=True)
        return len(empty)

    def cleanup_orphan_messages(self) -> int:
        """Delete message files (*.json, *.jsonl) that have no matching session entry."""
        payload = self._read_sessions()
        valid_ids = {str(it.get("id", "")) for it in payload.get("sessions", []) if it.get("id")}
        removed = 0
        for path in sorted(self.messages_dir.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            # Extract session ID: s_xxx.json or s_xxx.jsonl
            sid = name.rsplit(".", 1)[0] if "." in name else name
            sid = _safe_session_id(sid)
            if sid and sid not in valid_ids:
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    pass
        return removed

    def clear_all(self) -> None:
        with self._lock:
            if self.root_dir.exists():
                for p in sorted(self.root_dir.rglob("*"), reverse=True):
                    try:
                        (p.unlink() if p.is_file() else p.rmdir())
                    except Exception:
                        continue
            self.root_dir.mkdir(parents=True, exist_ok=True)
            self.messages_dir.mkdir(parents=True, exist_ok=True)
            self.exports_dir.mkdir(parents=True, exist_ok=True)
            self._write_sessions({"sessions": []})
            self._session_locks.clear()

    def set_session_title(self, session_id: str, title: str) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        with self._lock:
            return self._touch_session_meta(sid, title=title)

    def mark_reset_anchor(self, session_id: str) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        lock = self._get_session_lock(sid)
        with lock:
            self.ensure_session(sid)
            data = self._load_messages_raw(sid)
            keys = sorted(int(k) for k in data if k.isdigit())
            anchor_id = data[str(keys[-1])].get("id", "") if keys else ""
            meta = self._touch_session_meta(sid, reset_anchor_msg_id=anchor_id,
                                            summary_anchor_msg_id="",
                                            latest_summary_message_id="",
                                            message_count=len(data))
            return {"session_id": sid, "reset_anchor_msg_id": anchor_id,
                    "message_count": len(data)}

    def maybe_first_round_messages(self, session_id: str) -> tuple[str, str] | None:
        data = self._load_messages_raw(session_id)
        entries = [(int(k), data[k]) for k in data if k.isdigit() and isinstance(data[k], dict)]
        entries.sort()
        user_msgs = []
        asst_msgs = []
        for _, e in entries:
            if e.get("role") == "user":
                c = e.get("content", {})
                text = ""
                if isinstance(c, dict):
                    for sk in sorted((int(k2) for k2 in c if k2.isdigit()), key=int):
                        v = c[str(sk)]
                        if isinstance(v, dict):
                            text = str(v.get("user") or v.get("text") or "")
                            if text:
                                break
                    if not text:
                        text = str(c.get("user") or c.get("text") or "")
                elif isinstance(c, str):
                    text = c
                user_msgs.append(text)
            elif e.get("role") == "assistant":
                c = e.get("content", {})
                if isinstance(c, dict):
                    thinking_texts = []
                    for sk in sorted((int(k2) for k2 in c if k2.isdigit()), key=int):
                        v = c[str(sk)]
                        if not isinstance(v, dict):
                            continue
                        if "text" in v:
                            asst_msgs.append(str(v["text"]))
                        elif "thinking" in v:
                            thinking_texts.append(str(v["thinking"]))
                    if not asst_msgs and thinking_texts:
                        asst_msgs.append(thinking_texts[0])
                    if not asst_msgs:
                        first_sk = min((int(k2) for k2 in c if k2.isdigit()), default=None)
                        if first_sk is not None:
                            v = c[str(first_sk)]
                            if isinstance(v, dict):
                                asst_msgs.append(str(next(iter(v.values()), "")))
                elif isinstance(c, str):
                    asst_msgs.append(c)
        if len(user_msgs) == 1 and len(asst_msgs) == 1:
            return user_msgs[0], asst_msgs[0]
        return None

    def compress_context(self, session_id: str, summary_text: str) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        lock = self._get_session_lock(sid)
        with lock:
            data = self._load_messages_raw(sid)
            raw_rows = sa.filter_raw_chat_entries(data)
            if len(raw_rows) < 6:
                raise SessionStoreError("消息数量不足，至少需要 6 条消息才能压缩")
            keep_tail = raw_rows[-4:]
            older = raw_rows[:-4]
            if not older:
                raise SessionStoreError("消息数量不足，至少需要 6 条消息才能压缩")
            # Insert summary as system message
            keys = sorted(int(k) for k in data if k.isdigit())
            max_key = keys[-1] if keys else 0
            summary_id = sa.make_message_id()
            summary_msg = sa.build_system_message(summary_text)
            summary_msg["id"] = summary_id
            # Find approximate insertion point before the tail entries
            tail_start_key = keys[-4] if len(keys) >= 4 else keys[0]
            for k in range(tail_start_key, max_key + 1):
                if str(k) not in data:
                    data[str(k)] = summary_msg
                    break
            else:
                max_key += 1
                data[str(max_key)] = summary_msg
            self._write_messages(sid, data)
            anchor_id = raw_rows[-4].get("id", "") if raw_rows[-4:] else ""
            self._touch_session_meta(sid, message_count=len(data),
                                     latest_summary_message_id=summary_id,
                                     summary_anchor_msg_id=str(anchor_id))
            self._render_exports_for_session(sid)
            return {"session_id": sid, "compressed_count": len(older),
                    "summary_message_id": summary_id, "anchor_message_id": str(anchor_id),
                    "visible_count": 1 + len(keep_tail)}

    def get_tool_events_after(self, session_id: str, after_seq: int = 0,
                               limit: int = 200) -> dict[str, Any]:
        self.ensure_session(session_id)
        return {"session_id": _safe_session_id(session_id), "after_seq": max(0, int(after_seq)),
                "events": [], "next_seq": max(0, int(after_seq)), "total": 0}

    # ── Terminal storage (separate file) ──

    def _terminal_path(self, session_id: str) -> Path:
        sid = _safe_session_id(session_id)
        return self.messages_dir / f"{sid}.terminal.json"

    def append_terminal(self, session_id: str, entries: list[dict]) -> dict:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        path = self._terminal_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._get_session_lock(sid)
        with lock:
            existing: list[dict] = []
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            existing.extend(entries)
            temp = path.with_suffix(".terminal.json.tmp")
            temp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
        return {"session_id": sid, "terminal_entries": len(existing)}

    def load_terminal(self, session_id: str) -> list[dict]:
        sid = _safe_session_id(session_id)
        if not sid:
            return []
        path = self._terminal_path(sid)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    # ── Exports ──

    def _render_exports_for_session(self, session_id: str) -> None:
        data = self._load_messages_raw(session_id)
        sid = _safe_session_id(session_id)
        meta = self.get_session(sid) or {}
        md_path = self.exports_dir / f"{sid}.md"
        txt_path = self.exports_dir / f"{sid}.txt"
        md = [f"# TindaAgent Session Export\n\n- session_id: `{sid}`",
              f"- title: `{meta.get('title', '新对话')}`",
              f"- message_count: `{len(data)}`\n\n---\n"]
        txt = [f"session_id={sid}", f"title={meta.get('title', '新对话')}",
               f"message_count={len(data)}\n"]
        for sk in sorted((int(k) for k in data if k.isdigit()), key=int):
            entry = data[str(sk)]
            if not isinstance(entry, dict):
                continue
            role = entry.get("role", "?")
            ts = str(entry.get("id", ""))
            md.append(f"## {sk}. {role} · {ts}\n")
            txt.append(f"[{sk}] role={role} id={ts}")
            content = entry.get("content", {})
            if isinstance(content, dict):
                for ck in sorted((int(k2) for k2 in content if k2.isdigit()), key=int):
                    v = content[str(ck)]
                    if isinstance(v, dict):
                        for kind, val in v.items():
                            text = str(val)[:500]
                            md.append(f"```text\n[{kind}] {text}\n```\n")
                            txt.append(f"  [{kind}] {text}")
            elif isinstance(content, str):
                md.append(f"```text\n{content[:500]}\n```\n")
                txt.append(f"  {content[:500]}")
            md.append("")
            txt.append("")
        md_path.write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
        txt_path.write_text("\n".join(txt).rstrip() + "\n", encoding="utf-8")


def cleanup_legacy_chat_records(chat_records_root: Path) -> None:
    root = chat_records_root.resolve()
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            (path.unlink() if path.is_file() else path.rmdir())
        except Exception:
            continue
    try:
        root.rmdir()
    except Exception:
        pass


def _migrate_jsonl_to_dict(legacy_path: Path) -> dict[str, Any]:
    """Minimal migration: read old JSONL, produce new dict format."""
    rows = []
    try:
        for line in legacy_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return {}
    from TindaAgent.Web import session_adapter as _sa
    result: dict[str, Any] = {}
    seq = 0
    for row in rows:
        role = row.get("role", "").strip()
        et = row.get("entry_type", "chat")
        content = row.get("content", "")
        if role == "user":
            seq += 1
            result[str(seq)] = _sa.build_user_message(content, raw=True)
        elif et == "notice":
            seq += 1
            result[str(seq)] = _sa.build_system_message(content)
        elif role == "assistant" and et == "chat":
            seq += 1
            result[str(seq)] = _sa.build_assistant_message(
                [{"kind": "text", "content": content}])
        elif et == "tool_marker":
            # Attach to previous assistant as a tool_marker sub-step
            pass
    return result
