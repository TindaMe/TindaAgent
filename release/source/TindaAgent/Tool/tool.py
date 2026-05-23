import functools
import contextlib
import io
import json
import os
import re
import subprocess
import shutil
import gzip
import ast
import signal
import threading
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Callable
from TindaAgent.Process.Architecture import perm
from TindaAgent.Process.Architecture.paths import get_memory_file
from TindaAgent.Process.Observability import audit_event
from TindaAgent.Process.Observability.audit import redact_sensitive_text
from TindaAgent.Process.Architecture.paths import get_log_root, get_legacy_log_root
from TindaAgent.Process.Security import terminal_policy
from TindaAgent.Tool.editing import apply_text_edit, read_text_file, search_text_files
from TindaAgent.Tool.web_search import search_web as perform_web_search
from TindaAgent.Tool.mcp_client import (
    cancel_mcp_call,
    call_mcp_tool,
    list_mcp_servers as list_configured_mcp_servers,
    list_mcp_tools as list_remote_mcp_tools,
    upsert_mcp_server,
)
from TindaAgent.Tool.skills import discover_skills, read_skill
from TindaAgent.Permission import (
    has_perm,
    validate_registered_tool_perm,
    build_permission_denied_payload,
    PermissionDeniedError,
)

# 系统工具注册表 {func_name: {"des": str, "perm": int, "func": function}}
SYSTEM_TOOL: dict[str, dict[str, Any]] = {}

# 备用工具注册表 {func_name: {"des": str, "perm": int, "func": function}}
SPARE_TOOL: dict[str, dict[str, Any]] = {}
_TOOL_SCHEMA_CACHE: dict[int, list[dict[str, Any]]] = {}
_MCP_TOOL_SCHEMA_CACHE: dict[int, list[dict[str, Any]]] = {}
_MCP_TOOL_ALIAS_CACHE: dict[int, dict[str, dict[str, Any]]] = {}
_RUNNING_TOOL_CONTEXTS: dict[str, dict[str, Any]] = {}
_RUNNING_TOOL_LOCK = threading.RLock()

MAX_TEXT_LEN = 8000
DEFAULT_TIMEZONE = "Asia/Shanghai"
MEMORY_MAX_ITEMS = 500
MEMORY_MAX_DATA_LEN = 2000
ASK_USER_NONE_OF_THEM_VALUE = "__none_of_them__"
ASK_USER_NONE_OF_THEM_LABEL = "以上都不是，我自己补充"
PLAN_SCHEMA_VERSION = 2
PLAN_ACTION_VALUES = {"create", "update", "set_step_status", "block", "clear"}
PLAN_STATUS_VALUES = {"planned", "revised", "blocked", "complete"}
PLAN_STEP_STATUS_VALUES = {"pending", "in_progress", "done", "blocked"}
PLAN_HIDDEN_PARAMETER_NAMES = {"requires_completion_confirmation", "completion_confirmation_state"}
ASK_USER_QUESTION_TOOL_DESCRIPTION = (
    "Ask the human user exactly one concise clarification question and pause the current workflow until the user answers. "
    "HARD RULES: use this tool only when a missing requirement, unsafe ambiguity, required choice, permission-sensitive decision, or user preference blocks correct execution; do not use it for small details you can safely infer; do not ask multiple questions in one call; do not continue the task, call other task tools, fabricate an answer, or simulate the user's answer until the tool result is returned; after the tool result arrives, treat its answer/choice as authoritative user input for the current request. "
    "Arguments: question is required and must be user-facing; options is an optional array of mutually exclusive choice strings. Put each choice in its own array item; never pack A/B/C choices into one string and never use newline/semicolon/pipe-separated option text. allow_custom_answer controls whether the user may type a free-form answer; placeholder is a short input hint."
)
ASK_USER_QUESTION_PARAMETER_DESCRIPTIONS = {
    "question": (
        "Required. One concise, user-facing clarification question. Ask only one thing. "
        "Do not include hidden reasoning, implementation details, or multiple subquestions."
    ),
    "options": (
        "Optional array of mutually exclusive answer choices. Use only when choices are clear. "
        "Put exactly one clean choice in each array item; do not combine multiple choices in one item and do not use newline/semicolon/pipe-separated option text. "
        "The system will add a 'none of them / custom answer' option automatically."
    ),
    "allow_custom_answer": (
        "Optional string boolean, default true. Keep true when the user may need to provide details not covered by options; use false only for strict closed choices."
    ),
    "placeholder": (
        "Optional. Short placeholder text for the custom answer input, describing what useful information the user should provide."
    ),
}
PLAN_PARAMETER_DESCRIPTIONS = {
    "action": (
        "Plan API action for this call: create, update, set_step_status, block, or clear. "
        "Use create/update for normal planning, "
        "set_step_status to update existing step progress without rewriting the plan, "
        "block when planning is blocked, and clear to remove the visible plan."
    ),
    "goal": "Plan objective. Keep it concise and user-facing.",
    "steps": (
        "Array of plan step objects. Put each step in its own item, for example "
        "[{\"text\":\"Inspect current implementation\",\"status\":\"pending\"},{\"text\":\"Patch API\",\"status\":\"in_progress\"}]. "
        "Put progress only in the status field; keep text clean without numbering, emoji, checkmarks, or phrases such as 'done/in progress'. "
        "Do not pass newline/semicolon-separated steps. If a step's status changes after the plan exists, do not rewrite steps; call action=set_step_status."
    ),
    "status": (
        "Plan lifecycle status: planned, revised, blocked, or complete. "
        "Do not express lifecycle status with emoji or free-form labels."
    ),
    "notes": "Optional assumptions, risks, or constraints for the plan.",
    "completed": "Boolean. Set true only when the plan is actually complete.",
    "completion_note": "Optional short note explaining what was completed.",
    "step_index": "For action=set_step_status. 1-based index of the existing plan step to update.",
    "step_text": "For action=set_step_status. Optional exact/short step text used as a fallback when index is unavailable.",
    "step_status": "For action=set_step_status. New step status: pending, in_progress, done, or blocked.",
    "step_updates": (
        "For action=set_step_status. Optional array of step status updates. "
        "Each item must contain enum status and either index or text. Do not rewrite the full plan to mark progress."
    ),
    "update_note": "For action=set_step_status. Optional short note explaining why the step status changed.",
}
TOOL_PARAMETER_SCHEMA_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    ("ask_user_question", "options"): {
        "type": "array",
        "items": {"type": "string"},
        "description": ASK_USER_QUESTION_PARAMETER_DESCRIPTIONS["options"],
    },
    ("plan", "action"): {
        "type": "string",
        "enum": ["create", "update", "set_step_status", "block", "clear"],
        "description": PLAN_PARAMETER_DESCRIPTIONS["action"],
    },
    ("plan", "status"): {
        "type": "string",
        "enum": ["planned", "revised", "blocked", "complete"],
        "description": PLAN_PARAMETER_DESCRIPTIONS["status"],
    },
    ("plan", "completed"): {
        "type": "boolean",
        "description": PLAN_PARAMETER_DESCRIPTIONS["completed"],
    },
    ("plan", "steps"): {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "One concise user-facing plan step. Do not include numbering, emoji, checkmarks, or progress labels; use status for initial state and set_step_status for later progress.",
                },
                "status": {
                    "type": "string",
                    "description": "Optional step status: pending, in_progress, done, or blocked.",
                    "enum": ["pending", "in_progress", "done", "blocked"],
                },
            },
            "required": ["text", "status"],
        },
        "description": PLAN_PARAMETER_DESCRIPTIONS["steps"],
    },
    ("plan", "step_index"): {
        "type": "integer",
        "description": PLAN_PARAMETER_DESCRIPTIONS["step_index"],
    },
    ("plan", "step_text"): {
        "type": "string",
        "description": PLAN_PARAMETER_DESCRIPTIONS["step_text"],
    },
    ("plan", "step_status"): {
        "type": "string",
        "enum": ["pending", "in_progress", "done", "blocked"],
        "description": PLAN_PARAMETER_DESCRIPTIONS["step_status"],
    },
    ("plan", "step_updates"): {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": "1-based step index to update.",
                },
                "text": {
                    "type": "string",
                    "description": "Optional exact/short step text fallback for matching.",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "blocked"],
                    "description": "New structured step status. This is the only supported way to mark existing plan steps complete/in progress/blocked.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional short status-change note.",
                },
            },
            "required": ["status"],
        },
        "description": PLAN_PARAMETER_DESCRIPTIONS["step_updates"],
    },
    ("plan", "update_note"): {
        "type": "string",
        "description": PLAN_PARAMETER_DESCRIPTIONS["update_note"],
    },
}
_MEMORY_FILE = get_memory_file()
PROFILE_SNIPPETS = {
    "bio": "我是Tinda，来自中国的一名开发者。自2025.8.23学习计算机相关知识。",
    "project": "当前项目：TindaAgent",
    "contact": "联系方式：3431955251@qq.com（或搜索qq号，备注来意）",
    "slogan": "Tinda · Touch into new dimensions anytime",
}
STOPWORDS = {
    "the", "and", "for", "you", "that", "with", "this", "from", "have", "your",
    "what", "when", "where", "which", "will", "would", "there", "about", "into",
    "一个", "一些", "这个", "那个", "我们", "你们", "他们", "以及", "或者", "可以", "需要",
    "进行", "如果", "为了", "然后", "已经", "现在", "就是", "还是", "因为", "所以",
}
_THIS_FILE = str(Path(__file__).resolve())

def _normalize_text(raw_text: str, max_len: int = MAX_TEXT_LEN) -> str:
    text = str(raw_text or "").strip()
    if len(text) > max_len:
        return text[:max_len]
    return text


