from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
import gzip
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from TindaAgent.Process.AI.agent import Agent
from TindaAgent.Process.AI.client import LLMClient
from TindaAgent.Process.AI.tokenizer import estimate_messages_tokens
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
from TindaAgent.Web import records_store
from TindaAgent.Web.session_store import SessionStore, SessionStoreError, cleanup_legacy_chat_records
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
_title_client = LLMClient(model="deepseek-v4-flash")
_compress_client = LLMClient(model="deepseek-v4-flash")
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

_sessions: dict[str, Agent] = {}
_session_last_access: dict[str, float] = {}
_MAX_SESSIONS = 300
_LLM_EXECUTE_PERM = int(perm.PUBLIC_EXECUTE)

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

_HTML_HOME = (Path(__file__).parent / "home.html").read_text(encoding="utf-8")
_HTML_CHAT = (Path(__file__).parent / "chat.html").read_text(encoding="utf-8")
_HTML_USER_ADMIN = (Path(__file__).parent / "user_admin.html").read_text(encoding="utf-8")
_HTML_LOG_VIEW = (Path(__file__).parent / "logs.html").read_text(encoding="utf-8")
_HTML_MODEL_DIAGNOSTICS = (Path(__file__).parent / "model_diagnostics.html").read_text(encoding="utf-8")
_HTML_SETTINGS = (Path(__file__).parent / "settings.html").read_text(encoding="utf-8")
_LOG_ROOT = get_log_root()
_ACTIVE_LOG_ROOT_ENV = "TINDA_ACTIVE_LOG_ROOT"
_LOG_MAX_READ_BYTES = 2 * 1024 * 1024
_AUTH_OPEN_PATHS = {
    "/",
    "/home",
    "/chat",
    "/app",
    "/logs",
    "/model-diagnostics",
    "/settings",
    "/favicon.ico",
    "/system/version",
    "/user-admin",
    "/auth/users",
    "/auth/status",
    "/auth/select-user",
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

    # 产品策略：切换禁用时，不保留"已选版本"历史错位；检测到错位自动对齐 current.json。
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
        "switch_enabled": False,
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


def _new_turn_id() -> str:
    return f"turn_{uuid.uuid4().hex[:16]}"


def _normalize_turn_id(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"[^A-Za-z0-9._:-]+", "_", text)
    return text[:80]


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
    message = str(raw_message or "").strip()
    if "[USER_META]" in message and "[/USER_META]" in message:
        return message

    values = {
        "name": _sanitize_meta_value(meta_user_name),
        "uid": _sanitize_meta_value(meta_user_id),
        "perm": _sanitize_meta_value(meta_user_perm),
        "time_iso": _sanitize_meta_value(meta_time_iso),
        "time_text": _sanitize_meta_value(meta_time_text),
    }
    has_meta = any(str(v or "").strip() not in {"", "N/A"} for v in values.values())
    if not has_meta:
        return message

    meta_block = (
        "---\n"
        "[USER_META]\n"
        f"name={values['name']}\n"
        f"uid={values['uid']}\n"
        f"perm={values['perm']}\n"
        f"time_iso={values['time_iso']}\n"
        f"time_text={values['time_text']}\n"
        "[/USER_META]"
    )
    if not message:
        return meta_block
    return f"{message}\n\n{meta_block}"


def _strip_user_meta_block(content: str) -> str:
    text = str(content or "")
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, dict) and str(parsed.get("format", "")).strip() == _LLM_CONTEXT_PAYLOAD_FORMAT:
        return str(parsed.get("content", "") or "").strip()
    return records_store.strip_user_meta_block(text)


def _trim_terminal_followup_output(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return "(no output)"
    if len(text) <= _TERMINAL_FOLLOWUP_OUTPUT_MAX_CHARS:
        return text
    return text[:_TERMINAL_FOLLOWUP_OUTPUT_MAX_CHARS] + "\n...(output truncated)"


def _extract_terminal_confirm_origin_user_intent(rows: list[dict], confirm_id: str) -> str:
    pos = -1
    cid = str(confirm_id or "").strip()
    for idx, row in enumerate(rows or []):
        if str(row.get("id", "") or "").strip() == cid and str(row.get("entry_type", "")).strip() == "terminal_confirm":
            pos = idx
            break
    if pos < 0:
        pos = len(rows or [])

    for idx in range(pos - 1, -1, -1):
        row = rows[idx] if isinstance(rows[idx], dict) else {}
        if str(row.get("role", "")).strip() != "user":
            continue
        if str(row.get("entry_type", "")).strip() != "chat":
            continue
        content = _strip_user_meta_block(str(row.get("content", "") or "")).strip()
        if not content:
            continue
        if _is_tool_command_text(content):
            continue
        return content
    return ""


def _build_terminal_followup_user_input_json(
    *,
    confirm_id: str,
    cmd: str,
    result: dict,
    origin_user_intent: str,
) -> str:
    intent = str(origin_user_intent or "").strip() or "（未提供明确诉求）"
    rc = result.get("returncode", "")
    output_text = _trim_terminal_followup_output(str(result.get("output", "") or ""))
    content = (
        "[TERMINAL_FOLLOWUP]\n"
        "你正在处理一条已确认执行的终端命令。请仅围绕本次执行结果回复，不要输出身份介绍或底层模型信息。\n"
        f"用户原始诉求: {intent}\n"
        f"命令: {str(cmd or '').strip()}\n"
        f"返回码: {rc}\n"
        "输出:\n"
        f"{output_text}\n\n"
        "回答要求:\n"
        "1) 明确命令是否成功。\n"
        "2) 给出关键输出结论。\n"
        "3) 若失败，给出下一步建议。"
    )
    return content


def _build_terminal_confirm_fallback_reply(cmd: str, result: dict) -> str:
    rc = result.get("returncode", "")
    output_text = _trim_terminal_followup_output(str(result.get("output", "") or ""))
    status_text = "成功" if str(rc) == "0" else "失败"
    return (
        f"命令 `{cmd}` 已执行（返回码 {rc}，{status_text}）。\n\n"
        f"输出结果：\n\n```\n{output_text}\n```"
    )


def _is_terminal_followup_reply_relevant(reply: str, *, cmd: str, origin_user_intent: str) -> bool:
    text = str(reply or "").strip()
    if not text:
        return False
    low = text.lower()
    if "底层技术信息保密" in text:
        return False
    if "我是 tindaagent" in low and ("命令" not in text and "输出" not in text):
        return False

    cmd_head = str(cmd or "").strip().split(" ", 1)[0].strip().lower()
    if cmd_head and cmd_head in low:
        return True

    intent_tokens = [tok for tok in re.findall(r"[a-zA-Z0-9_./:-]+", str(origin_user_intent or "").lower()) if len(tok) >= 2]
    for tok in intent_tokens[:4]:
        if tok in low:
            return True

    for kw in ("命令", "执行", "输出", "返回码", "成功", "失败", "超时", "确认", "完成", "result", "stdout", "stderr", "error"):
        if kw in text or kw in low:
            return True
    return False


def _normalize_terminal_confirm_status(raw: str | None) -> str:
    text = str(raw or "").strip().lower()
    if text in {"allow", "allowed"}:
        return "allowed"
    if text in {"deny", "denied"}:
        return "denied"
    return "pending"


def _parse_terminal_confirm_row_payload(row: dict) -> dict:
    raw = row.get("content", "{}") if isinstance(row, dict) else "{}"
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    payload = dict(data)
    payload["cmd"] = str(payload.get("cmd", "") or "").strip()
    payload["status"] = _normalize_terminal_confirm_status(str(payload.get("status", "pending") or "pending"))
    action = str(payload.get("action", "") or "").strip().lower()
    if not action:
        if payload["status"] == "allowed":
            action = "allow"
        elif payload["status"] == "denied":
            action = "deny"
    if action:
        payload["action"] = action
    return payload


def _collect_pending_terminal_confirms(rows: list[dict], *, turn_id: str | None = None) -> list[dict]:
    tid_filter = _normalize_turn_id(turn_id) if turn_id is not None else ""
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("entry_type", "") or "").strip() != "terminal_confirm":
            continue
        row_tid = _normalize_turn_id(str(row.get("turn_id", "") or ""))
        if tid_filter and row_tid != tid_filter:
            continue
        cid = str(row.get("id", "") or "").strip()
        if not cid or cid in seen:
            continue
        payload = _parse_terminal_confirm_row_payload(row)
        if str(payload.get("status", "pending")) != "pending":
            continue
        seen.add(cid)
        out.append(
            {
                "confirm_id": cid,
                "cmd": str(payload.get("cmd", "") or ""),
                "turn_id": row_tid,
            }
        )
    return out


