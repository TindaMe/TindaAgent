from __future__ import annotations

import json
import logging
import os
import re
import ipaddress
import calendar
import shutil
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta

from fastapi import Body
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from TindaAgent.Process.AI.agent import Agent
from TindaAgent.Process.AI.client import has_tool_protocol_artifacts, strip_tool_protocol_artifacts
from TindaAgent.Process.AI.dispatcher import LlmDispatcher
from TindaAgent.Process.Versioning import get_version_manager
from TindaAgent.Process.Security import (
    get_current_principal,
    get_current_user as sec_get_current_user,
    has_perm as sec_has_perm,
    push_current_user,
    reset_current_user,
)
from TindaAgent.Permission import perm_labels
from TindaAgent.User import userdata
from TindaAgent.Process.Architecture import perm
from TindaAgent.Process.Architecture.versioning import get_app_version
from TindaAgent.Process.Architecture.migration import bootstrap_storage
from TindaAgent.Process.Architecture.paths import (
    get_chat_records_root,
    get_legacy_log_root,
    get_log_root,
    get_legacy_sessions_root,
    get_runtime_root,
    get_sessions_root,
    get_system_root,
    get_users_file,
)
from TindaAgent.Process.Observability import audit_event
from TindaAgent.Web.session_store import SessionStore, SessionStoreError, cleanup_legacy_chat_records
from TindaAgent.Web.session_sqlite_index import SessionSQLiteIndex
from TindaAgent.Web import session_adapter as sa
from TindaAgent.Web.settings_backend import (
    load_web_settings, save_web_settings,
    validate_context_token_limit,
    get_restore_last_session, get_last_session_id, set_last_session_id,
    load_terminal_settings, save_terminal_settings,
)
from TindaAgent.Web.tool_runtime import ToolRuntimeManager

app = FastAPI()

_cors_origins_env = str(os.getenv("TINDA_CORS_ORIGINS", "")).strip()
_CORS_ALLOW_ORIGINS = (
    [x.strip() for x in _cors_origins_env.split(",") if x.strip()]
    if _cors_origins_env
    else ["http://localhost", "http://127.0.0.1"]
)
_CORS_LOCAL_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ALLOW_ORIGINS,
    allow_origin_regex=_CORS_LOCAL_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("tinda.web")
_THIS_FILE = str(Path(__file__).resolve())


def _infer_http_op_type(method: str, path: str) -> str:
    m = str(method or "").upper()
    p = str(path or "")
    if "/tool" in p:
        return "TOOL_EXECUTE"
    if p.startswith("/admin") or p.startswith("/auth") or p.startswith("/user") or p.startswith("/users"):
        return "SYSTEM_EXECUTE" if m != "GET" else "SYSTEM_READ"
    if p.startswith("/chat") or p.startswith("/model"):
        return "SYSTEM_EXECUTE" if m != "GET" else "SYSTEM_READ"
    if m == "GET":
        return "PUBLIC_READ"
    return "PUBLIC_WRITE"

_llm = LlmDispatcher()
_client = _llm.primary_client
_tool_client = _llm.tool_client


def _find_first_reasoning(agent: Agent) -> str | None:
    """Return the FIRST assistant reasoning in history — the initial thinking that triggered tool calls."""
    for m in (getattr(agent, "history", []) or []):
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("reasoning_content"):
            return str(m["reasoning_content"])
    return None


def _build_substeps_from_history(agent: Agent, tool_trace: list[dict] | None) -> list[dict]:
    """Reconstruct execution-order substeps from agent's internal history.
    This captures the REAL sequence: thinking → text → tool → thinking → text → tool → ...
    No hardcoded pattern — mirrors exactly what the LLM produced per internal turn.
    """
    history = getattr(agent, "history", []) or []
    tool_trace = tool_trace or []
    # Build ordered list of tool trace steps (preserves execution order)
    ordered_trace: list[dict] = []
    for step in (tool_trace or []):
        if isinstance(step, dict):
            ordered_trace.append(step)
    trace_idx = 0

    substeps: list[dict] = []
    # Only process messages from the current turn:
    # find the position right after the last user message in history.
    user_idx = -1
    for i in range(len(history) - 1, -1, -1):
        if isinstance(history[i], dict) and history[i].get("role") == "user":
            user_idx = i
            break
    turn_start = user_idx + 1 if user_idx >= 0 else len(getattr(agent, "_build_base_history", lambda: [])())
    for m in history[turn_start:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        if role != "assistant":
            continue
        # reasoning → thinking substep
        rc = m.get("reasoning_content")
        if rc and str(rc).strip():
            substeps.append({"kind": "thinking", "content": str(rc).strip()})
        # text content (non-tool-call part)
        text = m.get("content", "")
        if isinstance(text, str) and text.strip():
            clean_text = _sanitize_assistant_visible_text(text)
            if clean_text.strip():
                substeps.append({"kind": "text", "content": clean_text.strip()})
        # tool_calls → tool_marker substeps (matched by execution order)
        tool_calls = m.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {})
                if not isinstance(fn, dict):
                    fn = {}
                name = str(fn.get("name", "unknown"))
                cid = str(tc.get("id", "") or "").strip()
                raw_args = fn.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    parsed_args = {}
                # Match by execution order: consume trace steps in sequence
                tinfo = {}
                if trace_idx < len(ordered_trace):
                    tinfo = ordered_trace[trace_idx]
                    trace_idx += 1
                trace_cid = str(tinfo.get("call_id", "") or "").strip() if isinstance(tinfo, dict) else ""
                use_cid = trace_cid or cid
                tresult = tinfo.get("result", {}) if isinstance(tinfo, dict) else {}
                ok = _tool_result_ok(tresult)
                stdout = _tool_result_output(tresult)
                stdin = (
                    str(parsed_args.get("cmd") or parsed_args.get("text") or parsed_args.get("key") or "")
                    if isinstance(parsed_args, dict)
                    else ""
                )
                model_id = str(tinfo.get("tool_call_id", "") or cid).strip() if isinstance(tinfo, dict) else cid
                substeps.append({
                    "kind": "tool_marker",
                    "name": name,
                    "ok": bool(ok),
                    "stdin": stdin[:500],
                    "stdout": stdout[:500],
                    "id": use_cid.lstrip("tc_"),
                    "tool_call_id": model_id,
                    "status": "done",
                    "arguments": parsed_args if isinstance(parsed_args, dict) else {},
                    "result": tresult if isinstance(tresult, dict) else {},
                })
    return substeps


def _get_aux_client(model_key: str, env_var: str, default_model: str):
    """Lazily get or create an auxiliary LLM client.

    Resolution order: web settings → env var → default.
    Clients are cached by model name so settings changes take effect
    without recreating on every call.
    """
    model = (
        str(load_web_settings().get(model_key, "")).strip()
        or os.getenv(env_var, "").strip()
        or default_model
    )
    return _llm.get_client(model=model, provider="deepseek", purpose=model_key)
_version_mgr = get_version_manager()

_MIGRATION = bootstrap_storage()
_SESSIONS_ROOT = get_sessions_root()
_store = SessionStore(_SESSIONS_ROOT, legacy_root_dir=get_legacy_sessions_root())
_sqlite_index = SessionSQLiteIndex(get_system_root() / "session_index.sqlite3")
_tool_runtime = ToolRuntimeManager()


def _invalidate_session_index(session_id: str) -> None:
    try:
        _sqlite_index.delete_session(str(session_id or "").strip())
    except Exception:
        pass

_cleanup_flag_file = _SESSIONS_ROOT / ".legacy_cleaned"
if not _cleanup_flag_file.exists():
    cleanup_legacy_chat_records(get_chat_records_root())
    _cleanup_flag_file.parent.mkdir(parents=True, exist_ok=True)
    _cleanup_flag_file.write_text("ok\n", encoding="utf-8")

# 每次启动清理空会话（若开启恢复上次会话则保留最后会话）
_protected_session_id = ""
if get_restore_last_session():
    _protected_session_id = get_last_session_id()
_empties = _store.cleanup_empty_sessions(protect_session_id=_protected_session_id or None)
if _empties:
    logger.info("cleaned up %d empty sessions", _empties)
_orphans = _store.cleanup_orphan_messages()
if _orphans:
    logger.info("cleaned up %d orphan message files", _orphans)
_normalized_sessions = _store.normalize_all_sessions()
if _normalized_sessions:
    logger.info("normalized %d session message files", _normalized_sessions)

_sessions: dict[str, Agent] = {}
_session_last_access: dict[str, float] = {}
_session_config: dict[str, dict] = {}


def _effective_context_token_limit(session_id: str) -> int:
    sid = str(session_id or "").strip()
    cfg = _session_config.get(sid, {}) if sid else {}
    from TindaAgent.Web.settings_backend import get_context_token_limit
    try:
        value = int(get_context_token_limit())
    except Exception:
        value = 16000
    if value >= 16000:
        return int(value)
    value = cfg.get("max_context_tokens")
    if isinstance(value, int) and 16000 <= value <= 200000:
        return int(value)
    return 16000
_MAX_SESSIONS = 300
_LLM_EXECUTE_PERM = int(perm.PUBLIC_EXECUTE)
_terminal_pending_lock = threading.Lock()
_terminal_pending: dict[str, list[dict]] = {}

_MODEL_CHOICES: tuple[dict[str, str], ...] = (
    {"id": "deepseek-chat", "label": "deepseek-chat"},
    {"id": "deepseek-v4-pro", "label": "deepseek-pro"},
    {"id": "deepseek-reasoner", "label": "deepseek-reasoner"},
    {"id": "deepseek-v4-flash", "label": "deepseek-v4-flash"},
)
_MODEL_ALIAS: dict[str, str] = {
    "deepseek-chat": "deepseek-chat",
    "chat": "deepseek-chat",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-pro": "deepseek-v4-pro",
    "pro": "deepseek-v4-pro",
    "deepseek-reasoner": "deepseek-reasoner",
    "reasoner": "deepseek-reasoner",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "v4-flash": "deepseek-v4-flash",
    "flash": "deepseek-v4-flash",
}

_APP_VERSION = get_app_version()

def _load_html(file_name: str) -> str:
    try:
        return (Path(__file__).parent / file_name).read_text(encoding="utf-8")
    except Exception:
        return f"<!-- {file_name} not found -->"

_HTML_HOME = _load_html("home.html")
_HTML_CHAT = _load_html("chat.html")
_HTML_SETTINGS = _load_html("settings.html")
_HTML_USER_ADMIN = _load_html("user_admin.html")
_HTML_LOG_VIEW = _load_html("logs.html")
_HTML_MODEL_DIAGNOSTICS = _load_html("model_diagnostics.html")
_HTML_LLM_REQUEST = _load_html("llm_request.html")
_JS_CHAT_RENDERER = _load_html("chat_renderer.js")
_JS_MARKDOWN_RENDERER = _load_html("markdown_renderer.js")
_JS_THEME_TOGGLE = _load_html("theme_toggle.js")
_LOG_ROOT = get_log_root()
_CONTEXT_LOG_FILE = _LOG_ROOT / "llm_context.jsonl"
_LLM_REQUEST_LOG_FILE = Path(os.getenv("TINDA_LLM_REQUEST_LOG", str(_LOG_ROOT / "llm_request.jsonl")))
_LOG_MAX_READ_BYTES = 2 * 1024 * 1024
_SERVER_STARTED_AT = time.time()
_HOME_USAGE_FILE = get_system_root() / "home_usage.jsonl"
_CHANGELOG_FILE = Path(__file__).resolve().parents[1] / "docs" / "CHANGELOG.md"
_usage_lock = threading.Lock()


def _safe_percent(used: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (used / total) * 100.0)), 1)


