from __future__ import annotations

import base64
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

FORMAT_VERSION = "2"
SUPPORTED_FORMAT_VERSIONS = {"1", "2"}
MAX_RECORD_FILE_BYTES = 2 * 1024 * 1024
MAX_IMPORT_MESSAGES = 2000
MAX_SINGLE_MESSAGE_CHARS = 32000
ENTRY_TYPES = {"chat", "notice", "tool_marker", "terminal"}
CHATLIKE_ENTRY_TYPES = {"chat", "tool_marker"}
TERMINAL_ENTRY_KINDS = {"cmd", "out", "sep"}

_USER_META_BLOCK_RE = re.compile(
    r"\n?\n?---\n\[USER_META\][\s\S]*?\[/USER_META\]\s*$",
    flags=re.MULTILINE,
)


class RecordStoreError(ValueError):
    pass


def strip_user_meta_block(content: str) -> str:
    text = str(content or "")
    return _USER_META_BLOCK_RE.sub("", text).rstrip()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_session_id(session_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(session_id or "default").strip())
    return cleaned[:80] or "default"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _normalize_record_id(record_id: str) -> str:
    rid = str(record_id or "").strip().replace("\\", "/")
    if rid.endswith(".txt") or rid.endswith(".md"):
        rid = rid.rsplit(".", 1)[0]
    rid = rid.strip("/")
    if not rid:
        raise RecordStoreError("record_id 不能为空")
    return rid