def _parse_int(raw_value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(raw_value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _parse_boolish(raw_value: Any, default: bool = False) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    if raw_value is None:
        return bool(default)
    text = str(raw_value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "complete", "completed"}:
        return True
    if text in {"0", "false", "no", "n", "off", "none", "null", ""}:
        return False
    return bool(default)


def _split_sentences(text: str) -> list[str]:
    parts = [x.strip() for x in re.split(r"[。！？!?]\s*|\n+", text) if x.strip()]
    if len(parts) <= 1:
        parts = [x.strip() for x in re.split(r"[；;，,]\s*", text) if x.strip()]
    return parts


def _split_options(raw_options: Any) -> list[str]:
    if isinstance(raw_options, list):
        values = raw_options
    else:
        text = str(raw_options or "")
        text = re.sub(r"^\s*[（(][^）)]{0,40}(?:选项|option|choices?)[^）)]{0,40}[）)]\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^\s*(?:以下|下面|这些|these|the following)[^。\n:：]{0,40}(?:选项|options?|choices?)[：:]\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"(?<![\w])([A-Ha-h])\s*([.．、)])\s*", r"\n\1\2 ", text)
        values = re.split(r"[\n|；;]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            item = str(value.get("label") or value.get("text") or value.get("content") or value.get("value") or "").strip()
        else:
            item = str(value or "").strip()
        if not item:
            continue
        if item == ASK_USER_NONE_OF_THEM_VALUE:
            continue
        if len(item) > 120:
            item = item[:120]
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= 8:
            break
    if out:
        out.append(ASK_USER_NONE_OF_THEM_VALUE)
    return out


def _parse_plan_step_sequence(raw_steps: Any) -> tuple[list[Any], bool]:
    if isinstance(raw_steps, list):
        return raw_steps, True
    if isinstance(raw_steps, tuple):
        return list(raw_steps), True
    text = str(raw_steps or "")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed, True
    except Exception:
        pass
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, (list, tuple)):
            return list(parsed), True
    except Exception:
        pass
    return re.split(r"[\n；;]+", text), False


def _coerce_plan_step_values(raw_steps: Any) -> list[Any]:
    values, _ = _parse_plan_step_sequence(raw_steps)
    return values


def _tool_param_expects_text(param: Any) -> bool:
    try:
        import inspect as _inspect
        if param is None or param.annotation is _inspect.Parameter.empty:
            return True
        return param.annotation is str
    except Exception:
        return True


def _normalize_plan_steps(raw_steps: Any, *, infer_text_status: bool = False) -> list[dict[str, Any]]:
    values = _coerce_plan_step_values(raw_steps)
    out: list[dict[str, Any]] = []
    for value in values:
        status = "pending"
        if isinstance(value, dict):
            raw_text = _normalize_text(
                value.get("text") or value.get("content") or value.get("title") or value.get("label") or "",
                800,
            )
            raw_status = str(value.get("status", "") or "").strip().lower()
            if raw_status in PLAN_STEP_STATUS_VALUES:
                status = raw_status
        else:
            raw_text = _normalize_text(value, 800)
        inferred_status = _infer_plan_step_status(raw_text) if infer_text_status else ""
        if infer_text_status and inferred_status and status == "pending":
            status = inferred_status
        text = _clean_plan_step_text(raw_text) if infer_text_status else _clean_plan_step_prefix(raw_text)
        if not text:
            continue
        out.append({"index": len(out) + 1, "text": text, "status": status})
        if len(out) >= 12:
            break
    return out


def _normalize_step_status(raw_status: Any) -> str:
    text = str(raw_status or "").strip().lower()
    aliases = {
        "doing": "in_progress",
        "working": "in_progress",
        "progress": "in_progress",
        "complete": "done",
        "completed": "done",
        "finish": "done",
        "finished": "done",
        "blocked": "blocked",
        "block": "blocked",
        "todo": "pending",
    }
    text = aliases.get(text, text)
    return text if text in PLAN_STEP_STATUS_VALUES else "pending"


def _normalize_plan_step_updates(
    step_updates: Any = None,
    *,
    step_index: Any = 0,
    step_text: Any = "",
    step_status: Any = "",
    update_note: Any = "",
) -> list[dict[str, Any]]:
    values: list[Any]
    if isinstance(step_updates, list):
        values = step_updates
    elif isinstance(step_updates, dict):
        values = [step_updates]
    else:
        values = []
    if step_index or step_text or step_status:
        values.append({
            "index": step_index,
            "text": step_text,
            "status": step_status,
            "note": update_note,
        })
    out: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, dict):
            continue
        status = _normalize_step_status(raw.get("status") or raw.get("step_status"))
        idx = _parse_int(str(raw.get("index") or raw.get("step_index") or 0), 0, 0, 1000)
        text = _clean_plan_step_text(raw.get("text") or raw.get("step_text") or "")
        note = _normalize_text(raw.get("note") or raw.get("update_note") or "", 500)
        if idx <= 0 and not text:
            continue
        item: dict[str, Any] = {"status": status}
        if idx > 0:
            item["index"] = idx
        if text:
            item["text"] = text
        if note:
            item["note"] = note
        out.append(item)
        if len(out) >= 12:
            break
    return out


def _infer_plan_step_status(text: str) -> str:
    raw = str(text or "").strip()
    lowered = raw.lower()
    if re.search(r"✅|(?:→|—|-)\s*(?:已完成|已实施|完成|done)(?:\s*[（(][^）)]*[）)])?\s*$", raw, flags=re.IGNORECASE):
        return "done"
    if re.search(r"(?:^|[→—\-\s])(?:进行中|处理中|in[_ -]?progress)(?:$|[，。,.\s])", raw, flags=re.IGNORECASE):
        return "in_progress"
    if re.search(r"⏳|(?:^|[→—\-\s])(?:未实施|搁置|待定|阻塞|blocked)(?:$|[：:，。,.\s])", raw, flags=re.IGNORECASE):
        return "blocked"
    if re.search(r"(?:^|[→—\-\s])(?:待办|未开始|pending)(?:$|[，。,.\s])", raw, flags=re.IGNORECASE):
        return "pending"
    if "blocked" in lowered:
        return "blocked"
    return ""


def _clean_plan_step_prefix(text: str) -> str:
    clean = _normalize_text(text, 800)
    clean = re.sub(r"^\s*[-*•]\s*", "", clean)
    clean = re.sub(r"^\s*\d{1,3}[.、)]\s*", "", clean)
    return re.sub(r"\s{2,}", " ", clean).strip()


def _clean_plan_step_text(text: str) -> str:
    clean = _clean_plan_step_prefix(text)
    clean = clean.replace("✅", "").replace("⏳", "").replace("🏁", "")
    clean = clean.strip()
    clean = re.sub(r"\s*(?:→|—|-)\s*(?:已完成|已实施|完成|done)(?:\s*[（(][^）)]*[）)])?\s*$", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s*(?:→|—|-)\s*(?:进行中|处理中|in[_ -]?progress)\s*$", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s*(?:→|—|-)\s*(?:待定|未实施|搁置|阻塞|blocked)(?:\s*[：:].*)?\s*$", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^(?:未实施|待定|搁置)(?:[（(][^）)]*[）)])?\s*[：:]\s*", "", clean)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    return clean


def _plan_step_text_has_status_marker(text: Any) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    return bool(re.search(
        r"✅|⏳|🏁|(?:→|—|-)\s*(?:已完成|已实施|完成|done|进行中|处理中|in[_ -]?progress|待办|未开始|pending|未实施|搁置|待定|阻塞|blocked)(?:\s*[（(][^）)]*[）)])?\s*$",
        raw,
        flags=re.IGNORECASE,
    ))


def _validate_plan_steps_contract(raw_steps: Any, *, action: str) -> list[str]:
    if action not in {"create", "update", "block"}:
        return []
    if raw_steps in (None, ""):
        return []
    errors: list[str] = []
    values, is_structured_sequence = _parse_plan_step_sequence(raw_steps)
    if not is_structured_sequence:
        errors.append("steps must be an array of objects; do not pass newline/semicolon-separated step text")
    for idx, value in enumerate(values, start=1):
        if value in (None, ""):
            continue
        if isinstance(value, dict):
            raw_text = value.get("text") or value.get("content") or value.get("title") or value.get("label") or ""
            raw_status = str(value.get("status", "pending") or "pending").strip().lower()
            if raw_status not in PLAN_STEP_STATUS_VALUES:
                errors.append(f"step {idx} status must be one of pending/in_progress/done/blocked")
        else:
            raw_text = value
            errors.append(f"step {idx} must be an object with text and status fields")
        if _plan_step_text_has_status_marker(raw_text):
            errors.append(f"step {idx} text encodes progress; use the status field for initial state or action=set_step_status for later changes")
    return errors[:8]


def _validate_plan_step_updates_contract(
    step_updates: Any = None,
    *,
    step_index: Any = 0,
    step_text: Any = "",
    step_status: Any = "",
) -> list[str]:
    values: list[Any]
    if isinstance(step_updates, list):
        values = step_updates
    elif isinstance(step_updates, dict):
        values = [step_updates]
    else:
        values = []
    if step_index or step_text or step_status:
        values.append({"index": step_index, "text": step_text, "status": step_status})
    if not values:
        return ["step_updates or step_index/step_text plus step_status is required"]
    errors: list[str] = []
    for idx, raw in enumerate(values, start=1):
        if not isinstance(raw, dict):
            errors.append(f"step update {idx} must be an object")
            continue
        raw_status = str(raw.get("status") or raw.get("step_status") or "").strip().lower()
        raw_index = _parse_int(str(raw.get("index") or raw.get("step_index") or 0), 0, 0, 1000)
        raw_text = raw.get("text") or raw.get("step_text") or ""
        if raw_status not in PLAN_STEP_STATUS_VALUES:
            errors.append(f"step update {idx} status must be one of pending/in_progress/done/blocked")
        if raw_index <= 0 and not str(raw_text or "").strip():
            errors.append(f"step update {idx} must include index or text")
        if _plan_step_text_has_status_marker(raw_text):
            errors.append(f"step update {idx} text must identify the step only; put progress in status")
    return errors[:8]


def _normalize_plan_action_status(
    *,
    action: Any = "create",
    status: Any = "planned",
    completed: Any = False,
    requires_completion_confirmation: Any = False,
    completion_confirmation_state: Any = "none",
) -> tuple[str, str, bool, bool, str]:
    raw_action_text = str(action or "create").strip().lower()
    legacy_request_confirmation = raw_action_text == "request_completion_confirmation"
    legacy_confirm_complete = raw_action_text == "confirm_complete"
    action_text = raw_action_text
    if action_text not in PLAN_ACTION_VALUES:
        action_text = "create"
    status_text = str(status or "planned").strip().lower()
    completed_flag = _parse_boolish(completed, default=False)
    needs_completion_confirmation = False
    confirmation_state = str(completion_confirmation_state or "").strip().lower()
    if confirmation_state not in {"none", "pending", "confirmed"}:
        confirmation_state = "none"

    if legacy_request_confirmation:
        action_text = "update"
        status_text = "revised"
        confirmation_state = "none"
    elif legacy_confirm_complete:
        action_text = "update"
        status_text = "complete"
        completed_flag = True
        confirmation_state = "confirmed"

    if status_text == "awaiting_completion_confirmation":
        status_text = "revised"

    if action_text == "clear":
        status_text = "complete" if completed_flag else "planned"
        confirmation_state = "none"
    elif action_text == "block":
        status_text = "blocked"
        confirmation_state = "none"
    elif confirmation_state == "confirmed":
        completed_flag = True

    if completed_flag:
        status_text = "complete"
        needs_completion_confirmation = False
        confirmation_state = "confirmed"
    if status_text not in PLAN_STATUS_VALUES:
        status_text = "planned"
    if confirmation_state == "none" and status_text == "complete":
        confirmation_state = "confirmed"
    elif status_text != "complete":
        confirmation_state = "none"
    return action_text, status_text, completed_flag, needs_completion_confirmation, confirmation_state


def normalize_plan_payload(payload: Any) -> dict[str, Any] | None:
    """Normalize plan objects at read/render boundaries.

    Text-status inference here is a legacy adapter for already persisted
    malformed plan records. New plan() tool calls are validated separately and
    must use structured fields.
    """
    if not isinstance(payload, dict):
        return None
    if str(payload.get("kind", "") or "").strip() != "plan" and not (
        "goal" in payload or "steps" in payload or "completion_note" in payload
    ):
        return None
    raw_schema_version = payload.get("schema_version", payload.get("schemaVersion", None))
    has_schema_version = raw_schema_version not in (None, "")
    schema_version = _parse_int(str(raw_schema_version), 1, 1, 99) if has_schema_version else 1
    legacy_text_adapter = schema_version < PLAN_SCHEMA_VERSION
    action_text, status_text, completed_flag, needs_completion_confirmation, confirmation_state = _normalize_plan_action_status(
        action=payload.get("action", "create"),
        status=payload.get("status", "planned"),
        completed=payload.get("completed", False),
        requires_completion_confirmation=payload.get("requires_completion_confirmation", False),
        completion_confirmation_state=payload.get("completion_confirmation_state", "none"),
    )
    normalized: dict[str, Any] = {
        "ok": bool(payload.get("ok", True)),
        "kind": "plan",
        "action": action_text,
        "status": status_text,
        "completed": bool(completed_flag or status_text == "complete"),
        "schema_version": schema_version,
        "legacy_text_adapter": bool(legacy_text_adapter),
        "goal": _normalize_text(payload.get("goal", ""), 1200),
        "steps": _normalize_plan_steps(payload.get("steps", []), infer_text_status=legacy_text_adapter),
        "notes": _normalize_text(payload.get("notes", ""), 1600),
        "completion_note": _normalize_text(payload.get("completion_note", payload.get("completionNote", "")), 1200),
    }
    step_updates = _normalize_plan_step_updates(
        payload.get("step_updates"),
        step_index=payload.get("step_index", 0),
        step_text=payload.get("step_text", ""),
        step_status=payload.get("step_status", ""),
        update_note=payload.get("update_note", ""),
    )
    if action_text == "set_step_status":
        normalized["step_updates"] = step_updates
        normalized["update_note"] = _normalize_text(payload.get("update_note", ""), 500)
    for key in ("message", "error", "error_code", "call_id", "pending_confirmation"):
        if key in payload:
            normalized[key] = payload.get(key)
    return normalized


def normalize_plan_tool_result(payload: Any, *, call_id: str = "", tool_name: str = "plan") -> Any:
    """Normalize plan results at API boundaries while preserving wrapper metadata."""
    if not isinstance(payload, dict):
        return payload
    direct = normalize_plan_payload(payload)
    if direct is not None:
        return direct
    inner = payload.get("result")
    normalized_inner = normalize_plan_payload(inner)
    if normalized_inner is None:
        return payload
    out = dict(payload)
    out["result"] = normalized_inner
    out["ok"] = bool(out.get("ok", normalized_inner.get("ok", True)))
    out["tool_name"] = str(out.get("tool_name") or tool_name or "plan")
    if call_id:
        out["call_id"] = str(out.get("call_id") or call_id)
    return out


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_memory_payload() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": _now_iso(),
        "items": [],
    }