def _read_proc_memory() -> dict[str, object]:
    try:
        meminfo: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                meminfo[parts[0].rstrip(":")] = int(parts[1]) * 1024
        total = int(meminfo.get("MemTotal", 0))
        available = int(meminfo.get("MemAvailable", 0))
        used = max(0, total - available)
        return {
            "scope": "system",
            "source": "/proc/meminfo",
            "total": total,
            "used": used,
            "available": available,
            "percent": _safe_percent(used, total),
        }
    except Exception:
        pass
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        page_count = int(os.sysconf("SC_PHYS_PAGES"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        total = max(0, page_size * page_count)
        available = max(0, page_size * available_pages)
        used = max(0, total - available)
        return {
            "scope": "system",
            "source": "os.sysconf",
            "total": total,
            "used": used,
            "available": available,
            "percent": _safe_percent(used, total),
        }
    except Exception:
        return {"scope": "system", "source": "unavailable", "total": 0, "used": 0, "available": 0, "percent": 0.0}


def _read_process_memory() -> dict[str, object]:
    try:
        statm = Path("/proc/self/statm").read_text(encoding="utf-8", errors="ignore").split()
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        size = int(statm[0]) * page_size if len(statm) >= 1 else 0
        resident = int(statm[1]) * page_size if len(statm) >= 2 else 0
        return {"scope": "process", "source": "/proc/self/statm", "rss": resident, "vms": size}
    except Exception:
        return {"scope": "process", "source": "unavailable", "rss": 0, "vms": 0}


def _read_load_average() -> dict[str, object]:
    try:
        one, five, fifteen = os.getloadavg()
        return {"source": "os.getloadavg", "one": round(one, 2), "five": round(five, 2), "fifteen": round(fifteen, 2)}
    except Exception:
        return {"source": "unavailable", "one": None, "five": None, "fifteen": None}


def _disk_usage_payload(path: Path, *, label: str, scope: str, is_runtime: bool = False) -> dict[str, object] | None:
    try:
        disk = shutil.disk_usage(path)
        return {
            "label": label,
            "scope": scope,
            "source": "shutil.disk_usage",
            "root": str(path),
            "total": int(disk.total),
            "used": int(disk.used),
            "free": int(disk.free),
            "percent": _safe_percent(float(disk.used), float(disk.total)),
            "is_runtime": bool(is_runtime),
        }
    except Exception:
        return None


def _read_storage_volumes() -> list[dict[str, object]]:
    runtime_root = get_runtime_root()
    volumes: list[dict[str, object]] = []
    seen_roots: set[str] = set()

    for letter in "abcdefghijklmnopqrstuvwxyz":
        mount = Path("/mnt") / letter
        if not mount.exists():
            continue
        payload = _disk_usage_payload(
            mount,
            label=letter.upper(),
            scope="windows_drive",
            is_runtime=str(runtime_root).startswith(str(mount) + os.sep) or runtime_root == mount,
        )
        if payload and str(payload["root"]) not in seen_roots:
            volumes.append(payload)
            seen_roots.add(str(payload["root"]))

    if not any(bool(item.get("is_runtime")) for item in volumes):
        payload = _disk_usage_payload(runtime_root, label="运行", scope="runtime_root", is_runtime=True)
        if payload and str(payload["root"]) not in seen_roots:
            volumes.append(payload)
            seen_roots.add(str(payload["root"]))

    return volumes


def _usage_event_from_request(request: Request) -> dict[str, object] | None:
    path = str(request.url.path or "")
    method = str(request.method or "GET").upper()
    if method not in {"GET", "POST"}:
        return None
    if path.startswith(("/static/", "/assets/")) or path in {
        "/favicon.ico",
        "/chat_renderer.js",
        "/markdown_renderer.js",
        "/theme_toggle.js",
    }:
        return None
    if path.startswith(("/home/stats", "/home/changelog")):
        return None
    return {
        "ts": _now_iso(),
        "path": path,
        "method": method,
    }


def _record_usage_event(request: Request) -> None:
    event = _usage_event_from_request(request)
    if not event:
        return
    try:
        line = json.dumps(event, ensure_ascii=False)
        with _usage_lock:
            _HOME_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _HOME_USAGE_FILE.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
    except Exception as e:
        logger.debug("home usage record failed: %s", e)


def _load_usage_events(days: int = 370) -> list[dict[str, object]]:
    cutoff = datetime.now().astimezone() - timedelta(days=max(1, int(days)))
    rows: list[dict[str, object]] = []
    try:
        if not _HOME_USAGE_FILE.exists():
            return rows
        with _HOME_USAGE_FILE.open("r", encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                try:
                    item = json.loads(line)
                    ts = datetime.fromisoformat(str(item.get("ts", "")).strip())
                    if ts >= cutoff:
                        rows.append({"ts": ts, "path": str(item.get("path", "")), "method": str(item.get("method", ""))})
                except Exception:
                    continue
    except Exception:
        return rows
    return rows


def _build_month_days(year: int, month: int, events: list[dict[str, object]]) -> list[dict[str, object]]:
    _, day_count = calendar.monthrange(year, month)
    counts = {day: 0 for day in range(1, day_count + 1)}
    for item in events:
        ts = item.get("ts")
        if isinstance(ts, datetime) and ts.year == year and ts.month == month:
            counts[ts.day] = counts.get(ts.day, 0) + 1
    max_count = max(counts.values() or [0])
    out: list[dict[str, object]] = []
    for day in range(1, day_count + 1):
        count = int(counts.get(day, 0))
        level = 0 if count <= 0 else max(1, min(4, int(round((count / max(max_count, 1)) * 4))))
        out.append({"date": f"{year:04d}-{month:02d}-{day:02d}", "count": count, "level": level})
    return out


def _build_usage_24h(events: list[dict[str, object]]) -> list[dict[str, object]]:
    now = datetime.now().astimezone()
    start = now - timedelta(hours=24)
    buckets = [{"label": f"{(start + timedelta(hours=i * 3)).hour:02d}:00", "count": 0} for i in range(8)]
    for item in events:
        ts = item.get("ts")
        if not isinstance(ts, datetime) or ts < start or ts > now:
            continue
        idx = min(7, max(0, int((ts - start).total_seconds() // (3 * 3600))))
        buckets[idx]["count"] = int(buckets[idx]["count"]) + 1
    max_count = max([int(x["count"]) for x in buckets] or [0])
    for bucket in buckets:
        count = int(bucket["count"])
        bucket["percent"] = 0 if max_count <= 0 else round((count / max_count) * 100, 1)
    return buckets


def _serialize_context_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        item = {"role": str(m.get("role", "")), "content": str(m.get("content", "") or "")}
        rc = m.get("reasoning_content")
        if rc is not None and str(rc).strip():
            item["reasoning_content"] = rc
        out.append(item)
    return out


def _write_context_log(session_id: str, messages: list[dict], *, model: str = "", trigger: str = "") -> None:
    try:
        payload = json.dumps(
            {
                "ts": _now_iso(),
                "session_id": str(session_id),
                "trigger": str(trigger),
                "model": str(model),
                "message_count": len(messages),
                "messages": messages,
            },
            ensure_ascii=False,
        )
        with _CONTEXT_LOG_FILE.open("a", encoding="utf-8") as fp:
            fp.write(payload + "\n")
    except Exception as e:
        logger.warning("_write_context_log failed: %s", e)


def _read_latest_jsonl(path: Path, *, max_bytes: int = _LOG_MAX_READ_BYTES) -> dict[str, Any] | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        size = path.stat().st_size
        with path.open("rb") as fp:
            if size > max_bytes:
                fp.seek(max(0, size - max_bytes))
            raw = fp.read().decode("utf-8", errors="ignore")
        for line in reversed(raw.splitlines()):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
                return item if isinstance(item, dict) else {"value": item}
            except Exception:
                continue
    except Exception as e:
        logger.warning("_read_latest_jsonl failed path=%s err=%s", path, e)
    return None


def _llm_request_summary(row: dict[str, Any]) -> dict[str, Any]:
    from TindaAgent.Process.AI.tokenizer import estimate_request_token_usage

    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    messages = payload.get("messages") if isinstance(payload, dict) else []
    tools = payload.get("tools") if isinstance(payload, dict) else []
    try:
        payload_chars = len(json.dumps(payload, ensure_ascii=False))
    except Exception:
        payload_chars = len(str(payload or ""))
    content_chars = 0
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                content_chars += len(content)
            else:
                try:
                    content_chars += len(json.dumps(content, ensure_ascii=False))
                except Exception:
                    content_chars += len(str(content or ""))
    token_usage = row.get("token_usage") if isinstance(row.get("token_usage"), dict) else None
    if not token_usage:
        token_usage = estimate_request_token_usage(payload)
    request_tokens = int(row.get("request_tokens") or token_usage.get("total", 0) or 0)
    payload_tokens = int(row.get("payload_tokens") or token_usage.get("payload", 0) or 0)
    tokenizer_info = token_usage.get("tokenizer") if isinstance(token_usage.get("tokenizer"), dict) else {}
    response_usage = row.get("response_usage") if isinstance(row.get("response_usage"), dict) else {}
    return {
        "ts": str(row.get("ts", "")),
        "source": str(row.get("source", "")),
        "stream": bool(row.get("stream", False)),
        "model": str(row.get("model") or payload.get("model", "")),
        "message_count": len(messages) if isinstance(messages, list) else int(row.get("message_count", 0) or 0),
        "tool_count": len(tools) if isinstance(tools, list) else int(row.get("tool_count", 0) or 0),
        "payload_chars": payload_chars,
        "content_chars": content_chars,
        "estimated_tokens": request_tokens,
        "request_tokens": request_tokens,
        "payload_tokens": payload_tokens,
        "message_tokens": int(token_usage.get("messages", 0) or 0),
        "tool_tokens": int(token_usage.get("tools", 0) or 0),
        "token_source": str(token_usage.get("source") or ("api_usage" if response_usage else "official_tokenizer")),
        "response_prompt_tokens": response_usage.get("prompt_tokens"),
        "response_completion_tokens": response_usage.get("completion_tokens"),
        "response_total_tokens": response_usage.get("total_tokens"),
        "tokenizer_engine": str(tokenizer_info.get("engine", "")),
        "tokenizer_official_files": bool(tokenizer_info.get("official_files", False)),
        "temperature": payload.get("temperature") if isinstance(payload, dict) else None,
        "tool_choice": payload.get("tool_choice") if isinstance(payload, dict) else None,
    }


def _mask_api_key(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return "***" if text else ""
    return f"{text[:4]}...{text[-4:]}"


def _deepseek_api_root() -> str:
    return _llm.deepseek_api_root


def _normalize_model_provider(raw: object = None) -> str:
    return _llm.normalize_provider(raw)


def _model_provider_meta(provider: str = "deepseek") -> dict[str, Any]:
    return _llm.provider_meta(provider)


def _model_providers_payload() -> dict[str, Any]:
    return _llm.providers_payload()


def _fetch_deepseek_balance() -> dict[str, Any]:
    return _llm.fetch_deepseek_balance(mask_api_key=_mask_api_key)


def _model_payload() -> dict[str, Any]:
    providers_payload = _llm.providers_payload()
    return {
        "ok": True,
        "current_provider": _llm.current_provider,
        "current_model": _llm.current_model,
        "available_models": _llm.available_models() or list(_MODEL_CHOICES),
        "all_models": _llm.available_models(),
        "providers": providers_payload.get("providers", []),
    }


def _pydantic_to_dict(model: BaseModel) -> dict[str, Any]:
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        return dump()
    return model.dict()
_AUTH_OPEN_PATHS = {
    "/",
    "/home",
    "/home/changelog",
    "/home/stats",
    "/chat",
    "/app",
    "/settings",
    "/logs",
    "/model-diagnostics",
    "/model-data",
    "/llm-request",
    "/favicon.ico",
    "/system/version",
    "/user-admin",
    "/auth/status",
    "/auth/select-user",
    "/auth/local-users",
    "/auth/local-login",
    "/chat_renderer.js",
    "/markdown_renderer.js",
    "/theme_toggle.js",
}


def _normalize_version_text(value: object) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("v"):
        return text[1:]
    return text


def _detect_app_version_from_path(app_path: str) -> str:
    raw = str(app_path or "").strip()
    if not raw:
        return ""
    root = Path(raw)
    candidates = [root / "pyproject.toml", root.parent / "pyproject.toml"]
    version_re = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)
    for cand in candidates:
        try:
            if not cand.exists():
                continue
            text = cand.read_text(encoding="utf-8")
            m = version_re.search(text)
            if m:
                return _normalize_version_text(m.group(1))
        except Exception:
            continue
    return ""


def _build_runtime_version_state() -> dict[str, object]:
    # 以当前运行代码版本为准，避免被历史 current.json 指针污染前端显示。
    current = _version_mgr.get_current()
    source = str(current.get("source", "local"))
    verified = bool(current.get("verified", False))
    selected_version_raw = _normalize_version_text(current.get("version", ""))
    runtime_version = _detect_app_version_from_path(str(current.get("app_path", ""))) or _normalize_version_text(_APP_VERSION) or selected_version_raw
    runtime_app_path = str(Path(__file__).resolve().parents[2])
    version_consistent = not (runtime_version and selected_version_raw and runtime_version != selected_version_raw)

    # 产品策略：切换禁用时，不保留“已选版本”历史错位；检测到错位自动对齐 current.json。
    if not version_consistent:
        try:
            aligned = _version_mgr.align_current_to_runtime(runtime_version, runtime_app_path, keep_switched_at=True)
            current = aligned if isinstance(aligned, dict) else _version_mgr.get_current()
            source = str(current.get("source", "local"))
            verified = bool(current.get("verified", False))
            selected_version_raw = _normalize_version_text(current.get("version", ""))
            version_consistent = True
        except Exception:
            # 对齐失败不影响接口可用性，仍以运行时版本继续返回。
            pass

    selected_version = runtime_version
    if source in {"local", "local_snapshot"}:
        verify_label = "本地开发版（未签名）" if source == "local" else "本地快照版（未签名）"
    else:
        verify_label = "已签名验证" if verified else "签名未验证"
    source_label = {
        "local": "本地源码",
        "local_snapshot": "本地快照",
        "github_releases": "GitHub Release",
    }.get(source, source)
    return {
        "running_version": runtime_version,
        "running_display": f"v{runtime_version}" if runtime_version else "",
        "version": runtime_version,
        "display": f"v{runtime_version}" if runtime_version else "",
        "effective_version": runtime_version,
        "effective_display": f"v{runtime_version}" if runtime_version else "",
        "app_version": _APP_VERSION,
        "selected_version": selected_version,
        "selected_display": f"v{selected_version}" if selected_version else "",
        "selected_version_raw": selected_version_raw,
        "version_consistent": version_consistent,
        "signature_id": str(current.get("signature_id", "")),
        "verified": verified,
        "verify_label": verify_label,
        "source": source,
        "source_label": source_label,
        "current_path": runtime_app_path,
        "switched_at": str(current.get("switched_at", "")),
        "switch_enabled": True,
    }


class ChatRequest(BaseModel):
    message: str
    session_id: str
    file_names: list[str] = Field(default_factory=list)
    file_contents: list[str] = Field(default_factory=list)
    meta_user_name: str | None = None
    meta_user_id: str | None = None
    meta_user_perm: str | None = None
    meta_time_iso: str | None = None
    meta_time_text: str | None = None


class ModelSwitchRequest(BaseModel):
    model: str
    provider: str | None = None


class ModelProviderUpsertRequest(BaseModel):
    key: str
    label: str | None = None
    name: str | None = None
    adapter: str = "openai_compatible"
    base_url: str | None = None
    chat_path: str | None = None
    api_key_env: str | None = None
    api_key: str | None = None
    enabled: bool = True
    anthropic_version: str | None = None


class ModelAddRequest(BaseModel):
    provider: str = "deepseek"
    model_id: str
    label: str | None = None


class SessionCreateRequest(BaseModel):
    title: str | None = "新对话"
    session_id: str | None = None
    current_session_id: str | None = None
    reuse_if_current_empty: bool = False


class SessionTitleRequest(BaseModel):
    title: str


class ToolJobCreateRequest(BaseModel):
    session_id: str
    command: str


class SessionEventsRequest(BaseModel):
    session_id: str
    entries: list[dict] = Field(default_factory=list)


class ResetRequest(BaseModel):
    session_id: str | None = None


class TerminalConfirmRequest(BaseModel):
    session_id: str
    approval: bool
    call_id: str | None = None
    cmd: str | None = None


class UserProfileResponse(BaseModel):
    name: str
    uid: str
    perm: int
    perm_label: str
    token: str


class UserSwitchRequest(BaseModel):
    uid: str


class UserCreateRequest(BaseModel):
    name: str
    perm: int
    token: str | None = None


class UserUpdateRequest(BaseModel):
    name: str | None = None
    perm: int | None = None
    token: str | None = None


class UserPermUpdateRequest(BaseModel):
    perm: int


class VersionInstallRequest(BaseModel):
    version: str


class VersionSwitchRequest(BaseModel):
    version: str


class VersionSnapshotRequest(BaseModel):
    version: str


class VersionSnapshotCurrentRequest(BaseModel):
    force: bool = False


class ModelDiagnosticsRequest(BaseModel):
    model: str | None = None
    provider: str | None = None
    tests: list[str] = Field(default_factory=list)
    image_url: str | None = None
    video_url: str | None = None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_model_choice(raw: str | None) -> str | None:
    key = str(raw or "").strip().lower()
    if not key:
        return None
    return _MODEL_ALIAS.get(key) or str(raw or "").strip()


def _sanitize_diagnostic_url(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    low = text.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return ""
    return text


def _truncate_context_preview(text: str, max_chars: int = 300) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if len(raw) > int(max_chars):
        raw = raw[: int(max_chars)] + "..."
    # v1.8.2: 上下文预览写入审计前必须脱敏
    from TindaAgent.Process.Observability.audit import redact_sensitive_text
    return redact_sensitive_text(raw)


def _estimate_context_usage_length(rows: list[dict]) -> int:
    from TindaAgent.Process.AI.tokenizer import estimate_request_messages_tokens

    llm_roles = {"system", "user", "assistant", "tool"}
    messages: list[dict] = []
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        role = str(row.get("role", "") or "").strip()
        if role not in llm_roles:
            continue
        entry_type = str(row.get("entry_type", "") or "").strip()
        if entry_type in {"terminal", "tool_marker"}:
            continue
        content = "" if row.get("content") is None else str(row.get("content", ""))
        has_tool_calls = isinstance(row.get("tool_calls"), list) and bool(row.get("tool_calls"))
        if role == "user" and not content.strip():
            continue
        if role == "assistant" and not content.strip() and not has_tool_calls and not str(row.get("reasoning_content", "") or "").strip():
            continue
        msg = {"role": role, "content": content}
        if role == "assistant":
            rc = row.get("reasoning_content")
            if rc is not None and str(rc).strip():
                msg["reasoning_content"] = str(rc)
            if has_tool_calls:
                msg["tool_calls"] = row.get("tool_calls", [])
        if role == "tool":
            tool_call_id = str(row.get("tool_call_id", "") or "").strip()
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
        messages.append(msg)
    return int(estimate_request_messages_tokens(messages))


def _raw_chat_rows_for_compression(session_id: str, fallback_rows: list[dict] | None = None) -> list[dict]:
    """Return raw user/assistant text rows that are safe to summarize."""
    sid = str(session_id or "").strip()
    try:
        load_effective = getattr(_store, "load_effective_messages", None)
        load_messages = getattr(_store, "load_messages", None)
        loader = load_effective if callable(load_effective) else load_messages
        if callable(loader):
            data = loader(sid)
            if isinstance(data, dict):
                return sa.filter_raw_chat_entries(data)
    except Exception:
        pass

    raw_rows: list[dict] = []
    for row in fallback_rows or []:
        if not isinstance(row, dict):
            continue
        if bool(row.get("is_summary", False)):
            continue
        if str(row.get("entry_type", "chat")) != "chat":
            continue
        role = str(row.get("role", "") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        content = str(row.get("content", "") or "")
        if content.strip():
            raw_rows.append({"role": role, "content": content, "id": str(row.get("id", "") or "")})
    return raw_rows


def _summary_rows_for_compression(session_id: str, raw_rows: list[dict]) -> list[dict]:
    """Build summary input from the currently effective context only."""
    sid = str(session_id or "").strip()
    summary_text = ""
    try:
        meta = _store.get_session(sid) or {}
        latest_summary_id = str(meta.get("latest_summary_message_id", "") or "").strip()
        load_effective = getattr(_store, "load_effective_messages", None)
        if latest_summary_id and callable(load_effective):
            data = load_effective(sid)
            if isinstance(data, dict):
                for entry in data.values():
                    if not isinstance(entry, dict):
                        continue
                    if str(entry.get("id", "") or "") != latest_summary_id:
                        continue
                    content = entry.get("content", {})
                    if isinstance(content, dict):
                        summary_text = str(content.get("text", "") or "")
                    elif isinstance(content, str):
                        summary_text = content
                    break
    except Exception:
        summary_text = ""

    rows: list[dict] = []
    if summary_text.strip():
        rows.append({"role": "system", "content": f"[已有上下文摘要] {summary_text.strip()}"})
    rows.extend(dict(x) for x in raw_rows)
    return rows


def _is_duplicate_compression_request(session_id: str, raw_rows: list[dict]) -> bool:
    if len(raw_rows) < 4:
        return False
    try:
        meta = _store.get_session(session_id) or {}
    except Exception:
        return False
    anchor_id = str(raw_rows[-4].get("id", "") or "")
    return bool(anchor_id and anchor_id == str(meta.get("last_compress_anchor_msg_id", "") or ""))


def _build_context_compression_system_substep(result: dict[str, Any]) -> dict[str, Any]:
    compressed_count = int(result.get("compressed_count", 0) or 0)
    usage_before = int(result.get("usage_before", result.get("estimated_tokens_before", 0)) or 0)
    usage_after = int(result.get("usage_after", result.get("estimated_tokens_after", 0)) or 0)
    max_context_tokens = int(result.get("max_context_tokens", 0) or 0)
    summary = str(result.get("summary", "") or "").strip()
    detail_lines = [f"  压缩 {compressed_count} 条历史消息"]
    if usage_before > 0 and usage_after >= 0:
        detail_lines.append(f"  token {usage_before} -> {usage_after}")
    if max_context_tokens > 0:
        detail_lines.append(f"  阈值 {max_context_tokens}")
    if summary:
        summary_lines = summary.splitlines() or [summary]
        detail_lines.append(f"  摘要: {summary_lines[0]}")
        detail_lines.extend(f"  {line}" for line in summary_lines[1:])
    quoted_lines = [
        "> --正在压缩上下文--",
        ">",
        "> 压缩完成:",
        *[f"> {line}" for line in detail_lines],
    ]
    return {
        "kind": "system",
        "content": {
            "kind": "context_compression",
            "phase": "done",
            "display": "inline",
            "context_policy": "exclude",
            "compressed_count": compressed_count,
            "usage_before": usage_before,
            "usage_after": usage_after,
            "max_context_tokens": max_context_tokens,
            "summary_message_id": str(result.get("summary_message_id", "") or ""),
            "anchor_message_id": str(result.get("anchor_message_id", "") or ""),
            "text": "\n".join(quoted_lines),
        },
    }


def _refresh_agent_context_after_compression(session_id: str) -> None:
    sid = str(session_id or "").strip()
    agent = _sessions.get(sid)
    if agent is None:
        return
    rows = _store.get_context_messages(sid)
    agent_rows, _filter_stats = _store_to_agent_messages(rows)
    agent.replace_conversation(agent_rows)
    agent.max_context_tokens = _effective_context_token_limit(sid)


def _maybe_auto_compress_after_llm(session_id: str) -> dict[str, Any]:
    """Run only after a real LLM request completes and the assistant reply is persisted."""
    sid = str(session_id or "").strip()
    rows = _store.get_context_messages(sid)
    usage_before = int(_estimate_context_usage_length(rows))
    max_tokens = int(_effective_context_token_limit(sid))
    raw_rows = _raw_chat_rows_for_compression(sid, rows)
    chat_count = len(raw_rows)
    if usage_before <= max_tokens:
        return {
            "compressed": False,
            "reason": "below_threshold",
            "usage_before": usage_before,
            "max_context_tokens": max_tokens,
            "chat_count": chat_count,
        }
    if len(raw_rows) < 6:
        return {
            "compressed": False,
            "reason": "insufficient_messages",
            "usage_before": usage_before,
            "max_context_tokens": max_tokens,
            "chat_count": chat_count,
        }
    trigger = "token"
    if _is_duplicate_compression_request(sid, raw_rows):
        return {
            "compressed": False,
            "reason": "already_compressed",
            "usage_before": usage_before,
            "max_context_tokens": max_tokens,
            "chat_count": chat_count,
        }
    try:
        summary_src = _summary_rows_for_compression(sid, raw_rows[:-4])
        summary = _compress_messages_with_llm(summary_src)
        if not summary:
            return {
                "compressed": False,
                "reason": "empty_summary",
                "usage_before": usage_before,
                "max_context_tokens": max_tokens,
                "chat_count": chat_count,
            }
        result = _store.compress_context(sid, summary, display_target="context")
        if isinstance(result, dict):
            result.setdefault("compressed", True)
        else:
            result = {"compressed": True}
        _invalidate_session_index(sid)
        rows_after = _store.get_context_messages(sid)
        usage_after = int(_estimate_context_usage_length(rows_after))
        result.update({
            "usage_before": usage_before,
            "usage_after": usage_after,
            "max_context_tokens": max_tokens,
            "trigger": trigger,
            "summary": summary,
        })
        _refresh_agent_context_after_compression(sid)
        return result
    except Exception as e:
        logger.warning("auto compress failed: session=%s err=%s", sid, e)
        return {
            "compressed": False,
            "reason": "compress_error",
            "usage_before": usage_before,
            "max_context_tokens": max_tokens,
            "chat_count": chat_count,
            "error": str(e),
        }


def _maybe_auto_compress(session_id: str, context_rows: list[dict] | None = None) -> dict[str, Any]:
    """Compatibility wrapper. Auto compression is now only token-threshold based after LLM completion."""
    _ = context_rows
    return _maybe_auto_compress_after_llm(session_id)


def _require_session_access(session_id: str, *, user: userdata.UserManager | None = None,
                            create: bool = True) -> tuple[str, dict]:
    current = user or _require_login()
    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id required")
    get_uid = getattr(current, "get_uid", None)
    uid = str(get_uid() if callable(get_uid) else "")
    if create:
        try:
            meta = _store.ensure_session(sid, owner_uid=uid)
        except TypeError:
            meta = _store.ensure_session(sid)
    else:
        meta = _store.get_session(sid) or {}
    if not meta:
        if create or bool(getattr(_store, "has_message_file", lambda _sid: False)(sid)):
            meta = {"id": sid, "owner_uid": uid}
            try:
                meta = _store.ensure_session(sid, owner_uid=uid)
            except Exception:
                pass
        else:
            raise HTTPException(status_code=404, detail="session not found")
    if not isinstance(meta, dict):
        meta = {"id": sid, "owner_uid": uid}
    owner_uid = str(meta.get("owner_uid", "") or "").strip()
    if owner_uid and owner_uid != uid:
        raise HTTPException(status_code=403, detail="session access denied")
    if not owner_uid:
        try:
            meta = _store.ensure_session(sid, owner_uid=uid)
        except TypeError:
            meta = _store.ensure_session(sid)
    return sid, meta


def _to_diagnostic_fail(test: str, err: Exception) -> dict:
    msg = str(err or "unknown error")
    em = msg.lower()
    status = "fail"
    if "not support" in em or "unsupported" in em or "invalid type" in em:
        status = "unsupported"
    return {
        "test": test,
        "status": status,
        "latency_ms": 0,
        "summary": "执行失败",
        "error_code": "api_error",
        "error_message": msg[:380],
        "raw_excerpt": "",
    }


def _run_model_diagnostic_single(
    *,
    provider: str,
    test_key: str,
    model_name: str,
    image_url: str,
    video_url: str,
) -> dict:
    start = time.perf_counter()
    result = {
        "test": test_key,
        "status": "fail",
        "latency_ms": 0,
        "summary": "",
        "error_code": "",
        "error_message": "",
        "raw_excerpt": "",
    }
    try:
        messages: list[dict] = []
        if test_key == "connectivity":
            messages = [{"role": "user", "content": "请仅回复：PONG"}]
        elif test_key == "reasoning":
            messages = [{"role": "user", "content": "小李比小王大2岁，小王比小张大3岁。请问小李比小张大几岁？"}]
        elif test_key == "image":
            if not image_url:
                result["status"] = "skipped"
                result["summary"] = "未提供 image_url，已跳过"
                return result
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请简短描述这张图片的主要内容。"},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ]
        elif test_key == "video":
            if not video_url:
                result["status"] = "skipped"
                result["summary"] = "未提供 video_url，已跳过"
                return result
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请简短描述这个视频的大意。"},
                        {"type": "input_video", "input_video": {"url": video_url}},
                    ],
                }
            ]
        else:
            result["status"] = "fail"
            result["summary"] = "未知测试项"
            result["error_code"] = "invalid_test"
            result["error_message"] = f"unsupported test: {test_key}"
            return result

        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 120,
            "timeout": 25,
        }
        resp = _llm.create_completion(payload, provider=provider, purpose="model_diagnostics", stream=False)
        msg = resp.choices[0].message
        content = str(getattr(msg, "content", "") or "").strip()
        reasoning = str(getattr(msg, "reasoning_content", "") or "").strip()

        if test_key == "reasoning":
            if reasoning:
                result["status"] = "pass"
                result["summary"] = "检测到 reasoning_content，支持思考输出"
                result["raw_excerpt"] = reasoning[:240]
            else:
                result["status"] = "unsupported"
                result["summary"] = "未检测到 reasoning_content"
                result["raw_excerpt"] = content[:240]
        else:
            if content:
                result["status"] = "pass"
                result["summary"] = "响应成功"
                result["raw_excerpt"] = content[:240]
            else:
                result["status"] = "fail"
                result["summary"] = "响应为空"
                result["error_code"] = "empty_reply"
                result["error_message"] = "model reply is empty"
    except Exception as e:
        fail = _to_diagnostic_fail(test_key, e)
        result.update(fail)
    finally:
        result["latency_ms"] = max(0, int((time.perf_counter() - start) * 1000))
    return result


def _run_model_diagnostics(
    *,
    provider: str,
    model_name: str,
    tests: list[str],
    image_url: str,
    video_url: str,
) -> list[dict]:
    rows: list[dict] = []
    for key in tests:
        started = _audit_web(
            "SYSTEM_EXECUTE",
            "_run_model_diagnostics",
            f"model_diag_start test={key}",
            {"test": key, "provider": provider, "model": model_name},
        )
        row = _run_model_diagnostic_single(
            provider=provider,
            test_key=key,
            model_name=model_name,
            image_url=image_url,
            video_url=video_url,
        )
        rows.append(row)
        _audit_web(
            "SYSTEM_EXECUTE",
            "_run_model_diagnostics",
            f"model_diag_done test={key} status={row.get('status')}",
            {
                "test": key,
                "provider": provider,
                "model": model_name,
                "status": row.get("status"),
                "latency_ms": int(row.get("latency_ms", 0)),
                "parent_audit_id": int(started),
                "error_code": str(row.get("error_code", "")),
            },
        )
    return rows


def _sanitize_meta_value(value: str | None, max_len: int = 240) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if not text:
        return "N/A"
    return text[:max_len]


def _build_user_message_with_meta(
    raw_message: str,
    *,
    meta_user_name: str | None,
    meta_user_id: str | None,
    meta_user_perm: str | None,
    meta_time_iso: str | None,
    meta_time_text: str | None,
) -> str:
    message = str(raw_message or "")
    if "[USER_META]" in message and "[/USER_META]" in message:
        return message

    block = (
        "\n\n---\n"
        "[USER_META]\n"
        f"name: {_sanitize_meta_value(meta_user_name)}\n"
        f"uid: {_sanitize_meta_value(meta_user_id)}\n"
        f"perm: {_sanitize_meta_value(meta_user_perm)}\n"
        f"time_iso: {_sanitize_meta_value(meta_time_iso)}\n"
        f"time_text: {_sanitize_meta_value(meta_time_text)}\n"
        "[/USER_META]"
    )
    return f"{message}{block}" if message else block.strip()


def _strip_user_meta_block(content: str) -> str:
    text = str(content or "")
    marker_start = text.find("\n\n---\n[USER_META]")
    if marker_start >= 0:
        return text[:marker_start].rstrip()
    return text


def _perm_label(p: int) -> str:
    labels = perm_labels(int(p))
    return " | ".join(labels) if labels else ("NONE" if int(p) == 0 else str(p))


_TERMINAL_DUMP_PATTERNS: tuple[str, ...] = (
    r"(?im)^\s*\[tool\]\s+",
    r"(?im)^\s*tool:\s*[a-z_][a-z0-9_]*(?:\s+#tc_\d+)?\s*$",
    r"(?im)^\s*[-]{16,}\s*$",
    r"(?m)^────────────────",
)


def _sanitize_assistant_visible_text(text: str) -> str:
    return strip_tool_protocol_artifacts(str(text or ""))


def _looks_like_terminal_dump(text: str) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
    if has_tool_protocol_artifacts(raw):
        return True
    score = 0
    for pattern in _TERMINAL_DUMP_PATTERNS:
        if re.search(pattern, raw):
            score += 1
    if raw.count("[tool]") >= 2:
        score += 1
    if raw.count("\n") >= 24 and len(raw) >= 1200:
        score += 1
    if raw.count("\"tool_name\"") >= 2:
        score += 1
    return score >= 3


def _tool_execution_summary_reply(tool_steps: int, tool_trace: list[dict] | None) -> str:
    trace = tool_trace if isinstance(tool_trace, list) else []
    names: list[str] = []
    for step in trace:
        if not isinstance(step, dict):
            continue
        name = str(step.get("agent_tool", "")).strip()
        if not name:
            continue
        if name not in names:
            names.append(name)
    count = len(trace)
    if count <= 0 and int(tool_steps) > 0:
        count = int(tool_steps)
    if names:
        preview = "、".join(names[:5]) + (" 等" if len(names) > 5 else "")
        return f"本轮已执行 {count} 个工具（{preview}）。详细调用过程已写入终端。"
    if count > 0:
        return f"本轮已执行 {count} 个工具。详细调用过程已写入终端。"
    return "工具调用明细已写入终端。"


def _sanitize_terminal_dump_reply(
    *,
    reply_text: str,
    tool_steps: int,
    tool_trace: list[dict] | None,
) -> str:
    raw = str(reply_text or "")
    if not raw.strip():
        return raw
    if has_tool_protocol_artifacts(raw):
        cleaned = strip_tool_protocol_artifacts(raw)
        if cleaned.strip():
            return cleaned
        if int(tool_steps) > 0 or (isinstance(tool_trace, list) and tool_trace):
            return _tool_execution_summary_reply(tool_steps, tool_trace)
        return ""
    if not _looks_like_terminal_dump(raw):
        return raw
    return _tool_execution_summary_reply(tool_steps, tool_trace)


def _sanitize_tool_trace_for_user(trace: list[dict] | None) -> list[dict]:
    if not isinstance(trace, list):
        return []
    out: list[dict] = []
    for step in trace:
        if not isinstance(step, dict):
            continue
        row = dict(step)
        result = row.get("result")
        if isinstance(result, dict):
            err_code = str(result.get("error_code", "") or "")
            if err_code == "permission_denied" and result.get("expose_to_user") is False:
                safe = dict(result)
                safe["error"] = str(result.get("user_message") or "该工具当前不可用，请尝试其它方式。")
                safe.pop("llm_message", None)
                safe.pop("missing_perm_labels", None)
                safe.pop("required_perm_labels", None)
                safe.pop("required_perm_bits", None)
                safe.pop("user_perm", None)
                safe.pop("user_perm_labels", None)
                row["result"] = safe
        out.append(row)
    return out


def _tool_result_ok(result: Any) -> bool:
    if not isinstance(result, dict):
        return bool(result)
    inner = result.get("result")
    if isinstance(inner, dict):
        if inner.get("pending_confirmation") is True:
            return False
        if "success" in inner:
            return bool(inner.get("success"))
        if "returncode" in inner and inner.get("returncode") is not None:
            return int(inner.get("returncode") or 0) == 0
        if "ok" in inner:
            return bool(inner.get("ok"))
        if inner.get("error"):
            return False
    if result.get("pending_confirmation") is True:
        return False
    if "success" in result:
        return bool(result.get("success"))
    if "returncode" in result and result.get("returncode") is not None:
        return int(result.get("returncode") or 0) == 0
    if "ok" in result:
        return bool(result.get("ok"))
    if result.get("error"):
        return False
    return True


def _tool_result_output(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result or "")
    inner = result.get("result")
    if not isinstance(inner, dict):
        inner = {}
    for source in (result, inner):
        for key in ("stdout", "stderr", "output", "error"):
            value = source.get(key)
            if value:
                return str(value)
    actual = result.get("result", {}) if isinstance(result.get("result"), dict) else {}
    if actual:
        import json as _json
        return _json.dumps(actual, ensure_ascii=False)
    if result:
        import json as _json
        return _json.dumps(result, ensure_ascii=False)
    return ""


def _extract_pending_confirmation_items(tool_trace: list[dict] | None) -> list[dict]:
    if not isinstance(tool_trace, list):
        return []
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for step in tool_trace:
        if not isinstance(step, dict):
            continue
        result = step.get("result")
        candidates: list[dict] = []
        if isinstance(result, dict):
            candidates.append(result)
            inner = result.get("result")
            if isinstance(inner, dict):
                candidates.append(inner)
        for cand in candidates:
            if cand.get("pending_confirmation") is not True:
                continue
            cmd = str(cand.get("cmd", "") or "").strip()
            if not cmd:
                continue
            call_id = str(cand.get("call_id", "") or "").strip()
            if not call_id and isinstance(result, dict):
                call_id = str(result.get("call_id", "") or "").strip()
            if not call_id:
                call_id = str(step.get("call_id", "") or "").strip()
            if not call_id:
                call_id = str(step.get("tool_call_id", "") or "").strip()
            confirm_id = call_id or f"tcf_{uuid.uuid4().hex[:12]}"
            key = (confirm_id, cmd)
            if key in seen:
                continue
            seen.add(key)
            note = str(cand.get("note", "") or "").strip()[:80]
            items.append(
                {
                    "confirm_id": confirm_id,
                    "call_id": call_id or confirm_id,
                    "cmd": cmd,
                    "note": note,
                    "approval": None,
                    "status": "pending",
                    "created_at": _now_iso(),
                }
            )
    return items


def _set_terminal_pending(session_id: str, items: list[dict] | None) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    normalized: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        cmd = str(item.get("cmd", "") or "").strip()
        if not cmd:
            continue
        call_id = str(item.get("call_id", "") or "").strip()
        confirm_id = str(item.get("confirm_id", "") or "").strip() or call_id or f"tcf_{uuid.uuid4().hex[:12]}"
        if not call_id:
            call_id = confirm_id
        note = str(item.get("note", "") or "").strip()[:80]
        normalized.append(
            {
                "confirm_id": confirm_id,
                "call_id": call_id,
                "cmd": cmd,
                "note": note,
                "approval": None if item.get("approval") is None else bool(item.get("approval")),
                "status": str(item.get("status", "pending") or "pending"),
                "created_at": str(item.get("created_at", "") or _now_iso()),
                "turn_id": str(item.get("turn_id", "") or "").strip(),
            }
        )
    with _terminal_pending_lock:
        if normalized:
            _terminal_pending[sid] = normalized
        else:
            _terminal_pending.pop(sid, None)


def _get_terminal_pending(session_id: str) -> list[dict]:
    sid = str(session_id or "").strip()
    if not sid:
        return []
    with _terminal_pending_lock:
        rows = _terminal_pending.get(sid, [])
        if isinstance(rows, dict):
            return [dict(rows)]
        return [dict(x) for x in rows if isinstance(x, dict)]


def _clear_terminal_pending(session_id: str) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _terminal_pending_lock:
        _terminal_pending.pop(sid, None)


def _pending_confirm_count(session_id: str) -> int:
    return len(_get_terminal_pending(session_id))


def _build_pending_required_payload(session_id: str) -> dict:
    pending = _get_terminal_pending(session_id)
    return {
        "ok": False,
        "error": "存在待确认终端命令，请先允许/拒绝后继续。",
        "error_code": "pending_confirmation_required",
        "session_id": str(session_id or ""),
        "pending_confirm_count": len(pending),
        "pending": pending,
    }


def _get_web_profile(user: userdata.UserManager | None = None) -> UserProfileResponse:
    current = user or sec_get_current_user()
    if current is None:
        return UserProfileResponse(name="", uid="", perm=0, perm_label="NONE", token="")
    info = userdata.export_public_user(current)
    perm_value = int(info.get("perm", 0))
    return UserProfileResponse(
        name=str(info.get("name", "")),
        uid=str(info.get("uid", "")),
        perm=perm_value,
        perm_label=_perm_label(perm_value),
        token=str(info.get("token", "")),
    )


def _profile_payload(user: userdata.UserManager | None = None) -> dict:
    p = _get_web_profile(user)
    return {
        "name": p.name,
        "uid": p.uid,
        "perm": p.perm,
        "perm_label": p.perm_label,
    }


def _mask_token(token: str) -> str:
    text = str(token or "")
    if not text:
        return ""
    if len(text) <= 12:
        return "*" * len(text)
    return f"{text[:6]}...{text[-4:]}"


def _as_public_user_row(user: userdata.UserManager, *, current_uid: str = "") -> dict:
    up = int(user.get_perm())
    return {
        "uid": str(user.get_uid()),
        "name": str(user.get_name()),
        "perm": up,
        "perm_label": _perm_label(up),
        "is_current": bool(current_uid and str(user.get_uid()) == current_uid),
    }


def _is_system_user_row(row: dict) -> bool:
    return str(row.get("name", "") or "").startswith("web-bot-")


def _public_user_row_from_json(row: dict, *, current_uid: str = "") -> dict | None:
    if not isinstance(row, dict) or _is_system_user_row(row):
        return None
    uid = str(row.get("uid", "") or "").strip()
    name = str(row.get("name", "") or "").strip()
    if not uid or not name:
        return None
    try:
        up = int(row.get("perm", 0) or 0)
    except Exception:
        up = 0
    return {
        "uid": uid,
        "name": name,
        "perm": up,
        "perm_label": _perm_label(up),
        "is_current": bool(current_uid and uid == current_uid),
    }


def _load_user_rows_from_json() -> list[dict]:
    path = get_users_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = data.get("users", []) if isinstance(data, dict) else []
    return [dict(x) for x in rows if isinstance(x, dict)]


def _find_user_row_from_json(uid: str) -> dict | None:
    key = str(uid or "").strip()
    if not key:
        return None
    for row in _load_user_rows_from_json():
        if str(row.get("uid", "") or "").strip() == key and not _is_system_user_row(row):
            return row
    return None


def _touch_session_cache(session_id: str) -> None:
    _session_last_access[session_id] = time.time()


def _audit_web(op_type: str, func: str, content: str, extra: dict | None = None) -> int:
    return audit_event(
        op_type=op_type,
        subsystem="web",
        func=func,
        file_path=_THIS_FILE,
        content=content,
        extra=extra,
    )


def _resolve_user_from_token_header(request: Request) -> userdata.UserManager | None:
    token = str(request.headers.get("X-User-Token", "")).strip()
    if not token:
        return None
    user = userdata.get_user_from_token(token)
    if user is None or userdata.is_system_user(user):
        return None
    return user


def _iter_wsl_host_gateway_ips() -> set[str]:
    ips: set[str] = set()
    try:
        text = Path("/etc/resolv.conf").read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                ips.add(parts[1])
    except Exception:
        pass
    try:
        route_text = Path("/proc/net/route").read_text(encoding="utf-8", errors="ignore")
        for line in route_text.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 3 or cols[1] != "00000000":
                continue
            raw_gateway = int(cols[2], 16).to_bytes(4, "little")
            ips.add(str(ipaddress.IPv4Address(raw_gateway)))
    except Exception:
        pass
    return ips


def _is_local_client_host(client_host: str) -> bool:
    host = str(client_host or "").strip().strip("[]").lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if ip.version == 4 and str(ip) in _iter_wsl_host_gateway_ips():
        return True
    return False


def _is_local_login_request(request: Request) -> bool:
    try:
        client_host = str(request.client.host if request.client else "")
    except Exception:
        client_host = ""
    return _is_local_client_host(client_host)


def _require_local_login_request(request: Request) -> None:
    if not _is_local_login_request(request):
        raise HTTPException(status_code=403, detail="local login is only allowed from this machine")


@app.middleware("http")
async def audit_http_requests(request: Request, call_next):
    start_ms = int(time.time() * 1000)
    method = str(request.method or "").upper()
    path = str(request.url.path or "")
    op_type = _infer_http_op_type(method, path)
    endpoint_name = "unknown"
    # 鉴权中间件（token -> SecurityContext），对公开入口放行；
    # 若公开入口携带了有效 token，也注入上下文以统一后续逻辑与审计 uid。
    path_only = path.split("?", 1)[0]
    need_auth = not (
        path_only in _AUTH_OPEN_PATHS
        or path_only.startswith("/static/")
        or path_only.startswith("/assets/")
    )
    ctx_tokens = None
    try:
        user = _resolve_user_from_token_header(request)
        if need_auth:
            if user is None:
                _audit_web(
                    "SYSTEM_READ",
                    "auth_middleware",
                    f"invalid or missing token for {method} {path}",
                    {"ok": False, "path": path},
                )
                return JSONResponse({"detail": "not logged in"}, status_code=401)
        if user is not None:
            ctx_tokens = push_current_user(user)

        response = await call_next(request)
        _record_usage_event(request)
        endpoint = request.scope.get("endpoint")
        if endpoint is not None:
            endpoint_name = str(getattr(endpoint, "__name__", "unknown"))
        principal = get_current_principal()
        uid = str(principal.uid) if principal is not None else ""
        _audit_web(
            op_type,
            f"http.{endpoint_name}",
            f"{method} {path} -> {int(getattr(response, 'status_code', 0) or 0)}",
            {
                "method": method,
                "path": path,
                "status_code": int(getattr(response, "status_code", 0) or 0),
                "duration_ms": max(0, int(time.time() * 1000) - start_ms),
                "uid": uid,
            },
        )
        return response
    except Exception as e:
        endpoint = request.scope.get("endpoint")
        if endpoint is not None:
            endpoint_name = str(getattr(endpoint, "__name__", "unknown"))
        principal = get_current_principal()
        uid = str(principal.uid) if principal is not None else ""
        _audit_web(
            op_type,
            f"http.{endpoint_name}",
            f"{method} {path} -> exception {e}",
            {
                "method": method,
                "path": path,
                "ok": False,
                "error": str(e),
                "duration_ms": max(0, int(time.time() * 1000) - start_ms),
                "uid": uid,
            },
        )
        raise
    finally:
        if ctx_tokens is not None:
            reset_current_user(ctx_tokens)


def _evict_if_needed() -> None:
    if len(_sessions) < _MAX_SESSIONS:
        return
    oldest = min(_session_last_access.items(), key=lambda x: x[1])[0]
    _sessions.pop(oldest, None)
    _session_last_access.pop(oldest, None)
    _session_config.pop(oldest, None)


def _is_tool_command_text(content: str) -> bool:
    raw = str(content or "").strip().lower()
    return raw.startswith("/tool") or raw.startswith("/tools") or raw.startswith("/help")


def _is_tool_marker_text(content: str) -> bool:
    raw = str(content or "").strip()
    if not raw:
        return False
    if raw.startswith("> >_<") and "调用工具中" in raw:
        return True
    if raw.startswith("本轮已执行") and "工具" in raw:
        return True
    if raw == "工具调用明细已写入终端。":
        return True
    return False


def _store_to_agent_messages(rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    if isinstance(rows, dict):
        return sa.store_dict_to_agent_messages(rows)
    out: list[dict] = []
    stats = {"input_rows": len(rows or []), "included": 0, "skipped": 0}
    for item in (rows or []):
        if not isinstance(item, dict):
            stats["skipped"] += 1
            continue
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", ""))
        has_tool_calls = isinstance(item.get("tool_calls"), list) and bool(item.get("tool_calls"))
        if not content.strip() and not has_tool_calls:
            stats["skipped"] += 1
            continue
        if role in {"system", "user", "assistant", "tool"}:
            row = {"role": role, "content": content}
            if role == "assistant":
                rc = item.get("reasoning_content")
                if rc is not None:
                    row["reasoning_content"] = rc
                if has_tool_calls:
                    row["tool_calls"] = item.get("tool_calls", [])
            if role == "tool":
                tool_call_id = str(item.get("tool_call_id", "") or "").strip()
                if tool_call_id:
                    row["tool_call_id"] = tool_call_id
            out.append(row)
            stats["included"] += 1
        else:
            stats["skipped"] += 1
    return out, stats


def _is_logged_in() -> bool:
    return sec_get_current_user() is not None


def _require_login() -> userdata.UserManager:
    current = sec_get_current_user()
    if current is None:
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="web",
            func="_require_login",
            file_path=_THIS_FILE,
            content="require_login_failed",
            extra={"ok": False},
        )
        raise HTTPException(status_code=401, detail="not logged in")
    audit_event(
        op_type="SYSTEM_READ",
        subsystem="web",
        func="_require_login",
        file_path=_THIS_FILE,
        content=f"require_login_ok uid={current.get_uid()}",
        extra={"ok": True, "uid": str(current.get_uid())},
    )
    return current


def _has_perm(user: userdata.UserManager, needed: int) -> bool:
    try:
        return sec_has_perm(int(needed))
    except Exception:
        try:
            user_perm = int(user.get_perm())
        except Exception:
            user_perm = 0
        return (user_perm & int(needed)) == int(needed)


def _require_admin_user() -> userdata.UserManager:
    current = _require_login()
    # 管理面板要求满权限账号（USER_ADMIN），避免半管理员账号进入高危用户管理操作。
    if not _has_perm(current, perm.USER_ADMIN):
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="web",
            func="_require_admin_user",
            file_path=_THIS_FILE,
            content=f"require_admin_failed uid={current.get_uid()}",
            extra={"ok": False, "uid": str(current.get_uid()), "perm": int(current.get_perm())},
        )
        raise HTTPException(status_code=403, detail="permission denied")
    audit_event(
        op_type="SYSTEM_READ",
        subsystem="web",
        func="_require_admin_user",
        file_path=_THIS_FILE,
        content=f"require_admin_ok uid={current.get_uid()}",
        extra={"ok": True, "uid": str(current.get_uid())},
    )
    return current


def _has_llm_perm(user: userdata.UserManager) -> bool:
    return _has_perm(user, _LLM_EXECUTE_PERM)


def _require_public_read_user() -> userdata.UserManager:
    current = _require_login()
    if not _has_perm(current, perm.PUBLIC_READ):
        audit_event(
            op_type="PUBLIC_READ",
            subsystem="web",
            func="_require_public_read_user",
            file_path=_THIS_FILE,
            content=f"require_public_read_failed uid={current.get_uid()}",
            extra={"ok": False, "uid": str(current.get_uid()), "perm": int(current.get_perm())},
        )
        raise HTTPException(status_code=403, detail="permission denied")
    audit_event(
        op_type="PUBLIC_READ",
        subsystem="web",
        func="_require_public_read_user",
        file_path=_THIS_FILE,
        content=f"require_public_read_ok uid={current.get_uid()}",
        extra={"ok": True, "uid": str(current.get_uid())},
    )
    return current


def _as_user_row(user: userdata.UserManager, *, current_uid: str = "") -> dict:
    up = int(user.get_perm())
    return {
        "uid": str(user.get_uid()),
        "name": str(user.get_name()),
        "perm": up,
        "perm_label": _perm_label(up),
        "token_masked": _mask_token(str(user.get_token())),
        "is_current": bool(current_uid and str(user.get_uid()) == current_uid),
    }


def _perm_items() -> list[dict]:
    return [
        {"bit": int(perm.PUBLIC_READ), "key": "PUBLIC_READ", "label": "公共读取"},
        {"bit": int(perm.PUBLIC_WRITE), "key": "PUBLIC_WRITE", "label": "公共写入"},
        {"bit": int(perm.PUBLIC_EXECUTE), "key": "PUBLIC_EXECUTE", "label": "公共执行"},
        {"bit": int(perm.TOOL_READ), "key": "TOOL_READ", "label": "工具读取"},
        {"bit": int(perm.TOOL_WRITE), "key": "TOOL_WRITE", "label": "工具写入"},
        {"bit": int(perm.TOOL_EXECUTE), "key": "TOOL_EXECUTE", "label": "工具执行"},
        {"bit": int(perm.SYSTEM_READ), "key": "SYSTEM_READ", "label": "系统读取"},
        {"bit": int(perm.SYSTEM_WRITE), "key": "SYSTEM_WRITE", "label": "系统写入"},
        {"bit": int(perm.SYSTEM_EXECUTE), "key": "SYSTEM_EXECUTE", "label": "系统执行"},
    ]


def _get_agent(session_id: str, *, refresh_context: bool = True):
    sid = str(session_id)
    current = _require_login()
    current_perm = int(current.get_perm())
    if sid not in _sessions:
        _evict_if_needed()
        agent = Agent(
            f"web-bot-{sid}",
            user_perm=current_perm,
            client=_tool_client,
            model_name=_llm.current_model,
        )
        model = _llm.current_model
        agent._context_logger = lambda hist, trigger="agent_ready", _sid=sid, _model=model: _write_context_log(
            _sid,
            _serialize_context_messages(hist),
            model=_model,
            trigger=trigger,
        )
        _sessions[sid] = agent
    else:
        # 会话 Agent 需实时跟随当前登录用户权限，避免工具可见性与鉴权失真
        agent = _sessions[sid]
        agent._client = _tool_client
        model = _llm.current_model
        agent._context_logger = lambda hist, trigger="agent_ready", _sid=sid, _model=model: _write_context_log(
            _sid,
            _serialize_context_messages(hist),
            model=_model,
            trigger=trigger,
        )
        if int(getattr(agent, "perm", 0)) != current_perm:
            agent.perm = current_perm
            try:
                agent.user.change_perm(current_perm)
            except Exception:
                pass
    _touch_session_cache(sid)

    if refresh_context:
        # 每次都基于 store 的“有效上下文”回灌，确保压缩边界生效
        rows = _store.get_context_messages(sid)
        agent_rows, filter_stats = _store_to_agent_messages(rows)
        _sessions[sid].replace_conversation(agent_rows)
        _audit_web(
            "SYSTEM_EXECUTE",
            "_get_agent",
            f"agent_ready session_id={sid}",
            {
                "session_id": sid,
                "context_rows": len(rows),
                "agent_rows": len(agent_rows),
                "context_filter": filter_stats,
                "model": _llm.current_model,
                "refresh_context": True,
            },
        )
    else:
        _audit_web(
            "SYSTEM_EXECUTE",
            "_get_agent",
            f"agent_ready_live session_id={sid}",
            {
                "session_id": sid,
                "model": _llm.current_model,
                "refresh_context": False,
            },
        )
    _sessions[sid].max_context_tokens = _effective_context_token_limit(sid)
    return _sessions[sid]


def _stringify_trace_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _tool_trace_to_terminal_items(tool_trace: list[dict] | None, *, turn_id: str = "") -> list[dict]:
    if not isinstance(tool_trace, list) or not tool_trace:
        return []

    tid = str(turn_id or "").strip()
    items: list[dict] = []
    for step in tool_trace:
        if not isinstance(step, dict):
            continue

        name = str(step.get("agent_tool", "") or "unknown_tool")
        args_text = _stringify_trace_value(step.get("arguments", {}))
        result = step.get("result")
        call_id = ""
        if isinstance(result, dict):
            call_id = str(result.get("call_id", "") or "").strip()
        if not call_id:
            call_id = str(step.get("call_id", "") or "").strip()
        if not call_id:
            call_id = str(step.get("tool_call_id", "") or "").strip()
        call_suffix = f" #{call_id}" if call_id else ""
        items.append(
            {
                "id": f"m_{uuid.uuid4().hex[:16]}",
                "role": "assistant",
                "content": f"[tool] {name}{call_suffix} {args_text}",
                "entry_type": "terminal",
                "terminal_kind": "cmd",
                "created_at": _now_iso(),
                "is_summary": False,
                "turn_id": tid,
            }
        )

        out_lines: list[str] = []
        if isinstance(result, dict):
            if result.get("ok") is False:
                if (
                    str(result.get("error_code", "") or "") == "permission_denied"
                    and result.get("expose_to_user") is False
                ):
                    out_lines.append(
                        f"[error] {str(result.get('user_message') or '该工具当前不可用，请尝试其它方式。')}"
                    )
                else:
                    out_lines.append(f"[error] {str(result.get('error') or '工具执行失败')}")
            else:
                tool_name = str(result.get("tool_name", "") or "").strip()
                if tool_name:
                    out_lines.append(f"tool: {tool_name}{call_suffix}")
                stdout_text = str(result.get("stdout", "") or "")
                if stdout_text.strip():
                    out_lines.extend(stdout_text.split("\n"))
                if "result" in result:
                    rendered = _stringify_trace_value(result.get("result"))
                    if rendered:
                        out_lines.extend(rendered.split("\n"))
                tools_obj = result.get("tools")
                if isinstance(tools_obj, dict):
                    out_lines.append("可用工具列表：")
                    for k, v in tools_obj.items():
                        out_lines.append(f"- {k}: {v}")
        else:
            raw = _stringify_trace_value(step.get("raw_result"))
            if raw:
                out_lines.extend(raw.split("\n"))

        for line in out_lines:
            items.append(
                {
                    "id": f"m_{uuid.uuid4().hex[:16]}",
                    "role": "assistant",
                    "content": str(line),
                    "entry_type": "terminal",
                    "terminal_kind": "out",
                    "created_at": _now_iso(),
                    "is_summary": False,
                }
            )

        items.append(
            {
                "id": f"m_{uuid.uuid4().hex[:16]}",
                "role": "assistant",
                "content": "─" * 36,
                "entry_type": "terminal",
                "terminal_kind": "sep",
                "created_at": _now_iso(),
                "is_summary": False,
            }
        )
    return items


def _save_chat_messages(
    session_id: str,
    user_text: str,
    assistant_text: str,
    *,
    reasoning_content: str | None = None,
    tool_marker: bool = False,
    tool_trace: list[dict] | None = None,
    turn_id: str = "",
    reasoning_after: str | None = None,
) -> None:
    items = [sa.build_user_message(_strip_user_meta_block(user_text))]
    # Build assistant message with sub-steps
    substeps: list[dict] = []
    if reasoning_content and reasoning_content.strip():
        substeps.append({"kind": "thinking", "content": reasoning_content.strip()})
    if tool_marker and isinstance(tool_trace, list):
        for step in tool_trace:
            if not isinstance(step, dict):
                continue
            name = step.get("agent_tool", "unknown")
            cid = str(step.get("call_id", "") or "").strip()
            args = step.get("arguments", {}) or {}
            result = step.get("result", {}) or {}
            ok = _tool_result_ok(result)
            stdin = ""
            if isinstance(args, dict):
                stdin = str(args.get("cmd") or args.get("text") or args.get("key") or "")
            stdout = _tool_result_output(result)
            substeps.append({
                "kind": "tool_marker",
                "name": name,
                "ok": bool(ok),
                "stdin": stdin[:500],
                "stdout": stdout[:500],
                "id": cid.lstrip("tc_"),
                "arguments": args if isinstance(args, dict) else {},
                "result": result if isinstance(result, dict) else {},
            })
    if assistant_text.strip():
        substeps.append({"kind": "text", "content": assistant_text.strip()})
    if reasoning_after and tool_marker:
        # Reorder for interleaved flow: thinking(pre) → text(pre) → tool → thinking(post) → text(post)
        thinking_pre = [s for s in substeps if s.get("kind") == "thinking"]
        text_pre = [s for s in substeps if s.get("kind") == "text"]
        markers = [s for s in substeps if s.get("kind") == "tool_marker"]
        substeps = thinking_pre + text_pre + markers
        substeps.append({"kind": "thinking", "content": reasoning_after.strip()})
    if substeps:
        items.append(sa.build_assistant_message(substeps))
    else:
        items.append(sa.build_assistant_message([{"kind": "text", "content": assistant_text}]))
    _store.append_messages(session_id, items)
    _invalidate_session_index(session_id)
    _audit_web(
        "PUBLIC_WRITE",
        "_save_chat_messages",
        f"chat_messages_saved session_id={session_id}",
        {"session_id": session_id, "items_count": len(items),
         "tool_marker": bool(tool_marker), "tool_trace_count": len(tool_trace or [])},
    )


def _append_assistant_continuation_messages(
    session_id: str,
    assistant_text: str,
    *,
    turn_id: str = "",
    tool_marker: bool = False,
    tool_trace: list[dict] | None = None,
) -> None:
    reply = str(assistant_text or "").strip()
    if not reply and not tool_trace:
        return
    substeps: list[dict] = []
    if tool_marker and isinstance(tool_trace, list):
        for step in tool_trace:
            if not isinstance(step, dict):
                continue
            name = step.get("agent_tool", "unknown")
            cid = str(step.get("call_id", "") or "").strip()
            args = step.get("arguments", {}) or {}
            result = step.get("result", {}) or {}
            ok = _tool_result_ok(result)
            stdin = str(args.get("cmd") or args.get("text") or "") if isinstance(args, dict) else ""
            stdout = _tool_result_output(result)
            substeps.append({
                "kind": "tool_marker",
                "name": name, "ok": bool(ok),
                "stdin": stdin[:500], "stdout": stdout[:500],
                "id": cid.lstrip("tc_"),
                "arguments": args if isinstance(args, dict) else {},
                "result": result if isinstance(result, dict) else {},
            })
    if reply:
        substeps.append({"kind": "text", "content": reply})
    if substeps:
        _store.append_to_last_assistant(session_id, substeps)
    _audit_web("PUBLIC_WRITE", "_append_assistant_continuation_messages",
               f"assistant_continuation_appended session_id={session_id}",
               {"session_id": session_id, "substeps": len(substeps),
                "tool_marker": bool(tool_marker)})


def _tool_trace_to_substeps(tool_trace: list[dict] | None) -> list[dict]:
    substeps: list[dict] = []
    if not isinstance(tool_trace, list):
        return substeps
    for step in tool_trace:
        if not isinstance(step, dict):
            continue
        name = str(step.get("agent_tool", "unknown") or "unknown")
        cid = str(step.get("call_id", "") or "").strip()
        model_id = str(step.get("tool_call_id", "") or "").strip()
        args = step.get("arguments", {}) or {}
        result = step.get("result", {}) or {}
        ok = _tool_result_ok(result)
        stdin = str(args.get("cmd") or args.get("text") or args.get("key") or "") if isinstance(args, dict) else ""
        stdout = _tool_result_output(result)
        substeps.append({
            "kind": "tool_marker",
            "name": name,
            "ok": bool(ok),
            "stdin": stdin[:500],
            "stdout": stdout[:500],
            "id": cid.lstrip("tc_"),
            "tool_call_id": model_id,
            "status": "done",
            "arguments": args if isinstance(args, dict) else {},
            "result": result if isinstance(result, dict) else {},
        })
    return substeps


def _tool_call_start_to_substeps(calls: list[dict] | None) -> list[dict]:
    substeps: list[dict] = []
    if not isinstance(calls, list):
        return substeps
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("agent_tool", "unknown") or "unknown")
        model_id = str(call.get("tool_call_id", "") or "").strip()
        marker_id = str(call.get("call_id", "") or call.get("id", "") or model_id).strip()
        args = call.get("arguments", {}) or {}
        stdin = str(args.get("cmd") or args.get("text") or args.get("key") or "") if isinstance(args, dict) else ""
        substeps.append({
            "kind": "tool_marker",
            "name": name,
            "ok": False,
            "stdin": stdin[:500],
            "stdout": "工具调用已开始，等待执行结果...",
            "id": marker_id.lstrip("tc_"),
            "tool_call_id": model_id,
            "status": "running",
            "arguments": args if isinstance(args, dict) else {},
            "result": {"ok": False, "pending": True, "tool_call_id": model_id},
        })
    return substeps


def _persist_terminal_events(session_id: str, events: list[dict]) -> None:
    rows: list[dict] = []
    for e in events:
        if e.get("type") != "terminal":
            continue
        kind = str(e.get("kind", "out")).strip() or "out"
        if kind not in {"cmd", "out", "sep"}:
            kind = "out"
        rows.append(
            {
                "id": str(e.get("id", "") or f"tool_event_{e.get('seq', '')}" or f"terminal_{uuid.uuid4().hex[:16]}"),
                "type": "terminal",
                "display_target": "terminal",
                "context_policy": "include",
                "source": "tool_runtime",
                "source_seq": int(e.get("seq", 0) or 0),
                "job_id": str(e.get("job_id", "") or ""),
                "kind": kind,
                "class": str(e.get("class", "") or "").strip().lower(),
                "content": str(e.get("text", "")),
                "ts": str(e.get("ts", "")) or _now_iso(),
            }
        )
    if rows:
        _store.append_terminal(session_id, rows)
        _invalidate_session_index(session_id)
    _audit_web(
        "PUBLIC_WRITE",
        "_persist_terminal_events",
        f"terminal_events_persisted session_id={session_id}",
        {"session_id": session_id, "incoming_events": len(events), "saved_rows": len(rows)},
    )


def _generate_title_from_first_round(session_id: str) -> None:
    current = sec_get_current_user()
    if current is None or not _has_llm_perm(current):
        return

    pair = _store.maybe_first_round_messages(session_id)
    if not pair:
        _audit_web(
            "SYSTEM_EXECUTE",
            "_generate_title_from_first_round",
            f"skip_generate_title_no_pair session_id={session_id}",
            {"session_id": session_id},
        )
        return

    meta = _store.get_session(session_id) or {}
    if str(meta.get("title", "")).strip() not in {"", "新对话"}:
        _audit_web(
            "SYSTEM_EXECUTE",
            "_generate_title_from_first_round",
            f"skip_generate_title_existing_title session_id={session_id}",
            {"session_id": session_id, "title": str(meta.get("title", ""))},
        )
        return

    user_msg, assistant_msg = pair

    def run() -> None:
        prompt = (
            "请根据以下对话生成一个不超过 15 字的简洁标题，"
            "直接返回标题文本，不要加引号或说明。\n\n"
            f"用户：{user_msg}\n"
            f"助手：{assistant_msg}"
        )
        try:
            title = _get_aux_client("title_model", "TINDA_TITLE_MODEL", "deepseek-v4-flash").chat(
                [
                    {"role": "system", "content": "你是对话标题生成助手。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            clean = str(title or "").strip().strip('"\'')
            if clean:
                _store.set_session_title(session_id, clean[:15])
                _audit_web(
                    "PUBLIC_WRITE",
                    "_generate_title_from_first_round.run",
                    f"generate_title_done session_id={session_id}",
                    {"session_id": session_id, "title": clean[:15]},
                )
        except Exception as e:
            logger.warning("generate session title failed: session=%s err=%s", session_id, e)
            _audit_web(
                "SYSTEM_EXECUTE",
                "_generate_title_from_first_round.run",
                f"generate_title_failed session_id={session_id} err={e}",
                {"session_id": session_id, "ok": False, "error": str(e)},
            )

    threading.Thread(target=run, daemon=True, name=f"title-gen-{session_id}").start()


def _compress_messages_with_llm(rows: list[dict]) -> str:
    parts: list[str] = []
    for item in rows:
        role = str(item.get("role", "")).strip() or "unknown"
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        parts.append(f"{role}: {content}")
    dialog = "\n".join(parts)
    prompt = (
        "你是对话摘要助手。请将以下多轮对话压缩为一段简洁摘要。"
        "要求：保留关键信息（用户需求、重要决策、结论）；"
        "保留技术细节（代码、配置、专有名词）；"
        "使用第三人称陈述；压缩为原内容的 20% 到 30% 长度。"
        "直接输出摘要内容，不要添加前缀或说明。\n\n"
        f"对话内容：\n{dialog}"
    )
    text = _get_aux_client("compress_model", "TINDA_COMPRESS_MODEL", "deepseek-v4-flash").chat(
        [
            {"role": "system", "content": "你是严谨的对话摘要助手。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    out = str(text or "").strip()
    _audit_web(
        "SYSTEM_EXECUTE",
        "_compress_messages_with_llm",
        "compress_messages_with_llm_done",
        {"source_rows": len(rows), "summary_len": len(out)},
    )
    return out


def _sse_event(name: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {name}\ndata: {payload}\n\n"


def _safe_log_name(raw_name: str) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return ""
    base = Path(name).name
    if base != name:
        return ""
    if base.startswith("."):
        return ""
    return base


def _read_log_tail(path: Path, *, max_lines: int, max_bytes: int) -> tuple[list[str], bool]:
    size = int(path.stat().st_size)
    read_bytes = min(max(1, int(max_bytes)), max(1, size))
    seek_pos = max(0, size - read_bytes)
    with path.open("rb") as fp:
        if seek_pos > 0:
            fp.seek(seek_pos)
        data = fp.read(read_bytes)
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if seek_pos > 0 and lines:
        lines = lines[1:]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    truncated = seek_pos > 0
    return lines, truncated


def _resolve_log_file_path(safe_name: str) -> Path | None:
    primary = _LOG_ROOT / safe_name
    if primary.exists() and primary.is_file():
        return primary
    legacy = get_legacy_log_root() / safe_name
    if legacy.exists() and legacy.is_file():
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="storage_migration",
            func="_resolve_log_file_path",
            file_path=_THIS_FILE,
            content=f"legacy_fallback_read_log file={safe_name}",
            extra={"legacy_file": str(legacy)},
        )
        return legacy
    return None


def _parse_event_id(raw: str | int | None) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
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


def _resolve_total_jsonl_candidates() -> list[Path]:
    """
    汇总所有可能含有审计事件的文件,顺序:
      1. 当前 _LOG_ROOT / total.jsonl
      2. legacy log_root / total.jsonl
      3. HOME 默认 ~/.tinda/agent/log / total.jsonl
      4. 每个 root 下的 total.*.jsonl.gz 归档(按文件名时间倒序,优先扫最新)
    """
    rows: list[Path] = []
    seen: set[Path] = set()

    def _push(path: Path) -> None:
        try:
            r = path.resolve()
        except Exception:
            r = path
        if r in seen:
            return
        seen.add(r)
        rows.append(path)

    primary = _LOG_ROOT / "total.jsonl"
    if primary.exists() and primary.is_file():
        _push(primary)
    legacy = get_legacy_log_root() / "total.jsonl"
    if legacy.exists() and legacy.is_file():
        _push(legacy)
    try:
        home_default = Path.home() / ".tinda" / "agent" / "log" / "total.jsonl"
        if home_default.exists() and home_default.is_file():
            _push(home_default)
    except Exception:
        pass

    # gzip 归档:每个 root 下的 total.*.jsonl.gz,按文件名时间倒序
    roots: list[Path] = [_LOG_ROOT]
    try:
        roots.append(get_legacy_log_root())
    except Exception:
        pass
    try:
        roots.append(Path.home() / ".tinda" / "agent" / "log")
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
    return rows


def _find_audit_event_by_id(event_id: int) -> dict | None:
    """线性扫描 total.jsonl 及 .jsonl.gz 归档文件查找事件。"""
    target = int(event_id)
    for path in _resolve_total_jsonl_candidates():
        try:
            if path.suffix == ".gz":
                import gzip as _gzip
                opener = lambda p: _gzip.open(p, "rt", encoding="utf-8", errors="ignore")
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
                    if rid == target:
                        return {
                            "event": row,
                            "source_file": str(path.name),
                            "source_path": str(path),
                            "source_line": int(line_no),
                        }
        except Exception:
            pass
    return None


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML_HOME


@app.get("/home", response_class=HTMLResponse)
async def home_alias():
    return RedirectResponse(url="/", status_code=307)


@app.get("/home/changelog")
async def home_changelog():
    try:
        text = _CHANGELOG_FILE.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = "# CHANGELOG\n\n暂无变更记录。"
    return JSONResponse({"ok": True, "markdown": text[:120000]})


@app.get("/home/stats")
async def home_stats(month: str = Query(default="")):
    now = datetime.now().astimezone()
    month_text = str(month or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}", month_text):
            year, mon = [int(x) for x in month_text.split("-", 1)]
            selected = datetime(year, mon, 1).astimezone()
        else:
            selected = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    except Exception:
        selected = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    events = _load_usage_events(days=370)
    storage_volumes = _read_storage_volumes()
    selected_storage = next((item for item in storage_volumes if bool(item.get("is_runtime"))), None)
    if selected_storage is None:
        selected_storage = _disk_usage_payload(get_runtime_root(), label="运行", scope="runtime_root", is_runtime=True)
    memory = _read_proc_memory()
    process_memory = _read_process_memory()
    load_average = _read_load_average()
    months: list[str] = []
    cursor = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(12):
        months.append(f"{cursor.year:04d}-{cursor.month:02d}")
        prev_month = cursor.month - 1 or 12
        prev_year = cursor.year - 1 if cursor.month == 1 else cursor.year
        cursor = cursor.replace(year=prev_year, month=prev_month)

    return JSONResponse(
        {
            "ok": True,
            "started_at": datetime.fromtimestamp(_SERVER_STARTED_AT).astimezone().isoformat(timespec="seconds"),
            "uptime_seconds": max(0, int(time.time() - _SERVER_STARTED_AT)),
            "system_time": now.isoformat(timespec="seconds"),
            "collected_at": now.isoformat(timespec="seconds"),
            "memory": memory,
            "process_memory": process_memory,
            "load_average": load_average,
            "storage": selected_storage or {},
            "storages": storage_volumes,
            "usage": {
                "month": f"{selected.year:04d}-{selected.month:02d}",
                "months": months,
                "days": _build_month_days(selected.year, selected.month, events),
                "last24h": _build_usage_24h(events),
                "total_events": len(events),
            },
        }
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/chat", response_class=HTMLResponse)
async def chat_page_legacy():
    return RedirectResponse(url="/", status_code=307)


@app.get("/app", response_class=HTMLResponse)
async def chat_page():
    return _HTML_CHAT


@app.get("/chat_renderer.js")
async def chat_renderer_js():
    from fastapi.responses import Response
    return Response(content=_JS_CHAT_RENDERER, media_type="application/javascript")


@app.get("/markdown_renderer.js")
async def markdown_renderer_js():
    from fastapi.responses import Response
    return Response(content=_JS_MARKDOWN_RENDERER, media_type="application/javascript")


@app.get("/theme_toggle.js")
async def theme_toggle_js():
    from fastapi.responses import Response
    return Response(content=_JS_THEME_TOGGLE, media_type="application/javascript")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return _HTML_SETTINGS


# ── Web Settings API ───────────────────────────────────────────────
@app.get("/web-settings")
async def get_web_settings():
    _require_login()
    return load_web_settings()


@app.put("/web-settings")
async def put_web_settings(data: dict[str, Any] = Body(...)):
    _require_login()
    new_limit = 0
    if "token_limit" in data:
        ok, parsed, error = validate_context_token_limit(data.get("token_limit"))
        if not ok:
            return JSONResponse(
                {"ok": False, "error": error, "min": 16000, "max": 200000, "default": 16000},
                status_code=400,
            )
        data = dict(data)
        data["token_limit"] = parsed
        new_limit = parsed
    save_web_settings(data)
    if new_limit:
        for sid, cfg in list(_session_config.items()):
            cfg["max_context_tokens"] = new_limit
        for sid, agent in list(_sessions.items()):
            try:
                agent.max_context_tokens = new_limit
            except Exception:
                pass
    return load_web_settings()


# ── Terminal Settings API ──────────────────────────────────────────
@app.get("/terminal/settings")
async def get_terminal_settings():
    _require_login()
    return load_terminal_settings()


@app.put("/terminal/settings")
async def put_terminal_settings(data: dict[str, Any] = Body(...)):
    _require_admin_user()
    return save_terminal_settings(
        whitelist=data.get("whitelist"),
        blacklist=data.get("blacklist"),
        bypass_terminal_confirm=data.get("bypass_terminal_confirm"),
    )


@app.get("/model-diagnostics", response_class=HTMLResponse)
async def model_diagnostics_page():
    return _HTML_MODEL_DIAGNOSTICS


@app.get("/llm-request", response_class=HTMLResponse)
async def llm_request_page():
    return _HTML_LLM_REQUEST


@app.get("/model-data", response_class=HTMLResponse)
async def model_data_page():
    return _HTML_LLM_REQUEST


@app.get("/llm-request/latest")
async def get_latest_llm_request():
    current = _require_login()
    if not _has_llm_perm(current):
        return JSONResponse({"ok": False, "error": "权限不足：当前账户不可查看模型数据"}, status_code=403)
    row = _read_latest_jsonl(_LLM_REQUEST_LOG_FILE)
    if not row:
        return JSONResponse({
            "ok": True,
            "exists": False,
            "summary": {},
            "payload": None,
            "log_file": str(_LLM_REQUEST_LOG_FILE),
        })
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    return JSONResponse({
        "ok": True,
        "exists": True,
        "summary": _llm_request_summary(row),
        "payload": payload,
        "raw": row,
        "log_file": str(_LLM_REQUEST_LOG_FILE),
    })


@app.get("/model-data/latest")
async def get_latest_model_data_request():
    return await get_latest_llm_request()


@app.get("/model-data/providers")
async def get_model_data_providers():
    current = _require_login()
    if not _has_llm_perm(current):
        return JSONResponse({"ok": False, "error": "权限不足：当前账户不可查看模型供应商数据"}, status_code=403)
    return JSONResponse(_model_providers_payload())


@app.post("/model-data/providers")
async def upsert_model_data_provider(req: ModelProviderUpsertRequest):
    _require_admin_user()
    try:
        payload = _llm.upsert_provider(_pydantic_to_dict(req))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse(payload)


@app.post("/model-data/models")
async def add_model_data_model(req: ModelAddRequest):
    _require_admin_user()
    try:
        payload = _llm.add_model(req.provider, req.model_id, req.label or req.model_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse(payload)


@app.delete("/model-data/models")
async def delete_model_data_model(provider: str = Query(default="deepseek"), model_id: str = Query(default="")):
    _require_admin_user()
    try:
        payload = _llm.remove_model(provider, model_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse(payload)


@app.get("/model-data/balance")
async def get_model_data_balance(provider: str = Query(default="deepseek")):
    current = _require_login()
    if not _has_llm_perm(current):
        return JSONResponse({"ok": False, "error": "权限不足：当前账户不可查看模型账户数据"}, status_code=403)
    provider_key = _normalize_model_provider(provider)
    return JSONResponse(_llm.fetch_balance(provider_key, mask_api_key=_mask_api_key))


@app.post("/model-diagnostics/run")
async def run_model_diagnostics(req: ModelDiagnosticsRequest):
    current = _require_login()
    if not _has_llm_perm(current):
        return JSONResponse({"ok": False, "error": "权限不足：当前账户不可执行模型检测"}, status_code=403)

    allowed_tests = ("connectivity", "reasoning", "image", "video")
    raw_tests = [str(x or "").strip().lower() for x in (req.tests or []) if str(x or "").strip()]
    if not raw_tests:
        return JSONResponse({"ok": False, "error": "tests 不能为空"}, status_code=400)
    tests = []
    for key in raw_tests:
        if key not in allowed_tests:
            return JSONResponse({"ok": False, "error": f"不支持的 tests 项: {key}"}, status_code=400)
        if key not in tests:
            tests.append(key)

    provider_key = _normalize_model_provider(req.provider)
    target_model = _normalize_model_choice(req.model) or str(_llm.provider_meta(provider_key).get("current_model") or _llm.current_model or "").strip()
    if not target_model:
        return JSONResponse({"ok": False, "error": "model 无效"}, status_code=400)

    image_url = _sanitize_diagnostic_url(req.image_url)
    video_url = _sanitize_diagnostic_url(req.video_url)

    started_at = _now_iso()
    rows = _run_model_diagnostics(
        provider=provider_key,
        model_name=target_model,
        tests=tests,
        image_url=image_url,
        video_url=video_url,
    )
    return JSONResponse(
        {
            "ok": True,
            "provider": provider_key,
            "model": target_model,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "results": rows,
        }
    )


@app.get("/system/version")
async def system_version():
    state = _build_runtime_version_state()
    return JSONResponse(
        {
            "ok": True,
            "version": state.get("version", ""),
            "display": state.get("display", ""),
            "running_version": state.get("running_version", ""),
            "running_display": state.get("running_display", ""),
            "effective_version": state.get("effective_version", state.get("running_version", "")),
            "effective_display": state.get("effective_display", state.get("running_display", "")),
            "app_version": state.get("app_version", _APP_VERSION),
            "selected_version": state.get("selected_version", ""),
            "selected_display": state.get("selected_display", ""),
            "selected_version_raw": state.get("selected_version_raw", ""),
            "version_consistent": state.get("version_consistent", True),
            "signature_id": state.get("signature_id", ""),
            "verified": state.get("verified", False),
            "verify_label": state.get("verify_label", ""),
            "source": state.get("source", ""),
            "source_label": state.get("source_label", ""),
            "current_path": state.get("current_path", ""),
            "switched_at": state.get("switched_at", ""),
            "switch_enabled": state.get("switch_enabled", False),
        }
    )


@app.get("/system/versions")
async def list_system_versions():
    _require_public_read_user()
    local_rows = _version_mgr.list_local_versions()
    remote_payload = _version_mgr.list_remote_releases()
    runtime_state = _build_runtime_version_state()
    remote_ok = bool(remote_payload.get("ok", True))
    return JSONResponse(
        {
            "ok": True,
            "source": "github_releases",
            "repo": str(remote_payload.get("repo", "TindaMe/TindaAgent")),
            "current": runtime_state,
            "local_versions": local_rows,
            "remote_versions": remote_payload.get("releases", []),
            "latest_verified": remote_payload.get("latest_verified"),
            "remote_ok": remote_ok,
            "error": str(remote_payload.get("error", "")),
        }
    )


@app.post("/system/version/install")
async def install_system_version(req: VersionInstallRequest):
    _require_admin_user()
    result = _version_mgr.install_from_release(str(req.version or "").strip())
    if not bool(result.get("ok", False)):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.post("/system/version/switch")
async def switch_system_version(req: VersionSwitchRequest):
    _require_admin_user()
    version = str(req.version or "").strip()
    if not version:
        return JSONResponse({"ok": False, "error": "version required"}, status_code=400)
    result = _version_mgr.switch_version(version)
    if not bool(result.get("ok", False)):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.post("/system/version/snapshot")
async def create_system_version_snapshot(req: VersionSnapshotRequest):
    _require_admin_user()
    result = _version_mgr.create_local_snapshot(str(req.version or "").strip())
    if not bool(result.get("ok", False)):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.post("/system/version/snapshot/current")
async def create_system_version_snapshot_current(req: VersionSnapshotCurrentRequest):
    _require_admin_user()
    result = _version_mgr.create_snapshot_from_current_code()
    if not bool(result.get("ok", False)):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.get("/system/version/compat")
async def system_version_compat(target: str = Query(...)):
    _require_public_read_user()
    version = str(target or "").strip()
    if not version:
        return JSONResponse({"ok": False, "error": "target required"}, status_code=400)
    remote = _version_mgr.list_remote_releases()
    manifest = None
    if bool(remote.get("ok", False)):
        for item in remote.get("releases", []):
            if str(item.get("version", "")).strip().lstrip("v") == version.lstrip("v"):
                # compat 先按远端元数据粗判，详细以实际切换时 manifest 为准
                manifest = {
                    "data_schema_version": item.get("data_schema_version", 1),
                    "min_compatible_schema": item.get("min_compatible_schema", 1),
                    "max_compatible_schema": item.get("max_compatible_schema", 1),
                }
                break
    result = _version_mgr.check_target_compat(version, manifest if isinstance(manifest, dict) else None)
    code = 200 if bool(result.get("ok", False)) else 400
    return JSONResponse(result, status_code=code)


@app.get("/logs", response_class=HTMLResponse)
async def logs_page():
    return _HTML_LOG_VIEW


@app.get("/user-admin", response_class=HTMLResponse)
async def user_admin_page(request: Request):
    # 页面本身允许打开；真正权限由 /admin/* 接口强校验，
    # 前端页面加载后会基于 /auth/status 再次判定并回跳。
    return _HTML_USER_ADMIN


@app.get("/auth/status")
async def auth_status(request: Request):
    # 公开接口：基于 header token 实时判断，不依赖全局状态。
    current = sec_get_current_user() or _resolve_user_from_token_header(request)
    if current is None:
        return JSONResponse({"logged_in": False, "user": None})
    return JSONResponse(
        {
            "logged_in": True,
            "user": _profile_payload(current),
        }
    )


@app.post("/auth/select-user")
async def auth_select_user(req: UserSwitchRequest, request: Request):
    # 新模式下不再切服务端全局用户；前端切换后应携带目标 token 发起请求。
    current = sec_get_current_user() or _resolve_user_from_token_header(request)
    if current is None:
        return JSONResponse(
            {"ok": False, "logged_in": False, "user": None, "error": "not logged in"},
            status_code=401,
        )
    return JSONResponse(
        {
            "ok": True,
            "logged_in": True,
            "user": _profile_payload(current),
        }
    )


@app.get("/auth/users")
async def auth_users():
    current = _require_login()
    current_uid = str(current.get_uid())
    users = [_as_public_user_row(u, current_uid=current_uid)
             for u in userdata.iter_users() if not userdata.is_system_user(u)]
    return JSONResponse({"users": users, "current_uid": current_uid})


@app.get("/auth/local-users")
async def auth_local_users(request: Request):
    users = []
    for row in _load_user_rows_from_json():
        public = _public_user_row_from_json(row)
        if public is not None:
            users.append(public)
    return JSONResponse({"users": users})


@app.post("/auth/local-login")
async def auth_local_login(req: UserSwitchRequest, request: Request):
    _require_local_login_request(request)
    uid = str(req.uid or "").strip()
    if not uid:
        return JSONResponse({"ok": False, "error": "uid required"}, status_code=400)
    target = _find_user_row_from_json(uid)
    public = _public_user_row_from_json(target or {})
    if target is None or public is None:
        return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            "logged_in": True,
            "user": public,
            "token": str(target.get("token", "") or ""),
        }
    )


@app.get("/user/profile")
async def user_profile():
    _require_login()
    return JSONResponse(_profile_payload())


@app.get("/users")
async def list_users():
    _require_login()
    current = sec_get_current_user()
    current_uid = str(current.get_uid()) if current is not None else ""
    users = [_as_public_user_row(u, current_uid=current_uid)
             for u in userdata.iter_users() if not userdata.is_system_user(u)]
    return JSONResponse({"users": users, "current_uid": current_uid})


@app.get("/logs/files")
async def list_log_files():
    _require_public_read_user()
    roots: list[Path] = []
    if _LOG_ROOT.exists():
        roots.append(_LOG_ROOT)
    legacy = get_legacy_log_root()
    if legacy.exists() and legacy.resolve() != _LOG_ROOT.resolve():
        roots.append(legacy)
    if not roots:
        return JSONResponse({"ok": True, "files": []})

    rows: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        for p in root.iterdir():
            if not p.is_file():
                continue
            if p.name.startswith("."):
                continue
            if p.name in seen:
                continue
            try:
                st = p.stat()
            except Exception:
                continue
            from datetime import datetime

            rows.append(
                {
                    "name": str(p.name),
                    "size_bytes": int(st.st_size),
                    "updated_at": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
                }
            )
            seen.add(p.name)
    rows.sort(key=lambda x: str(x.get("updated_at", "")), reverse=True)
    return JSONResponse({"ok": True, "files": rows})


@app.get("/logs/read")
async def read_log_file(file: str = Query(...), lines: int = Query(300)):
    _require_public_read_user()
    safe_name = _safe_log_name(file)
    if not safe_name:
        return JSONResponse({"ok": False, "error": "invalid file name"}, status_code=400)

    path = _resolve_log_file_path(safe_name)
    if path is None:
        return JSONResponse({"ok": False, "error": "file not found"}, status_code=404)

    limit = max(20, min(int(lines), 2000))
    try:
        text_lines, truncated = _read_log_tail(
            path,
            max_lines=limit,
            max_bytes=_LOG_MAX_READ_BYTES,
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"read failed: {e}"}, status_code=500)

    return JSONResponse(
        {
            "ok": True,
            "file": safe_name,
            "line_count": len(text_lines),
            "truncated": bool(truncated),
            "lines": text_lines,
        }
    )


@app.get("/logs/by-id")
async def read_log_event_by_id(id: str = Query(...)):
    _require_public_read_user()
    parsed_id = _parse_event_id(id)
    if parsed_id is None:
        return JSONResponse({"ok": False, "error": "invalid id"}, status_code=400)
    row = _find_audit_event_by_id(parsed_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "id not found", "id": parsed_id}, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            "id": parsed_id,
            "event": row.get("event", {}),
            "source_file": str(row.get("source_file", "")),
            "source_path": str(row.get("source_path", "")),
            "source_line": int(row.get("source_line", 0) or 0),
        }
    )


@app.post("/user/switch")
async def switch_user(req: UserSwitchRequest):
    _require_login()
    uid = str(req.uid or "").strip()  # 兼容旧前端入参，不再切换服务端全局状态。
    if uid:
        target = userdata.get_user_from_uid(uid)
        if target is None or userdata.is_system_user(target):
            return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            **_profile_payload(),
        }
    )


@app.get("/admin/users")
async def admin_list_users():
    current = _require_admin_user()
    current_uid = str(current.get_uid())
    rows = []
    for u in userdata.iter_users():
        if userdata.is_system_user(u):
            continue
        rows.append(_as_user_row(u, current_uid=current_uid))
    return JSONResponse({"ok": True, "users": rows, "current_uid": current_uid})


@app.get("/admin/permissions")
async def admin_permissions():
    _require_admin_user()
    return JSONResponse({"ok": True, "items": _perm_items()})


@app.post("/admin/users")
async def admin_create_user(req: UserCreateRequest):
    current = _require_admin_user()
    try:
        created = userdata.create_user(req.name, int(req.perm), req.token, actor=current)
    except (ValueError, PermissionError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "user": _as_user_row(created), "token": str(created.get_token())})


@app.patch("/admin/users/{uid}")
async def admin_update_user(uid: str, req: UserUpdateRequest):
    current = _require_admin_user()
    target = userdata.get_user_from_uid(uid)
    if target is None or userdata.is_system_user(target):
        return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
    if str(target.get_uid()) == str(current.get_uid()):
        return JSONResponse({"ok": False, "error": "cannot modify current user"}, status_code=400)
    if req.name is None and req.perm is None and req.token is None:
        return JSONResponse({"ok": False, "error": "no fields to update"}, status_code=400)
    try:
        updated = userdata.update_user(
            uid,
            name=req.name,
            userperm=req.perm,
            usertoken=req.token,
            actor=current,
        )
    except (ValueError, PermissionError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if updated is None:
        return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
    return JSONResponse({"ok": True, "user": _as_user_row(updated, current_uid=str(current.get_uid()))})


@app.patch("/admin/users/{uid}/permissions")
async def admin_update_user_permissions(uid: str, req: UserPermUpdateRequest):
    current = _require_admin_user()
    target = userdata.get_user_from_uid(uid)
    if target is None or userdata.is_system_user(target):
        return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
    if str(target.get_uid()) == str(current.get_uid()):
        return JSONResponse({"ok": False, "error": "cannot modify current user"}, status_code=400)
    try:
        updated = userdata.update_user(uid, userperm=int(req.perm), actor=current)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if updated is None:
        return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
    return JSONResponse({"ok": True, "user": _as_user_row(updated, current_uid=str(current.get_uid()))})


@app.post("/admin/users/{uid}/token/reset")
async def admin_reset_user_token(uid: str):
    current = _require_admin_user()
    target = userdata.get_user_from_uid(uid)
    if target is None or userdata.is_system_user(target):
        return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
    if str(target.get_uid()) == str(current.get_uid()):
        return JSONResponse({"ok": False, "error": "cannot modify current user"}, status_code=400)
    try:
        updated = userdata.update_user(
            uid,
            usertoken=uuid.uuid4().hex + uuid.uuid4().hex,
            actor=current,
        )
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if updated is None:
        return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            "user": _as_user_row(updated, current_uid=str(current.get_uid())),
            "token": str(updated.get_token()),
        }
    )


@app.delete("/admin/users/{uid}")
async def admin_delete_user(uid: str):
    current = _require_admin_user()
    target = userdata.get_user_from_uid(uid)
    if target is None or userdata.is_system_user(target):
        return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
    if str(target.get_uid()) == str(current.get_uid()):
        return JSONResponse({"ok": False, "error": "cannot delete current user"}, status_code=400)
    try:
        ok = userdata.delete_user(uid, actor=current)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if not ok:
        return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
    return JSONResponse({"ok": True, "uid": str(uid)})


@app.get("/model")
async def get_model():
    _require_login()
    return JSONResponse(_model_payload())


@app.post("/model")
async def switch_model(req: ModelSwitchRequest):
    _require_admin_user()
    target = _normalize_model_choice(req.model)
    if not target:
        return JSONResponse(
            {
                "ok": False,
                "error": "unsupported model",
                **_model_payload(),
            },
            status_code=400,
        )
    provider_key = _normalize_model_provider(req.provider)
    try:
        _llm.switch_model(target, provider=provider_key)
        for agent in list(_sessions.values()):
            try:
                agent._client = _tool_client
            except Exception:
                pass
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), **_model_payload()}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            **_model_payload(),
        }
    )


