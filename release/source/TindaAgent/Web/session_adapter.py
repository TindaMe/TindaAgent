"""
Session adapter — new-format JSON dict to LLM / frontend / store conversion.

All format conversion logic lives here. server.py and session_store.py
only call these functions, no internal format knowledge.
"""

from __future__ import annotations

import json
import logging
import hashlib
import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from TindaAgent.Process.AI.tokenizer import estimate_tokens
from TindaAgent.Process.Observability.audit import get_audit_engine
from TindaAgent.Tool import tool as tool_registry

LOGGER = logging.getLogger("tinda.session_adapter")

_DSML_TOOL_CALLS_BLOCK_RE = re.compile(
    r"\s*<[^>]*(?:tool[_\-\u2581]?calls|toolcalls)[^>]*>.*?</[^>]*(?:tool[_\-\u2581]?calls|toolcalls)[^>]*>\s*",
    flags=re.DOTALL | re.IGNORECASE,
)
_DSML_INVOKE_BLOCK_RE = re.compile(
    r"\s*<[^>]*invoke[^>]*name\s*=\s*(['\"])(.*?)\1[^>]*>.*?</[^>]*invoke[^>]*>\s*",
    flags=re.DOTALL | re.IGNORECASE,
)
_DSML_INVOKE_RE = re.compile(
    r"<[^>]*invoke[^>]*name\s*=\s*(['\"])(.*?)\1[^>]*>(.*?)</[^>]*invoke[^>]*>",
    flags=re.DOTALL | re.IGNORECASE,
)
_DSML_PARAMETER_RE = re.compile(
    r"<[^>]*parameter[^>]*name\s*=\s*(['\"])(.*?)\1[^>]*>(.*?)</[^>]*parameter[^>]*>",
    flags=re.DOTALL | re.IGNORECASE,
)
_DSML_TOOL_CALLS_TAG_RE = re.compile(
    r"\s*</?[^>]*(?:tool[_\-\u2581]?calls|toolcalls)[^>]*>\s*",
    flags=re.IGNORECASE,
)
_TOOL_PROTOCOL_START_RE = re.compile(
    r"<[^>\n]{0,240}(?:dsml|tool[_\-\u2581]?calls|toolcalls|invoke\b)[^>]*>",
    flags=re.IGNORECASE,
)


def _has_tool_protocol_marker(content: str) -> bool:
    lower = str(content or "").lower()
    return (
        "dsml" in lower
        or "tool_calls" in lower
        or "tool-calls" in lower
        or "toolcalls" in lower
        or "tool▁calls" in lower
    )


def has_tool_protocol_artifacts(content: str) -> bool:
    text = str(content or "")
    if not text or not _has_tool_protocol_marker(text):
        return False
    return bool(
        _DSML_TOOL_CALLS_BLOCK_RE.search(text)
        or _DSML_INVOKE_BLOCK_RE.search(text)
        or _TOOL_PROTOCOL_START_RE.search(text)
        or _find_tool_protocol_start(text) >= 0
    )


def _find_tool_protocol_start(content: str) -> int:
    text = str(content or "")
    if not text:
        return -1
    match = _TOOL_PROTOCOL_START_RE.search(text)
    if match:
        return match.start()
    lower = text.lower()
    for marker in ("dsml", "tool_calls", "tool-calls", "toolcalls", "tool▁calls", "invoke"):
        idx = lower.find(marker)
        if idx < 0:
            continue
        tag_start = text.rfind("<", 0, idx + 1)
        if tag_start >= 0 and idx - tag_start <= 240:
            return tag_start
    return -1


