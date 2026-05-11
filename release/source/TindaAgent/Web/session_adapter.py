"""
Session adapter — new-format JSON dict to LLM / frontend / store conversion.

All format conversion logic lives here. server.py and session_store.py
only call these functions, no internal format knowledge.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from TindaAgent.Process.AI.tokenizer import estimate_tokens
from TindaAgent.Process.Observability.audit import get_audit_engine

LOGGER = logging.getLogger("tinda.session_adapter")

# ── ID helpers ──────────────────────────────────────────────────────────


def make_message_id(audit_event_id: int | None = None) -> str:
    """Generate a new message ID: {year}-{month}-{day}-{audit_event_id}"""
    eid = int(audit_event_id) if audit_event_id is not None else get_audit_engine().next_id()
    return datetime.now().strftime(f"%Y-%-m-%-d-{eid}")


def parse_message_id(msg_id: str) -> dict | None:
    """Parse message ID into components. Returns None if invalid format."""
    parts = str(msg_id).split("-")
    if len(parts) != 4:
        return None
    try:
        return {"year": int(parts[0]), "month": int(parts[1]),
                "day": int(parts[2]), "event_id": int(parts[3])}
    except (ValueError, TypeError):
        return None


# ── Builders ─────────────────────────────────────────────────────────────


def build_user_message(text: str, *, raw: bool = False,
                       audit_id: int | None = None) -> dict:
    content = {"1": {"user": text}} if raw else {"1": {"text": text}}
    return {"role": "user", "id": make_message_id(audit_id), "content": content}


def build_assistant_message(substeps: list[dict],
                            audit_id: int | None = None) -> dict:
    """substeps: [{"kind": "thinking", "content": "..."},
                  {"kind": "tool_marker", "name": "...", "ok": True, ...},
                  {"kind": "text", "content": "..."}]"""
    content: dict[str, dict] = {}
    n = 0
    for s in substeps:
        kind = str(s.get("kind", "")).strip()
        if kind == "thinking":
            n += 1
            content[str(n)] = {"thinking": str(s.get("content", ""))}
        elif kind == "tool_marker":
            n += 1
            content[str(n)] = {
                "tool_marker": {
                    "name": str(s.get("name", s.get("tool_name", "unknown"))),
                    "ok": bool(s.get("ok", False)),
                    "stdin": str(s.get("stdin", ""))[:500],
                    "stdout": str(s.get("stdout", ""))[:500],
                    "id": str(s.get("id", s.get("call_id", ""))),
                }
            }
        elif kind == "text":
            n += 1
            content[str(n)] = {"text": str(s.get("content", ""))}
    return {"role": "assistant", "id": make_message_id(audit_id),
            "content": content if content else {"text": ""}}


def build_system_message(text: str, audit_id: int | None = None,
                         *, note_type: str | None = None) -> dict:
    content: dict = {"text": str(text)}
    if note_type:
        content["note_type"] = note_type
    return {"role": "system", "id": make_message_id(audit_id),
            "content": content}


# ── Store → LLM ──────────────────────────────────────────────────────────


def store_dict_to_agent_messages(store_dict: dict,
                                  meta: dict | None = None) -> tuple[list[dict], dict]:
    """Convert new session dict to LLM-compatible message list."""
    out: list[dict] = []
    stats = {"input_rows": len(store_dict), "included": 0, "skipped": 0}

    meta = meta or {}
    reset_anchor = str(meta.get("reset_anchor_msg_id", "") or "").strip()
    latest_summary_id = str(meta.get("latest_summary_message_id", "") or "").strip()
    summary_anchor_id = str(meta.get("summary_anchor_msg_id", "") or "").strip()

    # Gather entries in key order (filter non-dict values from corrupted data)
    entries = sorted(
        [(int(k), v) for k, v in store_dict.items() if k.isdigit() and isinstance(v, dict)],
        key=lambda x: x[0],
    )

    # Apply reset anchor
    if reset_anchor:
        reset_after = -1
        for seq, entry in entries:
            if entry.get("id", "") == reset_anchor:
                reset_after = seq
                break
        if reset_after >= 0:
            entries = [(s, e) for s, e in entries if s > reset_after]

    # Apply summary compression
    if latest_summary_id and summary_anchor_id:
        summary_msg = None
        anchor_idx = -1
        for i, (seq, entry) in enumerate(entries):
            if entry.get("id", "") == latest_summary_id:
                summary_msg = _entry_to_llm_rows(entry)
            if entry.get("id", "") == summary_anchor_id:
                anchor_idx = i
        if summary_msg and anchor_idx >= 0:
            out.extend(summary_msg)
            stats["included"] += len(summary_msg)
            for seq, entry in entries[anchor_idx:]:
                if entry.get("id", "") != latest_summary_id:
                    rows = _entry_to_llm_rows(entry)
                    if rows:
                        out.extend(rows)
                        stats["included"] += len(rows)
            return out, stats

    for seq, entry in entries:
        rows = _entry_to_llm_rows(entry)
        if rows:
            out.extend(rows)
            stats["included"] += len(rows)
        else:
            stats["skipped"] += 1
    return out, stats


def _entry_to_llm_rows(entry: dict) -> list[dict]:
    """Convert a single store entry to one or more LLM message rows.

    For assistant messages with tool_marker sub-steps, splits into
    interleaved assistant + tool messages following key order.
    """
    if not isinstance(entry, dict):
        return []
    role = str(entry.get("role", "")).strip()
    content = entry.get("content", {})

    if role == "user":
        text = ""
        if isinstance(content, dict):
            for sk in sorted((int(k2) for k2 in content if k2.isdigit()), key=int):
                v = content[str(sk)]
                if isinstance(v, dict):
                    text = str(v.get("user") or v.get("text") or "")
                    if text:
                        break
            if not text:
                text = str(content.get("user") or content.get("text") or "")
        elif isinstance(content, str):
            text = content
        if not text.strip():
            return []
        return [{"role": "user", "content": text}]

    elif role == "assistant":
        if not isinstance(content, dict):
            txt = str(content or "")
            return [{"role": "assistant", "content": txt}] if txt.strip() else []

        rows: list[dict] = []
        pending_reasoning: list[str] = []
        pending_text: list[str] = []
        pending_calls: list[dict] = []

        def _flush_asst():
            if pending_text or pending_calls or pending_reasoning:
                asst = {"role": "assistant",
                        "content": "\n\n".join(p for p in pending_text if p.strip())}
                if pending_reasoning:
                    asst["reasoning_content"] = "\n\n".join(p for p in pending_reasoning if p.strip())
                if pending_calls:
                    asst["tool_calls"] = pending_calls.copy()
                rows.append(asst)
                pending_reasoning.clear()
                pending_text.clear()
                pending_calls.clear()

        for k in sorted((int(k2) for k2 in content if k2.isdigit()), key=int):
            v = content[str(k)]
            if not isinstance(v, dict):
                continue
            if "tool_marker" in v:
                tm = v["tool_marker"]
                if not isinstance(tm, dict):
                    continue
                cid = str(tm.get("id", tm.get("call_id", "")) or "").strip()
                name = str(tm.get("name", tm.get("tool_name", "unknown")))
                stdout = str(tm.get("stdout", ""))
                stdin = str(tm.get("stdin", ""))
                # Flush pending text + calls before tool messages
                _flush_asst()
                # Build minimal tool_calls for assistant message
                pending_calls.append({
                    "id": cid or f"call_{k}",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                })
                # Emit tool result message with stdin + stdout
                content_parts = []
                if stdin.strip():
                    content_parts.append(stdin.strip())
                if stdout.strip():
                    content_parts.append(stdout.strip())
                rows.append({
                    "role": "tool",
                    "tool_call_id": cid or f"call_{k}",
                    "content": "\n".join(content_parts) or "{}",
                })
            elif "thinking" in v:
                pending_reasoning.append(str(v["thinking"]))
            elif "text" in v:
                pending_text.append(str(v["text"]))
        _flush_asst()
        return rows

    elif role == "system":
        text = ""
        if isinstance(content, dict):
            text = str(content.get("text", ""))
        elif isinstance(content, str):
            text = content
        if not text.strip():
            return []
        return [{"role": "assistant", "content": f"[Context Summary] {text}"}]

    return []


# ── Store → Frontend ─────────────────────────────────────────────────────


def store_dict_to_frontend(store_dict: dict) -> list[dict]:
    """Convert new session dict to frontend-renderable entry list."""
    entries = []
    for k in sorted((int(k2) for k2 in store_dict if k2.isdigit()), key=int):
        entry = store_dict[str(k)]
        if not isinstance(entry, dict):
            continue
        entries.append(_entry_to_frontend(entry))
    return entries


def _entry_to_frontend(entry: dict) -> dict:
    role = str(entry.get("role", "")).strip()
    content = entry.get("content", {})
    msg_id = str(entry.get("id", ""))

    if role == "user":
        text = ""
        if isinstance(content, dict):
            # Try substeps format first {"1": {"text": "..."}}
            for sk in sorted((int(k2) for k2 in content if k2.isdigit()), key=int):
                v = content[str(sk)]
                if isinstance(v, dict):
                    text = str(v.get("user") or v.get("text") or "")
                    if text:
                        break
            if not text:
                text = str(content.get("user") or content.get("text") or "")
        elif isinstance(content, str):
            text = content
        return {"role": "user", "id": msg_id, "content": text}

    elif role == "assistant":
        if isinstance(content, str):
            return {"role": "assistant", "id": msg_id, "content": content}
        sub_steps = []
        for sk in sorted((int(k2) for k2 in content if k2.isdigit()), key=int):
            v = content[str(sk)]
            if isinstance(v, dict):
                for kind, val in v.items():
                    sub_steps.append({"kind": kind, "data": val})
        return {"role": "assistant", "id": msg_id, "content": sub_steps}

    elif role == "system":
        text = ""
        if isinstance(content, dict):
            text = str(content.get("text", ""))
        elif isinstance(content, str):
            text = content
        return {"role": "system", "id": msg_id, "content": text}

    return {"role": role, "id": msg_id, "content": str(content)}


# ── Token estimation ─────────────────────────────────────────────────────


def estimate_context_tokens(store_dict: dict, meta: dict | None = None) -> int:
    """Estimate token count of messages that would go to LLM."""
    llm_rows, _ = store_dict_to_agent_messages(store_dict, meta)
    total = 0
    for row in llm_rows:
        content = str(row.get("content", "") or "")
        if content.strip():
            total += estimate_tokens(content)
    return int(total)


# ── Compression helpers ──────────────────────────────────────────────────


def filter_raw_chat_entries(store_dict: dict) -> list[dict]:
    """Return only raw user+assistant chat entries (excluding system, excluding tool_marker sub-steps)."""
    raw = []
    for k in sorted((int(k2) for k2 in store_dict if k2.isdigit()), key=int):
        entry = store_dict[str(k)]
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role", "")).strip()
        if role not in ("user", "assistant"):
            continue
        content = entry.get("content", {})
        if role == "user":
            text = ""
            if isinstance(content, dict):
                for sk in sorted((int(k2) for k2 in content if k2.isdigit()), key=int):
                    v = content[str(sk)]
                    if isinstance(v, dict):
                        text = str(v.get("user") or v.get("text") or "")
                        if text:
                            break
                if not text:
                    text = str(content.get("user") or content.get("text") or "")
            else:
                text = str(content or "")
            raw.append({"role": "user", "content": text})
        elif role == "assistant":
            if isinstance(content, dict):
                for sk in sorted((int(k2) for k2 in content if k2.isdigit()), key=int):
                    v = content[str(sk)]
                    if isinstance(v, dict) and "text" in v:
                        raw.append({"role": "assistant", "content": str(v["text"])})
            elif isinstance(content, str):
                raw.append({"role": "assistant", "content": content})
    return raw