def _normalize_memory_item(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    data = str(item.get("data", "")).strip()
    if not data:
        return None
    if len(data) > MEMORY_MAX_DATA_LEN:
        data = data[:MEMORY_MAX_DATA_LEN]
    time_raw = str(item.get("time", "")).strip() or _now_iso()
    return {"time": time_raw, "data": data}


def _normalize_memory_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _default_memory_payload()

    items_raw = raw.get("items", [])
    if not isinstance(items_raw, list):
        items_raw = []

    items: list[dict[str, str]] = []
    for item in items_raw:
        normalized = _normalize_memory_item(item)
        if normalized is not None:
            items.append(normalized)
    if len(items) > MEMORY_MAX_ITEMS:
        items = items[-MEMORY_MAX_ITEMS:]

    version = raw.get("version", 1)
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 1

    updated_at = str(raw.get("updated_at", "")).strip() or _now_iso()
    return {
        "version": version,
        "updated_at": updated_at,
        "items": items,
    }


def _load_memory_payload() -> dict[str, Any]:
    try:
        if not _MEMORY_FILE.exists():
            return _default_memory_payload()
        text = _MEMORY_FILE.read_text(encoding="utf-8")
        raw = json.loads(text) if text.strip() else {}
        return _normalize_memory_payload(raw)
    except Exception:
        return _default_memory_payload()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _save_memory_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_memory_payload(payload)
    normalized["updated_at"] = _now_iso()
    _atomic_write_json(_MEMORY_FILE, normalized)
    return normalized


def register_running_tool(call_id: str, handle: Any) -> None:
    cid = str(call_id or "").strip()
    if not cid:
        return
    with _RUNNING_TOOL_LOCK:
        _RUNNING_TOOL_CONTEXTS[cid] = {"handle": handle, "ts": _now_iso()}


def clear_running_tool(call_id: str) -> None:
    cid = str(call_id or "").strip()
    if not cid:
        return
    with _RUNNING_TOOL_LOCK:
        _RUNNING_TOOL_CONTEXTS.pop(cid, None)


def skip_running_tool(call_id: str) -> bool:
    cid = str(call_id or "").strip()
    if not cid:
        return False
    with _RUNNING_TOOL_LOCK:
        ctx = _RUNNING_TOOL_CONTEXTS.get(cid)
    if not isinstance(ctx, dict):
        return cancel_mcp_call(cid)
    handle = ctx.get("handle")
    try:
        if isinstance(handle, subprocess.Popen):
            try:
                if hasattr(os, "killpg") and hasattr(handle, "pid") and int(handle.pid or 0) > 0:
                    os.killpg(handle.pid, signal.SIGTERM)
                else:
                    handle.terminate()
            except Exception:
                try:
                    handle.terminate()
                except Exception:
                    pass
            return True
    except Exception:
        pass
    return cancel_mcp_call(cid)


def tool(tool_perm: int, tool_des: str, must: bool = False) -> Callable:
    """
    工具装饰器

    参数:
        tool_perm: int - 工具所需权限级别
        tool_des: str - 工具描述
        must: bool - True注册到SYSTEM_TOOL，False注册到SPARE_TOOL
    """
    def decorator(func):
        tool_name = func.__name__
        effective_perm, _ = validate_registered_tool_perm(tool_name, int(tool_perm))

        tool_info = {
            "des": tool_des,
            "perm": effective_perm,
            "func": func
        }

        if must:
            SYSTEM_TOOL[tool_name] = tool_info
        else:
            SPARE_TOOL[tool_name] = tool_info
        _TOOL_SCHEMA_CACHE.clear()
        _MCP_TOOL_SCHEMA_CACHE.clear()
        _MCP_TOOL_ALIAS_CACHE.clear()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return decorator


def find_tool(tool_name: str) -> dict[str, Any] | None:
    """
    用处： 根据名称在系统工具与备用工具表中查找工具

    参数：
        tool_name: str // 工具名称

    返回：
        dict | None // 工具信息字典；找不到返回 None
    """
    if tool_name in SYSTEM_TOOL:
        return SYSTEM_TOOL[tool_name]
    if tool_name in SPARE_TOOL:
        return SPARE_TOOL[tool_name]
    return None


def run_tool(tool_name: str, user_perm: int, *args, **kwargs):
    """
    用处： 按权限校验后调用已注册的工具

    参数：
        tool_name: str // 工具名称
        user_perm: int // 调用方的权限位
        *args, **kwargs // 透传给工具函数

    返回：
        工具函数的返回值

    异常：
        ValueError: 工具未注册
        PermissionError: 调用方权限不足
    """
    tool_info = find_tool(tool_name)
    if tool_info is None:
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool",
            func="run_tool",
            file_path=_THIS_FILE,
            content=f"tool_not_registered tool={tool_name}",
            extra={"tool_name": tool_name, "user_perm": int(user_perm), "ok": False},
        )
        raise ValueError(f"Tool not registered: {tool_name}")

    required = int(tool_info["perm"])
    if not has_perm(int(user_perm), required):
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool",
            func="run_tool",
            file_path=_THIS_FILE,
            content=f"tool_permission_denied tool={tool_name}",
            extra={
                "tool_name": tool_name,
                "user_perm": int(user_perm),
                "required_perm": required,
                "ok": False,
            },
        )
        payload = build_permission_denied_payload(tool_name, int(user_perm), required)
        raise PermissionDeniedError(f"Permission denied for {tool_name}", payload=payload)

    audit_event(
        op_type="TOOL_EXECUTE",
        subsystem="tool",
        func="run_tool",
        file_path=_THIS_FILE,
        content=f"tool_execute_start tool={tool_name}",
        extra={
            "tool_name": tool_name,
            "user_perm": int(user_perm),
            "required_perm": required,
            "args_count": len(args),
            "kwargs_keys": sorted(str(k) for k in kwargs.keys()),
        },
    )
    try:
        # Inject caller perm so tools like run_terminal can check bypass/perms
        mutable_kwargs = dict(kwargs)
        import inspect as _inspect
        try:
            _sig = _inspect.signature(tool_info["func"])
            if "_caller_perm" in _sig.parameters:
                mutable_kwargs.setdefault("_caller_perm", int(user_perm))
        except Exception:
            pass
        result = tool_info["func"](*args, **mutable_kwargs)
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool",
            func="run_tool",
            file_path=_THIS_FILE,
            content=f"tool_execute_done tool={tool_name}",
            extra={"tool_name": tool_name, "ok": True},
        )
        return result
    except Exception as e:
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool",
            func="run_tool",
            file_path=_THIS_FILE,
            content=f"tool_execute_failed tool={tool_name} err={e}",
            extra={"tool_name": tool_name, "ok": False, "error": str(e)},
        )
        raise