def strip_tool_protocol_artifacts(content: str) -> str:
    text = str(content or "")
    if not text or not _has_tool_protocol_marker(text):
        return text
    cleaned = _DSML_TOOL_CALLS_BLOCK_RE.sub("\n", text)
    cleaned = _DSML_INVOKE_BLOCK_RE.sub("\n", cleaned)
    cleaned = _DSML_TOOL_CALLS_TAG_RE.sub("\n", cleaned)
    start = _find_tool_protocol_start(cleaned)
    if start >= 0:
        cleaned = cleaned[:start]
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _stable_dsml_call_id(name: str, arguments: dict[str, Any], *, salt: str) -> str:
    payload = json.dumps({"name": name, "arguments": arguments, "salt": salt}, ensure_ascii=False, sort_keys=True)
    return "hist_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _extract_dsml_tool_markers(content: str, *, id_prefix: str = "dsml") -> list[dict[str, Any]]:
    text = str(content or "")
    if not text or not _has_tool_protocol_marker(text):
        return []
    markers: list[dict[str, Any]] = []
    for idx, (_quote, raw_name, body) in enumerate(_DSML_INVOKE_RE.findall(text)):
        name = html.unescape(str(raw_name or "").strip())
        if not name:
            continue
        arguments: dict[str, Any] = {}
        for _p_quote, raw_param_name, raw_value in _DSML_PARAMETER_RE.findall(body):
            param_name = html.unescape(str(raw_param_name or "").strip())
            if not param_name:
                continue
            arguments[param_name] = html.unescape(str(raw_value or "").strip())
        call_id = _stable_dsml_call_id(name, arguments, salt=f"{id_prefix}:{idx}")
        stdin = str(arguments.get("cmd") or arguments.get("text") or arguments.get("key") or "")[:500]
        result = {
            "ok": False,
            "tool_name": name,
            "call_id": call_id,
            "error": "historical tool-call protocol text was persisted and was not executed",
            "source": "tool_protocol_fallback",
        }
        markers.append({
            "name": name,
            "ok": False,
            "stdin": stdin,
            "stdout": "历史工具调用协议文本未执行",
            "id": call_id,
            "arguments": arguments,
            "result": result,
        })
    return markers


def _normalize_tool_marker(raw: dict[str, Any]) -> dict[str, Any]:
    marker = raw.get("tool_marker") if isinstance(raw.get("tool_marker"), dict) else raw
    arguments = marker.get("arguments", marker.get("args", {}))
    if not isinstance(arguments, (dict, list, str, int, float, bool)) and arguments is not None:
        arguments = str(arguments)
    result = marker.get("result")
    if not isinstance(result, (dict, list, str, int, float, bool)) and result is not None:
        result = str(result)
    marker_id = str(marker.get("id", marker.get("call_id", "")))
    if marker_id.startswith("dsml_"):
        marker_id = "hist_" + marker_id.removeprefix("dsml_")
    stdout = str(marker.get("stdout", ""))[:500]
    if "DSML" in stdout:
        stdout = stdout.replace("历史 DSML 工具调用未执行", "历史工具调用协议文本未执行")
        stdout = stdout.replace("DSML", "工具调用协议")
    if isinstance(result, dict):
        result = dict(result)
        call_id = str(result.get("call_id", "") or "")
        if call_id.startswith("dsml_"):
            result["call_id"] = "hist_" + call_id.removeprefix("dsml_")
        if str(result.get("source", "") or "") == "dsml_fallback":
            result["source"] = "tool_protocol_fallback"
        error = str(result.get("error", "") or "")
        if "DSML" in error:
            result["error"] = error.replace(
                "historical DSML tool call was persisted as text and was not executed",
                "historical tool-call protocol text was persisted and was not executed",
            ).replace("DSML", "tool-call protocol")
    out = {
        "name": str(marker.get("name", marker.get("tool_name", "unknown"))),
        "ok": bool(marker.get("ok", False)),
        "stdin": str(marker.get("stdin", ""))[:500],
        "stdout": stdout,
        "id": marker_id,
    }
    tool_call_id = str(marker.get("tool_call_id", "") or "").strip()
    if tool_call_id:
        out["tool_call_id"] = tool_call_id
    status = str(marker.get("status", "") or "").strip()
    if status:
        out["status"] = status
    if arguments not in (None, "", {}, []):
        out["arguments"] = arguments
    if result not in (None, "", {}, []):
        out["result"] = result
    return out