def _collect_turn_terminal_confirm_results(rows: list[dict], *, turn_id: str) -> list[dict]:
    tid = _normalize_turn_id(turn_id)
    if not tid:
        return []
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("entry_type", "") or "").strip() != "terminal_confirm":
            continue
        row_tid = _normalize_turn_id(str(row.get("turn_id", "") or ""))
        if row_tid != tid:
            continue
        payload = _parse_terminal_confirm_row_payload(row)
        status = str(payload.get("status", "pending"))
        if status not in {"allowed", "denied"}:
            continue
        out.append(
            {
                "confirm_id": str(row.get("id", "") or "").strip(),
                "cmd": str(payload.get("cmd", "") or "").strip(),
                "status": status,
                "action": str(payload.get("action", "") or ""),
                "executed": bool(payload.get("executed") is True),
                "returncode": payload.get("returncode", None),
                "output": str(payload.get("output", "") or ""),
            }
        )
    return out


def _build_terminal_batch_followup_user_input_json(
    *,
    confirm_results: list[dict],
    origin_user_intent: str,
) -> str:
    intent = str(origin_user_intent or "").strip() or "（未提供明确诉求）"
    lines = [
        "[TERMINAL_FOLLOWUP_BATCH]",
        "你正在处理一批已确认的终端命令。请仅围绕这批执行结果回复，不要输出身份介绍或底层模型信息。",
        f"用户原始诉求: {intent}",
        "本轮确认结果：",
    ]
    for idx, item in enumerate(confirm_results[:8], start=1):
        cmd = str(item.get("cmd", "") or "").strip()
        status = str(item.get("status", "pending") or "pending").strip().lower()
        if status == "denied":
            lines.append(f"{idx}) [已拒绝] 命令: {cmd}")
            continue
        rc = item.get("returncode", "")
        lines.append(f"{idx}) [已执行] 命令: {cmd} | 返回码: {rc}")
        out = _trim_terminal_followup_output(str(item.get("output", "") or "(no output)"))
        lines.append(f"输出:\n{out}")
    lines.extend(
        [
            "",
            "回答要求:",
            "1) 逐条说明命令是否成功；",
            "2) 提炼关键信息（避免复读全部输出）；",
            "3) 若存在失败项，给出下一步建议。",
        ]
    )
    return "\n".join(lines)


def _build_terminal_confirm_batch_fallback_reply(confirm_results: list[dict]) -> str:
    if not confirm_results:
        return "本轮终端确认已完成。"
    lines = ["本轮终端确认已完成："]
    for idx, item in enumerate(confirm_results[:8], start=1):
        cmd = str(item.get("cmd", "") or "").strip()
        status = str(item.get("status", "") or "").strip().lower()
        if status == "denied":
            lines.append(f"{idx}. 已拒绝：`{cmd}`")
            continue
        rc = item.get("returncode", "")
        out = _trim_terminal_followup_output(str(item.get("output", "") or "(no output)"))
        lines.append(f"{idx}. 已执行：`{cmd}`（返回码 {rc}）")
        lines.append(f"输出：`{out}`")
    return "\n".join(lines)


_TOOL_MARKER_BLOCK_RE = re.compile(
    r"(?:[ \t]*>\s*>_<[ \t]*\n[ \t]*>\s*--调用工具中--[ \t]*(?:\n[ \t]*>\s*--(?:call_id:\s*)?[^\n]+--[ \t]*)*\n?)+",
    re.MULTILINE,
)
_SYSTEM_SUMMARY_PREFIX_RE = re.compile(r"(?im)^\s*\[系统摘要\]\s*")
_LLM_TERMINAL_CONTEXT_MAX_ROWS = 40
_AUTO_COMPRESS_RAW_CHAT_ROWS_TRIGGER = 80
_LLM_CONTEXT_PAYLOAD_VERSION = 1
_LLM_CONTEXT_PAYLOAD_FORMAT = "tinda_llm_json_input"
_CONTEXT_INJECTION_PREVIEW_MAX_ITEMS = 6
_CONTEXT_INJECTION_PREVIEW_HEAD_ITEMS = 3
_CONTEXT_INJECTION_PREVIEW_TAIL_ITEMS = 2
_CONTEXT_INJECTION_PREVIEW_MAX_CHARS = 240
_TERMINAL_FOLLOWUP_OUTPUT_MAX_CHARS = 3200


def _strip_tool_marker_noise(content: str) -> str:
    text = str(content or "")
    if not text:
        return text
    cleaned = _TOOL_MARKER_BLOCK_RE.sub("\n", text)
    cleaned = _SYSTEM_SUMMARY_PREFIX_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _inject_tool_call_ids_into_marker_text(content: str, tool_trace: list[dict] | None) -> str:
    text = str(content or "")
    if not text.strip():
        return text
    if "调用工具中" not in text:
        return text
    if re.search(r"(?im)^\s*>\s*--call_id:\s*[^\n]+--\s*$", text):
        return text

    call_ids = _extract_tool_trace_call_ids(tool_trace)
    if not call_ids:
        return text

    marker_re = re.compile(r"(?im)^([ \t]*>\s*--调用工具中--[ \t]*)$")
    idx = 0

    def _repl(m: re.Match) -> str:
        nonlocal idx
        line = str(m.group(1) or "").rstrip()
        if idx >= len(call_ids):
            return line
        cid = str(call_ids[idx] or "").strip()
        idx += 1
        if not cid:
            return line
        return f"{line}\n> --call_id: {cid}--"

    out = marker_re.sub(_repl, text)
    if idx <= 0:
        return text
    return out


def _to_llm_context_role(*, role: str, entry_type: str) -> str:
    et = str(entry_type or "").strip()
    if et in {"terminal", "terminal_exec", "terminal_confirm", "tool_marker"}:
        return "system"
    r = str(role or "").strip()
    if r == "user":
        return "user"
    if r == "assistant":
        return "llm"
    return "system"