@app.post("/sessions")
async def create_session(req: SessionCreateRequest):
    current = _require_login()
    if bool(req.reuse_if_current_empty):
        current_id = str(req.current_session_id or "").strip()
        if current_id:
            meta = _store.get_session(current_id) or {}
            if str(meta.get("owner_uid", "") or "").strip() not in {"", str(current.get_uid())}:
                meta = {}
            try:
                msg_count = int(meta.get("message_count", 0))
            except Exception:
                msg_count = 0
            if msg_count <= 0 and str(meta.get("id", "")).strip():
                if not str(meta.get("owner_uid", "") or "").strip():
                    meta = _store.ensure_session(str(meta.get("id", "")), owner_uid=str(current.get_uid()))
                return JSONResponse({"ok": True, "session": meta, "reused": True})
    row = _store.create_session(
        session_id=str(req.session_id or "").strip() or None,
        title=str(req.title or "新对话"),
        owner_uid=str(current.get_uid()),
    )
    return JSONResponse({"ok": True, "session": row, "reused": False})


@app.get("/sessions")
async def list_sessions(limit: int = 100, offset: int = 0):
    current = _require_login()
    return JSONResponse(_store.list_sessions(limit=limit, offset=offset, owner_uid=str(current.get_uid())))


@app.patch("/sessions/{session_id}/config")
async def patch_session_config(session_id: str, data: dict[str, Any] = Body(...)):
    sid, _meta = _require_session_access(session_id)
    cfg = _session_config.setdefault(sid, {})
    if "max_context_tokens" in data:
        v = data["max_context_tokens"]
        if v is None:
            cfg["max_context_tokens"] = None
        else:
            ok, parsed, error = validate_context_token_limit(v)
            if not ok:
                return JSONResponse(
                    {"ok": False, "error": error, "min": 16000, "max": 200000, "default": 16000},
                    status_code=400,
                )
            cfg["max_context_tokens"] = parsed
    _audit_web("SYSTEM_WRITE", "patch_session_config",
               f"session_config_updated session_id={sid}",
               {"session_id": sid, "config": dict(cfg)})
    return JSONResponse({"ok": True, "session_id": sid, "config": dict(cfg)})