class ChatRecordStore:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._session_record_map: dict[str, str] = {}
        self._map_lock = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._map_lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _resolve_record_paths(self, record_id: str) -> tuple[Path, Path, str]:
        rid = _normalize_record_id(record_id)
        rel = Path(rid)
        if rel.is_absolute() or ".." in rel.parts:
            raise RecordStoreError("record_id 非法")

        base_path = (self.root_dir / rel).resolve()
        if self.root_dir != base_path and self.root_dir not in base_path.parents:
            raise RecordStoreError("record_id 越界")

        normalized_id = base_path.relative_to(self.root_dir).as_posix()
        return base_path.with_suffix(".txt"), base_path.with_suffix(".md"), normalized_id

    def _txt_to_payload(self, txt_path: Path) -> dict[str, Any]:
        if not txt_path.exists():
            raise RecordStoreError("记录文件不存在")
        if txt_path.stat().st_size > MAX_RECORD_FILE_BYTES:
            raise RecordStoreError("记录文件过大，拒绝读取")

        text = txt_path.read_text(encoding="utf-8")
        meta_match = re.search(r"\[TINDA_RECORD\]\n([\s\S]*?)\n\[/TINDA_RECORD\]", text)
        if not meta_match:
            raise RecordStoreError("记录头损坏或格式不支持")

        meta: dict[str, str] = {}
        for line in meta_match.group(1).splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            meta[key.strip()] = value.strip()

        fmt_ver = meta.get("format_version", "") or "1"
        if fmt_ver not in SUPPORTED_FORMAT_VERSIONS:
            raise RecordStoreError(f"不支持的记录格式版本: {fmt_ver}")

        entries: list[dict[str, str]] = []
        for block in re.findall(r"\[TINDA_MSG\]\n([\s\S]*?)\n\[/TINDA_MSG\]", text):
            fields: dict[str, str] = {}
            for line in block.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                fields[key.strip()] = value.strip()

            role = fields.get("role", "")
            if role not in {"user", "assistant"}:
                continue
            entry_type = fields.get("entry_type", "").strip() or "chat"
            if entry_type not in ENTRY_TYPES:
                entry_type = "chat"
            terminal_kind = fields.get("terminal_kind", "").strip()
            if entry_type == "terminal" and terminal_kind not in TERMINAL_ENTRY_KINDS:
                terminal_kind = "out"
            if entry_type != "terminal":
                terminal_kind = ""
            try:
                decoded = base64.b64decode(fields.get("content_b64", "").encode("ascii"), validate=True)
                content = decoded.decode("utf-8")
            except Exception as e:
                raise RecordStoreError(f"记录内容损坏: {e}") from e
            entries.append(
                {
                    "role": role,
                    "entry_type": entry_type,
                    "terminal_kind": terminal_kind,
                    "ts": fields.get("ts", ""),
                    "content": content,
                }
            )

        if len(entries) > MAX_IMPORT_MESSAGES:
            raise RecordStoreError("记录消息过多，拒绝导入")

        payload: dict[str, Any] = {
            "record_id": txt_path.relative_to(self.root_dir).with_suffix("").as_posix(),
            "session_id": meta.get("session_id", ""),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "message_count": len(entries),
            "format_version": fmt_ver,
            "entries": entries,
        }
        return payload

    def _choose_record_id_for_session(self, session_id: str) -> str:
        sid = _safe_session_id(session_id)
        with self._map_lock:
            existing = self._session_record_map.get(sid)
            if existing:
                return existing

        latest = self.find_latest_record_id_for_session(sid)
        if latest:
            with self._map_lock:
                self._session_record_map[sid] = latest
            return latest

        now = datetime.now().astimezone()
        date_dir = now.strftime("%Y-%m-%d")
        stamp = now.strftime("%Y%m%d_%H%M%S")
        rid = f"{date_dir}/{sid}__{stamp}"
        with self._map_lock:
            self._session_record_map[sid] = rid
        return rid

    def _render_txt(self, meta: dict[str, Any], entries: list[dict[str, str]]) -> str:
        lines = [
            "[TINDA_RECORD]",
            f"format_version={FORMAT_VERSION}",
            f"session_id={meta['session_id']}",
            f"created_at={meta['created_at']}",
            f"updated_at={meta['updated_at']}",
            f"message_count={len(entries)}",
            "[/TINDA_RECORD]",
            "",
        ]

        for idx, entry in enumerate(entries, start=1):
            raw_content = str(entry.get("content", ""))
            content_b64 = base64.b64encode(raw_content.encode("utf-8")).decode("ascii")
            lines.extend(
                [
                    "[TINDA_MSG]",
                    f"index={idx}",
                    f"role={entry.get('role', '')}",
                    f"entry_type={entry.get('entry_type', 'chat')}",
                    f"terminal_kind={entry.get('terminal_kind', '')}",
                    f"ts={entry.get('ts', '')}",
                    f"content_b64={content_b64}",
                    "[/TINDA_MSG]",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _render_md(self, meta: dict[str, Any], entries: list[dict[str, str]]) -> str:
        lines = [
            "# TindaAgent Chat Record",
            "",
            f"- session_id: `{meta['session_id']}`",
            f"- created_at: `{meta['created_at']}`",
            f"- updated_at: `{meta['updated_at']}`",
            f"- message_count: `{len(entries)}`",
            f"- format_version: `{FORMAT_VERSION}`",
            "",
            "---",
            "",
        ]
        for idx, entry in enumerate(entries, start=1):
            role = str(entry.get("role", "")).upper() or "UNKNOWN"
            entry_type = str(entry.get("entry_type", "")).upper() or "CHAT"
            terminal_kind = str(entry.get("terminal_kind", "")).upper()
            ts = str(entry.get("ts", "")) or "N/A"
            content = str(entry.get("content", ""))
            max_ticks = max((len(m.group(0)) for m in re.finditer(r"`+", content)), default=0)
            fence = "`" * max(3, max_ticks + 1)
            kind_suffix = f"/{terminal_kind}" if terminal_kind else ""
            lines.append(f"## {idx}. {role} · {entry_type}{kind_suffix} · {ts}")
            lines.append("")
            lines.append(f"{fence}text")
            lines.append(content)
            lines.append(fence)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _normalize_entries(self, raw_entries: list[dict[str, Any]]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip()
            if role not in {"user", "assistant"}:
                continue
            entry_type = str(item.get("entry_type", "chat")).strip() or "chat"
            if entry_type not in ENTRY_TYPES:
                entry_type = "chat"
            if role == "user":
                entry_type = "chat"
            terminal_kind = str(item.get("terminal_kind", "")).strip()
            if entry_type == "terminal":
                if terminal_kind not in TERMINAL_ENTRY_KINDS:
                    terminal_kind = "out"
            else:
                terminal_kind = ""
            content = str(item.get("content", ""))
            if role == "user":
                content = strip_user_meta_block(content)
            if entry_type == "terminal" and terminal_kind == "sep" and not content.strip():
                content = "─" * 36
            if not content.strip():
                continue
            if len(content) > MAX_SINGLE_MESSAGE_CHARS:
                content = content[:MAX_SINGLE_MESSAGE_CHARS]
            normalized.append(
                {
                    "role": role,
                    "entry_type": entry_type,
                    "terminal_kind": terminal_kind,
                    "ts": str(item.get("ts", "")) or _now_iso(),
                    "content": content,
                }
            )
        return normalized

    @staticmethod
    def _entry_sort_key(entry: dict[str, Any], fallback_idx: int) -> tuple[int, float, int]:
        ts = str(entry.get("ts", "")).strip()
        if not ts:
            return (1, 0.0, fallback_idx)
        try:
            score = datetime.fromisoformat(ts).timestamp()
            return (0, score, fallback_idx)
        except Exception:
            return (1, 0.0, fallback_idx)

    def _sort_entries_by_ts(self, entries: list[dict[str, str]]) -> list[dict[str, str]]:
        indexed = list(enumerate(entries))
        indexed.sort(key=lambda pair: self._entry_sort_key(pair[1], pair[0]))
        return [item for _, item in indexed]

    def bind_session_record(self, session_id: str, record_id: str) -> None:
        sid = _safe_session_id(session_id)
        rid = _normalize_record_id(record_id)
        with self._map_lock:
            self._session_record_map[sid] = rid

    def find_latest_record_id_for_session(self, session_id: str) -> str | None:
        sid = _safe_session_id(session_id)
        prefix = f"{sid}__"
        candidates: list[tuple[float, str]] = []
        for path in self.root_dir.rglob("*.txt"):
            try:
                if path.stem.startswith(prefix):
                    mtime = path.stat().st_mtime
                    rid = path.relative_to(self.root_dir).with_suffix("").as_posix()
                    candidates.append((mtime, rid))
            except Exception:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def save_session_entries(
        self,
        session_id: str,
        entries: list[dict[str, Any]],
        *,
        preserve_non_chat: bool = True,
    ) -> dict[str, Any] | None:
        sid = _safe_session_id(session_id)
        clean_entries = self._normalize_entries(entries)
        if not clean_entries:
            return None

        lock = self._get_session_lock(sid)
        with lock:
            rid = self._choose_record_id_for_session(sid)
            txt_path, md_path, normalized_id = self._resolve_record_paths(rid)
            created_at = _now_iso()
            old_entries: list[dict[str, str]] = []

            if txt_path.exists():
                try:
                    old_payload = self._txt_to_payload(txt_path)
                    created_at = old_payload.get("created_at") or created_at
                    old_entries = old_payload.get("entries", [])
                except Exception:
                    pass

            now_iso = _now_iso()
            old_chatlike = [
                item for item in old_entries
                if str(item.get("entry_type", "chat")) in CHATLIKE_ENTRY_TYPES
            ]
            normalized_chatlike: list[dict[str, str]] = []
            chatlike_idx = 0
            for entry in clean_entries:
                item = {
                    "role": entry["role"],
                    "entry_type": entry.get("entry_type", "chat"),
                    "terminal_kind": entry.get("terminal_kind", ""),
                    "ts": entry.get("ts", "") or now_iso,
                    "content": entry["content"],
                }
                if preserve_non_chat and item["entry_type"] in CHATLIKE_ENTRY_TYPES:
                    old_ts = old_chatlike[chatlike_idx]["ts"] if chatlike_idx < len(old_chatlike) else ""
                    if chatlike_idx < len(old_chatlike):
                        chatlike_idx += 1
                    if old_ts:
                        item["ts"] = str(old_ts)
                normalized_chatlike.append(item)

            merged = normalized_chatlike
            if preserve_non_chat and old_entries:
                extras = [
                    {
                        "role": str(item.get("role", "")),
                        "entry_type": str(item.get("entry_type", "chat")),
                        "terminal_kind": str(item.get("terminal_kind", "")),
                        "ts": str(item.get("ts", "")),
                        "content": str(item.get("content", "")),
                    }
                    for item in old_entries
                    if str(item.get("entry_type", "chat")) not in CHATLIKE_ENTRY_TYPES
                ]
                merged = self._sort_entries_by_ts(normalized_chatlike + extras)

            meta = {
                "session_id": sid,
                "created_at": created_at,
                "updated_at": now_iso,
            }
            _atomic_write_text(txt_path, self._render_txt(meta, merged))
            _atomic_write_text(md_path, self._render_md(meta, merged))
            return {
                "record_id": normalized_id,
                "session_id": sid,
                "created_at": created_at,
                "updated_at": now_iso,
                "message_count": len(merged),
                "md_path": md_path.relative_to(self.root_dir).as_posix(),
                "txt_path": txt_path.relative_to(self.root_dir).as_posix(),
            }

    def append_session_entries(self, session_id: str, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        sid = _safe_session_id(session_id)
        clean_entries = self._normalize_entries(entries)
        if not clean_entries:
            return None

        lock = self._get_session_lock(sid)
        with lock:
            rid = self._choose_record_id_for_session(sid)
            txt_path, md_path, normalized_id = self._resolve_record_paths(rid)
            created_at = _now_iso()
            old_entries: list[dict[str, str]] = []

            if txt_path.exists():
                try:
                    old_payload = self._txt_to_payload(txt_path)
                    created_at = old_payload.get("created_at") or created_at
                    old_entries = old_payload.get("entries", [])
                except Exception:
                    pass

            now_iso = _now_iso()
            merged = self._sort_entries_by_ts(old_entries + clean_entries)

            meta = {
                "session_id": sid,
                "created_at": created_at,
                "updated_at": now_iso,
            }
            _atomic_write_text(txt_path, self._render_txt(meta, merged))
            _atomic_write_text(md_path, self._render_md(meta, merged))
            return {
                "record_id": normalized_id,
                "session_id": sid,
                "created_at": created_at,
                "updated_at": now_iso,
                "message_count": len(merged),
                "md_path": md_path.relative_to(self.root_dir).as_posix(),
                "txt_path": txt_path.relative_to(self.root_dir).as_posix(),
            }

    def load_record(self, record_id: str) -> dict[str, Any]:
        txt_path, md_path, normalized_id = self._resolve_record_paths(record_id)
        payload = self._txt_to_payload(txt_path)
        payload["record_id"] = normalized_id
        payload["has_md"] = md_path.exists()
        payload["has_txt"] = txt_path.exists()
        return payload

    def load_latest_for_session(self, session_id: str) -> dict[str, Any] | None:
        sid = _safe_session_id(session_id)
        with self._map_lock:
            rid = self._session_record_map.get(sid)
        if rid is None:
            rid = self.find_latest_record_id_for_session(sid)
            if not rid:
                return None
        payload = self.load_record(rid)
        self.bind_session_record(sid, payload["record_id"])
        return payload

    def list_records(self, limit: int = 50, offset: int = 0, query: str = "") -> dict[str, Any]:
        q = str(query or "").strip().lower()
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))

        rows: list[tuple[float, dict[str, Any]]] = []
        for txt_path in self.root_dir.rglob("*.txt"):
            try:
                payload = self._txt_to_payload(txt_path)
            except Exception:
                continue

            rid = payload.get("record_id", "")
            sid = str(payload.get("session_id", ""))
            if q and q not in rid.lower() and q not in sid.lower():
                continue

            md_path = txt_path.with_suffix(".md")
            try:
                st = txt_path.stat()
                mtime = st.st_mtime
                size_bytes = st.st_size
            except Exception:
                mtime = 0.0
                size_bytes = 0

            rows.append(
                (
                    mtime,
                    {
                        "record_id": rid,
                        "session_id": sid,
                        "created_at": payload.get("created_at", ""),
                        "updated_at": payload.get("updated_at", ""),
                        "message_count": int(payload.get("message_count", 0)),
                        "size_bytes": size_bytes,
                        "has_md": md_path.exists(),
                        "has_txt": txt_path.exists(),
                    },
                )
            )

        rows.sort(key=lambda x: x[0], reverse=True)
        total = len(rows)
        paged = [item for _, item in rows[offset:offset + limit]]
        return {
            "records": paged,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