def _build_llm_system_input_json(
    *,
    input_role: str,
    entry_type: str,
    content: str,
    message_id: str,
    created_at: str,
) -> str:
    payload = {
        "format": _LLM_CONTEXT_PAYLOAD_FORMAT,
        "version": _LLM_CONTEXT_PAYLOAD_VERSION,
        "input_role": str(input_role or "system"),
        "entry_type": str(entry_type or "chat"),
        "content": str(content or ""),
        "meta": {
            "id": str(message_id or ""),
            "created_at": str(created_at or ""),
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _parse_llm_context_payload(content: str) -> dict | None:
    raw = str(content or "")
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    if str(parsed.get("format", "")).strip() != _LLM_CONTEXT_PAYLOAD_FORMAT:
        return None
    return parsed


def _truncate_context_preview(content: str, max_chars: int = _CONTEXT_INJECTION_PREVIEW_MAX_CHARS) -> str:
    text = str(content or "").replace("\r", "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip().replace("\n", "\\n")
    if len(text) <= max_chars:
        return text
    return text[: max(0, int(max_chars) - 3)] + "..."


def _summarize_context_injection_messages(
    messages: list[dict],
    *,
    max_preview_items: int = _CONTEXT_INJECTION_PREVIEW_MAX_ITEMS,
) -> dict:
    rows = messages if isinstance(messages, list) else []
    role_counts: dict[str, int] = {}
    legacy_input_role_counts: dict[str, int] = {}
    entry_type_counts: dict[str, int] = {}

    def _inc(bucket: dict[str, int], key: str) -> None:
        k = str(key or "").strip() or "unknown"
        bucket[k] = int(bucket.get(k, 0)) + 1

    parsed_cache: list[dict | None] = []
    for row in rows:
        item = row if isinstance(row, dict) else {}
        role = str(item.get("role", "")).strip() or "unknown"
        _inc(role_counts, role)
        parsed = _parse_llm_context_payload(str(item.get("content", "")))
        parsed_cache.append(parsed)
        raw_entry_type = str(item.get("entry_type", "")).strip()
        payload_entry_type = str(parsed.get("entry_type", "")).strip() if isinstance(parsed, dict) else ""
        entry_type = raw_entry_type or payload_entry_type
        if entry_type:
            _inc(entry_type_counts, entry_type)
        if parsed is not None:
            _inc(legacy_input_role_counts, str(parsed.get("input_role", "")).strip() or "unknown")

    total = len(rows)
    sample_indexes: list[int]
    max_items = max(0, int(max_preview_items))
    if total <= max_items:
        sample_indexes = list(range(total))
    else:
        head = min(_CONTEXT_INJECTION_PREVIEW_HEAD_ITEMS, max_items, total)
        remaining = max(0, max_items - head)
        tail = min(_CONTEXT_INJECTION_PREVIEW_TAIL_ITEMS, remaining, max(0, total - head))
        sample_indexes = list(range(head))
        if tail > 0:
            sample_indexes.extend(list(range(total - tail, total)))

    preview_rows: list[dict] = []
    for idx in sample_indexes:
        row = rows[idx] if isinstance(rows[idx], dict) else {}
        payload = parsed_cache[idx]
        role = str(row.get("role", "")).strip() or "unknown"
        row_entry_type = str(row.get("entry_type", "")).strip()
        if payload is not None:
            preview_content = str(payload.get("content", ""))
            entry_type = row_entry_type or (str(payload.get("entry_type", "")).strip() or "chat")
        else:
            preview_content = str(row.get("content", ""))
            entry_type = row_entry_type
        preview_rows.append(
            {
                "idx": int(idx),
                "role": role,
                "entry_type": entry_type,
                "is_legacy_json_payload": bool(payload is not None),
                "content_preview": _truncate_context_preview(preview_content),
            }
        )

    legacy_json_payload_count = int(sum(1 for item in parsed_cache if item is not None))
    return {
        "message_count": total,
        "legacy_json_payload_count": legacy_json_payload_count,
        # backward-compat: 老字段保留，便于旧前端/脚本平滑过渡
        "json_payload_count": legacy_json_payload_count,
        "preview_count": len(preview_rows),
        "preview_omitted_count": max(0, total - len(preview_rows)),
        "role_counts": role_counts,
        "legacy_input_role_counts": legacy_input_role_counts,
        # backward-compat: 老字段保留，便于旧前端/脚本平滑过渡
        "input_role_counts": legacy_input_role_counts,
        "entry_type_counts": entry_type_counts,
        "preview": preview_rows,
    }


def _audit_context_injection(
    phase: str,
    session_id: str,
    messages: list[dict],
    *,
    op_type: str = "SYSTEM_EXECUTE",
    extra: dict | None = None,
) -> int:
    sid = str(session_id or "").strip()
    summary = _summarize_context_injection_messages(messages)
    payload = {"phase": str(phase or ""), "session_id": sid, **summary}
    if isinstance(extra, dict) and extra:
        payload.update(extra)
    return audit_event(
        op_type=op_type,
        subsystem="context_injection",
        func="_audit_context_injection",
        file_path=_THIS_FILE,
        content=f"{phase} session_id={sid} message_count={summary.get('message_count', 0)}",
        extra=payload,
    )


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


def _is_tool_command_text(content: str) -> bool:
    raw = str(content or "").strip().lower()
    return raw.startswith("/tool") or raw.startswith("/tools") or raw.startswith("/help")


def _store_to_agent_messages(rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    out: list[dict] = []
    staged: list[dict] = []
    stats = {
        "input_rows": int(len(rows or [])),
        "skipped_entry_type": 0,
        "skipped_role": 0,
        "skipped_empty": 0,
        "skipped_tool_cmd": 0,
        "included_chat": 0,
        "included_notice": 0,
        "included_terminal": 0,
        "dropped_terminal": 0,
    }
    allowed_entry_types = {"chat", "notice", "terminal", "terminal_exec", "terminal_confirm", "tool_marker"}
    for item in rows:
        entry_type = str(item.get("entry_type", "chat")).strip() or "chat"
        # 统一上下文输入：chat/notice + 工具上下文（terminal*）
        if entry_type not in allowed_entry_types:
            stats["skipped_entry_type"] += 1
            continue
        role = str(item.get("role", "")).strip()
        if role not in {"user", "assistant", "system"}:
            stats["skipped_role"] += 1
            continue
        content = str(item.get("content", ""))
        # 历史里可能混入大量工具占位标记，回灌给 LLM 前做噪声清理，避免复读刷屏。
        content = _strip_tool_marker_noise(content)
        if not content.strip():
            stats["skipped_empty"] += 1
            continue
        # /tool 命令保留在会话与终端，但不参与后续 LLM 上下文
        if role == "user" and entry_type == "chat" and _is_tool_command_text(content):
            stats["skipped_tool_cmd"] += 1
            continue
        inject_role = role
        if entry_type in {"notice", "terminal", "terminal_exec", "terminal_confirm", "tool_marker"}:
            inject_role = "system"
        is_terminal = entry_type in {"terminal", "terminal_exec", "terminal_confirm"}
        staged.append(
            {
                "role": inject_role,
                "content": content,
                "entry_type": entry_type,
                "_is_terminal": is_terminal,
            }
        )
        if entry_type == "notice":
            stats["included_notice"] += 1
        elif is_terminal:
            stats["included_terminal"] += 1
        else:
            stats["included_chat"] += 1

    terminal_positions = [i for i, x in enumerate(staged) if bool(x.get("_is_terminal"))]
    overflow = len(terminal_positions) - _LLM_TERMINAL_CONTEXT_MAX_ROWS
    drop_pos_set: set[int] = set()
    if overflow > 0:
        drop_pos_set = set(terminal_positions[:overflow])
        stats["dropped_terminal"] = int(overflow)

    for idx, item in enumerate(staged):
        if idx in drop_pos_set:
            continue
        out.append(
            {
                "role": str(item.get("role", "system") or "system"),
                "content": str(item.get("content", "")),
                "entry_type": str(item.get("entry_type", "") or "chat"),
            }
        )
    return out, stats


def _estimate_context_usage_length(agent_rows: list[dict]) -> int:
    msgs: list[dict] = []
    for item in (agent_rows or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        content = str(item.get("content", ""))
        if role in {"system", "user", "assistant"} and not content.strip():
            continue
        row = {"role": role, "content": content}
        if role == "tool":
            tcid = str(item.get("tool_call_id", "")).strip()
            if tcid:
                row["tool_call_id"] = tcid
        msgs.append(row)
    return int(estimate_messages_tokens(msgs))


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


def _maybe_auto_compress(session_id: str, *, context_rows: list[dict] | None = None) -> dict[str, Any]:
    sid = str(session_id)
    agent = _sessions.get(sid)
    info: dict[str, Any] = {
        "compressed": False,
        "trigger": "",
        "reason": "",
        "raw_chat_count": 0,
        "estimated_tokens_before": 0,
        "estimated_tokens_after": 0,
        "max_context_tokens": 0,
    }
    if not agent:
        info["reason"] = "agent_not_found"
        return info
    tokens_before = int(agent.estimate_current_tokens())
    max_tokens = int(getattr(agent, "max_context_tokens", 0) or 0)
    info["estimated_tokens_before"] = tokens_before
    info["estimated_tokens_after"] = tokens_before
    info["max_context_tokens"] = max_tokens
    rows = context_rows if context_rows is not None else _store.get_context_messages(sid)
    raw_rows = [
        x for x in rows
        if not bool(x.get("is_summary", False))
        and str(x.get("entry_type", "chat")) == "chat"
        and str(x.get("role", "")) in {"user", "assistant"}
    ]
    raw_count = len(raw_rows)
    info["raw_chat_count"] = raw_count
    token_trigger = tokens_before > max_tokens
    row_trigger = raw_count >= _AUTO_COMPRESS_RAW_CHAT_ROWS_TRIGGER
    if token_trigger:
        info["trigger"] = "token"
    elif row_trigger:
        info["trigger"] = "raw_chat_count"
    else:
        info["reason"] = "below_threshold"
        return info
    if len(raw_rows) < 6:
        info["reason"] = "insufficient_messages"
        return info
    older = raw_rows[:-4]
    try:
        summary = _compress_messages_with_llm(older)
        if not summary:
            info["reason"] = "summary_empty"
            return info
        _store.compress_context(sid, summary)
        new_rows = _store.get_context_messages(sid)
        agent_rows, _ = _store_to_agent_messages(new_rows)
        agent.replace_conversation(agent_rows)
        tokens_after = int(agent.estimate_current_tokens())
        info["compressed"] = True
        info["reason"] = "ok"
        info["estimated_tokens_after"] = tokens_after
        _audit_web(
            "SYSTEM_EXECUTE",
            "_maybe_auto_compress",
            f"auto_compress_done session_id={sid}",
            {
                "session_id": sid,
                "compressed_count": len(older),
                "trigger": info["trigger"],
                "raw_chat_count_before": raw_count,
                "context_tokens_before": tokens_before,
                "context_tokens_after": tokens_after,
                "max_context_tokens": max_tokens,
            },
        )
        return info
    except Exception as e:
        info["reason"] = f"exception:{e}"
        return info


def _get_agent(session_id: str):
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
        _sessions[sid] = agent
    else:
        # 会话 Agent 需实时跟随当前登录用户权限，避免工具可见性与鉴权失真
        agent = _sessions[sid]
        if int(getattr(agent, "perm", 0)) != current_perm:
            agent.perm = current_perm
            try:
                agent.user.change_perm(current_perm)
            except Exception:
                pass
    try:
        agent.refresh_model_identity(_client.model)
    except Exception:
        pass
    _touch_session_cache(sid)

    # 同步会话级阈值到 agent
    meta = _store.get_session(sid) or {}
    agent.max_context_tokens = max(100, int(meta.get("max_context_tokens", 16000)))

    # 每次都基于 store 的"有效上下文"回灌，确保压缩边界生效
    rows = _store.get_context_messages(sid)
    agent_rows, filter_stats = _store_to_agent_messages(rows)
    _sessions[sid].replace_conversation(agent_rows)
    _audit_context_injection(
        "get_agent.rebuild_context",
        sid,
        agent_rows,
        extra={
            "source_context_rows": len(rows),
            "context_filter": filter_stats,
            "model": _client.model,
            "estimated_tokens": agent.estimate_current_tokens(),
            "max_context_tokens": agent.max_context_tokens,
        },
    )
    # 自动压缩：token 超阈值或原始聊天消息过长时触发。
    auto_compress = _maybe_auto_compress(sid, context_rows=rows)
    if bool(auto_compress.get("compressed")):
        rows = _store.get_context_messages(sid)
        agent_rows, filter_stats = _store_to_agent_messages(rows)
        _audit_context_injection(
            "get_agent.rebuild_context_after_compress",
            sid,
            agent_rows,
            extra={
                "source_context_rows": len(rows),
                "context_filter": filter_stats,
                "model": _client.model,
                "estimated_tokens": agent.estimate_current_tokens(),
                "max_context_tokens": agent.max_context_tokens,
                "auto_compress": auto_compress,
            },
        )

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
            "estimated_tokens": agent.estimate_current_tokens(),
            "max_context_tokens": agent.max_context_tokens,
            "auto_compress": auto_compress,
        },
    )

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


def _extract_call_id_from_trace_step(step: dict) -> str:
    if not isinstance(step, dict):
        return ""
    result = step.get("result")
    if isinstance(result, dict):
        cid = str(result.get("call_id", "") or "").strip()
        if cid:
            return cid
    for key in ("call_id", "tool_call_id"):
        cid = str(step.get(key, "") or "").strip()
        if cid:
            return cid
    return ""


def _extract_tool_trace_call_ids(tool_trace: list[dict] | None) -> list[str]:
    if not isinstance(tool_trace, list) or not tool_trace:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for step in tool_trace:
        cid = _extract_call_id_from_trace_step(step)
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def _tool_trace_to_terminal_items(tool_trace: list[dict] | None, *, turn_id: str = "") -> list[dict]:
    if not isinstance(tool_trace, list) or not tool_trace:
        return []

    tid = _normalize_turn_id(turn_id)
    items: list[dict] = []
    for step in tool_trace:
        if not isinstance(step, dict):
            continue

        name = str(step.get("agent_tool", "") or "unknown_tool")
        args_text = _stringify_trace_value(step.get("arguments", {}))
        result = step.get("result")
        call_id = _extract_call_id_from_trace_step(step)
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
                    "turn_id": tid,
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
                "turn_id": tid,
            }
        )

        # run_terminal: 额外生成聊天内联终端气泡
        inner_tool = ""
        args = step.get("arguments")
        if isinstance(args, dict):
            inner_tool = str(args.get("tool_name", "") or "").strip()
        if inner_tool == "run_terminal" and isinstance(result, dict):
            inner = result.get("result")
            if isinstance(inner, dict):
                if inner.get("pending_confirmation") is True:
                    confirm_id = str(inner.get("confirm_id", "") or "").strip() or f"m_{uuid.uuid4().hex[:16]}"
                    confirm_data = json.dumps({
                        "cmd": str(inner.get("cmd", "") or ""),
                        "status": "pending",
                    }, ensure_ascii=False)
                    items.append({
                        "id": confirm_id,
                        "role": "user",
                        "content": confirm_data,
                        "entry_type": "terminal_confirm",
                        "created_at": _now_iso(),
                        "is_summary": False,
                        "turn_id": tid,
                    })
                else:
                    cmd = str(inner.get("cmd", "") or "")
                    output = str(inner.get("output", "") or "(no output)")
                    rc = inner.get("returncode", None)
                    exec_data = json.dumps({
                        "cmd": cmd,
                        "output": output,
                        "returncode": rc,
                        "status": "ok" if inner.get("ok") else "error",
                    }, ensure_ascii=False)
                    items.append({
                        "id": f"m_{uuid.uuid4().hex[:16]}",
                        "role": "user",
                        "content": exec_data,
                        "entry_type": "terminal_exec",
                        "created_at": _now_iso(),
                        "is_summary": False,
                        "turn_id": tid,
                    })
    return items


def _save_chat_messages(
    session_id: str,
    user_text: str,
    assistant_text: str,
    *,
    tool_marker: bool = False,
    tool_trace: list[dict] | None = None,
    turn_id: str | None = None,
) -> str:
    tid = _normalize_turn_id(turn_id) or _new_turn_id()
    assistant_content = _inject_tool_call_ids_into_marker_text(str(assistant_text or ""), tool_trace)
    items = [
        {
            "id": f"m_{uuid.uuid4().hex[:16]}",
            "role": "user",
            "content": _strip_user_meta_block(user_text),
            "entry_type": "chat",
            "created_at": _now_iso(),
            "is_summary": False,
            "turn_id": tid,
        },
        {
            "id": f"m_{uuid.uuid4().hex[:16]}",
            "role": "assistant",
            "content": assistant_content,
            "entry_type": "chat",
            "created_at": _now_iso(),
            "is_summary": False,
            "turn_id": tid,
        },
    ]
    if tool_marker:
        call_ids = _extract_tool_trace_call_ids(tool_trace)
        marker = "> >_<\n> --调用工具中--\n"
        if call_ids:
            marker += "\n".join(f"> --call_id: {cid}--" for cid in call_ids) + "\n"
        items.append(
            {
                "id": f"m_{uuid.uuid4().hex[:16]}",
                "role": "assistant",
                "content": marker.rstrip("\n"),
                "entry_type": "tool_marker",
                "created_at": _now_iso(),
                "is_summary": False,
                "turn_id": tid,
            }
        )
    items.extend(_tool_trace_to_terminal_items(tool_trace, turn_id=tid))
    _store.append_messages(session_id, items)
    _audit_web(
        "PUBLIC_WRITE",
        "_save_chat_messages",
        f"chat_messages_saved session_id={session_id}",
        {
            "session_id": session_id,
            "items_count": len(items),
            "tool_marker": bool(tool_marker),
            "tool_trace_count": len(tool_trace or []),
            "turn_id": tid,
        },
    )
    return tid


def _persist_terminal_events(session_id: str, events: list[dict]) -> None:
    rows: list[dict] = []
    for e in events:
        if e.get("type") != "terminal":
            continue
        turn_id = _normalize_turn_id(str(e.get("turn_id", "") or ""))
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
                "turn_id": turn_id,
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
            title = _title_client.chat(
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
    text = _compress_client.chat(
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


def _iter_log_roots(*, include_legacy: bool = True) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    def _append(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            resolved = Path(path)
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        roots.append(resolved)

    _append(get_log_root())
    _append(_LOG_ROOT)
    active_raw = str(os.getenv(_ACTIVE_LOG_ROOT_ENV, "")).strip()
    if active_raw:
        _append(Path(active_raw))
    if include_legacy:
        _append(get_legacy_log_root())
    return roots


def _resolve_log_file_path(safe_name: str) -> Path | None:
    for root in _iter_log_roots(include_legacy=True):
        primary = root / safe_name
        if not primary.exists() or not primary.is_file():
            continue
        legacy_root = get_legacy_log_root()
        try:
            is_legacy = primary.resolve().is_relative_to(legacy_root.resolve())
        except Exception:
            is_legacy = False
        if is_legacy:
            audit_event(
                op_type="SYSTEM_READ",
                subsystem="storage_migration",
                func="_resolve_log_file_path",
                file_path=_THIS_FILE,
                content=f"legacy_fallback_read_log file={safe_name}",
                extra={"legacy_file": str(primary)},
            )
        return primary
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


def _resolve_total_jsonl_candidates(*, include_archives: bool = True) -> list[Path]:
    rows: list[Path] = []
    seen: set[str] = set()
    roots = [root for root in _iter_log_roots(include_legacy=True) if root.exists()]

    def _append(path: Path) -> None:
        try:
            key = str(path.resolve())
        except Exception:
            key = str(path)
        if key in seen:
            return
        if not path.exists() or not path.is_file():
            return
        seen.add(key)
        rows.append(path)

    for root in roots:
        _append(root / "total.jsonl")
        if not include_archives:
            continue
        for arc in sorted(root.glob("total.*.jsonl.gz"), reverse=True):
            _append(arc)
    return rows


def _iter_audit_rows(path: Path):
    opener = gzip.open if str(path).lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fp:
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
            if rid <= 0:
                continue
            yield line_no, rid, row


def _find_audit_event_by_id(event_id: int) -> dict | None:
    """先走 total.idx 快速查当前日志，再回退扫描归档 .jsonl.gz。"""
    target = int(event_id)
    # Fast path: 只对未压缩 total.jsonl 使用 idx 定位。
    for path in _resolve_total_jsonl_candidates(include_archives=False):
        idx_path = path.with_suffix(".idx")
        idx = {}
        try:
            raw = idx_path.read_text(encoding="utf-8")
            idx = json.loads(raw) if raw.strip() else {}
        except Exception:
            idx = {}
        if not isinstance(idx, dict):
            idx = {}
        offset = idx.get(str(target))
        if offset is None:
            continue
        if not isinstance(offset, (int, str)):
            continue
        try:
            offset = int(offset)
        except (ValueError, TypeError):
            continue
        if offset < 0:
            continue
        try:
            with path.open("r", encoding="utf-8") as fp:
                fp.seek(int(offset))
                line = fp.readline()
                row = json.loads(line.strip())
                rid = int(row.get("id", -1))
                if rid == target:
                    return {
                        "event": row,
                        "source_file": str(path.name),
                        "source_path": str(path),
                        "source_line": 0,
                    }
        except Exception:
            pass

    # Slow path: 回退扫描当前日志+历史归档。
    for path in _resolve_total_jsonl_candidates(include_archives=True):
        try:
            for line_no, rid, row in _iter_audit_rows(path):
                if rid != target:
                    continue
                return {
                    "event": row,
                    "source_file": str(path.name),
                    "source_path": str(path),
                    "source_line": int(line_no),
                }
        except Exception:
            continue
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


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return HTMLResponse(_HTML_SETTINGS)


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
    return JSONResponse(
        {"ok": False, "error": "version switch is disabled by policy"},
        status_code=410,
    )


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
    roots = [root for root in _iter_log_roots(include_legacy=True) if root.exists()]
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


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    _require_login()
    ok = _store.delete_session(session_id)
    _sessions.pop(session_id, None)
    _session_last_access.pop(session_id, None)
    try:
        _tool_runtime.stop_session(session_id)
    except Exception as e:
        _audit_web(
            "SYSTEM_EXECUTE",
            "delete_session",
            f"stop_tool_runtime_failed session_id={session_id}",
            {"session_id": session_id, "ok": False, "error": str(e)},
        )
    if not ok:
        return JSONResponse({"ok": False, "error": "session not found"}, status_code=404)
    return JSONResponse({"ok": True, "session_id": session_id})


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    _require_login()
    _store.ensure_session(session_id)
    rows = _store.load_messages(session_id)
    return JSONResponse({"ok": True, "session_id": session_id, "entries": rows})


@app.get("/sessions/{session_id}/context-usage")
async def get_session_context_usage(session_id: str):
    _require_login()
    _store.ensure_session(session_id)
    sid = str(session_id or "").strip()
    context_rows = _store.get_context_messages(sid)
    agent_rows, _ = _store_to_agent_messages(context_rows)
    usage = _estimate_context_usage_length(agent_rows)
    return JSONResponse(
        {
            "ok": True,
            "session_id": sid,
            "usage_length": int(usage),
            "context_rows": len(context_rows),
            "agent_rows": len(agent_rows),
        }
    )


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
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("compress failed: session=%s err=%s", session_id, e)
        return JSONResponse({"ok": False, "error": "压缩失败"}, status_code=500)

    return JSONResponse({"ok": True, **result})


class SessionConfigRequest(BaseModel):
    max_context_tokens: int | None = None


@app.patch("/sessions/{session_id}/config")
async def patch_session_config(session_id: str, req: SessionConfigRequest):
    current = _require_login()
    if not _has_llm_perm(current):
        return JSONResponse({"ok": False, "error": "权限不足"}, status_code=403)
    try:
        result = _store.set_session_config(session_id, max_context_tokens=req.max_context_tokens)
        # 同步到运行中的 agent
        agent = _sessions.get(session_id)
        if agent and req.max_context_tokens is not None:
            agent.max_context_tokens = max(100, int(req.max_context_tokens))
        return JSONResponse({"ok": True, **result})
    except SessionStoreError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


class TerminalConfirmRequest(BaseModel):
    session_id: str
    confirm_id: str | None = None
    action: str  # "allow" | "deny"
    cmd: str | None = None  # 前端直接传命令，避免依赖异步落盘后的 confirm_entry 查询


@app.post("/terminal/confirm")
async def terminal_confirm(req: TerminalConfirmRequest):
    current = _require_login()
    sid = str(req.session_id or "").strip()
    cid = str(req.confirm_id or "").strip()
    action = str(req.action or "").strip().lower()
    cmd = str(req.cmd or "").strip()

    if not sid or action not in ("allow", "deny"):
        return JSONResponse({"ok": False, "error": "invalid params"}, status_code=400)
    if not cid and not cmd:
        return JSONResponse({"ok": False, "error": "confirm_id or cmd required"}, status_code=400)
    if (int(current.get_perm()) & 511) != 511:
        return JSONResponse({"ok": False, "error": "permission denied"}, status_code=403)

    rows = _store.load_messages(sid)
    confirm_row = None

    if cid:
        for row in rows:
            if row.get("id") == cid and row.get("entry_type") == "terminal_confirm":
                confirm_row = row
                break

    if confirm_row is None and cmd:
        for row in reversed(rows):
            if row.get("entry_type") != "terminal_confirm":
                continue
            try:
                row_data = json.loads(row.get("content", "{}"))
            except Exception:
                row_data = {}
            row_cmd = str(row_data.get("cmd", "") or "").strip()
            row_status = str(row_data.get("status", "pending") or "pending").strip().lower()
            if row_cmd == cmd and row_status == "pending":
                confirm_row = row
                cid = str(row.get("id", "") or cid).strip()
                break

    if confirm_row is None:
        return JSONResponse({"ok": False, "error": "confirm entry not found or already handled"}, status_code=404)
    turn_id = _normalize_turn_id(str(confirm_row.get("turn_id", "") or "")) or _new_turn_id()

    try:
        row_data = json.loads(confirm_row.get("content", "{}"))
    except Exception:
        row_data = {}

    row_cmd = str(row_data.get("cmd", "") or "").strip()
    row_status = str(row_data.get("status", "pending") or "pending").strip().lower()
    cmd = cmd or row_cmd

    if not cmd:
        return JSONResponse({"ok": False, "error": "empty command"}, status_code=400)

    # 幂等：已处理过的确认请求直接返回，不重复执行命令与落盘 notice。
    if row_status in {"allow", "deny", "allowed", "denied"}:
        replay = dict(row_data)
        replay.setdefault("cmd", cmd)
        normalized = str(replay.get("status", row_status)).strip().lower()
        if normalized == "allow":
            replay["status"] = "allowed"
            replay["action"] = "allow"
        elif normalized == "deny":
            replay["status"] = "denied"
            replay["action"] = "deny"
        elif normalized == "allowed":
            replay["action"] = "allow"
        elif normalized == "denied":
            replay["action"] = "deny"

        replay_action = str(replay.get("action", "") or "").strip().lower()
        reply = ""
        if replay_action == "allow":
            if replay.get("executed") is True or ("output" in replay):
                out_text = str(replay.get("output", "(no output)"))
                reply = (
                    f"已执行命令 `{cmd}`。\n\n"
                    f"输出结果：\n\n```\n{out_text}\n```"
                )
            else:
                reply = f"命令 `{cmd}` 已允许执行。"
        elif replay_action == "deny":
            reply = f"已拒绝执行命令 `{cmd}`。"

        return JSONResponse(
            {
                "ok": True,
                "confirm_id": cid,
                "already_processed": True,
                "reply": reply,
                "tool_trace": [],
                "turn_id": turn_id,
                **replay,
            }
        )

    status = "allowed" if action == "allow" else "denied"
    result = {"cmd": cmd, "status": status, "action": action}
    if action == "allow":
        import subprocess
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30,
                               env={**os.environ, "PYTHONUNBUFFERED": "1"})
            out = (r.stdout or "") + (r.stderr or "")
            if len(out) > 8000:
                out = out[:8000] + "\n...(output truncated)"
            result["output"] = out.strip() or "(no output)"
            result["returncode"] = r.returncode
            result["executed"] = True
        except subprocess.TimeoutExpired:
            result["output"] = f"(命令超时 >30s)"
            result["returncode"] = -1
            result["executed"] = True
        except Exception as e:
            result["output"] = f"(执行失败: {e})"
            result["returncode"] = -1
            result["executed"] = True

    confirm_row["turn_id"] = turn_id
    confirm_row["content"] = json.dumps(result, ensure_ascii=False)
    _store._write_messages(sid, rows)

    action_text = "允许" if action == "allow" else "拒绝"
    exec_summary = f"[终端确认] 用户已{action_text}执行命令: {cmd}"
    if result.get("executed"):
        exec_summary += f"\n输出:\n{result.get('output', '(no output)')}"
    _store.append_messages(sid, [{
        "id": f"m_{uuid.uuid4().hex[:16]}",
        "role": "system",
        "content": exec_summary,
        "entry_type": "notice",
        "created_at": _now_iso(),
        "is_summary": False,
        "turn_id": turn_id,
    }])

    pending_turn_confirms = _collect_pending_terminal_confirms(rows, turn_id=turn_id)
    if pending_turn_confirms:
        return JSONResponse(
            {
                "ok": True,
                "confirm_id": cid,
                "reply": "",
                "tool_trace": [],
                "turn_id": turn_id,
                "awaiting_other_confirmations": True,
                "pending_confirm_count": len(pending_turn_confirms),
                "pending_confirmations": pending_turn_confirms,
                **result,
            }
        )

    turn_confirm_results = _collect_turn_terminal_confirm_results(rows, turn_id=turn_id)
    is_batch_confirm = len(turn_confirm_results) > 1

    tool_trace = []
    reply = ""
    if is_batch_confirm:
        any_allowed_executed = any(
            (str(it.get("status", "")).strip().lower() == "allowed") and bool(it.get("executed") is True)
            for it in turn_confirm_results
        )
        if any_allowed_executed:
            origin_confirm_id = str(turn_confirm_results[0].get("confirm_id", "") or cid).strip()
            origin_cmd = str(turn_confirm_results[0].get("cmd", "") or "")
            origin_user_intent = _extract_terminal_confirm_origin_user_intent(rows, origin_confirm_id)
            fallback_reply = _build_terminal_confirm_batch_fallback_reply(turn_confirm_results)
            try:
                agent = _get_agent(sid)
                continuation_messages = list(agent.history)
                followup_user_input = _build_terminal_batch_followup_user_input_json(
                    confirm_results=turn_confirm_results,
                    origin_user_intent=origin_user_intent,
                )
                continuation_messages.append({"role": "user", "content": followup_user_input})
                _audit_context_injection(
                    "terminal_confirm.batch_allow_continue",
                    sid,
                    continuation_messages,
                    extra={
                        "confirm_id": cid,
                        "action": action,
                        "cmd": cmd,
                        "user_perm": int(current.get_perm()),
                        "origin_user_intent": origin_user_intent,
                        "followup_injected": True,
                        "followup_entry_type": "terminal_followup_batch",
                        "confirm_count": len(turn_confirm_results),
                    },
                )
                llm_result = _client.chat_with_tools(
                    continuation_messages,
                    user_perm=int(current.get_perm()),
                    max_tool_steps=0,
                )
                reply = str(llm_result.get("reply", "") or "").strip()
                tool_trace = _sanitize_tool_trace_for_user(llm_result.get("tool_trace", []))
                delta = llm_result.get("history_delta", [])
                if delta:
                    agent.history.extend(delta)
                    agent._trim_history()
            except Exception as e:
                _audit_web(
                    "SYSTEM_EXECUTE",
                    "terminal_confirm",
                    f"batch_allow_continue_failed session_id={sid}",
                    {
                        "session_id": sid,
                        "confirm_id": cid,
                        "cmd": cmd,
                        "ok": False,
                        "error": str(e),
                    },
                )
                reply = ""

            if not _is_terminal_followup_reply_relevant(reply, cmd=origin_cmd, origin_user_intent=origin_user_intent):
                reason = "empty_or_failed" if not reply else "off_topic"
                _audit_web(
                    "SYSTEM_EXECUTE",
                    "terminal_confirm",
                    f"batch_allow_continue_fallback session_id={sid} reason={reason}",
                    {
                        "session_id": sid,
                        "confirm_id": cid,
                        "cmd": cmd,
                        "reason": reason,
                        "origin_user_intent": origin_user_intent,
                        "reply_preview": str(reply or "")[:160],
                        "confirm_count": len(turn_confirm_results),
                    },
                )
                reply = fallback_reply
                tool_trace = []
        else:
            reply = _build_terminal_confirm_batch_fallback_reply(turn_confirm_results)
    else:
        if action == "allow" and result.get("executed"):
            origin_user_intent = _extract_terminal_confirm_origin_user_intent(rows, cid)
            fallback_reply = _build_terminal_confirm_fallback_reply(cmd, result)
            try:
                # 协议约束：确认后续写必须补一个明确 user 回合，避免模型按历史漂移跑题。
                agent = _get_agent(sid)
                continuation_messages = list(agent.history)
                followup_user_input = _build_terminal_followup_user_input_json(
                    confirm_id=cid,
                    cmd=cmd,
                    result=result,
                    origin_user_intent=origin_user_intent,
                )
                continuation_messages.append({"role": "user", "content": followup_user_input})
                _audit_context_injection(
                    "terminal_confirm.allow_continue",
                    sid,
                    continuation_messages,
                    extra={
                        "confirm_id": cid,
                        "action": action,
                        "cmd": cmd,
                        "user_perm": int(current.get_perm()),
                        "origin_user_intent": origin_user_intent,
                        "followup_injected": True,
                        "followup_entry_type": "terminal_followup",
                    },
                )
                llm_result = _client.chat_with_tools(
                    continuation_messages,
                    user_perm=int(current.get_perm()),
                    max_tool_steps=0,
                )
                reply = str(llm_result.get("reply", "") or "").strip()
                tool_trace = _sanitize_tool_trace_for_user(llm_result.get("tool_trace", []))
                delta = llm_result.get("history_delta", [])
                if delta:
                    agent.history.extend(delta)
                    agent._trim_history()
            except Exception as e:
                _audit_web(
                    "SYSTEM_EXECUTE",
                    "terminal_confirm",
                    f"allow_continue_failed session_id={sid}",
                    {
                        "session_id": sid,
                        "confirm_id": cid,
                        "cmd": cmd,
                        "ok": False,
                        "error": str(e),
                    },
                )
                reply = ""

            if not _is_terminal_followup_reply_relevant(reply, cmd=cmd, origin_user_intent=origin_user_intent):
                reason = "empty_or_failed" if not reply else "off_topic"
                _audit_web(
                    "SYSTEM_EXECUTE",
                    "terminal_confirm",
                    f"allow_continue_fallback session_id={sid} reason={reason}",
                    {
                        "session_id": sid,
                        "confirm_id": cid,
                        "cmd": cmd,
                        "reason": reason,
                        "origin_user_intent": origin_user_intent,
                        "reply_preview": str(reply or "")[:160],
                    },
                )
                reply = fallback_reply
                tool_trace = []
        elif action == "deny":
            reply = f"已拒绝执行命令 `{cmd}`。"

    if reply:
        reply = _sanitize_terminal_dump_reply(
            reply_text=reply,
            tool_steps=len(tool_trace or []),
            tool_trace=tool_trace,
        )
        reply = _inject_tool_call_ids_into_marker_text(reply, tool_trace)
        items = [{
            "id": f"m_{uuid.uuid4().hex[:16]}",
            "role": "assistant",
            "content": reply,
            "entry_type": "chat",
            "created_at": _now_iso(),
            "is_summary": False,
            "turn_id": turn_id,
        }]
        items.extend(_tool_trace_to_terminal_items(tool_trace, turn_id=turn_id))
        _store.append_messages(sid, items)

    return JSONResponse(
        {
            "ok": True,
            "confirm_id": cid,
            "reply": reply,
            "tool_trace": tool_trace,
            "turn_id": turn_id,
            "awaiting_other_confirmations": False,
            "pending_confirm_count": 0,
            "pending_confirmations": [],
            "resolved_confirm_count": len(turn_confirm_results),
            "is_batch_confirm": bool(is_batch_confirm),
            **result,
        }
    )


@app.get("/terminal/settings")
async def get_terminal_settings():
    current = _require_login()
    if (int(current.get_perm()) & 511) != 511:
        return JSONResponse({"ok": False, "error": "permission denied"}, status_code=403)
    from TindaAgent.Process.Security.terminal_policy import load_settings
    s = load_settings()
    return JSONResponse({"ok": True, "whitelist": s.get("whitelist", []),
                         "blacklist": s.get("blacklist", []),
                         "bypass_terminal_confirm": s.get("bypass_terminal_confirm", False)})


class TerminalSettingsRequest(BaseModel):
    whitelist: list[str] | None = None
    blacklist: list[str] | None = None
    bypass_terminal_confirm: bool | None = None


@app.put("/terminal/settings")
async def update_terminal_settings(req: TerminalSettingsRequest):
    current = _require_login()
    if (int(current.get_perm()) & 511) != 511:
        return JSONResponse({"ok": False, "error": "permission denied"}, status_code=403)
    from TindaAgent.Process.Security.terminal_policy import load_settings, save_settings
    s = load_settings()
    if req.whitelist is not None:
        s["whitelist"] = [str(x).strip() for x in req.whitelist if str(x).strip()]
    if req.blacklist is not None:
        s["blacklist"] = [str(x).strip() for x in req.blacklist if str(x).strip()]
    if req.bypass_terminal_confirm is not None:
        s["bypass_terminal_confirm"] = bool(req.bypass_terminal_confirm)
    save_settings(s)
    return JSONResponse({"ok": True, **s})


@app.post("/chat")
async def chat(req: ChatRequest):
    current = _require_login()
    if not _has_llm_perm(current):
        return JSONResponse({"error": "权限不足：当前账户不可调用 LLM 对话"}, status_code=403)
    sid = str(req.session_id or "").strip()
    if not sid:
        return JSONResponse({"error": "session_id required"}, status_code=400)

    _store.ensure_session(sid)
    agent = _get_agent(sid)
    message = str(req.message or "").strip()
    if not message:
        return JSONResponse({"reply": "", "tool_trace": [], "tool_steps": 0})
    pending_confirms = _collect_pending_terminal_confirms(_store.load_messages(sid))
    if pending_confirms:
        return JSONResponse(
            {
                "error": "存在待确认终端命令，请先全部允许/拒绝后再发送新消息。",
                "pending_confirm_count": len(pending_confirms),
                "pending_confirmations": pending_confirms,
            },
            status_code=409,
        )
    turn_id = _new_turn_id()

    if message.startswith("/"):
        profile = _get_web_profile()
        try:
            job = _tool_runtime.submit_command(sid, message, profile.perm)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        marker = "> >_<\n> --调用工具中--"
        call_id = str((job or {}).get("call_id", "") or "").strip()
        if call_id:
            marker += f"\n> --call_id: {call_id}--"
        # 工具命令也写入 chat 消息（用户气泡独立）
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
                    "turn_id": turn_id,
                },
                {
                    "id": f"m_{uuid.uuid4().hex[:16]}",
                    "role": "assistant",
                    "content": marker,
                    "entry_type": "tool_marker",
                    "is_summary": False,
                    "created_at": _now_iso(),
                    "turn_id": turn_id,
                },
            ],
        )
        return JSONResponse(
            {
                "reply": marker,
                "tool_trace": [],
                "tool_steps": 0,
                "tool_job": job,
                "tool_async": True,
                "turn_id": turn_id,
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
    request_messages = list(agent.history)
    request_messages.append({"role": "user", "content": llm_message})
    _audit_context_injection(
        "chat.request",
        sid,
        request_messages,
        extra={
            "stream": False,
            "user_perm": int(current.get_perm()),
            "message_chars": len(message),
        },
    )

    result = agent.chat_with_meta(llm_message)
    tool_trace = _sanitize_tool_trace_for_user(result.get("tool_trace", []))
    tool_steps = int(result.get("tool_steps", 0))
    reply = str(result.get("reply", ""))

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
    reply = _inject_tool_call_ids_into_marker_text(reply, tool_trace)

    saved_turn_id = _save_chat_messages(
        sid,
        message,
        reply,
        tool_marker=bool(tool_steps > 0),
        tool_trace=tool_trace,
        turn_id=turn_id,
    )
    _generate_title_from_first_round(sid)

    return JSONResponse(
        {
            "reply": reply,
            "tool_trace": tool_trace,
            "tool_steps": tool_steps,
            "turn_id": saved_turn_id,
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
    current = _require_login()
    if not _has_llm_perm(current):
        chunks = [
            _sse_event("error", {"message": "权限不足：当前账户不可调用 LLM 对话"}),
            _sse_event("done", {"reply": "", "tool_trace": [], "tool_steps": 0}),
        ]
        return HTMLResponse("".join(chunks), media_type="text/event-stream")
    sid = str(session_id or "").strip()
    if not sid:
        return JSONResponse({"error": "session_id required"}, status_code=400)

    _store.ensure_session(sid)
    agent = _get_agent(sid)
    turn_id = _new_turn_id()

    text = str(message or "").strip()
    pending_confirms = _collect_pending_terminal_confirms(_store.load_messages(sid))
    if pending_confirms:
        chunks = [
            _sse_event(
                "error",
                {
                    "message": "存在待确认终端命令，请先全部允许/拒绝后再发送新消息。",
                    "pending_confirm_count": len(pending_confirms),
                    "pending_confirmations": pending_confirms,
                },
            ),
            _sse_event(
                "done",
                {
                    "reply": "",
                    "tool_trace": [],
                    "tool_steps": 0,
                    "pending_confirm_count": len(pending_confirms),
                    "pending_confirmations": pending_confirms,
                },
            ),
        ]
        return HTMLResponse("".join(chunks), media_type="text/event-stream")
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

        marker = "> >_<\n> --调用工具中--"
        call_id = str((job or {}).get("call_id", "") or "").strip()
        if call_id:
            marker += f"\n> --call_id: {call_id}--"

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
                    "turn_id": turn_id,
                },
                {
                    "id": f"m_{uuid.uuid4().hex[:16]}",
                    "role": "assistant",
                    "content": marker,
                    "entry_type": "tool_marker",
                    "is_summary": False,
                    "created_at": _now_iso(),
                    "turn_id": turn_id,
                },
            ],
        )

        chunks = [
            _sse_event("reset", {}),
            _sse_event("delta", {"content": marker}),
            _sse_event(
                "done",
                {
                    "reply": marker,
                    "tool_trace": [],
                    "tool_steps": 0,
                    "tool_job": job,
                    "tool_async": True,
                    "turn_id": turn_id,
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
    stream_request_messages = list(agent.history)
    stream_request_messages.append({"role": "user", "content": llm_message})
    _audit_context_injection(
        "chat_stream.request",
        sid,
        stream_request_messages,
        extra={
            "stream": True,
            "user_perm": int(current.get_perm()),
            "message_chars": len(text),
        },
    )

    def event_iter():
        final_reply = ""
        done_payload: dict | None = None
        try:
            for event in agent.stream_chat_events(llm_message):
                et = event.get("type", "")
                if et == "delta":
                    final_reply += str(event.get("content", ""))
                    yield _sse_event("delta", {"content": event.get("content", "")})
                elif et == "reset":
                    # 关键：把工具调用标记按流顺序写入持久化文本，确保刷新/导入后仍是 A-标记-B。
                    final_reply += "\n\n> >_<\n> --调用工具中--\n"
                    yield _sse_event("reset", {})
                elif et == "tool_step":
                    yield _sse_event("tool_step", {"trace": event.get("trace", [])})
                elif et == "done":
                    done_payload = {
                        "reply": event.get("reply", ""),
                        "tool_trace": _sanitize_tool_trace_for_user(event.get("tool_trace", [])),
                        "tool_steps": int(event.get("tool_steps", 0)),
                        "turn_id": turn_id,
                    }

            if done_payload is None:
                done_payload = {"reply": final_reply, "tool_trace": [], "tool_steps": 0, "turn_id": turn_id}

            safe_tool_trace = (
                done_payload.get("tool_trace", [])
                if isinstance(done_payload.get("tool_trace"), list)
                else []
            )
            safe_tool_steps = int(done_payload.get("tool_steps", 0))
            final_reply = str(final_reply or done_payload.get("reply", ""))
            done_payload = {
                "reply": final_reply,
                "tool_trace": safe_tool_trace,
                "tool_steps": safe_tool_steps,
                "turn_id": turn_id,
            }

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

            decorated_reply = _inject_tool_call_ids_into_marker_text(
                str(done_payload.get("reply", "")),
                done_payload.get("tool_trace", []),
            )
            if decorated_reply != str(done_payload.get("reply", "")):
                done_payload["reply"] = decorated_reply
                final_reply = decorated_reply

            yield _sse_event("done", done_payload)
            # 优先持久化带 reset 标记的 final_reply；没有内容时再回退 done_payload.reply
            reply = str(final_reply or done_payload.get("reply", ""))
            _save_chat_messages(
                sid,
                text,
                reply,
                # 流式已把标记内嵌到 assistant 文本，不再额外落一条 tool_marker，避免变成 A-B-标记
                tool_marker=False,
                tool_trace=done_payload.get("tool_trace", []),
                turn_id=turn_id,
            )
            _generate_title_from_first_round(sid)
        except Exception as e:
            yield _sse_event("error", {"message": str(e)})

    from starlette.responses import StreamingResponse

    return StreamingResponse(event_iter(), media_type="text/event-stream")


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
    _require_login()
    # 兼容旧前端写入路径
    sid = str(req.session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)

    rows: list[dict] = []
    for it in req.entries or []:
        if not isinstance(it, dict):
            continue
        role = str(it.get("role", "assistant")).strip()
        if role not in {"user", "assistant", "system"}:
            role = "assistant"
        rows.append(
            {
                "id": f"m_{uuid.uuid4().hex[:16]}",
                "role": role,
                "content": str(it.get("content", "")),
                "entry_type": str(it.get("entry_type", "chat")),
                "terminal_kind": str(it.get("terminal_kind", "")),
                "terminal_class": str(it.get("terminal_class", it.get("class", ""))),
                "is_summary": False,
                "created_at": str(it.get("ts", "")) or _now_iso(),
                "turn_id": _normalize_turn_id(str(it.get("turn_id", "") or "")),
            }
        )
    try:
        saved = _store.append_messages(sid, rows)
    except SessionStoreError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "session_id": sid, "record": saved})


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
    try:
        _tool_runtime.stop_session(req.session_id)
    except Exception as e:
        _audit_web(
            "SYSTEM_EXECUTE",
            "import_record_compat",
            f"stop_tool_runtime_failed session_id={req.session_id}",
            {"session_id": str(req.session_id), "ok": False, "error": str(e)},
        )
    _store.create_session(req.session_id, title="新对话")
    _store.append_messages(req.session_id, rows)
    return JSONResponse({"ok": True, "session_id": req.session_id, "entries": rows})


if __name__ == "__main__":
    import uvicorn

    _DEFAULT_PORT = 8000
    _DEFAULT_HOST = "0.0.0.0"
    uvicorn.run("TindaAgent.Web.server:app", host=_DEFAULT_HOST, port=_DEFAULT_PORT, reload=True)
