from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime

from fastapi import Body
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from TindaAgent.Process.AI.agent import Agent
from TindaAgent.Process.AI.client import LLMClient
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
    get_sessions_root,
)
from TindaAgent.Process.Observability import audit_event
from TindaAgent.Web.session_store import SessionStore, SessionStoreError, cleanup_legacy_chat_records
from TindaAgent.Web import session_adapter as sa
from TindaAgent.Web.settings_backend import (
    load_web_settings, save_web_settings,
    get_restore_last_session, get_last_session_id, set_last_session_id,
    load_terminal_settings, save_terminal_settings,
)
from TindaAgent.Web.tool_runtime import ToolRuntimeManager

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

_client = LLMClient()

_aux_model_cache: dict[str, LLMClient] = {}


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
            substeps.append({"kind": "text", "content": text.strip()})
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
                # Match by execution order: consume trace steps in sequence
                tinfo = {}
                if trace_idx < len(ordered_trace):
                    tinfo = ordered_trace[trace_idx]
                    trace_idx += 1
                tresult = tinfo.get("result", {}) if isinstance(tinfo, dict) else {}
                if isinstance(tresult, dict):
                    inner = tresult.get("result", {}) if isinstance(tresult, dict) else {}
                    if not isinstance(inner, dict):
                        inner = {}
                    # ok: explicit True/False, None=success, or infer from error/pending
                    if "ok" in tresult:
                        ok = tresult["ok"] if tresult["ok"] is not None else True
                    elif tresult.get("error") or tresult.get("pending_confirmation"):
                        ok = False
                    else:
                        ok = True
                    # stdout: explicit field, or format result payload as JSON
                    actual = tresult.get("result", {}) if isinstance(tresult.get("result"), dict) else {}
                    stdout = str(tresult.get("stdout", "") or inner.get("stdout", "")
                                or tresult.get("output", "") or inner.get("output", "")
                                or actual.get("stdout", "") or actual.get("output", "") or "")
                    if not stdout and actual:
                        import json as _json
                        stdout = _json.dumps(actual, ensure_ascii=False)
                    elif not stdout and tresult:
                        import json as _json
                        stdout = _json.dumps(tresult, ensure_ascii=False)
                else:
                    ok = True
                    stdout = ""
                    inner = {}
                stdin = str(fn.get("cmd", "") or fn.get("text", "") or fn.get("key", "") or "")
                substeps.append({
                    "kind": "tool_marker",
                    "name": name,
                    "ok": bool(ok),
                    "stdin": stdin[:500],
                    "stdout": stdout[:500],
                    "id": cid.lstrip("tc_"),
                })
    return substeps


def _get_aux_client(model_key: str, env_var: str, default_model: str) -> LLMClient:
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
    if model not in _aux_model_cache:
        _aux_model_cache[model] = LLMClient(model=model)
    return _aux_model_cache[model]
_version_mgr = get_version_manager()

_MIGRATION = bootstrap_storage()
_SESSIONS_ROOT = get_sessions_root()
_store = SessionStore(_SESSIONS_ROOT, legacy_root_dir=get_legacy_sessions_root())
_tool_runtime = ToolRuntimeManager()

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

