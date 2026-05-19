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
    owner_uid: str = ""
    message_count: int = 0
    reset_anchor_msg_id: str = ""
    summary_anchor_msg_id: str = ""
    latest_summary_message_id: str = ""
    last_compress_anchor_msg_id: str = ""


def _normalize_entry(raw: dict) -> dict | None:
    """Normalize old or new format entry to new format."""
    role = str(raw.get("role", "")).strip()
    if role not in ROLE_SET:
        return None
    content = raw.get("content", "")
    msg_id = raw.get("id", "") or sa.make_message_id()

    def with_meta(item: dict | None) -> dict | None:
        if item is None:
            return None
        for key in getattr(sa, "_ENTRY_META_KEYS", ("created_at", "turn_id", "is_summary")):
            value = raw.get(key)
            if value not in (None, ""):
                item[key] = value
        return item

    # Already new format?
    if isinstance(content, dict) and not content.get("user") and not content.get("text"):
        has_num_keys = any(k.isdigit() for k in content)
        if has_num_keys:
            return with_meta({"role": role, "id": str(msg_id), "content": content})

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
        msg = sa.build_system_message(text)
        msg.setdefault("type", "system_notice")
        msg.setdefault("display_target", "chat")
        msg.setdefault("context_policy", "exclude")
        return with_meta(msg)
    elif et == "tool_marker":
        msg = sa.build_assistant_message([{"kind": "text", "content": text}])
        msg["type"] = "tool_marker"
        msg["display_target"] = "chat"
        msg["context_policy"] = "exclude"
        return with_meta(msg)
    elif et == "terminal":
        return None  # terminal output merged into tool_marker
    elif role == "user":
        is_raw = content.get("user") if isinstance(content, dict) else False
        msg = sa.build_user_message(text, raw=bool(is_raw))
        msg.setdefault("type", "user_message")
        msg.setdefault("display_target", "chat")
        msg.setdefault("context_policy", "include")
        return with_meta(msg)
    elif role == "assistant":
        substeps = [{"kind": "text", "content": text}]
        reasoning = raw.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            substeps.insert(0, {"kind": "thinking", "content": reasoning})
        msg = sa.build_assistant_message(substeps)
        msg.setdefault("type", "assistant_message")
        msg.setdefault("display_target", "chat")
        msg.setdefault("context_policy", "include")
        return with_meta(msg)
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
        self._session_locks: dict[str, threading.RLock] = {}
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

    def _get_session_lock(self, session_id: str) -> threading.RLock:
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[session_id] = lock
            return lock

    # ── Messages path (now .json) ──

    def _messages_path(self, session_id: str) -> Path:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        return self.messages_dir / f"{sid}.json"

    def has_message_file(self, session_id: str) -> bool:
        sid = _safe_session_id(session_id)
        if not sid:
            return False
        if self._messages_path(sid).exists():
            return True
        legacy = self.legacy_messages_dir / f"{sid}.jsonl" if self.legacy_messages_dir else None
        return bool(legacy and legacy.exists())

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
        sid = _safe_session_id(session_id)
        if not sid:
            return {}
        data = self._load_messages_raw(sid)
        normalized, changed = sa.normalize_store_dict(data)
        if changed:
            lock = self._get_session_lock(sid)
            with lock:
                latest = self._load_messages_raw(sid)
                normalized, changed = sa.normalize_store_dict(latest)
                if changed:
                    self._write_messages(sid, normalized)
                    self._touch_session_meta(sid, message_count=len(normalized))
                    self._render_exports_for_session(sid)
        return normalized

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
                    for idx, ss in enumerate(substeps):
                        storage_steps = sa._storage_steps_from_substep(
                            ss,
                            id_prefix=f"{sid}:{entry.get('id', 'assistant')}:{max_sub + idx + 1}",
                        )
                        if not storage_steps:
                            continue
                        for storage_step in storage_steps:
                            max_sub += 1
                            content[str(max_sub)] = storage_step
                    normalized_entry, _changed = sa.normalize_store_entry({
                        **entry,
                        "content": content,
                    })
                    entry["content"] = normalized_entry.get("content", content)
                    data[str(k)] = entry
                    data, _changed = sa.normalize_store_dict(data)
                    self._write_messages(sid, data)
                    self._touch_session_meta(sid, message_count=len(data))
                    self._render_exports_for_session(sid)
                    return True
            return False

    def ensure_turn_draft(
        self,
        session_id: str,
        *,
        user_message: dict[str, Any],
        assistant_message: dict[str, Any],
        turn_id: str,
    ) -> dict[str, Any]:
        """Ensure the current streaming turn has user + assistant rows on disk."""
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        clean_turn = str(turn_id or "").strip()
        lock = self._get_session_lock(sid)
        with lock:
            self.ensure_session(sid)
            data = self._load_messages_raw(sid)
            data, _changed = sa.normalize_store_dict(data)

            user_norm = _normalize_entry(user_message)
            assistant_norm = _normalize_entry(assistant_message)
            if user_norm is None or assistant_norm is None:
                raise SessionStoreError("turn draft message invalid")
            user_norm, _ = sa.normalize_store_entry(user_norm)
            assistant_norm, _ = sa.normalize_store_entry(assistant_norm)
            if clean_turn:
                user_norm["turn_id"] = clean_turn
                assistant_norm["turn_id"] = clean_turn

            existing_user_key = ""
            existing_assistant_key = ""
            keys = sorted((int(k) for k in data if k.isdigit()))
            for k in keys:
                row = data.get(str(k))
                if not isinstance(row, dict) or str(row.get("turn_id", "") or "") != clean_turn:
                    continue
                if row.get("role") == "user" and not existing_user_key:
                    existing_user_key = str(k)
                elif row.get("role") == "assistant" and not existing_assistant_key:
                    existing_assistant_key = str(k)

            max_key = max(keys, default=0)
            if existing_user_key:
                data[existing_user_key] = {**data[existing_user_key], **user_norm}
            else:
                max_key += 1
                existing_user_key = str(max_key)
                data[existing_user_key] = user_norm

            if existing_assistant_key:
                current = data[existing_assistant_key]
                if not isinstance(current.get("content"), dict) or not any(
                    str(k).isdigit() for k in current.get("content", {})
                ):
                    data[existing_assistant_key] = {**current, **assistant_norm}
            else:
                max_key += 1
                existing_assistant_key = str(max_key)
                data[existing_assistant_key] = assistant_norm

            data, _changed = sa.normalize_store_dict(data)
            self._write_messages(sid, data)
            self._touch_session_meta(sid, message_count=len(data))
            self._render_exports_for_session(sid)
            return {
                "session_id": sid,
                "user_key": existing_user_key,
                "assistant_key": existing_assistant_key,
                "message_count": len(data),
            }

    def append_to_assistant_by_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        substeps: list[dict[str, Any]],
        replace_tool_results: bool = True,
    ) -> bool:
        """Append substeps to this turn's assistant, updating matching tool markers when possible."""
        sid = _safe_session_id(session_id)
        clean_turn = str(turn_id or "").strip()
        if not sid or not clean_turn:
            return False
        lock = self._get_session_lock(sid)
        with lock:
            data = self._load_messages_raw(sid)
            target_key = ""
            for k in sorted((int(k) for k in data if k.isdigit()), reverse=True):
                entry = data.get(str(k))
                if (
                    isinstance(entry, dict)
                    and entry.get("role") == "assistant"
                    and str(entry.get("turn_id", "") or "") == clean_turn
                ):
                    target_key = str(k)
                    break
            if not target_key:
                return False

            entry = data[target_key]
            content = entry.get("content", {})
            if not isinstance(content, dict):
                content = {}
            max_sub = max((int(sk) for sk in content if sk.isdigit()), default=0)

            def _marker_match(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
                if not isinstance(existing, dict) or not isinstance(incoming, dict):
                    return False
                marker = existing.get("tool_marker")
                if not isinstance(marker, dict):
                    return False
                inc_id = str(incoming.get("id", "") or "").strip()
                inc_model_id = str(incoming.get("tool_call_id", "") or "").strip()
                inc_name = str(incoming.get("name", incoming.get("tool_name", "")) or "").strip()
                inc_stdin = str(incoming.get("stdin", "") or "").strip()
                cur_id = str(marker.get("id", "") or "").strip()
                cur_model_id = str(marker.get("tool_call_id", "") or "").strip()
                cur_name = str(marker.get("name", marker.get("tool_name", "")) or "").strip()
                cur_stdin = str(marker.get("stdin", "") or "").strip()
                cur_status = str(marker.get("status", "") or "").strip().lower()
                if inc_id and cur_id and inc_id == cur_id:
                    return True
                if inc_model_id and cur_model_id and inc_model_id == cur_model_id:
                    return True
                if cur_status in {"running", "pending"} and inc_name and inc_name == cur_name:
                    if not inc_stdin or not cur_stdin or inc_stdin == cur_stdin:
                        return True
                return False

            changed = False
            for idx, ss in enumerate(substeps):
                storage_steps = sa._storage_steps_from_substep(
                    ss,
                    id_prefix=f"{sid}:{entry.get('id', 'assistant')}:{max_sub + idx + 1}",
                )
                for storage_step in storage_steps:
                    incoming_marker = storage_step.get("tool_marker") if isinstance(storage_step, dict) else None
                    if replace_tool_results and isinstance(incoming_marker, dict):
                        replaced = False
                        for sk in sorted((int(x) for x in content if str(x).isdigit())):
                            existing = content.get(str(sk), {})
                            if _marker_match(existing, incoming_marker):
                                content[str(sk)] = storage_step
                                replaced = True
                                changed = True
                                break
                        if replaced:
                            continue
                    max_sub += 1
                    content[str(max_sub)] = storage_step
                    changed = True

            if not changed:
                return False
            normalized_entry, _entry_changed = sa.normalize_store_entry({**entry, "content": content})
            data[target_key] = {**entry, "content": normalized_entry.get("content", content)}
            data, _changed = sa.normalize_store_dict(data)
            self._write_messages(sid, data)
            self._touch_session_meta(sid, message_count=len(data))
            self._render_exports_for_session(sid)
            return True

    def replace_assistant_by_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        substeps: list[dict[str, Any]],
    ) -> bool:
        """Replace this turn's assistant content with authoritative final substeps."""
        sid = _safe_session_id(session_id)
        clean_turn = str(turn_id or "").strip()
        if not sid or not clean_turn:
            return False
        lock = self._get_session_lock(sid)
        with lock:
            data = self._load_messages_raw(sid)
            target_key = ""
            for k in sorted((int(k) for k in data if k.isdigit()), reverse=True):
                entry = data.get(str(k))
                if (
                    isinstance(entry, dict)
                    and entry.get("role") == "assistant"
                    and str(entry.get("turn_id", "") or "") == clean_turn
                ):
                    target_key = str(k)
                    break
            if not target_key:
                return False
            entry = data[target_key]
            replacement = sa.build_assistant_message(substeps)
            normalized, _entry_changed = sa.normalize_store_entry({
                **entry,
                "content": replacement.get("content", {}),
            })
            data[target_key] = {
                **entry,
                "content": normalized.get("content", replacement.get("content", {})),
                "turn_id": clean_turn,
            }
            data, _changed = sa.normalize_store_dict(data)
            self._write_messages(sid, data)
            self._touch_session_meta(sid, message_count=len(data))
            self._render_exports_for_session(sid)
            return True

    def normalize_session_messages(self, session_id: str) -> bool:
        sid = _safe_session_id(session_id)
        if not sid:
            return False
        lock = self._get_session_lock(sid)
        with lock:
            data = self._load_messages_raw(sid)
            normalized, changed = sa.normalize_store_dict(data)
            if not changed:
                return False
            self._write_messages(sid, normalized)
            self._touch_session_meta(sid, message_count=len(normalized))
            self._render_exports_for_session(sid)
            return True

    def normalize_all_sessions(self) -> int:
        changed_count = 0
        for row in self._read_sessions().get("sessions", []):
            if not isinstance(row, dict):
                continue
            sid = _safe_session_id(str(row.get("id", "") or ""))
            if sid and self.normalize_session_messages(sid):
                changed_count += 1
        return changed_count

    def append_messages(self, session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        lock = self._get_session_lock(sid)
        with lock:
            self.ensure_session(sid)
            data = self._load_messages_raw(sid)
            data, _changed = sa.normalize_store_dict(data)
            max_key = max((int(k) for k in data if k.isdigit()), default=0)
            added = 0
            for msg in messages:
                normalized = _normalize_entry(msg)
                if normalized is None:
                    continue
                normalized, _entry_changed = sa.normalize_store_entry(normalized)
                max_key += 1
                data[str(max_key)] = normalized
                added += 1
            self._write_messages(sid, data)
            self._touch_session_meta(sid, message_count=len(data))
            self._render_exports_for_session(sid)
            return {"session_id": sid, "added": added, "message_count": len(data)}

    def get_context_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return LLM-ready message rows (delegates to session_adapter)."""
        sid = _safe_session_id(session_id)
        if not sid:
            return []
        data = self.load_messages(sid)
        terminal_entries = self.load_terminal(sid)
        meta = self.get_session(sid) or {}
        rows, _ = sa.store_dict_to_agent_messages(data, meta, terminal_entries=terminal_entries)
        return rows

    def load_effective_messages(self, session_id: str) -> dict[str, Any]:
        """Return frontend/export-visible messages after reset/compression anchors."""
        sid = _safe_session_id(session_id)
        if not sid:
            return {}
        data = self.load_messages(sid)
        meta = self.get_session(sid) or {}
        return sa.effective_store_dict(data, meta)

    # ── Session lifecycle ──

    def _touch_session_meta(self, session_id: str, *, title: str | None = None,
                            owner_uid: str | None = None,
                            reset_anchor_msg_id: str | None = None,
                            summary_anchor_msg_id: str | None = None,
                            latest_summary_message_id: str | None = None,
                            last_compress_anchor_msg_id: str | None = None,
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
                    "owner_uid": str(owner_uid or ""), "message_count": 0,
                    "reset_anchor_msg_id": "", "summary_anchor_msg_id": "",
                    "latest_summary_message_id": "", "last_compress_anchor_msg_id": ""}
            sessions.append(item)
            idx = len(sessions) - 1
        row = dict(sessions[idx])
        row.setdefault("owner_uid", "")
        row.setdefault("reset_anchor_msg_id", "")
        row.setdefault("summary_anchor_msg_id", "")
        row.setdefault("latest_summary_message_id", "")
        row.setdefault("last_compress_anchor_msg_id", "")
        row["updated_at"] = now
        if title is not None:
            t = str(title or "").strip().strip("\"'")
            row["title"] = t[:MAX_TITLE_LEN] or "新对话"
        if owner_uid is not None and not str(row.get("owner_uid", "") or "").strip():
            row["owner_uid"] = str(owner_uid or "")
        if reset_anchor_msg_id is not None:
            row["reset_anchor_msg_id"] = str(reset_anchor_msg_id or "")
        if summary_anchor_msg_id is not None:
            row["summary_anchor_msg_id"] = str(summary_anchor_msg_id or "")
        if latest_summary_message_id is not None:
            row["latest_summary_message_id"] = str(latest_summary_message_id or "")
        if last_compress_anchor_msg_id is not None:
            row["last_compress_anchor_msg_id"] = str(last_compress_anchor_msg_id or "")
        if message_count is not None:
            row["message_count"] = max(0, int(message_count))
        sessions[idx] = row
        sessions.sort(key=lambda x: str(x.get("updated_at", "")), reverse=True)
        payload["sessions"] = sessions
        self._write_sessions(payload)
        return row

    def create_session(self, session_id: str | None = None, title: str = "新对话",
                       owner_uid: str | None = None) -> dict[str, Any]:
        sid = _safe_session_id(session_id or f"s_{uuid.uuid4().hex[:12]}")
        if not sid:
            sid = f"s_{uuid.uuid4().hex[:12]}"
        with self._lock:
            payload = self._read_sessions()
            exists = {str(x.get("id", "")) for x in payload.get("sessions", [])}
            if sid in exists:
                sid = f"{sid}_{uuid.uuid4().hex[:6]}"
            row = self._touch_session_meta(sid, title=title, owner_uid=owner_uid, message_count=0)
            return row

    def ensure_session(self, session_id: str, owner_uid: str | None = None) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        with self._lock:
            row = self.get_session(sid)
            if row:
                if owner_uid is not None and not str(row.get("owner_uid", "") or "").strip():
                    return self._touch_session_meta(sid, owner_uid=owner_uid)
                return row
            message_count = len(self._load_messages_raw(sid)) if self.has_message_file(sid) else 0
            return self._touch_session_meta(sid, owner_uid=owner_uid, message_count=message_count)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        sid = _safe_session_id(session_id)
        if not sid:
            return None
        for it in self._read_sessions().get("sessions", []):
            if it.get("id") == sid:
                return dict(it)
        return None

    def list_sessions(self, limit: int = 200, offset: int = 0,
                      owner_uid: str | None = None) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        rows = sorted(self._read_sessions().get("sessions", []),
                      key=lambda x: str(x.get("updated_at", "")), reverse=True)
        if owner_uid is not None:
            owner = str(owner_uid or "")
            rows = [x for x in rows if str(x.get("owner_uid", "") or "") in {"", owner}]
        rows = [x for x in rows if int(x.get("message_count") or 0) > 0]
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
                                            last_compress_anchor_msg_id="",
                                            message_count=len(data))
            return {"session_id": sid, "reset_anchor_msg_id": anchor_id,
                    "message_count": len(data)}

    def maybe_first_round_messages(self, session_id: str) -> tuple[str, str] | None:
        """
        提取首轮对话用于自动生成标题。

        规则(v1.8.2):
          - 找到第一条 chat user 消息(忽略 system/notice/tool_marker/terminal)
          - 找到该 user 之后第一条 chat assistant 消息(同样忽略 tool_marker/terminal)
          - 若有 user 但无紧随 assistant,返回 (user_text, "") 作为 fallback
          - 完全无 user 消息时返回 None
        """
        def _extract_user_text(content: Any) -> str:
            if isinstance(content, dict):
                for sk in sorted((int(k2) for k2 in content if isinstance(k2, str) and k2.isdigit()), key=int):
                    v = content[str(sk)]
                    if isinstance(v, dict):
                        text = str(v.get("user") or v.get("text") or "")
                        if text:
                            return text
                return str(content.get("user") or content.get("text") or "")
            if isinstance(content, str):
                return content
            return ""

        def _extract_assistant_text(content: Any) -> str:
            if isinstance(content, dict):
                thinking_texts: list[str] = []
                for sk in sorted((int(k2) for k2 in content if isinstance(k2, str) and k2.isdigit()), key=int):
                    v = content[str(sk)]
                    if not isinstance(v, dict):
                        continue
                    if "text" in v:
                        return str(v["text"])
                    if "thinking" in v:
                        thinking_texts.append(str(v["thinking"]))
                if thinking_texts:
                    return thinking_texts[0]
                return ""
            if isinstance(content, str):
                return content
            return ""

        data = self._load_messages_raw(session_id)
        entries = [(int(k), data[k]) for k in data if isinstance(k, str) and k.isdigit() and isinstance(data[k], dict)]
        entries.sort()

        # 第一条 chat user
        first_user_idx = -1
        first_user_text = ""
        for i, (_, e) in enumerate(entries):
            if str(e.get("type", "") or "") == "tool_marker" or str(e.get("context_policy", "") or "") == "exclude":
                continue
            if e.get("role") != "user":
                continue
            text = _extract_user_text(e.get("content", {}))
            if text.strip():
                first_user_idx = i
                first_user_text = text
                break
        if first_user_idx < 0:
            return None

        # 第一条 chat user 之后的 assistant chat 消息(跳过 tool_marker/terminal/notice 等)
        for _, e in entries[first_user_idx + 1:]:
            if str(e.get("type", "") or "") == "tool_marker" or str(e.get("context_policy", "") or "") == "exclude":
                continue
            if e.get("role") != "assistant":
                continue
            text = _extract_assistant_text(e.get("content", {}))
            if text.strip():
                return first_user_text, text

        # user-only fallback(还没等到 assistant 回复就触发标题生成)
        return first_user_text, ""

    def compress_context(
            self,
            session_id: str,
            summary_text: str,
            *,
            display_target: str = "chat",
    ) -> dict[str, Any]:
        sid = _safe_session_id(session_id)
        if not sid:
            raise SessionStoreError("session_id invalid")
        lock = self._get_session_lock(sid)
        with lock:
            data = self._load_messages_raw(sid)
            meta = self.get_session(sid) or {}
            full_raw_rows = sa.filter_raw_chat_entries(data)
            if len(full_raw_rows) >= 4:
                last_anchor_id = str(full_raw_rows[-4].get("id", "") or "")
                if last_anchor_id and last_anchor_id == str(meta.get("last_compress_anchor_msg_id", "") or ""):
                    return {
                        "session_id": sid,
                        "compressed": False,
                        "reason": "already_compressed",
                        "anchor_message_id": last_anchor_id,
                        "visible_count": len(self.load_effective_messages(sid)),
                    }
            raw_rows = sa.filter_raw_chat_entries(sa.effective_store_dict(data, meta))
            if len(raw_rows) < 6:
                raise SessionStoreError("消息数量不足，至少需要 6 条消息才能压缩")
            keep_tail = raw_rows[-4:]
            older = raw_rows[:-4]
            if not older:
                raise SessionStoreError("消息数量不足，至少需要 6 条消息才能压缩")
            anchor_id = str(keep_tail[0].get("id", "") or "")
            # Insert summary as system message
            keys = sorted(int(k) for k in data if k.isdigit())
            max_key = keys[-1] if keys else 0
            summary_id = sa.make_message_id()
            summary_msg = sa.build_system_message(summary_text)
            summary_msg["id"] = summary_id
            summary_msg["is_summary"] = True
            summary_msg["type"] = "summary"
            summary_msg["display_target"] = str(display_target or "chat").strip() or "chat"
            summary_msg["context_policy"] = "summary"
            # Find approximate insertion point before the tail entries
            tail_start_key = int(keep_tail[0].get("seq", keys[-4] if len(keys) >= 4 else keys[0]))
            for k in range(tail_start_key, max_key + 1):
                if str(k) not in data:
                    data[str(k)] = summary_msg
                    break
            else:
                max_key += 1
                data[str(max_key)] = summary_msg
            self._write_messages(sid, data)
            meta = self._touch_session_meta(sid, message_count=len(data),
                                            latest_summary_message_id=summary_id,
                                            summary_anchor_msg_id=str(anchor_id),
                                            last_compress_anchor_msg_id=str(anchor_id))
            self._render_exports_for_session(sid)
            return {"session_id": sid, "compressed": True, "compressed_count": len(older),
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
            seen: set[tuple[str, str]] = set()
            for row in existing:
                if not isinstance(row, dict):
                    continue
                event_id = str(row.get("id", "") or "").strip()
                source_seq = str(row.get("source_seq", "") or "").strip()
                if event_id:
                    seen.add(("id", event_id))
                if source_seq and str(row.get("source", "") or "") == "tool_runtime":
                    seen.add(("tool_runtime", source_seq))
            for row in entries:
                if not isinstance(row, dict):
                    continue
                event_id = str(row.get("id", "") or "").strip()
                source_seq = str(row.get("source_seq", "") or "").strip()
                if event_id and ("id", event_id) in seen:
                    continue
                if source_seq and str(row.get("source", "") or "") == "tool_runtime" and ("tool_runtime", source_seq) in seen:
                    continue
                existing.append(row)
                if event_id:
                    seen.add(("id", event_id))
                if source_seq and str(row.get("source", "") or "") == "tool_runtime":
                    seen.add(("tool_runtime", source_seq))
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
        sid = _safe_session_id(session_id)
        meta = self.get_session(sid) or {}
        data = self.load_effective_messages(sid)
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
            substeps: list[dict] = []
            rc = row.get("reasoning_content")
            if isinstance(rc, str) and rc.strip():
                substeps.append({"kind": "thinking", "content": rc.strip()})
            substeps.append({"kind": "text", "content": str(content or "")})
            result[str(seq)] = _sa.build_assistant_message(substeps)
        elif et == "tool_marker":
            if seq > 0 and str(result[str(seq)].get("role", "")) == "assistant":
                prev_content = result[str(seq)].get("content", {})
                if isinstance(prev_content, dict):
                    max_sub = max((int(k) for k in prev_content if k.isdigit()), default=0)
                    prev_content[str(max_sub + 1)] = {
                        "tool_marker": {
                            "name": "unknown",
                            "ok": True,
                            "stdin": "",
                            "stdout": "",
                            "id": "",
                        }
                    }
                    result[str(seq)]["content"] = prev_content
    return result