def _storage_steps_from_text(text: str, *, id_prefix: str) -> list[dict[str, Any]]:
    raw = str(text or "")
    if not raw:
        return []
    if not has_tool_protocol_artifacts(raw):
        return [{"text": raw}]

    steps: list[dict[str, Any]] = []
    matches = list(_DSML_TOOL_CALLS_BLOCK_RE.finditer(raw))
    if not matches:
        matches = list(_DSML_INVOKE_BLOCK_RE.finditer(raw))

    pos = 0
    for idx, match in enumerate(matches):
        before = strip_tool_protocol_artifacts(raw[pos:match.start()])
        if before:
            steps.append({"text": before})
        for marker in _extract_dsml_tool_markers(match.group(0), id_prefix=f"{id_prefix}:{idx}"):
            steps.append({"tool_marker": marker})
        pos = match.end()

    after = strip_tool_protocol_artifacts(raw[pos:])
    if after:
        steps.append({"text": after})
    if not steps:
        clean = strip_tool_protocol_artifacts(raw)
        if clean:
            steps.append({"text": clean})
    return steps


def _storage_steps_from_substep(raw: dict[str, Any], *, id_prefix: str) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []

    kind = str(raw.get("kind", "") or "").strip()
    if kind == "thinking":
        return [{"thinking": str(raw.get("content", ""))}]
    if kind == "text":
        return _storage_steps_from_text(str(raw.get("content", "")), id_prefix=id_prefix)
    if kind == "tool_marker":
        return [{"tool_marker": _normalize_tool_marker(raw)}]

    if "thinking" in raw:
        return [{"thinking": str(raw.get("thinking", ""))}]
    if "text" in raw:
        return _storage_steps_from_text(str(raw.get("text", "")), id_prefix=id_prefix)
    if "tool_marker" in raw and isinstance(raw.get("tool_marker"), dict):
        return [{"tool_marker": _normalize_tool_marker(raw)}]
    return [dict(raw)]