_sessions: dict[str, Agent] = {}
_session_last_access: dict[str, float] = {}
_session_config: dict[str, dict] = {}
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
_JS_CHAT_RENDERER = _load_html("chat_renderer.js")
_LOG_ROOT = get_log_root()
_CONTEXT_LOG_FILE = _LOG_ROOT / "llm_context.jsonl"
_LOG_MAX_READ_BYTES = 2 * 1024 * 1024


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
_AUTH_OPEN_PATHS = {
    "/",
    "/home",
    "/chat",
    "/app",
    "/settings",
    "/logs",
    "/model-diagnostics",
    "/favicon.ico",
    "/system/version",
    "/user-admin",
    "/auth/users",
    "/auth/status",
    "/auth/select-user",
    "/web-settings",
    "/chat_renderer.js",
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
    meta_user_name: str | None = None
    meta_user_id: str | None = None
    meta_user_perm: str | None = None
    meta_time_iso: str | None = None
    meta_time_text: str | None = None


class ModelSwitchRequest(BaseModel):
    model: str


class SessionCreateRequest(BaseModel):
    title: str | None = "新对话"
    current_session_id: str | None = None
    reuse_if_current_empty: bool = False


class SessionTitleRequest(BaseModel):
    title: str


class SessionCompressRequest(BaseModel):
    session_id: str


class SessionMessagesQuery(BaseModel):
    include_hidden: bool = False


class ToolJobCreateRequest(BaseModel):
    session_id: str
    command: str


class SessionEventsRequest(BaseModel):
    session_id: str
    entries: list[dict] = Field(default_factory=list)


class ResetRequest(BaseModel):
    session_id: str | None = None


class ToolLegacyRequest(BaseModel):
    session_id: str


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
    tests: list[str] = Field(default_factory=list)
    image_url: str | None = None
    video_url: str | None = None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_model_choice(raw: str | None) -> str | None:
    key = str(raw or "").strip().lower()
    if not key:
        return None
    return _MODEL_ALIAS.get(key)


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
    return raw


def _estimate_context_usage_length(rows: list[dict]) -> int:
    from TindaAgent.Process.AI.tokenizer import estimate_tokens
    total = 0
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        content = str(row.get("content", "") or "")
        if content.strip():
            total += estimate_tokens(content)
    return int(total)


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

        resp = _client._client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.2,
            max_tokens=120,
            timeout=25,
        )
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
            {"test": key, "model": model_name},
        )
        row = _run_model_diagnostic_single(
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


def _looks_like_terminal_dump(text: str) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
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
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", ""))
        if not content.strip():
            stats["skipped"] += 1; continue
        if role == "system":
            out.append({"role": "assistant", "content": content})
        elif role in ("user", "assistant"):
            out.append({"role": role, "content": content})
        stats["included"] += 1
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
        "token": str(user.get_token()),
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
            client=_client,
            model_name=_client.model,
        )
        model = _client.model
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
        model = _client.model
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
                "model": _client.model,
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
                "model": _client.model,
                "refresh_context": False,
            },
        )
    cfg = _session_config.get(sid, {})
    mt = cfg.get("max_context_tokens")
    if isinstance(mt, int) and mt >= 100:
        _sessions[sid].max_context_tokens = int(mt)
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
            ok = result.get("ok", False) if isinstance(result, dict) else False
            inner = result.get("result", {}) if isinstance(result, dict) else {}
            stdin = ""
            if isinstance(args, dict):
                stdin = str(args.get("cmd") or args.get("text") or args.get("key") or "")
            stdout = ""
            if isinstance(result, dict):
                stdout = str(result.get("stdout") or inner.get("stdout")
                            or result.get("output") or inner.get("output") or "")
            substeps.append({
                "kind": "tool_marker",
                "name": name,
                "ok": bool(ok),
                "stdin": stdin[:500],
                "stdout": stdout[:500],
                "id": cid.lstrip("tc_"),
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
            inner = result.get("result", {}) if isinstance(result, dict) else {}
            ok = result.get("ok", False) if isinstance(result, dict) else False
            stdin = str(args.get("cmd") or args.get("text") or "") if isinstance(args, dict) else ""
            stdout = ""
            if isinstance(result, dict):
                stdout = str(result.get("stdout") or inner.get("stdout")
                            or result.get("output") or inner.get("output") or "")
            substeps.append({"tool_marker": {
                "name": name, "ok": bool(ok),
                "stdin": stdin[:500], "stdout": stdout[:500],
                "id": cid.lstrip("tc_"),
            }})
    if reply:
        substeps.append({"text": reply})
    if substeps:
        _store.append_to_last_assistant(session_id, substeps)
    _audit_web("PUBLIC_WRITE", "_append_assistant_continuation_messages",
               f"assistant_continuation_appended session_id={session_id}",
               {"session_id": session_id, "substeps": len(substeps),
                "tool_marker": bool(tool_marker)})
    rows: list[dict] = []
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
                "id": f"m_{uuid.uuid4().hex[:16]}",
                "role": "assistant",
                "content": str(e.get("text", "")),
                "entry_type": "terminal",
                "terminal_kind": kind,
                "terminal_class": str(e.get("class", "") or "").strip().lower(),
                "created_at": str(e.get("ts", "")) or _now_iso(),
                "is_summary": False,
            }
        )
    if rows:
        _store.append_messages(session_id, rows)
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
    rows: list[Path] = []
    primary = _LOG_ROOT / "total.jsonl"
    if primary.exists() and primary.is_file():
        rows.append(primary)
    legacy = get_legacy_log_root() / "total.jsonl"
    if legacy.exists() and legacy.is_file():
        try:
            if legacy.resolve() != primary.resolve():
                rows.append(legacy)
        except Exception:
            rows.append(legacy)
    # TINDA_HOME 迁移前的旧数据目录（默认 HOME 下的 .tinda/agent/log）
    try:
        home_default = Path.home() / ".tinda" / "agent" / "log" / "total.jsonl"
        if home_default.exists() and home_default.is_file():
            if home_default.resolve() != primary.resolve():
                rows.append(home_default)
    except Exception:
        pass
    return rows