def list_tools(user_perm: int | None = None) -> dict[str, str]:
    """
    用处： 列出所有工具名称与描述；若提供 user_perm 则只返回可调用的

    参数：
        user_perm: int | None // 调用方权限，None 表示不过滤

    返回：
        dict[str, str] // {工具名: 描述}
    """
    result: dict[str, str] = {}
    names = sorted(set(SYSTEM_TOOL) | set(SPARE_TOOL))
    for name in names:
        info = find_tool(name)
        if info is None:
            continue
        if user_perm is None or (user_perm & info["perm"]) == info["perm"]:
            result[name] = info["des"]
    return result


def _safe_tool_alias(raw: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return "tool"
    if not re.match(r"^[A-Za-z_]", text):
        text = f"tool_{text}"
    return text[:64]


def _local_tool_schema(tool_name: str, tool_desc: str, info: dict[str, Any] | None) -> dict[str, Any]:
    import inspect

    properties: dict[str, Any] = {}
    required: list[str] = []

    if info:
        has_var_positional = False
        try:
            sig = inspect.signature(info["func"])
            for pname, param in sig.parameters.items():
                if pname.startswith("_") or pname in ("call_id", "command"):
                    continue
                if tool_name == "plan" and pname in PLAN_HIDDEN_PARAMETER_NAMES:
                    continue
                if param.kind == inspect.Parameter.VAR_POSITIONAL:
                    has_var_positional = True
                    continue
                if param.kind == inspect.Parameter.VAR_KEYWORD:
                    continue
                ptype = "string"
                if param.annotation is not inspect.Parameter.empty:
                    a = param.annotation
                    if a is int:
                        ptype = "integer"
                    elif a is bool:
                        ptype = "boolean"
                override = TOOL_PARAMETER_SCHEMA_OVERRIDES.get((tool_name, pname))
                properties[pname] = json.loads(json.dumps(override, ensure_ascii=False)) if override else {"type": ptype}
                if tool_name == "ask_user_question" and pname in ASK_USER_QUESTION_PARAMETER_DESCRIPTIONS:
                    properties[pname]["description"] = ASK_USER_QUESTION_PARAMETER_DESCRIPTIONS[pname]
                if tool_name == "plan" and pname in PLAN_PARAMETER_DESCRIPTIONS:
                    properties[pname]["description"] = PLAN_PARAMETER_DESCRIPTIONS[pname]
                if param.default is inspect.Parameter.empty:
                    required.append(pname)
            if has_var_positional and not properties:
                properties["text"] = {"type": "string"}
        except Exception:
            pass

    schema_params: dict[str, Any] = {"type": "object"}
    if properties:
        schema_params["properties"] = {k: properties[k] for k in sorted(properties)}
        if tool_name == "ask_user_question" and "question" in properties:
            required = sorted(set(required) | {"question"})
        schema_params["required"] = sorted(required)
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": tool_desc,
            "parameters": schema_params,
        },
    }


def build_agent_tool_schemas(user_perm: int) -> list[dict[str, Any]]:
    """Build OpenAI tool schemas — each tool exposed directly, params from signature."""
    perm_key = int(user_perm)
    cached = _TOOL_SCHEMA_CACHE.get(perm_key)
    if cached is not None:
        return json.loads(json.dumps(cached, ensure_ascii=False))

    tools = list_tools(perm_key)
    schemas: list[dict[str, Any]] = []

    for tool_name, tool_desc in sorted(tools.items()):
        info = find_tool(tool_name)
        schemas.append(_local_tool_schema(tool_name, tool_desc, info))
    _TOOL_SCHEMA_CACHE[perm_key] = json.loads(json.dumps(schemas, ensure_ascii=False))
    return schemas


def _mcp_input_schema_to_openai(schema: Any) -> dict[str, Any]:
    if isinstance(schema, dict):
        out = json.loads(json.dumps(schema, ensure_ascii=False, default=str))
        if str(out.get("type", "") or "") != "object":
            out["type"] = "object"
        out.setdefault("properties", {})
        if not isinstance(out.get("properties"), dict):
            out["properties"] = {}
        return out
    return {"type": "object", "properties": {}}


def _build_mcp_catalog(user_perm: int) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    perm_key = int(user_perm)
    cached_schema = _MCP_TOOL_SCHEMA_CACHE.get(perm_key)
    cached_alias = _MCP_TOOL_ALIAS_CACHE.get(perm_key)
    if cached_schema is not None and cached_alias is not None:
        return (
            json.loads(json.dumps(cached_schema, ensure_ascii=False)),
            json.loads(json.dumps(cached_alias, ensure_ascii=False)),
        )

    schemas: list[dict[str, Any]] = []
    aliases: dict[str, dict[str, Any]] = {}
    used_aliases: set[str] = set()

    def add_schema(alias: str, schema: dict[str, Any], route: dict[str, Any]) -> None:
        clean_alias = _safe_tool_alias(alias)
        base = clean_alias
        suffix = 2
        while clean_alias in used_aliases:
            clean_alias = _safe_tool_alias(f"{base}_{suffix}")
            suffix += 1
        used_aliases.add(clean_alias)
        fn = schema.get("function") if isinstance(schema.get("function"), dict) else {}
        schemas.append({
            "type": "function",
            "function": {
                "name": clean_alias,
                "description": str(fn.get("description", "") or ""),
                "parameters": _mcp_input_schema_to_openai(fn.get("parameters")),
            },
        })
        aliases[clean_alias] = dict(route)

    for local_tool in local_mcp_list_tools(perm_key).get("tools", []):
        if not isinstance(local_tool, dict):
            continue
        tool_name = str(local_tool.get("name", "") or "").strip()
        if not tool_name:
            continue
        schema = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": f"[MCP local] {str(local_tool.get('description') or '')}",
                "parameters": _mcp_input_schema_to_openai(local_tool.get("inputSchema")),
            },
        }
        add_schema(tool_name, schema, {
            "backend": "local",
            "tool_name": tool_name,
            "display_name": tool_name,
        })

    try:
        server_rows = list_configured_mcp_servers().get("servers", [])
    except Exception:
        server_rows = []
    for server in server_rows if isinstance(server_rows, list) else []:
        server_name = str(server.get("name", "") or "").strip() if isinstance(server, dict) else ""
        if not server_name:
            continue
        try:
            listed = list_remote_mcp_tools(server_name)
        except Exception as exc:
            audit_event(
                op_type="TOOL_READ",
                subsystem="tool",
                func="_build_mcp_catalog",
                file_path=_THIS_FILE,
                content=f"mcp_list_tools_failed server={server_name} err={exc}",
                extra={"server": server_name, "ok": False, "error": str(exc)},
            )
            continue
        for remote in listed.get("tools", []) if isinstance(listed, dict) else []:
            if not isinstance(remote, dict):
                continue
            remote_name = str(remote.get("name", "") or "").strip()
            if not remote_name:
                continue
            desc = str(remote.get("description", "") or "")
            input_schema = remote.get("inputSchema") or remote.get("input_schema") or {}
            schema = {
                "type": "function",
                "function": {
                    "name": remote_name,
                    "description": f"[MCP {server_name}] {desc}".strip(),
                    "parameters": _mcp_input_schema_to_openai(input_schema),
                },
            }
            add_schema(f"{server_name}__{remote_name}", schema, {
                "backend": "remote",
                "server": server_name,
                "tool_name": remote_name,
                "display_name": f"{server_name}:{remote_name}",
            })

    _MCP_TOOL_SCHEMA_CACHE[perm_key] = json.loads(json.dumps(schemas, ensure_ascii=False))
    _MCP_TOOL_ALIAS_CACHE[perm_key] = json.loads(json.dumps(aliases, ensure_ascii=False))
    return schemas, aliases