@app.get("/sessions/{session_id}/config")
async def get_session_config(session_id: str):
    sid, _meta = _require_session_access(session_id)
    cfg = dict(_session_config.get(sid, {}))
    _audit_web("SYSTEM_READ", "get_session_config",
               f"session_config_read session_id={sid}",
               {"session_id": sid, "config": cfg})
    return JSONResponse({"ok": True, "session_id": sid, "config": cfg})


@app.get("/sessions/{session_id}/context-usage")
async def get_session_context_usage(session_id: str):
    sid, _meta = _require_session_access(session_id, create=False)
    rows = _store.get_context_messages(sid)
    usage_length = _estimate_context_usage_length(rows)
    meta = _store.get_session(sid) or {}
    max_context_tokens = _effective_context_token_limit(sid)
    _audit_web(
        "SYSTEM_READ",
        "get_session_context_usage",
        f"context_usage session_id={sid}",
        {"session_id": sid, "context_rows": len(rows), "usage_length": int(usage_length)},
    )
    title = str(meta.get("title", "") or "新对话").strip() or "新对话"

    if title in {"", "新对话"} and usage_length > 0:
        try:
            _generate_title_from_first_round(sid)
        except Exception:
            pass

    return JSONResponse(
        {
            "ok": True,
            "session_id": sid,
            "title": title,
            "usage_length": int(usage_length),
            "max_context_tokens": int(max_context_tokens),
        }
    )


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    sid, _meta = _require_session_access(session_id, create=False)
    ok = _store.delete_session(sid)
    _sessions.pop(sid, None)
    _session_last_access.pop(sid, None)
    _session_config.pop(sid, None)
    _clear_terminal_pending(sid)
    _invalidate_session_index(sid)
    if not ok:
        return JSONResponse({"ok": False, "error": "session not found"}, status_code=404)
    return JSONResponse({"ok": True, "session_id": sid})