def _find_audit_event_by_id(event_id: int) -> dict | None:
    """线性扫描 total.jsonl 及归档文件查找事件。"""
    target = int(event_id)
    for path in _resolve_total_jsonl_candidates():
        try:
            with path.open("r", encoding="utf-8") as fp:
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


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return _HTML_SETTINGS


# ── Web Settings API ───────────────────────────────────────────────
@app.get("/web-settings")
async def get_web_settings():
    return load_web_settings()


@app.put("/web-settings")
async def put_web_settings(data: dict[str, Any] = Body(...)):
    _require_login()
    save_web_settings(data)
    return load_web_settings()


# ── Terminal Settings API ──────────────────────────────────────────
@app.get("/terminal/settings")
async def get_terminal_settings():
    return load_terminal_settings()


@app.put("/terminal/settings")
async def put_terminal_settings(data: dict[str, Any] = Body(...)):
    _require_login()
    return save_terminal_settings(
        whitelist=data.get("whitelist"),
        blacklist=data.get("blacklist"),
        bypass_terminal_confirm=data.get("bypass_terminal_confirm"),
    )


@app.get("/model-diagnostics", response_class=HTMLResponse)
async def model_diagnostics_page():
    return _HTML_MODEL_DIAGNOSTICS


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

    target_model = _normalize_model_choice(req.model) or str(_client.model or "").strip()
    if not target_model:
        return JSONResponse({"ok": False, "error": "model 无效"}, status_code=400)

    image_url = _sanitize_diagnostic_url(req.image_url)
    video_url = _sanitize_diagnostic_url(req.video_url)

    started_at = _now_iso()
    rows = _run_model_diagnostics(
        model_name=target_model,
        tests=tests,
        image_url=image_url,
        video_url=video_url,
    )
    return JSONResponse(
        {
            "ok": True,
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
    p = _get_web_profile(current)
    return JSONResponse(
        {
            "logged_in": True,
            "user": {
                "name": p.name,
                "uid": p.uid,
                "perm": p.perm,
                "perm_label": p.perm_label,
                "token": p.token,
            },
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
    p = _get_web_profile(current)
    return JSONResponse(
        {
            "ok": True,
            "logged_in": True,
            "user": {
                "name": p.name,
                "uid": p.uid,
                "perm": p.perm,
                "perm_label": p.perm_label,
                "token": p.token,
            },
        }
    )


@app.get("/auth/users")
async def auth_users():
    users = []
    for u in userdata.iter_users():
        if userdata.is_system_user(u):
            continue
        users.append(
            {
                "uid": str(u.get_uid()),
                "name": str(u.get_name()),
                "perm": int(u.get_perm()),
                "perm_label": _perm_label(int(u.get_perm())),
                "token": str(u.get_token()),
            }
        )
    return JSONResponse({"users": users})


@app.get("/user/profile")
async def user_profile():
    _require_login()
    p = _get_web_profile()
    return JSONResponse(
        {
            "name": p.name,
            "uid": p.uid,
            "perm": p.perm,
            "perm_label": p.perm_label,
            "token": p.token,
        }
    )


@app.get("/users")
async def list_users():
    _require_login()
    users = []
    for u in userdata.iter_users():
        if userdata.is_system_user(u):
            continue
        users.append(
            {
                "uid": str(u.get_uid()),
                "name": str(u.get_name()),
                "perm": int(u.get_perm()),
                "perm_label": _perm_label(int(u.get_perm())),
                "token": str(u.get_token()),
            }
        )
    current = sec_get_current_user()
    current_uid = str(current.get_uid()) if current is not None else ""
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
    p = _get_web_profile()
    return JSONResponse(
        {
            "ok": True,
            "name": p.name,
            "uid": p.uid,
            "perm": p.perm,
            "perm_label": p.perm_label,
            "token": p.token,
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
    return JSONResponse({"ok": True, "user": _as_user_row(created)})


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
    return JSONResponse({"ok": True, "user": _as_user_row(updated, current_uid=str(current.get_uid()))})


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
    return JSONResponse({"current_model": _client.model, "available_models": list(_MODEL_CHOICES)})


@app.post("/model")
async def switch_model(req: ModelSwitchRequest):
    _require_admin_user()
    target = _normalize_model_choice(req.model)
    if not target:
        return JSONResponse(
            {
                "ok": False,
                "error": "unsupported model",
                "available_models": list(_MODEL_CHOICES),
            },
            status_code=400,
        )
    _client.model = target
    return JSONResponse(
        {
            "ok": True,
            "current_model": _client.model,
            "available_models": list(_MODEL_CHOICES),
        }
    )


@app.post("/sessions")
async def create_session(req: SessionCreateRequest):
    _require_login()
    if bool(req.reuse_if_current_empty):
        current_id = str(req.current_session_id or "").strip()
        if current_id:
            meta = _store.get_session(current_id) or {}
            try:
                msg_count = int(meta.get("message_count", 0))
            except Exception:
                msg_count = 0
            if msg_count <= 0 and str(meta.get("id", "")).strip():
                return JSONResponse({"ok": True, "session": meta, "reused": True})
    row = _store.create_session(title=str(req.title or "新对话"))
    return JSONResponse({"ok": True, "session": row, "reused": False})


@app.get("/sessions")
async def list_sessions(limit: int = 100, offset: int = 0):
    _require_login()
    return JSONResponse(_store.list_sessions(limit=limit, offset=offset))


@app.patch("/sessions/{session_id}/config")
async def patch_session_config(session_id: str, data: dict[str, Any] = Body(...)):
    _require_login()
    sid = str(session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
    _store.ensure_session(sid)
    cfg = _session_config.setdefault(sid, {})
    if "max_context_tokens" in data:
        v = data["max_context_tokens"]
        cfg["max_context_tokens"] = max(100, int(v)) if v is not None else None
    _audit_web("SYSTEM_WRITE", "patch_session_config",
               f"session_config_updated session_id={sid}",
               {"session_id": sid, "config": dict(cfg)})
    return JSONResponse({"ok": True, "session_id": sid, "config": dict(cfg)})


@app.get("/sessions/{session_id}/config")
async def get_session_config(session_id: str):
    _require_login()
    sid = str(session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
    cfg = dict(_session_config.get(sid, {}))
    _audit_web("SYSTEM_READ", "get_session_config",
               f"session_config_read session_id={sid}",
               {"session_id": sid, "config": cfg})
    return JSONResponse({"ok": True, "session_id": sid, "config": cfg})


def _maybe_auto_compress(session_id: str, context_rows: list[dict]) -> dict:
    """自动压缩：raw chat 消息数 >= 80 或 tokens 超限时触发。"""
    sid = str(session_id or "").strip()
    agent = _sessions.get(sid)
    if agent is None:
        return {"compressed": False, "reason": "no_agent"}

    max_tokens = int(getattr(agent, "max_context_tokens", 16000))
    tokens_before = int(agent.estimate_current_tokens())

    chat_count = sum(
        1 for r in context_rows
        if str(r.get("entry_type", "chat")) == "chat"
        and str(r.get("role", "")) in {"user", "assistant"}
        and not bool(r.get("is_summary", False))
    )

    trigger = ""
    if chat_count >= 80 and tokens_before <= max_tokens:
        trigger = "raw_chat_count"
    elif tokens_before > max_tokens:
        trigger = "token"

    if not trigger:
        return {
            "compressed": False,
            "reason": "below_threshold",
            "chat_count": chat_count,
            "estimated_tokens_before": tokens_before,
            "max_context_tokens": max_tokens,
        }

    raw_rows = [
        r for r in context_rows
        if str(r.get("entry_type", "chat")) == "chat"
        and str(r.get("role", "")) in {"user", "assistant"}
        and not bool(r.get("is_summary", False))
    ]
    if len(raw_rows) < 6:
        return {
            "compressed": False,
            "reason": "insufficient_messages",
            "estimated_tokens_before": tokens_before,
        }

    summary_src = raw_rows[:-4]
    try:
        summary = _compress_messages_with_llm(summary_src)
        if not summary:
            return {
                "compressed": False,
                "reason": "empty_summary",
                "estimated_tokens_before": tokens_before,
            }
        _store.compress_context(sid, summary)
        rows = _store.get_context_messages(sid)
        agent_rows, _filter_stats = _store_to_agent_messages(rows)
        agent.replace_conversation(agent_rows)
    except Exception:
        return {
            "compressed": False,
            "reason": "compress_error",
            "estimated_tokens_before": tokens_before,
        }

    tokens_after = int(agent.estimate_current_tokens())
    return {
        "compressed": True,
        "trigger": trigger,
        "estimated_tokens_before": tokens_before,
        "estimated_tokens_after": tokens_after,
    }


@app.get("/sessions/{session_id}/context-usage")
async def get_session_context_usage(session_id: str):
    _require_login()
    sid = str(session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)

    _store.ensure_session(sid)
    rows = _store.get_context_messages(sid)
    usage_length = _estimate_context_usage_length(rows)
    meta = _store.get_session(sid) or {}
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
            "max_context_tokens": _session_config.get(sid, {}).get("max_context_tokens"),
        }
    )


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    _require_login()
    ok = _store.delete_session(session_id)
    _sessions.pop(session_id, None)
    _session_last_access.pop(session_id, None)
    _session_config.pop(session_id, None)
    _clear_terminal_pending(session_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "session not found"}, status_code=404)
    return JSONResponse({"ok": True, "session_id": session_id})


@app.delete("/sessions")
async def delete_all_sessions():
    _require_login()
    payload = _store.list_sessions(limit=10000, offset=0)
    sessions_list = payload.get("sessions", [])
    deleted = 0
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
            deleted += 1
        except Exception:
            pass
    return JSONResponse({"ok": True, "deleted": deleted})


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    _require_login()
    _store.ensure_session(session_id)
    data = _store.load_messages(session_id)
    entries = sa.store_dict_to_frontend(data)
    return JSONResponse({"ok": True, "session_id": session_id, "entries": entries})


@app.post("/sessions/{session_id}/title")
async def update_session_title(session_id: str, req: SessionTitleRequest):
    _require_login()
    row = _store.set_session_title(session_id, req.title)
    return JSONResponse({"ok": True, "session": row})


@app.post("/sessions/{session_id}/compress")
async def compress_session_context(session_id: str):
    current = _require_login()
    if not _has_llm_perm(current):
        return JSONResponse({"ok": False, "error": "权限不足：当前账户不可执行上下文压缩"}, status_code=403)
    rows = _store.get_context_messages(session_id)
    _audit_web(
        "SYSTEM_EXECUTE",
        "compress_session_context",
        f"compress_start session_id={session_id}",
        {"session_id": session_id, "context_rows": len(rows)},
    )
    # 只拿原始 chat 做摘要，不把终端/notice/tool_marker 混进去
    raw_rows = [
        x for x in rows
        if not bool(x.get("is_summary", False))
        and str(x.get("entry_type", "chat")) == "chat"
        and str(x.get("role", "")) in {"user", "assistant"}
    ]
    if len(raw_rows) < 6:
        return JSONResponse({"ok": False, "error": "消息数量不足，至少需要 6 条消息才能压缩"}, status_code=400)

    summary_src = raw_rows[:-4]
    if not summary_src:
        return JSONResponse({"ok": False, "error": "消息数量不足，至少需要 6 条消息才能压缩"}, status_code=400)

    try:
        summary = _compress_messages_with_llm(summary_src)
        if not summary:
            raise ValueError("摘要为空")
        result = _store.compress_context(session_id, summary)
    except SessionStoreError as e:
        _audit_web("SYSTEM_EXECUTE", "compress_session_context",
                   f"compress_failed session_id={session_id} err={e}",
                   {"session_id": session_id, "ok": False, "error": str(e)})
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("compress failed: session=%s err=%s", session_id, e)
        _audit_web("SYSTEM_EXECUTE", "compress_session_context",
                   f"compress_error session_id={session_id} err={e}",
                   {"session_id": session_id, "ok": False, "error": str(e)})
        return JSONResponse({"ok": False, "error": "压缩失败"}, status_code=500)

    _audit_web("SYSTEM_EXECUTE", "compress_session_context",
               f"compress_done session_id={session_id}",
               {"session_id": session_id, "ok": True, **{k: v for k, v in result.items() if k != "session_id"}})
    return JSONResponse({"ok": True, **result})


@app.post("/chat")
async def chat(req: ChatRequest):
    current = _require_login()
    if not _has_llm_perm(current):
        return JSONResponse({"error": "权限不足：当前账户不可调用 LLM 对话"}, status_code=403)
    sid = str(req.session_id or "").strip()
    if not sid:
        return JSONResponse({"error": "session_id required"}, status_code=400)

    _store.ensure_session(sid)
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
    if not message:
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
        return JSONResponse(
            {
                "reply": "> --调用工具中--",
                "tool_trace": [],
                "tool_steps": 0,
                "tool_job": job,
                "tool_async": True,
            }
        )

    llm_message = _build_user_message_with_meta(
        message,
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
    items = [sa.build_user_message(_strip_user_meta_block(llm_message))]
    if substeps:
        items.append(sa.build_assistant_message(substeps))
    elif save_reply.strip():
        items.append(sa.build_assistant_message([{"kind": "text", "content": save_reply}]))
    else:
        items.append(sa.build_assistant_message([{"kind": "text", "content": reply}]))
    try:
        _store.append_messages(sid, items)
    except Exception:
        import traceback
        traceback.print_exc()
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
        }
    )


@app.get("/chat/stream")
async def chat_stream(
    message: str,
    session_id: str,
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

    _store.ensure_session(sid)
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

        chunks = [
            _sse_event("reset", {}),
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

    llm_message = _build_user_message_with_meta(
        text,
        meta_user_name=meta_user_name,
        meta_user_id=meta_user_id,
        meta_user_perm=meta_user_perm,
        meta_time_iso=meta_time_iso,
        meta_time_text=meta_time_text,
    )

    def event_iter():
        final_reply = ""
        reasoning_text = ""
        done_payload: dict | None = None
        try:
            for event in agent.stream_chat_events(llm_message):
                et = event.get("type", "")
                if et == "delta":
                    final_reply += str(event.get("content", ""))
                    yield _sse_event("delta", {"content": event.get("content", "")})
                elif et == "reasoning_delta":
                    reasoning_text += str(event.get("content", ""))
                    yield _sse_event("reasoning_delta", {"content": event.get("content", "")})
                elif et == "reset":
                    yield _sse_event("reset", {})
                elif et == "tool_step":
                    yield _sse_event("tool_step", {"trace": event.get("trace", [])})
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

            yield _sse_event("done", done_payload)
            substeps = _build_substeps_from_history(
                agent,
                done_payload.get("tool_trace", []),
            )
            if pending_items:
                # Don't save final text when there are pending confirmations;
                # the text will be added after confirmation.
                substeps = [s for s in substeps if s.get("kind") != "text"]
            items = [sa.build_user_message(_strip_user_meta_block(llm_message))]
            if substeps:
                items.append(sa.build_assistant_message(substeps))
            else:
                items.append(sa.build_assistant_message([{"kind": "text", "content": final_reply}]))
            _store.append_messages(sid, items)
            _audit_web(
                "PUBLIC_WRITE",
                "chat_stream",
                f"chat_messages_saved session_id={sid}",
                {"session_id": sid, "items_count": len(items),
                 "tool_marker": bool(done_payload.get("tool_trace")),
                 "tool_trace_count": len(done_payload.get("tool_trace", []) or [])},
            )
            _generate_title_from_first_round(sid)
        except Exception as e:
            import traceback
            traceback.print_exc()
            yield _sse_event("error", {"message": f"{e}\n{traceback.format_exc()}"})

    from starlette.responses import StreamingResponse

    return StreamingResponse(event_iter(), media_type="text/event-stream")


@app.get("/terminal/pending")
async def terminal_pending(session_id: str = Query(default="")):
    _require_login()
    sid = str(session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
    _store.ensure_session(sid)
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
    _require_login()
    sid = str(req.session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
    _store.ensure_session(sid)

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
                              "tool_calls": [{"id": call_id, "type": "function",
                                              "function": {"name": "run_terminal",
                                                           "arguments": _json.dumps({"cmd": cmd}, ensure_ascii=False)}}]})
        agent.history.append({"role": "tool", "tool_call_id": call_id,
                              "content": _json.dumps(exec_result, ensure_ascii=False)})
        _write_context_log(
            sid,
            _serialize_context_messages(agent.history),
            model=_client.model,
            trigger="terminal_confirm_recover",
        )
        result = agent._ensure_client().chat_with_tools(agent.history, user_perm=agent.perm, temperature=0.7)
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
            model=_client.model,
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

    _append_assistant_continuation_messages(
        sid,
        reply,
        turn_id=confirm_turn_id,
        tool_marker=False,
        tool_trace=tool_trace,
    )
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
        }
    )


@app.post("/reset")
async def reset_chat(req: ResetRequest):
    _require_login()
    sid = str(req.session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
    _store.ensure_session(sid)
    result = _store.mark_reset_anchor(sid)
    _sessions.pop(sid, None)
    _session_last_access.pop(sid, None)
    _session_config.pop(sid, None)
    _clear_terminal_pending(sid)
    return JSONResponse({"ok": True, **result})


@app.post("/tools")
async def tools_legacy(req: ToolLegacyRequest):
    _require_login()
    # 兼容旧前端：保留接口
    profile = _get_web_profile()
    from TindaAgent.Tool import tool as tool_registry

    return JSONResponse({"tools": tool_registry.list_tools(profile.perm)})


@app.post("/session/events")
async def session_events(req: SessionEventsRequest):
    """Persist frontend events: chat messages -> session file, terminal -> terminal file."""
    _require_login()
    sid = str(req.session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)

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
                "kind": str(it.get("terminal_kind", "out")).strip() or "out",
                "class": str(it.get("terminal_class", "") or "").strip().lower(),
                "content": content_text,
                "ts": str(it.get("ts", "") or _now_iso()),
            })
        else:
            # Chat entries go to session file
            if role not in {"user", "assistant", "system"}:
                role = "assistant"
            if role == "system":
                msg = sa.build_system_message(content_text)
            elif role == "assistant":
                msg = {"role": "assistant", "id": sa.make_message_id(),
                       "content": {"1": {"text": content_text}}}
            elif role == "user":
                msg = {"role": "user", "id": sa.make_message_id(),
                       "content": {"1": {"text": content_text}}}
            chat_rows.append(msg)

    result = {"ok": True, "session_id": sid}
    if chat_rows:
        try:
            result["chat_saved"] = _store.append_messages(sid, chat_rows)
        except SessionStoreError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if term_rows:
        result["terminal_saved"] = _store.append_terminal(sid, term_rows)
    return JSONResponse(result)


@app.get("/sessions/{session_id}/terminal")
async def get_session_terminal(session_id: str):
    _require_login()
    entries = _store.load_terminal(session_id)
    return JSONResponse({"ok": True, "session_id": session_id, "entries": entries})


@app.post("/sessions/{session_id}/tool-jobs")
async def create_tool_job(session_id: str, req: ToolJobCreateRequest):
    _require_login()
    if str(req.session_id or "").strip() != str(session_id).strip():
        return JSONResponse({"ok": False, "error": "session_id mismatch"}, status_code=400)
    profile = _get_web_profile()
    try:
        job = _tool_runtime.submit_command(session_id, req.command, profile.perm)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "job": job})


@app.get("/sessions/{session_id}/tool-events")
async def get_tool_events(session_id: str, after_seq: int = 0, limit: int = 200):
    _require_login()
    payload = _tool_runtime.get_events(session_id, after_seq=after_seq, limit=limit)
    events = payload.get("events", [])
    if events:
        _persist_terminal_events(session_id, events)
    return JSONResponse(payload)


@app.get("/sessions/{session_id}/tool-jobs/{job_id}")
async def get_tool_job(session_id: str, job_id: str):
    _require_login()
    row = _tool_runtime.get_job(session_id, job_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    return JSONResponse({"ok": True, "job": row})


# 兼容旧记录面板接口：映射到新会话结构
@app.get("/records")
async def records_compat(limit: int = 50, offset: int = 0, q: str = ""):
    rows = _store.list_sessions(limit=limit, offset=offset).get("sessions", [])
    if q:
        kw = q.lower().strip()
        rows = [x for x in rows if kw in str(x.get("id", "")).lower() or kw in str(x.get("title", "")).lower()]
    mapped = [
        {
            "record_id": str(x.get("id", "")),
            "session_id": str(x.get("id", "")),
            "created_at": x.get("created_at", ""),
            "updated_at": x.get("updated_at", ""),
            "message_count": x.get("message_count", 0),
            "size_bytes": 0,
            "has_md": True,
            "has_txt": True,
        }
        for x in rows
    ]
    return JSONResponse({"records": mapped, "total": len(mapped), "limit": limit, "offset": offset})


@app.get("/records/session")
async def records_session_compat(session_id: str):
    rows = _store.load_messages(session_id)
    if not rows:
        return JSONResponse({"found": False, "session_id": session_id})
    meta = _store.get_session(session_id) or {}
    return JSONResponse(
        {
            "found": True,
            "session_id": session_id,
            "record": {
                "record_id": session_id,
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "message_count": len(rows),
            },
            "entries": rows,
        }
    )


class ImportRecordRequest(BaseModel):
    session_id: str
    record_id: str


@app.post("/records/import")
async def import_record_compat(req: ImportRecordRequest):
    # 新架构不再导入旧 record，兼容返回当前会话
    rows = _store.load_messages(req.record_id)
    if not rows:
        return JSONResponse({"ok": False, "error": "记录不存在或不再支持旧格式导入"}, status_code=400)
    _store.delete_session(req.session_id)
    _store.create_session(req.session_id, title="新对话")
    _store.append_messages(req.session_id, rows)
    return JSONResponse({"ok": True, "session_id": req.session_id, "entries": rows})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("TindaAgent.Web.server:app", host="0.0.0.0", port=8000, reload=True)