def build_mcp_tool_schemas(user_perm: int) -> list[dict[str, Any]]:
    """Build OpenAI-compatible tool schemas from the MCP aggregate catalog."""
    schemas, _aliases = _build_mcp_catalog(user_perm)
    return schemas


def resolve_mcp_tool_alias(user_perm: int, alias: str) -> dict[str, Any] | None:
    _schemas, aliases = _build_mcp_catalog(user_perm)
    return aliases.get(str(alias or "").strip())


def find_any_mcp_tool_alias(alias: str) -> dict[str, Any] | None:
    clean = str(alias or "").strip()
    if not clean:
        return None
    info = find_tool(clean)
    if info is not None:
        return {"backend": "local", "tool_name": clean, "display_name": clean}
    try:
        _schemas, aliases = _build_mcp_catalog(0x7FFFFFFF)
        return aliases.get(clean)
    except Exception:
        return None


def display_name_for_mcp_tool(user_perm: int, alias: str) -> str:
    route = resolve_mcp_tool_alias(user_perm, alias)
    if isinstance(route, dict):
        return str(route.get("display_name") or route.get("tool_name") or alias)
    return str(alias or "")


def local_mcp_list_tools(user_perm: int) -> dict[str, Any]:
    """Return built-in tools in MCP tools/list shape."""
    tools: list[dict[str, Any]] = []
    for schema in build_agent_tool_schemas(user_perm):
        fn = schema.get("function") if isinstance(schema, dict) else {}
        name = str(fn.get("name", "") or "")
        if not name:
            continue
        tools.append({
            "name": name,
            "description": str(fn.get("description", "") or ""),
            "inputSchema": _mcp_input_schema_to_openai(fn.get("parameters")),
        })
    return {"ok": True, "server": "local", "tools": tools}


def local_mcp_call_tool(tool_name: str, user_perm: int, arguments: dict[str, Any] | None = None, *, call_id: str = "") -> dict[str, Any]:
    """Call a built-in tool through the local MCP backend."""
    raw = run_agent_tool(tool_name, user_perm, arguments if isinstance(arguments, dict) else {}, call_id=call_id)
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {"ok": True, "tool_name": str(tool_name), "result": raw}
    if isinstance(parsed, dict):
        return parsed
    return {"ok": True, "tool_name": str(tool_name), "result": parsed}


def run_agent_tool(
    agent_tool_name: str,
    user_perm: int,
    arguments: dict[str, Any] | None,
    *,
    call_id: str | None = None,
    skip_token: Any = None,
) -> str:
    """
    Execute a tool on behalf of the LLM agent. Returns JSON string.
    agent_tool_name is the registered tool name. arguments are treated as kwargs.
    """
    payload = arguments if isinstance(arguments, dict) else {}
    call_id_text = str(call_id or "").strip()
    skip_token_text = str(skip_token or "").strip()

    if skip_token_text:
        out = {
            "ok": False,
            "tool_name": str(agent_tool_name or "").strip(),
            "error": "tool skipped",
            "error_code": "user_skipped",
            "pending_confirmation": False,
        }
        if call_id_text:
            out["call_id"] = call_id_text
        return json.dumps(out, ensure_ascii=False)

    tool_name = str(agent_tool_name or "").strip()
    if not find_tool(tool_name):
        out = {"ok": False, "error": f"Unknown tool: {tool_name}"}
        if call_id_text:
            out["call_id"] = call_id_text
        return json.dumps(out, ensure_ascii=False)

    # Extract positional args (for variadic functions like echo)
    raw_args = payload.get("args")
    call_args: list[str] = []
    if isinstance(raw_args, list):
        call_args = [str(x) for x in raw_args]

    try:
        import inspect as _inspect
        info = find_tool(tool_name)
        sig = _inspect.signature(info["func"]) if info else None
    except Exception:
        sig = None

    # All other LLM arguments are kwargs — filter internal params.
    # Preserve structured JSON values for Any/list/dict parameters such as
    # plan.steps; stringifying them turns arrays into Python repr strings.
    call_kwargs: dict[str, Any] = {}
    for key, value in payload.items():
        clean_key = str(key).strip()
        if not clean_key or clean_key.startswith("_"):
            continue
        if clean_key == "args":
            continue  # already handled above
        param = sig.parameters.get(clean_key) if sig else None
        call_kwargs[clean_key] = str(value) if _tool_param_expects_text(param) else value
    if call_id_text:
        try:
            if sig and (
                "call_id" in sig.parameters
                or any(p.kind == _inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            ):
                call_kwargs.setdefault("call_id", call_id_text)
        except Exception:
            pass

    capture = io.StringIO()
    try:
        with contextlib.redirect_stdout(capture):
            result = run_tool(tool_name, user_perm, *call_args, **call_kwargs)
    except PermissionDeniedError as e:
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool",
            func="run_agent_tool",
            file_path=_THIS_FILE,
            content=f"agent_tool_permission_denied tool={tool_name}",
            extra={"ok": False, "user_perm": int(user_perm), "error": str(e)},
        )
        denied = dict(e.payload or {})
        denied.setdefault("ok", False)
        denied.setdefault("tool_name", tool_name)
        denied.setdefault("error", str(e))
        denied.setdefault("error_code", "permission_denied")
        denied.setdefault("expose_to_user", False)
        denied.setdefault("user_message", "该工具当前不可用，请尝试其它方式。")
        if call_id_text:
            denied.setdefault("call_id", call_id_text)
        return json.dumps(denied, ensure_ascii=False)
    except ValueError as e:
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool",
            func="run_agent_tool",
            file_path=_THIS_FILE,
            content=f"agent_tool_value_error tool={tool_name} err={e}",
            extra={"ok": False, "user_perm": int(user_perm), "error": str(e)},
        )
        out = {"ok": False, "tool_name": tool_name, "error": str(e)}
        if call_id_text:
            out["call_id"] = call_id_text
        return json.dumps(out, ensure_ascii=False)
    except PermissionError as e:
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool",
            func="run_agent_tool",
            file_path=_THIS_FILE,
            content=f"agent_tool_permission_error tool={tool_name}",
            extra={"ok": False, "user_perm": int(user_perm), "error": str(e)},
        )
        out = {
            "ok": False,
            "tool_name": tool_name,
            "error": str(e),
            "error_code": "permission_denied",
            "expose_to_user": False,
            "user_message": "该工具当前不可用，请尝试其它方式。",
        }
        if call_id_text:
            out["call_id"] = call_id_text
        return json.dumps(out, ensure_ascii=False)
    except Exception as e:
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool",
            func="run_agent_tool",
            file_path=_THIS_FILE,
            content=f"agent_tool_exception tool={tool_name} err={e}",
            extra={"ok": False, "user_perm": int(user_perm), "error": str(e)},
        )
        out = {"ok": False, "tool_name": tool_name, "error": f"执行异常: {e}"}
        if call_id_text:
            out["call_id"] = call_id_text
        return json.dumps(out, ensure_ascii=False)

    printed = capture.getvalue().strip()
    inner_ok = result.get("ok") if isinstance(result, dict) else True
    inner_error = result.get("error", "") if isinstance(result, dict) else ""
    payload: dict[str, Any] = {"ok": inner_ok, "tool_name": tool_name}
    if not inner_ok and inner_error:
        payload["error"] = inner_error  # 提到外层方便展示
    if call_id_text:
        payload["call_id"] = call_id_text
    if printed:
        payload["stdout"] = printed
    if result is not None:
        payload["result"] = result
    if not printed and result is None:
        payload["result"] = "工具执行完成"
    audit_event(
        op_type="TOOL_EXECUTE",
        subsystem="tool",
        func="run_agent_tool",
        file_path=_THIS_FILE,
        content=f"agent_tool_done tool={tool_name}",
        extra={
            "ok": inner_ok,
            "user_perm": int(user_perm),
            "has_stdout": bool(printed),
            "has_result": result is not None,
        },
    )
    return json.dumps(payload, ensure_ascii=False, default=str)