@app.delete("/sessions")
async def delete_all_sessions():
    current = _require_login()
    empty_deleted = 0
    try:
        empty_deleted = int(_store.cleanup_empty_sessions() or 0)
    except Exception:
        empty_deleted = 0
    payload = _store.list_sessions(limit=10000, offset=0, owner_uid=str(current.get_uid()))
    sessions_list = payload.get("sessions", [])
    deleted = empty_deleted
    for row in sessions_list:
        sid = str(row.get("id", "")).strip()
        if not sid:
            continue
        try:
            _store.delete_session(sid)
            _sessions.pop(sid, None)
            _session_last_access.pop(sid, None)
            _session_config.pop(sid, None)
            _clear_terminal_pending(sid)
            _invalidate_session_index(sid)
            deleted += 1
        except Exception:
            pass
    return JSONResponse({"ok": True, "deleted": deleted})


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(default=0),
    before_seq: int = Query(default=0),
):
    sid, _meta = _require_session_access(session_id, create=False)
    requested_limit = max(0, min(int(limit or 0), 500))
    if requested_limit > 0:
        try:
            meta = _store.get_session(sid) or {}
            if not _sqlite_index.is_session_current(sid, meta):
                load_effective = getattr(_store, "load_effective_messages", None)
                current_data = load_effective(sid) if callable(load_effective) else _store.load_messages(sid)
                _sqlite_index.index_session(sid, meta, current_data)
            cached = _sqlite_index.get_messages(sid, limit=requested_limit, before_seq=before_seq)
            if cached is not None:
                return JSONResponse(cached)
        except Exception as e:
            logger.debug("sqlite session index fallback session_id=%s: %s", sid, e)

    load_effective = getattr(_store, "load_effective_messages", None)
    data = load_effective(sid) if callable(load_effective) else _store.load_messages(sid)
    keys = sorted((int(k) for k in data if str(k).isdigit()), key=int)
    total = len(keys)
    if before_seq > 0:
        keys = [k for k in keys if k < before_seq]
    if requested_limit > 0:
        keys = keys[-requested_limit:]

    subset = {str(k): data[str(k)] for k in keys if str(k) in data}
    entries = sa.store_dict_to_frontend(subset)
    for idx, seq in enumerate(keys):
        if idx < len(entries) and isinstance(entries[idx], dict):
            entries[idx]["seq"] = int(seq)
    oldest_seq = int(keys[0]) if keys else 0
    newest_seq = int(keys[-1]) if keys else 0
    has_more = bool(oldest_seq > 1)
    return JSONResponse({
        "ok": True,
        "session_id": sid,
        "entries": entries,
        "total": total,
        "oldest_seq": oldest_seq,
        "newest_seq": newest_seq,
        "has_more": has_more,
        "limit": requested_limit,
        "source": "json_store",
    })


