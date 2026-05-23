"""Helpers for building compact, cache-friendly LLM context.

Storage and frontend rendering keep the rich Markdown/tool payloads.  These
helpers only normalize the wire-format text sent back to the model.
"""

from __future__ import annotations

import json
import re
from typing import Any

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FENCED_CODE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", flags=re.DOTALL)
_MD_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_OUTPUT_KEYS = {"stdout", "stderr", "output", "logs", "traceback"}
_DEFAULT_TEXT_LIMIT = 24000
_DEFAULT_OUTPUT_LIMIT = 12000


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", str(text or ""))


def _truncate_text(text: str, limit: int) -> str:
    raw = str(text or "")
    max_len = max(0, int(limit or 0))
    if max_len <= 0 or len(raw) <= max_len:
        return raw
    omitted = len(raw) - max_len
    return f"{raw[:max_len]}\n[truncated {omitted} chars]"


def compact_markdown_for_llm(text: str | None, *, limit: int | None = None) -> str:
    """Return a deterministic plain-text view of Markdown-heavy content.

    The goal is not semantic summarization.  It removes display-only Markdown
    scaffolding while preserving the actual words, code and URLs.
    """
    raw = _strip_ansi(str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return ""

    def code_repl(match: re.Match[str]) -> str:
        lang = str(match.group(1) or "").strip()
        body = str(match.group(2) or "").strip("\n")
        header = f"[code:{lang}]" if lang else "[code]"
        return f"{header}\n{body}\n[/code]"

    raw = _FENCED_CODE_RE.sub(code_repl, raw)

    lines: list[str] = []
    for line in raw.split("\n"):
        item = line.rstrip()
        if _MD_TABLE_SEPARATOR_RE.match(item):
            continue
        item = re.sub(r"^\s{0,3}#{1,6}\s*", "", item)
        item = re.sub(r"^\s{0,3}>\s?", "", item)
        item = re.sub(r"^\s*[-*_]{3,}\s*$", "", item)
        item = re.sub(r"^\s*([*+-])\s+", "- ", item)
        item = re.sub(r"^\s*(\d+)[.)]\s+", r"\1. ", item)
        table_match = _MD_TABLE_ROW_RE.match(item)
        if table_match:
            cells = [c.strip() for c in table_match.group(1).split("|")]
            item = " | ".join(c for c in cells if c)
        lines.append(item)

    raw = "\n".join(lines)
    raw = _MD_LINK_RE.sub(lambda m: f"{m.group(1).strip()} <{m.group(2).strip()}>" if m.group(1).strip() else m.group(2).strip(), raw)
    raw = re.sub(r"(\*\*|__|~~)", "", raw)
    raw = raw.replace("`", "")
    raw = re.sub(r"[ \t]+\n", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = raw.strip()
    return _truncate_text(raw, limit if limit is not None else _DEFAULT_TEXT_LIMIT)


def compact_terminal_context_for_llm(text: str | None) -> str:
    body = compact_markdown_for_llm(text, limit=_DEFAULT_OUTPUT_LIMIT)
    if not body:
        return ""
    return body


def _json_loads_maybe(raw: Any) -> Any:
    if isinstance(raw, (dict, list, int, float, bool)) or raw is None:
        return raw
    try:
        return json.loads(str(raw or ""))
    except Exception:
        return None


def _compact_json_value(value: Any, *, key: str = "", output_limit: int = _DEFAULT_OUTPUT_LIMIT) -> Any:
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for k in sorted(value):
            v = _compact_json_value(value.get(k), key=str(k), output_limit=output_limit)
            if v in ("", None, [], {}):
                continue
            compacted[str(k)] = v
        if compacted.get("output") == compacted.get("stdout"):
            compacted.pop("output", None)
        if compacted.get("success") == compacted.get("ok"):
            compacted.pop("success", None)
        return compacted
    if isinstance(value, list):
        return [_compact_json_value(v, key=key, output_limit=output_limit) for v in value]
    if isinstance(value, str):
        text = _strip_ansi(value).strip()
        if key in _OUTPUT_KEYS:
            return _truncate_text(text, output_limit)
        return _truncate_text(text, _DEFAULT_TEXT_LIMIT)
    return value


def compact_tool_result_for_llm(content: Any, *, tool_name: str = "", output_limit: int = _DEFAULT_OUTPUT_LIMIT) -> str:
    """Compact tool result content for the next model turn.

    Keeps structured facts, removes duplicate stdout/output fields, stable-sorts
    keys, strips ANSI/display Markdown, and caps huge output strings.
    """
    parsed = _json_loads_maybe(content)
    if parsed is None:
        return compact_markdown_for_llm(str(content or ""), limit=output_limit)

    compacted = _compact_json_value(parsed, output_limit=output_limit)
    if isinstance(compacted, dict) and tool_name and "tool_name" not in compacted:
        compacted["tool_name"] = str(tool_name)
    try:
        return json.dumps(compacted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return compact_markdown_for_llm(str(compacted), limit=output_limit)


def compact_message_for_llm(message: dict[str, Any]) -> dict[str, Any]:
    """Return a compact copy of one OpenAI-shaped chat message."""
    out = dict(message)
    role = str(out.get("role", "") or "")
    content = out.get("content")
    if isinstance(content, str):
        if role == "tool":
            out["content"] = compact_tool_result_for_llm(content, tool_name=str(out.get("name", "") or ""))
        elif role == "assistant":
            out["content"] = compact_markdown_for_llm(content)
        elif role == "system":
            out["content"] = _truncate_text(_strip_ansi(content).strip(), _DEFAULT_TEXT_LIMIT)
        else:
            out["content"] = content.strip()
    return out


def compact_messages_for_llm(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [compact_message_for_llm(m) if isinstance(m, dict) else m for m in (messages or [])]