def run_mcp_agent_tool(
    agent_tool_name: str,
    user_perm: int,
    arguments: dict[str, Any] | None,
    *,
    call_id: str | None = None,
    skip_token: Any = None,
) -> str:
    """
    Execute an LLM-selected tool through the MCP aggregate catalog.

    Local Python tools are treated as the built-in MCP backend. External MCP
    servers are resolved from the configured MCP server catalog by stable alias.
    """
    alias = str(agent_tool_name or "").strip()
    call_id_text = str(call_id or "").strip()
    route = resolve_mcp_tool_alias(user_perm, alias)
    if not isinstance(route, dict):
        # Legacy compatibility for old conversation replay or tests that still
        # call registered tool names directly.
        return run_agent_tool(alias, user_perm, arguments, call_id=call_id_text, skip_token=skip_token)

    if skip_token:
        out = {
            "ok": False,
            "tool_name": str(route.get("display_name") or route.get("tool_name") or alias),
            "mcp_alias": alias,
            "mcp_backend": str(route.get("backend") or ""),
            "error": "tool skipped",
            "error_code": "user_skipped",
            "pending_confirmation": False,
        }
        if call_id_text:
            out["call_id"] = call_id_text
        return json.dumps(out, ensure_ascii=False)

    backend = str(route.get("backend") or "").strip()
    tool_name = str(route.get("tool_name") or alias).strip()
    display_name = str(route.get("display_name") or tool_name).strip()
    payload = arguments if isinstance(arguments, dict) else {}

    if backend == "local":
        parsed = local_mcp_call_tool(tool_name, user_perm, payload, call_id=call_id_text)
        if isinstance(parsed, dict):
            parsed.setdefault("mcp_backend", "local")
            parsed.setdefault("mcp_alias", alias)
            parsed.setdefault("display_tool_name", display_name)
            return json.dumps(parsed, ensure_ascii=False, default=str)
        return json.dumps({"ok": True, "tool_name": display_name, "result": parsed}, ensure_ascii=False, default=str)

    if backend == "remote":
        server = str(route.get("server") or "").strip()
        try:
            result = call_mcp_tool(server, tool_name, payload, call_id=call_id_text)
            mcp_result = result.get("result") if isinstance(result, dict) else result
            is_error = bool(isinstance(mcp_result, dict) and mcp_result.get("isError") is True)
            out: dict[str, Any] = {
                "ok": not is_error,
                "tool_name": display_name,
                "mcp_backend": "remote",
                "mcp_alias": alias,
                "server": server,
                "remote_tool_name": tool_name,
                "result": mcp_result,
                "pending_confirmation": False,
            }
            if is_error:
                out["error"] = "MCP tool returned isError=true"
            if call_id_text:
                out["call_id"] = call_id_text
            return json.dumps(out, ensure_ascii=False, default=str)
        except Exception as exc:
            out = {
                "ok": False,
                "tool_name": display_name,
                "mcp_backend": "remote",
                "mcp_alias": alias,
                "server": server,
                "remote_tool_name": tool_name,
                "error": str(exc),
            }
            if call_id_text:
                out["call_id"] = call_id_text
            return json.dumps(out, ensure_ascii=False, default=str)

    out = {"ok": False, "tool_name": display_name or alias, "mcp_alias": alias, "error": f"Unknown MCP backend: {backend}"}
    if call_id_text:
        out["call_id"] = call_id_text
    return json.dumps(out, ensure_ascii=False)


@tool(perm.PUBLIC_EXECUTE, "Print text to tool stdout (param: text)", must=True)
def echo(text: str = "", *content_list: str) -> None:
    """Print text to stdout."""
    if text:
        print(text)
    for content in content_list:
        print(content)


@tool(
    perm.PUBLIC_READ,
    ASK_USER_QUESTION_TOOL_DESCRIPTION,
    must=True,
)
def ask_user_question(
    question: str = "",
    options: Any = "",
    allow_custom_answer: str = "true",
    placeholder: str = "",
    call_id: str = "",
) -> dict[str, Any]:
    question_text = _normalize_text(question, 1000)
    if not question_text:
        return {"ok": False, "error": "question is required", "pending_confirmation": False}
    choices = _split_options(options)
    allow_custom = str(allow_custom_answer or "true").strip().lower() not in {"0", "false", "no", "off"}
    confirm_id = str(call_id or "").strip() or f"ask_{uuid.uuid4().hex[:12]}"
    return {
        "ok": True,
        "pending_confirmation": True,
        "kind": "question",
        "confirm_id": confirm_id,
        "call_id": confirm_id,
        "question": question_text,
        "options": choices,
        "none_of_them_value": ASK_USER_NONE_OF_THEM_VALUE,
        "none_of_them_label": ASK_USER_NONE_OF_THEM_LABEL,
        "allow_custom_answer": allow_custom,
        "placeholder": _normalize_text(placeholder, 160) or "补充你的答案或限制条件...",
        "message": "Waiting for the user to answer a clarification question.",
    }


@tool(
    perm.PUBLIC_READ,
    "Create or update a concise execution plan without performing the task. "
    "Use when the user asks for /plan, planning mode, or when complex work needs a visible plan. "
    "This tool is the structured Plan state API. Choose action=create or update for normal planning, "
    "action=set_step_status to mark existing steps pending/in_progress/done/blocked without rewriting plan text, "
    "action=block when blocked, and action=clear to remove the visible plan. "
    "Parameters: goal=objective, steps=array of step objects with one step per item, notes=optional assumptions/risks, completion_note=short completion summary. "
    "Use the enum fields exactly as provided by the JSON schema. Never use emoji/free-form status labels. Never pack multiple steps into one newline/semicolon-separated string. "
    "Never write progress such as 'done' or 'completed' in step text; call action=set_step_status instead.",
    must=True,
)
def plan(
    action: str = "create",
    goal: str = "",
    steps: Any = "",
    status: str = "planned",
    notes: str = "",
    completed: Any = False,
    requires_completion_confirmation: Any = False,
    completion_confirmation_state: str = "none",
    completion_note: str = "",
    step_index: int = 0,
    step_text: str = "",
    step_status: str = "",
    step_updates: Any = None,
    update_note: str = "",
) -> dict[str, Any]:
    goal_text = _normalize_text(goal, 1200)
    action_text, status_text, completed_flag, needs_completion_confirmation, confirmation_state = _normalize_plan_action_status(
        action=action,
        status=status,
        completed=completed,
        requires_completion_confirmation=requires_completion_confirmation,
        completion_confirmation_state=completion_confirmation_state,
    )
    notes_text = _normalize_text(notes, 1600)
    completion_note_text = _normalize_text(completion_note, 1200)
    contract_errors = (
        _validate_plan_step_updates_contract(
            step_updates,
            step_index=step_index,
            step_text=step_text,
            step_status=step_status,
        )
        if action_text == "set_step_status"
        else _validate_plan_steps_contract(steps, action=action_text)
    )
    if contract_errors:
        return {
            "ok": False,
            "kind": "plan",
            "action": action_text,
            "status": status_text,
            "schema_version": PLAN_SCHEMA_VERSION,
            "error": "invalid_plan_contract",
            "error_code": "invalid_plan_contract",
            "details": contract_errors,
            "message": (
                "Plan state must be represented with structured API fields. "
                "Use action=create/update with steps as array objects for plan content, "
                "and action=set_step_status with step_updates to change progress."
            ),
        }
    step_rows = _normalize_plan_steps(steps, infer_text_status=False)
    normalized_step_updates = _normalize_plan_step_updates(
        step_updates,
        step_index=step_index,
        step_text=step_text,
        step_status=step_status,
        update_note=update_note,
    )
    if action_text == "set_step_status":
        if not normalized_step_updates:
            return {
                "ok": False,
                "error": "step_index/step_text and step_status are required for set_step_status",
                "kind": "plan",
                "action": action_text,
                "status": status_text,
                "schema_version": PLAN_SCHEMA_VERSION,
                "step_updates": [],
            }
        return {
            "ok": True,
            "kind": "plan",
            "action": action_text,
            "status": status_text if status_text != "planned" else "revised",
            "schema_version": PLAN_SCHEMA_VERSION,
            "completed": False,
            "goal": goal_text,
            "steps": [],
            "step_updates": normalized_step_updates,
            "notes": notes_text,
            "completion_note": completion_note_text,
            "update_note": _normalize_text(update_note, 500),
            "message": "Plan step status updated.",
        }
    if not goal_text and not step_rows and not notes_text and not completion_note_text:
        return {
        "ok": False,
        "error": "goal, steps, or notes is required",
        "kind": "plan",
        "action": action_text,
        "status": status_text,
        "schema_version": PLAN_SCHEMA_VERSION,
    }
    return {
        "ok": True,
        "kind": "plan",
        "action": action_text,
        "status": status_text,
        "schema_version": PLAN_SCHEMA_VERSION,
        "completed": bool(completed_flag or status_text == "complete"),
        "goal": goal_text,
        "steps": step_rows,
        "notes": notes_text,
        "completion_note": completion_note_text,
        "message": (
            "Plan marked complete."
            if completed_flag or status_text == "complete"
            else "Plan recorded. Do not execute the task until the user asks to continue."
        ),
    }


@tool(perm.PUBLIC_READ, "Get Tinda's profile for context about the user", must=True)
def get_tinda_profile() -> str:
    """
    返回 Tinda 的个人简介文本（不输出终端，仅返回给调用方）
    """
    return (
        "我是Tinda，来自中国的一名开发者。自2025.8.23学习计算机相关知识。\n"
        "当前项目：TindaAgent\n"
        "联系方式：3431955251@qq.com（或搜索qq号，备注来意）\n"
        "——\n"
        "Tinda · Touch into new dimensions anytime"
    )