@app.post("/sessions/{session_id}/title")
async def update_session_title(session_id: str, req: SessionTitleRequest):
    sid, _meta = _require_session_access(session_id)
    row = _store.set_session_title(sid, req.title)
    return JSONResponse({"ok": True, "session": row})


@app.post("/sessions/{session_id}/compress")
async def compress_session_context(session_id: str):
    current = _require_login()
    if not _has_llm_perm(current):
        return JSONResponse({"ok": False, "error": "权限不足：当前账户不可执行上下文压缩"}, status_code=403)
    sid, _meta = _require_session_access(session_id, user=current)
    rows = _store.get_context_messages(sid)
    usage_before = _estimate_context_usage_length(rows)
    _audit_web(
        "SYSTEM_EXECUTE",
        "compress_session_context",
        f"compress_start session_id={sid}",
        {"session_id": sid, "context_rows": len(rows)},
    )
    # 只拿原始 chat 做摘要，不把终端/notice/tool_marker 混进去
    raw_rows = _raw_chat_rows_for_compression(sid, rows)
    if _is_duplicate_compression_request(sid, raw_rows):
        return JSONResponse(
            {
                "ok": True,
                "session_id": sid,
                "compressed": False,
                "reason": "already_compressed",
                "anchor_message_id": str(raw_rows[-4].get("id", "") or ""),
            }
        )
    if len(raw_rows) < 6:
        return JSONResponse({"ok": False, "error": "消息数量不足，至少需要 6 条消息才能压缩"}, status_code=400)

    summary_src = _summary_rows_for_compression(sid, raw_rows[:-4])
    if not summary_src:
        return JSONResponse({"ok": False, "error": "消息数量不足，至少需要 6 条消息才能压缩"}, status_code=400)

    try:
        summary = _compress_messages_with_llm(summary_src)
        if not summary:
            raise ValueError("摘要为空")
        result = _store.compress_context(sid, summary)
        _invalidate_session_index(sid)
        rows_after = _store.get_context_messages(sid)
        result["usage_before"] = int(usage_before)
        result["usage_after"] = int(_estimate_context_usage_length(rows_after))
        result["max_context_tokens"] = int(_effective_context_token_limit(sid))
    except SessionStoreError as e:
        _audit_web("SYSTEM_EXECUTE", "compress_session_context",
                   f"compress_failed session_id={sid} err={e}",
                   {"session_id": sid, "ok": False, "error": str(e)})
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("compress failed: session=%s err=%s", sid, e)
        _audit_web("SYSTEM_EXECUTE", "compress_session_context",
                   f"compress_error session_id={sid} err={e}",
                   {"session_id": sid, "ok": False, "error": str(e)})
        return JSONResponse({"ok": False, "error": "压缩失败"}, status_code=500)

    _audit_web("SYSTEM_EXECUTE", "compress_session_context",
               f"compress_done session_id={session_id}",
               {"session_id": sid, "ok": True, **{k: v for k, v in result.items() if k != "session_id"}})
    return JSONResponse({"ok": True, **result})