def normalize_store_entry(entry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Normalize stored message content without changing the top-level message order."""
    if not isinstance(entry, dict):
        return entry, False
    role = str(entry.get("role", "") or "").strip()
    if role != "assistant":
        return entry, False

    msg_id = str(entry.get("id", "") or "assistant")
    content = entry.get("content", {})
    new_entry = dict(entry)
    new_content: dict[str, Any] = {}

    if isinstance(content, str):
        for idx, step in enumerate(_storage_steps_from_text(content, id_prefix=f"{msg_id}:content")):
            new_content[str(idx + 1)] = step
        new_entry["content"] = new_content if new_content else {"1": {"text": ""}}
        return new_entry, new_entry != entry

    if not isinstance(content, dict):
        new_entry["content"] = {"1": {"text": str(content or "")}}
        return new_entry, new_entry != entry

    numeric_keys = sorted((int(k) for k in content if isinstance(k, str) and k.isdigit()), key=int)
    if not numeric_keys:
        if "text" in content:
            steps = _storage_steps_from_text(str(content.get("text", "")), id_prefix=f"{msg_id}:text")
        else:
            steps = [dict(content)] if content else []
        for idx, step in enumerate(steps):
            new_content[str(idx + 1)] = step
        new_entry["content"] = new_content if new_content else {"1": {"text": ""}}
        return new_entry, new_entry != entry

    out_idx = 0
    for key in numeric_keys:
        raw_step = content.get(str(key))
        if not isinstance(raw_step, dict):
            continue
        for step in _storage_steps_from_substep(raw_step, id_prefix=f"{msg_id}:{key}"):
            out_idx += 1
            new_content[str(out_idx)] = step
    new_entry["content"] = new_content if new_content else {"1": {"text": ""}}
    return new_entry, new_entry != entry


def normalize_store_dict(store_dict: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if not isinstance(store_dict, dict):
        return {}, False
    normalized: dict[str, Any] = {}
    changed = False
    for key, value in store_dict.items():
        if isinstance(value, dict):
            new_value, entry_changed = normalize_store_entry(value)
            normalized[key] = new_value
            changed = changed or entry_changed
        else:
            normalized[key] = value
    return normalized, changed

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
                       file_names: list[str] | None = None,
                       file_contents: list[str] | None = None,
                       audit_id: int | None = None) -> dict:
    content: dict[str, dict] = {}
    n = 0
    if file_names:
        for fn, fc in zip(file_names, file_contents or []):
            n += 1
            content[str(n)] = {"file": {"file_name": fn, "file_content": fc or ""}}
    if text.strip():
        n += 1
        content[str(n)] = {"user": text} if raw else {"text": text}
    return {"role": "user", "id": make_message_id(audit_id), "content": content if content else {"text": ""}}


def build_assistant_message(substeps: list[dict],
                            audit_id: int | None = None) -> dict:
    """substeps: [{"kind": "thinking", "content": "..."},
                  {"kind": "tool_marker", "name": "...", "ok": True, ...},
                  {"kind": "text", "content": "..."}]"""
    content: dict[str, dict] = {}
    n = 0
    for idx, s in enumerate(substeps):
        for step in _storage_steps_from_substep(s, id_prefix=f"build:{audit_id or 'new'}:{idx}"):
            n += 1
            content[str(n)] = step
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
    store_dict, _changed = normalize_store_dict(store_dict)
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
    entry, _changed = normalize_store_entry(entry)
    role = str(entry.get("role", "")).strip()
    content = entry.get("content", {})

    if role == "user":
        file_blocks = []
        text_parts = []
        if isinstance(content, dict):
            for sk in sorted((int(k2) for k2 in content if k2.isdigit()), key=int):
                v = content[str(sk)]
                if isinstance(v, dict):
                    if "file" in v:
                        f = v["file"]
                        if isinstance(f, dict) and f.get("file_name"):
                            file_blocks.append(f"[文件: {f['file_name']}]\n```\n{f.get('file_content', '')}\n```")
                    elif "text" in v or "user" in v:
                        text_parts.append(str(v.get("text", v.get("user", ""))))
            if not text_parts and not file_blocks:
                text = str(content.get("user") or content.get("text") or "")
                if text.strip():
                    text_parts = [text]
        elif isinstance(content, str):
            text_parts = [content] if content.strip() else []
        text = "\n".join(file_blocks + text_parts)
        if not text.strip():
            return []
        return [{"role": "user", "content": text.strip()}]

    elif role == "assistant":
        if not isinstance(content, dict):
            txt = str(content or "")
            return [{"role": "assistant", "content": txt}] if txt.strip() else []

        rows: list[dict] = []
        pending_reasoning: list[str] = []
        pending_text: list[str] = []
        pending_calls: list[dict] = []
        pending_tool_rows: list[dict] = []

        def _flush_asst():
            if pending_text or pending_calls or pending_reasoning:
                asst = {"role": "assistant",
                        "content": "\n\n".join(p for p in pending_text if p.strip()),
                        "reasoning_content": "\n\n".join(p for p in pending_reasoning if p.strip())}
                if pending_calls:
                    asst["tool_calls"] = pending_calls.copy()
                rows.append(asst)
                if pending_calls and pending_tool_rows:
                    rows.extend(pending_tool_rows)
                pending_reasoning.clear()
                pending_text.clear()
                pending_calls.clear()
                pending_tool_rows.clear()

        def _tool_result_content(tm: dict, stdout: str, stdin: str) -> str:
            result_payload = tm.get("result")
            if result_payload not in (None, "", {}, []):
                try:
                    return json.dumps(result_payload, ensure_ascii=False)
                except TypeError:
                    return str(result_payload)
            payload: dict[str, Any] = {
                "ok": bool(tm.get("ok", False)),
                "tool_name": str(tm.get("name", tm.get("tool_name", "unknown"))),
            }
            if stdin.strip():
                payload["stdin"] = stdin.strip()
            if stdout.strip():
                payload["stdout"] = stdout.strip()
            return json.dumps(payload, ensure_ascii=False)

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
                if tool_registry.find_tool(name) is None:
                    clean_text = str(tm.get("stdout", "") or "").strip()
                    pending_text.append(f"[工具记录已忽略: 未注册工具 {name}]" + (f"\n{clean_text}" if clean_text else ""))
                    continue
                stdout = str(tm.get("stdout", ""))
                stdin = str(tm.get("stdin", ""))
                # Flush pending text + calls before tool messages
                _flush_asst()
                arguments = tm.get("arguments")
                if isinstance(arguments, str):
                    arguments_text = arguments if arguments.strip() else "{}"
                elif arguments not in (None, "", {}, []):
                    try:
                        arguments_text = json.dumps(arguments, ensure_ascii=False)
                    except TypeError:
                        arguments_text = json.dumps({"value": str(arguments)}, ensure_ascii=False)
                elif stdin.strip():
                    arguments_text = json.dumps({"cmd": stdin.strip()}, ensure_ascii=False)
                else:
                    arguments_text = "{}"
                pending_calls.append({
                    "id": cid or f"call_{k}",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments_text},
                })
                pending_tool_rows.append({
                    "role": "tool",
                    "tool_call_id": cid or f"call_{k}",
                    "content": _tool_result_content(tm, stdout, stdin),
                })
                _flush_asst()
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
    store_dict, _changed = normalize_store_dict(store_dict)
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
        if isinstance(content, dict):
            sub_steps = []
            for sk in sorted((int(k2) for k2 in content if k2.isdigit()), key=int):
                v = content[str(sk)]
                if isinstance(v, dict):
                    for kind, val in v.items():
                        sub_steps.append({"kind": kind, "data": val})
            if sub_steps:
                return {"role": "user", "id": msg_id, "content": sub_steps}
            # Fallback: flat format
            text = str(content.get("user") or content.get("text") or "")
            return {"role": "user", "id": msg_id, "content": text}
        elif isinstance(content, str):
            return {"role": "user", "id": msg_id, "content": content}
        return {"role": "user", "id": msg_id, "content": ""}

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


def effective_store_dict(store_dict: dict, meta: dict | None = None) -> dict[str, Any]:
    """Return frontend/export-visible entries after reset and compression anchors."""
    store_dict, _changed = normalize_store_dict(store_dict)
    meta = meta or {}
    reset_anchor = str(meta.get("reset_anchor_msg_id", "") or "").strip()
    latest_summary_id = str(meta.get("latest_summary_message_id", "") or "").strip()
    summary_anchor_id = str(meta.get("summary_anchor_msg_id", "") or "").strip()

    entries = sorted(
        [(int(k), v) for k, v in store_dict.items() if k.isdigit() and isinstance(v, dict)],
        key=lambda x: x[0],
    )

    if reset_anchor:
        reset_after = -1
        for seq, entry in entries:
            if str(entry.get("id", "") or "") == reset_anchor:
                reset_after = seq
                break
        if reset_after >= 0:
            entries = [(seq, entry) for seq, entry in entries if seq > reset_after]

    if latest_summary_id and summary_anchor_id:
        summary_entry = None
        anchor_seq = -1
        for seq, entry in entries:
            msg_id = str(entry.get("id", "") or "")
            if msg_id == latest_summary_id:
                summary_entry = entry
            if msg_id == summary_anchor_id:
                anchor_seq = seq
        if summary_entry is not None and anchor_seq >= 0:
            visible = [summary_entry]
            visible.extend(
                entry for seq, entry in entries
                if seq >= anchor_seq and str(entry.get("id", "") or "") != latest_summary_id
            )
            return {str(i + 1): entry for i, entry in enumerate(visible)}

    return {str(i + 1): entry for i, (_seq, entry) in enumerate(entries)}


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
    store_dict, _changed = normalize_store_dict(store_dict)
    raw = []
    for k in sorted((int(k2) for k2 in store_dict if k2.isdigit()), key=int):
        entry = store_dict[str(k)]
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role", "")).strip()
        if role not in ("user", "assistant"):
            continue
        content = entry.get("content", {})
        msg_id = str(entry.get("id", "") or "")
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
            if text.strip():
                raw.append({"role": "user", "content": text, "id": msg_id, "seq": int(k)})
        elif role == "assistant":
            parts: list[str] = []
            if isinstance(content, dict):
                for sk in sorted((int(k2) for k2 in content if k2.isdigit()), key=int):
                    v = content[str(sk)]
                    if isinstance(v, dict) and "text" in v:
                        parts.append(str(v["text"]))
            elif isinstance(content, str):
                parts.append(content)
            text = "\n\n".join(p for p in parts if p.strip())
            if text.strip():
                raw.append({"role": "assistant", "content": text, "id": msg_id, "seq": int(k)})
    return raw