@tool(perm.PUBLIC_READ, "Get current time (param: tz=timezone, e.g. Asia/Shanghai)", must=True)
def get_current_time(tz: str = DEFAULT_TIMEZONE) -> dict[str, Any]:
    """
    返回当前时间信息，供日期计算、截止时间判断等场景使用
    """
    tz_name = str(tz or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    warning = ""
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz_name = DEFAULT_TIMEZONE
        zone = ZoneInfo(DEFAULT_TIMEZONE)
        warning = "时区无效，已回退到 Asia/Shanghai"

    now = datetime.now(zone)
    payload: dict[str, Any] = {
        "timezone": tz_name,
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "unix": int(now.timestamp()),
        "weekday": now.strftime("%A"),
    }
    if warning:
        payload["warning"] = warning
    return payload


@tool(perm.PUBLIC_READ, "Summarize long text (params: text, max_sentences=1-8)", must=True)
def summarize_text(
    text: str,
    max_sentences: str = "3",
    sentences: str | None = None,
    n_sentences: str | None = None,
) -> str:
    """
    对输入文本做轻量摘要，返回压缩后的关键信息
    """
    clean_text = _normalize_text(text)
    if not clean_text:
        return "输入为空，无法摘要。"

    # 兼容模型偶发传参：sentences / n_sentences
    if n_sentences is not None:
        limit_raw = n_sentences
    elif sentences is not None:
        limit_raw = sentences
    else:
        limit_raw = max_sentences
    limit = _parse_int(limit_raw, default=3, minimum=1, maximum=8)
    sentences = _split_sentences(clean_text)
    if not sentences:
        return clean_text[:120]
    if len(sentences) <= limit:
        return "。".join(sentences)

    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", clean_text.lower())
    freq = Counter(tokens)

    scored: list[tuple[float, int, str]] = []
    for idx, sentence in enumerate(sentences):
        sentence_tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", sentence.lower())
        if sentence_tokens:
            score = sum(freq.get(tok, 0) for tok in sentence_tokens) / len(sentence_tokens)
        else:
            score = 0.0
        score += min(len(sentence), 100) / 1000
        scored.append((score, idx, sentence))

    top_indices = sorted(i for _, i, _ in sorted(scored, reverse=True)[:limit])
    selected = [sentences[i] for i in top_indices]
    return "。".join(selected)


@tool(perm.PUBLIC_READ, "Extract keywords from text (params: text, top_k=1-20)", must=True)
def extract_keywords(text: str, top_k: str = "8", n_keywords: str | None = None) -> list[str]:
    """
    从文本中抽取高频关键词，便于检索与标签化
    """
    clean_text = _normalize_text(text)
    if not clean_text:
        return []

    limit_raw = n_keywords if n_keywords is not None else top_k
    limit = _parse_int(limit_raw, default=8, minimum=1, maximum=20)
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", clean_text.lower())
    if not tokens:
        return []

    filtered = [tok for tok in tokens if tok not in STOPWORDS and not tok.isdigit()]
    if not filtered:
        return []

    freq = Counter(filtered)
    return [word for word, _ in freq.most_common(limit)]


@tool(perm.PUBLIC_READ, "Read Tinda profile snippet by key: full/bio/project/contact/slogan", must=True)
def read_profile_snippet(key: str = "full") -> str:
    """
    读取预置个人资料片段；仅白名单键，不支持任意文件读取
    """
    normalized = str(key or "full").strip().lower()
    alias_map = {
        "full": "full",
        "all": "full",
        "bio": "bio",
        "about": "bio",
        "简介": "bio",
        "project": "project",
        "项目": "project",
        "contact": "contact",
        "联系方式": "contact",
        "mail": "contact",
        "email": "contact",
        "slogan": "slogan",
        "签名": "slogan",
    }
    target = alias_map.get(normalized)
    if target is None:
        valid_keys = ", ".join(sorted(alias_map.keys()))
        return f"Unsupported key: {key}. Available keys: {valid_keys}"

    if target == "full":
        return (
            f"{PROFILE_SNIPPETS['bio']}\n"
            f"{PROFILE_SNIPPETS['project']}\n"
            f"{PROFILE_SNIPPETS['contact']}\n"
            "——\n"
            f"{PROFILE_SNIPPETS['slogan']}"
        )
    return PROFILE_SNIPPETS[target]


@tool(perm.PUBLIC_READ, "Read global memory as JSON (time/data entries)", must=True)
def read_memories() -> dict[str, Any]:
    """
    读取全局记忆；损坏时自动回退到空结构
    """
    return _load_memory_payload()


@tool(perm.PUBLIC_WRITE, "Write a global memory entry (params: data, time optional)", must=True)
def save_memory(data: str, time: str = "") -> dict[str, Any]:
    """
    写入一条长期记忆，自动更新 updated_at
    """
    content = str(data or "").strip()
    if not content:
        raise ValueError("data is required")
    if len(content) > MEMORY_MAX_DATA_LEN:
        content = content[:MEMORY_MAX_DATA_LEN]

    payload = _load_memory_payload()
    items = payload.get("items", [])
    if not isinstance(items, list):
        items = []

    item = {
        "time": str(time or "").strip() or _now_iso(),
        "data": content,
    }
    items.append(item)
    if len(items) > MEMORY_MAX_ITEMS:
        items = items[-MEMORY_MAX_ITEMS:]

    payload["items"] = items
    saved = _save_memory_payload(payload)
    return {
        "saved": True,
        "item": item,
        "count": len(saved.get("items", [])),
        "updated_at": saved.get("updated_at", ""),
    }


@tool(perm.PUBLIC_WRITE, "Delete memory entries by text match (param: contains)", must=True)
def delete_memory(contains: str) -> dict[str, Any]:
    """
    按子串匹配删除记忆，便于人工清理错误记忆
    """
    keyword = str(contains or "").strip()
    if not keyword:
        raise ValueError("contains is required")

    payload = _load_memory_payload()
    items = payload.get("items", [])
    if not isinstance(items, list):
        items = []

    kept: list[dict[str, str]] = []
    removed = 0
    for item in items:
        data = str(item.get("data", ""))
        if keyword in data:
            removed += 1
            continue
        normalized = _normalize_memory_item(item)
        if normalized is not None:
            kept.append(normalized)

    payload["items"] = kept
    saved = _save_memory_payload(payload)
    return {
        "removed": removed,
        "count": len(saved.get("items", [])),
        "updated_at": saved.get("updated_at", ""),
    }


@tool(perm.TOOL_READ | perm.PUBLIC_READ, "Read a UTF-8 text file before editing. Parameters: path", must=True)
def read_file(path: str) -> dict[str, Any]:
    """
    读取文本文件，返回内容和 sha256，供 edit_file 做并发保护。
    """
    return read_text_file(path)


@tool(perm.TOOL_READ | perm.PUBLIC_READ, "Search files by name and/or content. Parameters: root='.', query filename/path substring optional, content text optional, glob='*', max_results=1-200, max_depth=0-32.", must=True)
def search_files(
    root: str = ".",
    query: str = "",
    content: str = "",
    glob: str = "*",
    max_results: str = "50",
    max_depth: str = "8",
) -> dict[str, Any]:
    """
    搜索文件名/路径和可读文本内容，返回有限结果，避免大输出污染上下文。
    """
    return search_text_files(
        root=root,
        query=query,
        content=content,
        glob=glob,
        max_results=_parse_int(max_results, default=50, minimum=1, maximum=200),
        max_depth=_parse_int(max_depth, default=8, minimum=0, maximum=32),
    )


@tool(perm.TOOL_READ | perm.PUBLIC_READ, "Search the web. Parameters: query, max_results=1-20, source=auto|tavily|builtin|index, site optional index id/domain/category, topic=general|news|finance, search_depth=basic|advanced. Uses Tavily when TAVILY_API_KEY is set, otherwise built-in DuckDuckGo/index fallback.", must=True)
def search_web(
    query: str,
    max_results: str = "5",
    source: str = "auto",
    site: str = "",
    topic: str = "general",
    search_depth: str = "basic",
    time_range: str = "",
    include_answer: str = "true",
    include_raw_content: str = "false",
    exclude_domains: str = "",
    timeout: str = "",
) -> dict[str, Any]:
    """
    Network search tool with Tavily primary path and a local no-key fallback.
    """
    return perform_web_search(
        query=query,
        max_results=max_results,
        source=source,
        site=site,
        topic=topic,
        search_depth=search_depth,
        time_range=time_range,
        include_answer=include_answer,
        include_raw_content=include_raw_content,
        exclude_domains=exclude_domains,
        timeout=timeout,
    )


@tool(perm.TOOL_WRITE | perm.PUBLIC_WRITE, "Edit a UTF-8 text file by exact replacement. Parameters: path, old_text, new_text, expected_sha256 optional, create=false, dry_run=false. Use read_file first for sha256; old_text must be unique.", must=True)
def edit_file(
    path: str,
    old_text: str = "",
    new_text: str = "",
    expected_sha256: str = "",
    create: str = "false",
    dry_run: str = "false",
) -> dict[str, Any]:
    """
    类似 Claude Code/Codex 的最小编辑工具：精确替换，不做隐式猜测。
    """
    return apply_text_edit(
        path,
        old_text=old_text,
        new_text=new_text,
        expected_sha256=expected_sha256,
        create=str(create).strip().lower() in {"1", "true", "yes", "on"},
        dry_run=str(dry_run).strip().lower() in {"1", "true", "yes", "on"},
    )


@tool(perm.TOOL_WRITE, "Configure a local stdio MCP server. Parameters: name, command, args_json optional, env_json optional", must=True)
def mcp_add_server(name: str, command: str, args_json: str = "[]", env_json: str = "{}") -> dict[str, Any]:
    args_raw = json.loads(args_json) if str(args_json or "").strip() else []
    env_raw = json.loads(env_json) if str(env_json or "").strip() else {}
    if not isinstance(args_raw, list):
        raise ValueError("args_json must be a JSON array")
    if not isinstance(env_raw, dict):
        raise ValueError("env_json must be a JSON object")
    result = upsert_mcp_server(name, command, args_raw, env_raw)
    _MCP_TOOL_SCHEMA_CACHE.clear()
    _MCP_TOOL_ALIAS_CACHE.clear()
    return result


@tool(perm.TOOL_READ, "List configured MCP servers", must=True)
def mcp_list_servers() -> dict[str, Any]:
    return list_configured_mcp_servers()


@tool(perm.TOOL_READ, "List tools exposed by a configured MCP server. Parameters: server", must=True)
def mcp_list_tools(server: str) -> dict[str, Any]:
    return list_remote_mcp_tools(server)


@tool(perm.TOOL_EXECUTE, "Call a tool on a configured MCP server. Parameters: server, tool_name, arguments_json optional", must=True)
def mcp_call_tool(server: str, tool_name: str, arguments_json: str = "{}") -> dict[str, Any]:
    args = json.loads(arguments_json) if str(arguments_json or "").strip() else {}
    if not isinstance(args, dict):
        raise ValueError("arguments_json must be a JSON object")
    return call_mcp_tool(server, tool_name, args)


@tool(perm.TOOL_READ | perm.PUBLIC_READ, "List local TindaAgent skills from runtime skills directory and TINDA_SKILL_PATHS", must=True)
def skill_list() -> dict[str, Any]:
    return discover_skills()


@tool(perm.TOOL_READ | perm.PUBLIC_READ, "Read one local skill instruction file. Parameters: name", must=True)
def skill_read(name: str) -> dict[str, Any]:
    return read_skill(name)


@tool(perm.USER_ADMIN, "No-op tool for admin permission verification only", must=True)
def admin_noop() -> dict[str, Any]:
    """
    用于权限系统联调：该工具不执行任何业务逻辑，仅返回固定结果。
    """
    return {"ok": True, "message": "admin_noop executed"}


# 子进程不应继承的敏感环境变量
_SENSITIVE_ENV_KEYS = frozenset({
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "DEEPSEEK_BASE_URL", "OPENAI_BASE_URL",
    "TINDA_API_KEY", "TINDA_USER_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN", "GITLAB_TOKEN",
})


def _safe_env() -> dict[str, str]:
    """返回过滤掉敏感变量后的环境变量副本。"""
    return {
        k: v for k, v in os.environ.items()
        if k.upper() not in _SENSITIVE_ENV_KEYS
        and "KEY" not in k.upper().split("_")
        and "TOKEN" not in k.upper().split("_")
        and "SECRET" not in k.upper().split("_")
    }


@tool(perm.TOOL_EXECUTE | perm.PUBLIC_EXECUTE, "Execute a shell command in terminal. Parameters: cmd=command string, supports multiline bash/heredoc; note=purpose (max 80 chars); cwd=working dir (optional). Long-running commands stay connected through heartbeat/progress events. System operations (rm/mv/chmod etc) require SYSTEM_EXECUTE permission.", must=True)
def run_terminal(
    cmd: str = "",
    cwd: str | None = None,
    command: str | None = None,
    note: str = "",
    _caller_perm: int = 0,
    _approval: bool | None = None,
    call_id: str = "",
) -> dict[str, Any]:
    command = str(cmd or command or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    note_text = str(note or "").strip()[:80]
    cwd_info = ""

    if not command:
        return {"ok": False, "error": "cmd is required", "cmd": "", "note": note_text}

    blacklisted = terminal_policy.check_blacklist(command)
    if blacklisted:
        return {"ok": False, "error": f"Command blocked by blacklist: {', '.join(blacklisted)}",
                "cmd": command, "note": note_text}

    sys_ops = terminal_policy.detect_system_operations(command)
    needs_sys_perm = bool(sys_ops) and (_caller_perm & perm.SYSTEM_EXECUTE) != perm.SYSTEM_EXECUTE

    approval = _approval if isinstance(_approval, bool) else None

    if approval is False:
        return {
            "ok": False,
            "error": "Execution denied by user",
            "error_code": "user_denied",
            "pending_confirmation": False,
            "cmd": command,
            "note": note_text,
            "approval": False,
            "returncode": None,
            "output": "Execution denied by user",
        }

    if not terminal_policy.is_bypass_enabled(_caller_perm) or needs_sys_perm:
        if approval is None:
            import uuid
            _confirm_id = str(call_id).strip() if str(call_id).strip() else f"tcf_{uuid.uuid4().hex[:12]}"
            return {
                "ok": True,
                "pending_confirmation": True,
                "confirm_id": _confirm_id,
                "call_id": _confirm_id,
                "cmd": command,
                "note": note_text,
                "approval": None,
                "message": f"Command '{command}' is waiting for user confirmation.",
            }

    proc: subprocess.Popen[str] | None = None
    try:
        work_dir = str(cwd).strip() if cwd else None
        if work_dir and not Path(work_dir).is_dir():
            cwd_info = f" (cwd 不存在，已用当前目录)"
            work_dir = None
        exec_cwd = work_dir or os.getcwd()
        shell_path = shutil.which("bash") or "/bin/sh"
        proc = subprocess.Popen(
            command,
            shell=True,
            executable=shell_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=work_dir,
            env={**_safe_env(), "PYTHONUNBUFFERED": "1"},
            start_new_session=True,
        )
        register_running_tool(call_id, proc)
        stdout_raw, stderr_raw = proc.communicate()
        returncode = proc.returncode if proc.returncode is not None else 0
        out = stdout_raw + stderr_raw
        if len(out) > 8000:
            out = out[:8000] + "\n...(output truncated)"
        safe_stdout = redact_sensitive_text(stdout_raw)
        safe_stderr = redact_sensitive_text(stderr_raw)
        safe_output = redact_sensitive_text(out.strip() or "(no output)")
        ret = {
            "ok": returncode == 0,
            "success": returncode == 0,
            "cmd": command,
            "note": note_text,
            "cwd": exec_cwd,
            "shell": shell_path,
            "stdout": safe_stdout,
            "stderr": safe_stderr,
            "returncode": returncode,
            "output": safe_output,
            "pending_confirmation": False,
            "approval": True if approval is True else approval,
        }
        if returncode != 0:
            ret["error"] = f"Command failed with exit code {returncode}"
        if cwd_info:
            ret["cwd_note"] = cwd_info
        return ret
    except Exception as e:
        return {"ok": False, "error": str(e), "cmd": command[:120], "note": note_text}
    finally:
        clear_running_tool(call_id)


def _parse_event_id(raw: str | int | None) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    # 兼容日志页常见前缀写法：log#123 / log-123 / log:123
    text = re.sub(r"^\s*log[\s:_-]*#?\s*", "", text, flags=re.IGNORECASE)
    text = text.lstrip("#")
    if text.lower().startswith("tc_"):
        text = text[3:]
    if not re.fullmatch(r"\d{1,18}", text):
        return None
    try:
        value = int(text)
    except Exception:
        return None
    if value <= 0:
        return None
    return value


def _iter_total_jsonl_candidates() -> list[Path]:
    """
    汇总所有可能含有审计事件的文件,顺序:
      1. 当前 get_log_root() / total.jsonl
      2. legacy log_root / total.jsonl
      3. 每个 root 下的 total.*.jsonl.gz 归档(按文件名时间倒序,优先扫最新)
    """
    paths: list[Path] = []
    seen: set[Path] = set()

    def _push(p: Path) -> None:
        try:
            r = p.resolve()
        except Exception:
            r = p
        if r in seen:
            return
        seen.add(r)
        paths.append(p)

    primary = get_log_root() / "total.jsonl"
    if primary.exists() and primary.is_file():
        _push(primary)
    legacy = get_legacy_log_root() / "total.jsonl"
    if legacy.exists() and legacy.is_file():
        _push(legacy)

    # gzip 归档:每个 root 下的 total.*.jsonl.gz,按文件名时间倒序
    roots: list[Path] = []
    try:
        roots.append(get_log_root())
    except Exception:
        pass
    try:
        roots.append(get_legacy_log_root())
    except Exception:
        pass
    seen_roots: set[Path] = set()
    for root in roots:
        try:
            rroot = root.resolve()
        except Exception:
            rroot = root
        if rroot in seen_roots:
            continue
        seen_roots.add(rroot)
        if not root.is_dir():
            continue
        try:
            archives = sorted(root.glob("total.*.jsonl.gz"), reverse=True)
        except Exception:
            archives = []
        for arc in archives:
            if arc.is_file():
                _push(arc)
    return paths


@tool(perm.PUBLIC_READ, "Look up audit log event by ID (numeric or tc_ prefix)", must=True)
def get_log_event_by_id(id: str) -> dict[str, Any]:
    """
    根据审计事件 ID 查询 total.jsonl 及 .jsonl.gz 归档中的原始事件。
    """
    parsed_id = _parse_event_id(id)
    if parsed_id is None:
        raise ValueError("Invalid id, expected numeric or tc_ prefix")

    for path in _iter_total_jsonl_candidates():
        try:
            if path.suffix == ".gz":
                opener = lambda p: gzip.open(p, "rt", encoding="utf-8", errors="ignore")
            else:
                opener = lambda p: p.open("r", encoding="utf-8")
            with opener(path) as fp:
                for line_no, line in enumerate(fp, start=1):
                    row_text = str(line).strip()
                    if not row_text:
                        continue
                    try:
                        row = json.loads(row_text)
                    except Exception:
                        continue
                    try:
                        rid = int(row.get("id", -1))
                    except Exception:
                        continue
                    if rid != parsed_id:
                        continue
                    return {
                        "ok": True,
                        "id": parsed_id,
                        "source_file": str(path.name),
                        "source_line": int(line_no),
                        "event": row,
                    }
        except Exception:
            continue
    return {"ok": False, "id": parsed_id, "error": "id not found"}