@app.post("/chat")
async def chat(req: ChatRequest):
    current = _require_login()
    if not _has_llm_perm(current):
        return JSONResponse({"error": "权限不足：当前账户不可调用 LLM 对话"}, status_code=403)
    sid = str(req.session_id or "").strip()
    if not sid:
        return JSONResponse({"error": "session_id required"}, status_code=400)

    sid, _meta = _require_session_access(sid, user=current)
    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    pending_count = _pending_confirm_count(sid)
    if pending_count > 0:
        stale_agent = _sessions.get(sid)
        if stale_agent is None or not bool(getattr(stale_agent, "has_pending_confirmation", lambda: False)()):
            _clear_terminal_pending(sid)
            pending_count = 0
    if pending_count > 0:
        payload = _build_pending_required_payload(sid)
        return JSONResponse(payload, status_code=409)
    agent = _get_agent(sid)
    message = str(req.message or "").strip()
    has_file = bool(req.file_names and req.file_contents and len(req.file_names) > 0)
    if not message and not has_file:
        return JSONResponse({"reply": "", "tool_trace": [], "tool_steps": 0, "turn_id": turn_id})

    if message.startswith("/"):
        profile = _get_web_profile()
        try:
            job = _tool_runtime.submit_command(sid, message, profile.perm)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        # 工具命令也写入 chat 消息（用户气泡独立）
        tool_turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        _store.append_messages(
            sid,
            [
                {
                    "id": f"m_{uuid.uuid4().hex[:16]}",
                    "role": "user",
                    "content": message,
                    "entry_type": "chat",
                    "is_summary": False,
                    "created_at": _now_iso(),
                    "turn_id": tool_turn_id,
                },
                {
                    "id": f"m_{uuid.uuid4().hex[:16]}",
                    "role": "assistant",
                    "content": "> >_<\n> --调用工具中--",
                    "entry_type": "tool_marker",
                    "is_summary": False,
                    "created_at": _now_iso(),
                    "turn_id": tool_turn_id,
                },
            ],
        )
        _invalidate_session_index(sid)
        return JSONResponse(
            {
                "reply": "> --调用工具中--",
                "tool_trace": [],
                "tool_steps": 0,
                "tool_job": job,
                "tool_async": True,
            }
        )

    raw_text = message
    if req.file_names:
        for fn, fc in zip(req.file_names, req.file_contents):
            raw_text = f"[文件: {fn}]\n```\n{fc or ''}\n```\n" + raw_text
    llm_message = _build_user_message_with_meta(
        raw_text,
        meta_user_name=req.meta_user_name,
        meta_user_id=req.meta_user_id,
        meta_user_perm=req.meta_user_perm,
        meta_time_iso=req.meta_time_iso,
        meta_time_text=req.meta_time_text,
    )

    result = agent.chat_with_meta(llm_message)
    tool_trace_raw = result.get("tool_trace", [])
    tool_trace = _sanitize_tool_trace_for_user(tool_trace_raw)
    tool_steps = int(result.get("tool_steps", 0))
    reply = str(result.get("reply", ""))
    pending_items = _extract_pending_confirmation_items(tool_trace_raw)
    for it in pending_items:
        it["turn_id"] = turn_id
    _set_terminal_pending(sid, pending_items)

    sanitized_reply = _sanitize_terminal_dump_reply(
        reply_text=reply,
        tool_steps=tool_steps,
        tool_trace=tool_trace,
    )
    if sanitized_reply != reply:
        _audit_web(
            "TOOL_EXECUTE",
            "chat",
            f"terminal_dump_reply_sanitized session_id={sid}",
            {
                "session_id": sid,
                "tool_steps": int(tool_steps),
                "tool_trace_count": len(tool_trace or []),
                "reply_len_before": len(reply),
                "reply_len_after": len(sanitized_reply),
            },
        )
        reply = sanitized_reply

    if pending_items:
        save_reply = ""
    else:
        save_reply = reply
    substeps = _build_substeps_from_history(agent, tool_trace)
    if pending_items:
        substeps = [s for s in substeps if s.get("kind") != "text"]
    user_clean = _strip_user_meta_block(message)
    items = [sa.build_user_message(user_clean, file_names=req.file_names, file_contents=req.file_contents)]
    if substeps:
        items.append(sa.build_assistant_message(substeps))
    elif save_reply.strip():
        items.append(sa.build_assistant_message([{"kind": "text", "content": save_reply}]))
    else:
        items.append(sa.build_assistant_message([{"kind": "text", "content": reply}]))
    saved_messages = False
    try:
        _store.append_messages(sid, items)
        _invalidate_session_index(sid)
        saved_messages = True
    except Exception:
        import traceback
        traceback.print_exc()
    context_compression_payload: dict[str, Any] | None = None
    if saved_messages and not pending_items:
        compression_result = _maybe_auto_compress_after_llm(sid)
        if compression_result.get("compressed") is True:
            compression_step = _build_context_compression_system_substep(compression_result)
            if _store.append_to_last_assistant(sid, [compression_step]):
                _invalidate_session_index(sid)
                context_compression_payload = compression_step.get("content", {})
    _audit_web(
        "PUBLIC_WRITE",
        "chat",
        f"chat_messages_saved session_id={sid}",
        {"session_id": sid, "items_count": len(items),
         "tool_marker": bool(tool_steps > 0 and not pending_items),
         "tool_trace_count": len(tool_trace or [])},
    )
    try:
        _generate_title_from_first_round(sid)
    except Exception:
        import traceback
        traceback.print_exc()

    return JSONResponse(
        {
            "reply": reply,
            "reasoning_content": _find_first_reasoning(agent) or "",
            "tool_trace": tool_trace,
            "tool_steps": tool_steps,
            "turn_id": turn_id,
            "pending_confirmation": len(pending_items) > 0,
            "pending_confirm_count": len(pending_items),
            "pending": pending_items,
            "context_compression": context_compression_payload,
        }
    )


@app.get("/chat/stream")
async def chat_stream(
    message: str,
    session_id: str,
    file_names: list[str] = Query(default_factory=list),
    file_contents: list[str] = Query(default_factory=list),
    meta_user_name: str | None = None,
    meta_user_id: str | None = None,
    meta_user_perm: str | None = None,
    meta_time_iso: str | None = None,
    meta_time_text: str | None = None,
):
    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    current = _require_login()
    if not _has_llm_perm(current):
        chunks = [
            _sse_event("error", {"message": "权限不足：当前账户不可调用 LLM 对话"}),
            _sse_event("done", {"reply": "", "tool_trace": [], "tool_steps": 0, "turn_id": turn_id}),
        ]
        return HTMLResponse("".join(chunks), media_type="text/event-stream")
    sid = str(session_id or "").strip()
    if not sid:
        chunks = [
            _sse_event("error", {"message": "session_id required"}),
            _sse_event("done", {"reply": "", "tool_trace": [], "tool_steps": 0, "turn_id": turn_id}),
        ]
        return HTMLResponse("".join(chunks), media_type="text/event-stream")

    try:
        sid, _meta = _require_session_access(sid, user=current)
    except HTTPException as e:
        chunks = [
            _sse_event("error", {"message": str(e.detail)}),
            _sse_event("done", {"reply": "", "tool_trace": [], "tool_steps": 0, "turn_id": turn_id}),
        ]
        return HTMLResponse("".join(chunks), media_type="text/event-stream")
    pending_count = _pending_confirm_count(sid)
    if pending_count > 0:
        # 若 agent 已无挂起确认，说明 pending 列表是残留的，主动清理
        stale_agent = _sessions.get(sid)
        if stale_agent is None or not bool(getattr(stale_agent, "has_pending_confirmation", lambda: False)()):
            _clear_terminal_pending(sid)
            pending_count = 0
    if pending_count > 0:
        payload = _build_pending_required_payload(sid)
        chunks = [
            _sse_event(
                "error",
                {
                    "message": payload.get("error", "存在待确认终端命令"),
                    "error_code": "pending_confirmation_required",
                    "pending_confirm_count": int(payload.get("pending_confirm_count", 0)),
                    "pending": payload.get("pending", []),
                },
            ),
            _sse_event("done", {"reply": "", "tool_trace": [], "tool_steps": 0}),
        ]
        return HTMLResponse("".join(chunks), media_type="text/event-stream")
    agent = _get_agent(sid)

    text = str(message or "").strip()
    if text.startswith("/"):
        profile = _get_web_profile()
        try:
            job = _tool_runtime.submit_command(sid, text, profile.perm)
        except Exception as e:
            chunks = [
                _sse_event("error", {"message": str(e)}),
                _sse_event("done", {"reply": "", "tool_trace": [], "tool_steps": 0}),
            ]
            return HTMLResponse("".join(chunks), media_type="text/event-stream")

        tool_turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        _store.append_messages(
            sid,
            [
                {
                    "id": f"m_{uuid.uuid4().hex[:16]}",
                    "role": "user",
                    "content": text,
                    "entry_type": "chat",
                    "is_summary": False,
                    "created_at": _now_iso(),
                    "turn_id": tool_turn_id,
                },
                {
                    "id": f"m_{uuid.uuid4().hex[:16]}",
                    "role": "assistant",
                    "content": "> >_<\n> --调用工具中--",
                    "entry_type": "tool_marker",
                    "is_summary": False,
                    "created_at": _now_iso(),
                    "turn_id": tool_turn_id,
                },
            ],
        )
        _invalidate_session_index(sid)

        chunks = [
            _sse_event("replace_segment", {"content": ""}),
            _sse_event("delta", {"content": "> >_<\n> --调用工具中--"}),
            _sse_event(
                "done",
                {
                    "reply": "> --调用工具中--",
                    "tool_trace": [],
                    "tool_steps": 0,
                    "tool_job": job,
                    "tool_async": True,
                    "pending_confirmation": False,
                    "pending_confirm_count": 0,
                    "pending": [],
                },
            ),
        ]
        return HTMLResponse("".join(chunks), media_type="text/event-stream")

    raw_text = str(message or "").strip()
    if file_names:
        for fn, fc in zip(file_names, file_contents):
            raw_text = f"[文件: {fn}]\n```\n{fc or ''}\n```\n" + raw_text
    llm_message = _build_user_message_with_meta(
        raw_text,
        meta_user_name=meta_user_name,
        meta_user_id=meta_user_id,
        meta_user_perm=meta_user_perm,
        meta_time_iso=meta_time_iso,
        meta_time_text=meta_time_text,
    )

    def event_iter():
        final_reply = ""
        final_reply_segment_start = 0
        reasoning_text = ""
        done_payload: dict | None = None
        user_clean = _strip_user_meta_block(str(message or "").strip())
        draft_ready = False
        final_saved = False

        def ensure_stream_draft() -> None:
            nonlocal draft_ready
            if draft_ready:
                return
            _store.ensure_turn_draft(
                sid,
                user_message=sa.build_user_message(
                    user_clean,
                    file_names=file_names,
                    file_contents=file_contents,
                ),
                assistant_message=sa.build_assistant_message([{
                    "kind": "text",
                    "content": "（正在生成，若页面刷新可稍后继续查看）",
                }]),
                turn_id=turn_id,
            )
            draft_ready = True

        try:
            ensure_stream_draft()
            for event in agent.stream_chat_events(llm_message):
                et = event.get("type", "")
                if et == "delta":
                    final_reply += str(event.get("content", ""))
                    yield _sse_event("delta", {"content": event.get("content", "")})
                elif et == "reasoning_delta":
                    reasoning_text += str(event.get("content", ""))
                    yield _sse_event("reasoning_delta", {"content": event.get("content", "")})
                elif et == "replace_segment":
                    cleaned_segment = str(event.get("content", "") or "")
                    final_reply = final_reply[:final_reply_segment_start] + cleaned_segment
                    final_reply_segment_start = len(final_reply)
                    yield _sse_event("replace_segment", {"content": cleaned_segment})
                elif et == "tool_call_start":
                    ensure_stream_draft()
                    start_steps = _tool_call_start_to_substeps(event.get("calls", []))
                    if start_steps:
                        _store.append_to_assistant_by_turn(
                            sid,
                            turn_id=turn_id,
                            substeps=start_steps,
                            replace_tool_results=True,
                        )
                        _audit_web(
                            "PUBLIC_WRITE",
                            "chat_stream",
                            f"tool_call_started_saved session_id={sid}",
                            {"session_id": sid, "turn_id": turn_id, "tool_calls": len(start_steps)},
                        )
                    yield _sse_event("tool_call_start", {"calls": event.get("calls", []), "turn_id": turn_id})
                elif et == "tool_step":
                    ensure_stream_draft()
                    raw_trace = event.get("trace", [])
                    safe_trace = _sanitize_tool_trace_for_user(raw_trace if isinstance(raw_trace, list) else [])
                    done_steps = _tool_trace_to_substeps(safe_trace)
                    if done_steps:
                        _store.append_to_assistant_by_turn(
                            sid,
                            turn_id=turn_id,
                            substeps=done_steps,
                            replace_tool_results=True,
                        )
                        _audit_web(
                            "PUBLIC_WRITE",
                            "chat_stream",
                            f"tool_step_saved session_id={sid}",
                            {"session_id": sid, "turn_id": turn_id, "tool_steps": len(done_steps)},
                        )
                    yield _sse_event("tool_step", {"trace": safe_trace})
                elif et == "tool_heartbeat":
                    yield _sse_event(
                        "tool_heartbeat",
                        {
                            "elapsed_ms": int(event.get("elapsed_ms", 0) or 0),
                            "turn_id": turn_id,
                            "tool": event.get("tool", {}) if isinstance(event.get("tool"), dict) else {},
                        },
                    )
                elif et == "done":
                    done_payload = {
                        "reply": event.get("reply", ""),
                        "tool_trace": _sanitize_tool_trace_for_user(event.get("tool_trace", [])),
                        "tool_steps": int(event.get("tool_steps", 0)),
                    }

            if done_payload is None:
                done_payload = {"reply": final_reply, "tool_trace": [], "tool_steps": 0}

            safe_tool_trace = (
                done_payload.get("tool_trace", [])
                if isinstance(done_payload.get("tool_trace"), list)
                else []
            )
            safe_tool_steps = int(done_payload.get("tool_steps", 0))
            final_reply = str(final_reply or done_payload.get("reply", ""))
            final_reasoning = reasoning_text or _find_first_reasoning(agent) or ""
            done_payload = {
                "reply": final_reply,
                "tool_trace": safe_tool_trace,
                "tool_steps": safe_tool_steps,
                "turn_id": turn_id,
                "reasoning_content": final_reasoning,
            }
            pending_items = _extract_pending_confirmation_items(done_payload.get("tool_trace", []))
            for it in pending_items:
                it["turn_id"] = turn_id
            _set_terminal_pending(sid, pending_items)
            done_payload["pending_confirmation"] = len(pending_items) > 0
            done_payload["pending_confirm_count"] = len(pending_items)
            done_payload["pending"] = pending_items

            sanitized_reply = _sanitize_terminal_dump_reply(
                reply_text=str(done_payload.get("reply", "")),
                tool_steps=int(done_payload.get("tool_steps", 0)),
                tool_trace=done_payload.get("tool_trace", []),
            )
            if sanitized_reply != str(done_payload.get("reply", "")):
                _audit_web(
                    "TOOL_EXECUTE",
                    "chat_stream",
                    f"terminal_dump_reply_sanitized_stream session_id={sid}",
                    {
                        "session_id": sid,
                        "tool_steps": int(done_payload.get("tool_steps", 0)),
                        "tool_trace_count": len(done_payload.get("tool_trace", []) or []),
                        "reply_len_before": len(str(done_payload.get("reply", ""))),
                        "reply_len_after": len(sanitized_reply),
                    },
                )
                done_payload["reply"] = sanitized_reply
                final_reply = sanitized_reply

            substeps = _build_substeps_from_history(
                agent,
                done_payload.get("tool_trace", []),
            )
            if pending_items:
                # Don't save final text when there are pending confirmations;
                # the text will be added after confirmation.
                substeps = [s for s in substeps if s.get("kind") != "text"]
            if not substeps:
                substeps = [{"kind": "text", "content": final_reply}]
            _store.replace_assistant_by_turn(sid, turn_id=turn_id, substeps=substeps)
            final_saved = True
            if not pending_items:
                compression_result = _maybe_auto_compress_after_llm(sid)
                if compression_result.get("compressed") is True:
                    compression_step = _build_context_compression_system_substep(compression_result)
                    if _store.append_to_assistant_by_turn(
                        sid,
                        turn_id=turn_id,
                        substeps=[compression_step],
                        replace_tool_results=False,
                    ):
                        _invalidate_session_index(sid)
                        done_payload["context_compression"] = compression_step.get("content", {})
            _audit_web(
                "PUBLIC_WRITE",
                "chat_stream",
                f"chat_messages_saved session_id={sid}",
                {"session_id": sid, "items_count": 2,
                 "tool_marker": bool(done_payload.get("tool_trace")),
                 "tool_trace_count": len(done_payload.get("tool_trace", []) or [])},
            )
            _generate_title_from_first_round(sid)
            yield _sse_event("done", done_payload)
        except Exception as e:
            import traceback
            traceback.print_exc()
            if draft_ready and not final_saved:
                _store.append_to_assistant_by_turn(
                    sid,
                    turn_id=turn_id,
                    substeps=[{
                        "kind": "text",
                        "content": f"[error/chat] 请求中断：{e}",
                    }],
                    replace_tool_results=False,
                )
            yield _sse_event("error", {"message": f"{e}\n{traceback.format_exc()}"})

    from starlette.responses import StreamingResponse

    return StreamingResponse(event_iter(), media_type="text/event-stream")


@app.get("/terminal/pending")
async def terminal_pending(session_id: str = Query(default="")):
    sid, _meta = _require_session_access(session_id, create=False)
    pending = _get_terminal_pending(sid)
    agent = _sessions.get(sid)
    if pending and (agent is None or not bool(getattr(agent, "has_pending_confirmation", lambda: False)())):
        _clear_terminal_pending(sid)
        pending = []
    return JSONResponse(
        {
            "ok": True,
            "session_id": sid,
            "pending": pending,
            "pending_confirm_count": len(pending),
        }
    )


@app.post("/terminal/confirm")
async def terminal_confirm(req: TerminalConfirmRequest):
    sid = str(req.session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
    sid, _meta = _require_session_access(sid)

    pending = _get_terminal_pending(sid)
    if not pending:
        return JSONResponse(
            {
                "ok": False,
                "error": "no pending confirmation for this session",
                "error_code": "no_pending_confirmation",
                "pending_confirm_count": 0,
                "pending": [],
            },
            status_code=409,
        )

    target_index = 0
    requested_call_id = str(req.call_id or "").strip()
    requested_cmd = str(req.cmd or "").strip()
    if requested_call_id:
        idx = next((i for i, row in enumerate(pending) if str(row.get("call_id", "")).strip() == requested_call_id), -1)
        if idx < 0:
            return JSONResponse(
                {"ok": False, "error": f"call_id not pending: {requested_call_id}"},
                status_code=400,
            )
        target_index = idx
    elif requested_cmd:
        idx = next((i for i, row in enumerate(pending) if str(row.get("cmd", "")).strip() == requested_cmd), -1)
        if idx >= 0:
            target_index = idx

    target = pending[target_index]
    approval = bool(req.approval)
    action = "allow" if approval else "deny"

    agent = _get_agent(sid, refresh_context=False)
    fresh = not bool(getattr(agent, "has_pending_confirmation", lambda: False)())

    if fresh:
        # Agent 被淘汰或 _held_messages 丢失时，直接执行命令并重建上下文继续
        from TindaAgent.Tool.tool import run_terminal
        import json as _json
        cmd = str(target.get("cmd", "")).strip()
        exec_result = run_terminal(cmd=cmd, _caller_perm=int(getattr(agent, "perm", 0)), _approval=approval)
        rows = _store.get_context_messages(sid)
        agent_rows, _ = _store_to_agent_messages(rows)
        agent.replace_conversation(agent_rows)
        call_id = f"call_recover_{sid}"
        agent.history.append({"role": "assistant", "content": None,
                              "reasoning_content": "",
                              "tool_calls": [{"id": call_id, "type": "function",
                                              "function": {"name": "run_terminal",
                                                           "arguments": _json.dumps({"cmd": cmd}, ensure_ascii=False)}}]})
        agent.history.append({"role": "tool", "tool_call_id": call_id,
                              "content": _json.dumps(exec_result, ensure_ascii=False)})
        _write_context_log(
            sid,
            _serialize_context_messages(agent.history),
            model=_llm.current_model,
            trigger="terminal_confirm_recover",
        )
        request_messages = agent._messages_for_llm_request(agent.history)
        result = agent._ensure_client().chat_with_tools(request_messages, user_perm=agent.perm, temperature=0.7)
    else:
        decision = {
            "approval": approval,
            "action": action,
            "confirm_id": str(target.get("confirm_id", "") or target.get("call_id", "")),
        }
        held = getattr(agent, "_held_messages", None) or []
        _write_context_log(
            sid,
            _serialize_context_messages(held),
            model=_llm.current_model,
            trigger="terminal_confirm_resume",
        )
        result = agent.resume_with_confirmations([decision])

    reply = str(result.get("reply", ""))
    tool_trace_raw = result.get("tool_trace", [])
    tool_trace = _sanitize_tool_trace_for_user(tool_trace_raw)
    tool_steps = int(result.get("tool_steps", 0))

    sanitized_reply = _sanitize_terminal_dump_reply(
        reply_text=reply,
        tool_steps=tool_steps,
        tool_trace=tool_trace,
    )
    if sanitized_reply != reply:
        _audit_web(
            "TOOL_EXECUTE",
            "terminal_confirm",
            f"terminal_dump_reply_sanitized_confirm session_id={sid}",
            {
                "session_id": sid,
                "tool_steps": int(tool_steps),
                "tool_trace_count": len(tool_trace or []),
                "reply_len_before": len(reply),
                "reply_len_after": len(sanitized_reply),
            },
        )
        reply = sanitized_reply

    confirm_turn_id = str(target.get("turn_id", "") or "").strip() or f"turn_{uuid.uuid4().hex[:12]}"

    # Build new substeps only from messages added after the held history
    held_len = len(getattr(agent, "_held_messages", []) or [])
    # _held_messages includes system + user + assistant + tool from before pending
    # After resume, agent.history = held_msgs + [system_instruction] + [assistant_response] + [tool_results...]
    new_msgs = agent.history[held_len:]
    new_substeps: list[dict] = []
    for m in new_msgs:
        if not isinstance(m, dict):
            continue
        if m.get("role") != "assistant":
            continue
        rc = m.get("reasoning_content")
        if rc and str(rc).strip():
            new_substeps.append({"kind": "thinking", "content": str(rc).strip()})
        text = m.get("content", "")
        if isinstance(text, str) and text.strip():
            clean_text = _sanitize_assistant_visible_text(text)
            if clean_text.strip():
                new_substeps.append({"kind": "text", "content": clean_text.strip()})
        tool_calls = m.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = str(fn.get("name", "unknown")) if isinstance(fn, dict) else "unknown"
                cid = str(tc.get("id", "") or "").strip()
                raw_args = fn.get("arguments", "{}") if isinstance(fn, dict) else "{}"
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    parsed_args = {}
                new_substeps.append({
                    "kind": "tool_marker",
                    "name": name, "ok": True,
                    "stdin": "", "stdout": "",
                    "id": cid.lstrip("tc_"),
                    "arguments": parsed_args if isinstance(parsed_args, dict) else {},
                    "result": {},
                })
    # Also inject tool results from tool_trace for new tool calls
    for step in tool_trace or []:
        if not isinstance(step, dict):
            continue
        cid = str(step.get("call_id", "") or "").strip()
        result = step.get("result", {}) if isinstance(step, dict) else {}
        ok = _tool_result_ok(result)
        stdout = _tool_result_output(result)
        # Find matching tool_marker and update with real data
        for s in new_substeps:
            if s.get("kind") == "tool_marker" and s.get("id") == cid.lstrip("tc_"):
                s["ok"] = ok
                s["stdout"] = stdout[:500]
                s["result"] = result if isinstance(result, dict) else {}
                break
    if new_substeps:
        _store.append_to_last_assistant(sid, new_substeps)
    _audit_web("PUBLIC_WRITE", "_append_assistant_continuation_messages",
               f"assistant_continuation_appended session_id={sid}",
               {"session_id": sid, "substeps": len(new_substeps),
                "tool_marker": bool(tool_steps > 0)})
    _generate_title_from_first_round(sid)

    remaining = [row for idx, row in enumerate(pending) if idx != target_index]
    next_new = _extract_pending_confirmation_items(tool_trace_raw)
    for it in next_new:
        it["turn_id"] = confirm_turn_id
    next_pending = remaining + next_new
    dedup_next: list[dict] = []
    seen_next: set[tuple[str, str]] = set()
    for row in next_pending:
        if not isinstance(row, dict):
            continue
        call_id = str(row.get("call_id", "") or "").strip()
        cmd = str(row.get("cmd", "") or "").strip()
        if not cmd:
            continue
        key = (call_id, cmd)
        if key in seen_next:
            continue
        seen_next.add(key)
        dedup_next.append(row)
    _set_terminal_pending(sid, dedup_next)

    context_compression_payload: dict[str, Any] | None = None
    if not dedup_next:
        compression_result = _maybe_auto_compress_after_llm(sid)
        if compression_result.get("compressed") is True:
            compression_step = _build_context_compression_system_substep(compression_result)
            if _store.append_to_last_assistant(sid, [compression_step]):
                _invalidate_session_index(sid)
                context_compression_payload = compression_step.get("content", {})

    # 回传原始 turn_id 以便前端合并气泡
    return JSONResponse(
        {
            "ok": True,
            "turn_id": confirm_turn_id,
            "session_id": sid,
            "approval": approval,
            "action": action,
            "cmd": str(target.get("cmd", "") or ""),
            "call_id": str(target.get("call_id", "") or ""),
            "reply": reply,
            "tool_trace": tool_trace,
            "tool_steps": tool_steps,
            "executed": approval,
            "awaiting_other_confirmations": len(dedup_next) > 0,
            "pending_confirmation": len(dedup_next) > 0,
            "pending_confirm_count": len(dedup_next),
            "pending": dedup_next,
            "context_compression": context_compression_payload,
        }
    )


@app.post("/reset")
async def reset_chat(req: ResetRequest):
    sid = str(req.session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
    sid, _meta = _require_session_access(sid)
    result = _store.mark_reset_anchor(sid)
    _sessions.pop(sid, None)
    _session_last_access.pop(sid, None)
    _session_config.pop(sid, None)
    _clear_terminal_pending(sid)
    _invalidate_session_index(sid)
    return JSONResponse({"ok": True, **result})


@app.post("/session/events")
async def session_events(req: SessionEventsRequest):
    """Persist frontend events: chat messages -> session file, terminal -> terminal file."""
    sid = str(req.session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
    sid, _meta = _require_session_access(sid)

    chat_rows: list[dict] = []
    term_rows: list[dict] = []
    for it in req.entries or []:
        if not isinstance(it, dict):
            continue
        content_text = str(it.get("content", "") or "")
        # Skip error entries
        if content_text.startswith("[error"):
            continue
        entry_type = str(it.get("entry_type", "") or "").strip()
        role = str(it.get("role", "assistant")).strip()

        if entry_type == "terminal":
            # Terminal entries go to separate file
            term_rows.append({
                "id": f"terminal_{uuid.uuid4().hex[:16]}",
                "type": "terminal",
                "display_target": "terminal",
                "context_policy": "include",
                "kind": str(it.get("terminal_kind", "out")).strip() or "out",
                "class": str(it.get("terminal_class", "") or "").strip().lower(),
                "content": content_text,
                "ts": str(it.get("ts", "") or _now_iso()),
            })
        else:
            # Chat entries go to session file
            if entry_type == "notice":
                msg = sa.build_system_message(content_text)
                msg["type"] = "system_notice"
                msg["display_target"] = "chat"
                msg["context_policy"] = "exclude"
            elif role not in {"user", "assistant", "system"}:
                role = "assistant"
                msg = {"role": "assistant", "id": sa.make_message_id(),
                       "type": "assistant_message",
                       "display_target": "chat",
                       "context_policy": "include",
                       "content": {"1": {"text": content_text}}}
            elif role == "system":
                msg = sa.build_system_message(content_text)
                msg["type"] = "system_notice"
                msg["display_target"] = "chat"
                msg["context_policy"] = "exclude"
            elif role == "assistant":
                msg = {"role": "assistant", "id": sa.make_message_id(),
                       "type": "assistant_message",
                       "display_target": "chat",
                       "context_policy": "include",
                       "content": {"1": {"text": content_text}}}
            elif role == "user":
                msg = {"role": "user", "id": sa.make_message_id(),
                       "type": "user_message",
                       "display_target": "chat",
                       "context_policy": "include",
                       "content": {"1": {"text": content_text}}}
            chat_rows.append(msg)

    result = {"ok": True, "session_id": sid}
    if chat_rows:
        try:
            result["chat_saved"] = _store.append_messages(sid, chat_rows)
            _invalidate_session_index(sid)
        except SessionStoreError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if term_rows:
        result["terminal_saved"] = _store.append_terminal(sid, term_rows)
    return JSONResponse(result)


@app.get("/sessions/{session_id}/terminal")
async def get_session_terminal(session_id: str):
    sid, _meta = _require_session_access(session_id, create=False)
    entries = _store.load_terminal(sid)
    return JSONResponse({"ok": True, "session_id": sid, "entries": sa.terminal_entries_to_frontend(entries)})


@app.post("/sessions/{session_id}/tool-jobs")
async def create_tool_job(session_id: str, req: ToolJobCreateRequest):
    sid, _meta = _require_session_access(session_id)
    if str(req.session_id or "").strip() != str(session_id).strip():
        return JSONResponse({"ok": False, "error": "session_id mismatch"}, status_code=400)
    profile = _get_web_profile()
    try:
        job = _tool_runtime.submit_command(sid, req.command, profile.perm)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "job": job})


@app.get("/sessions/{session_id}/tool-events")
async def get_tool_events(session_id: str, after_seq: int = 0, limit: int = 200):
    sid, _meta = _require_session_access(session_id, create=False)
    payload = _tool_runtime.get_events(sid, after_seq=after_seq, limit=limit)
    events = payload.get("events", [])
    if events:
        _persist_terminal_events(sid, events)
    return JSONResponse(payload)


@app.get("/sessions/{session_id}/tool-jobs/{job_id}")
async def get_tool_job(session_id: str, job_id: str):
    sid, _meta = _require_session_access(session_id, create=False)
    row = _tool_runtime.get_job(sid, job_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    return JSONResponse({"ok": True, "job": row})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("TindaAgent.Web.server:app", host="0.0.0.0", port=8000, reload=True)
