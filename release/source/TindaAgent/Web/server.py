from pathlib import Path
import contextlib
import io
import json
import logging
import shlex
import time
from dataclasses import dataclass
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from TindaAgent.Process.AI.agent import Agent
from TindaAgent.Process.AI.client import LLMClient
from TindaAgent.Tool import tool as tool_registry
from TindaAgent.User import userstatus, userdata
from TindaAgent.Web.records_store import ChatRecordStore, RecordStoreError, strip_user_meta_block
from TindaAgent.log.error_logger import log_exception

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 初始化 client，让 Agent 能拿到 model 名写入 prompt
_client = LLMClient()
_sessions: dict[str, Agent] = {}
_session_last_access: dict[str, float] = {}
_MAX_SESSIONS = 200
_MAX_SESSION_ID_LEN = 64
_CHAT_RECORDS_ROOT = Path(__file__).resolve().parent.parent / "Data" / "ChatRecords"
_record_store = ChatRecordStore(_CHAT_RECORDS_ROOT)
_logger = logging.getLogger("tinda.web")
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

_HTML_HOME = (Path(__file__).parent / "home.html").read_text(encoding="utf-8")
_HTML_CHAT = (Path(__file__).parent / "chat.html").read_text(encoding="utf-8")


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    meta_user_name: str | None = None
    meta_user_id: str | None = None
    meta_user_perm: str | None = None
    meta_time_iso: str | None = None
    meta_time_text: str | None = None


class ResetRequest(BaseModel):
    session_id: str = "default"


class ToolsRequest(BaseModel):
    session_id: str = "default"


class ImportRecordRequest(BaseModel):
    session_id: str = "default"
    record_id: str


class SessionEventsRequest(BaseModel):
    session_id: str = "default"
    entries: list[dict] = Field(default_factory=list)


class ModelSwitchRequest(BaseModel):
    model: str


@dataclass
class UserProfileResponse:
    name: str
    uid: str
    perm: int
    perm_label: str
    token: str


def _sse_event(name: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {name}\ndata: {payload}\n\n"


def _perm_label(p: int) -> str:
    if p == 0:
        return "NONE"
    labels: list[str] = []
    mapping = [
        ("PUBLIC_READ", 1 << 0),
        ("PUBLIC_WRITE", 1 << 1),
        ("PUBLIC_EXECUTE", 1 << 2),
        ("TOOL_READ", 1 << 3),
        ("TOOL_WRITE", 1 << 4),
        ("TOOL_EXECUTE", 1 << 5),
        ("SYSTEM_READ", 1 << 6),
        ("SYSTEM_WRITE", 1 << 7),
        ("SYSTEM_EXECUTE", 1 << 8),
    ]
    for name, bit in mapping:
        if (p & bit) == bit:
            labels.append(name)
    return " | ".join(labels) if labels else str(p)


def _get_web_profile() -> UserProfileResponse:
    current = userstatus.user.get_current_user()
    if current is None:
        current = userdata.ensure_default_user("Tinda")
        userstatus.user.set_current_user(current)
    info = userdata.export_public_user(current)
    perm_value = int(info.get("perm", 0))
    return UserProfileResponse(
        name=str(info.get("name", "")),
        uid=str(info.get("uid", "")),
        perm=perm_value,
        perm_label=_perm_label(perm_value),
        token=str(info.get("token", "")),
    )


def _touch_session(session_id: str) -> None:
    _session_last_access[session_id] = time.time()


def _evict_if_needed() -> None:
    if len(_sessions) < _MAX_SESSIONS:
        return
    oldest = min(_session_last_access.items(), key=lambda x: x[1])[0]
    _sessions.pop(oldest, None)
    _session_last_access.pop(oldest, None)


def _get_agent(session_id: str) -> Agent:
    if session_id not in _sessions:
        _evict_if_needed()
        agent = Agent(
            f"web-bot-{session_id}",
            client=_client,
            model_name=_client.model,
        )
        try:
            saved = _record_store.load_latest_for_session(session_id)
            if saved and saved.get("entries"):
                saved_msgs = _entries_to_agent_messages(saved.get("entries", []))
                agent.replace_conversation(saved_msgs)
        except Exception as e:
            _logger.warning("restore session from records failed: session=%s err=%s", session_id, e)
            log_exception("web.restore_session", e, session_id=session_id)
        _sessions[session_id] = agent
    _touch_session(session_id)
    return _sessions[session_id]


def _normalize_session_id(session_id: str) -> str:
    sid = (session_id or "").strip()
    if not sid:
        return "default"
    if len(sid) > _MAX_SESSION_ID_LEN:
        sid = sid[:_MAX_SESSION_ID_LEN]
    return sid


def _sanitize_meta_value(value: str | None, max_len: int = 240) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if not text:
        return "N/A"
    return text[:max_len]


def _normalize_model_choice(raw: str | None) -> str | None:
    key = str(raw or "").strip().lower()
    if not key:
        return None
    return _MODEL_ALIAS.get(key)


def _build_user_message_with_meta(
    raw_message: str,
    *,
    meta_user_name: str | None,
    meta_user_id: str | None,
    meta_user_perm: str | None,
    meta_time_iso: str | None,
    meta_time_text: str | None,
) -> str:
    """
    将用户正文与元信息块拼接为发送给模型的最终消息。
    该元信息仅对模型可见，不直接展示在前端用户气泡。
    """
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


def _entry_to_agent_message(entry: dict) -> dict[str, str] | None:
    role = str(entry.get("role", "")).strip()
    if role not in {"user", "assistant"}:
        return None
    content = str(entry.get("content", "")).strip()
    if not content:
        return None

    entry_type = str(entry.get("entry_type", "chat")).strip() or "chat"
    if entry_type == "notice":
        role = "assistant"
        content = f"[系统提示] {content}"
    elif entry_type == "tool_marker":
        role = "assistant"
        content = f"[工具调用] {content}"
    elif entry_type == "terminal":
        role = "assistant"
        terminal_kind = str(entry.get("terminal_kind", "out")).strip() or "out"
        content = f"[终端/{terminal_kind}] {content}"
    return {"role": role, "content": content}


def _entries_to_agent_messages(entries: list[dict]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        msg = _entry_to_agent_message(item)
        if msg is not None:
            messages.append(msg)
    return messages


def _append_entries_to_agent(agent: Agent, entries: list[dict]) -> None:
    additions = _entries_to_agent_messages(entries)
    if not additions:
        return
    conv = agent.get_conversation_messages()
    conv.extend(additions)
    agent.replace_conversation(conv)


def _format_tool_list(agent: Agent) -> str:
    tools = tool_registry.list_tools(agent.perm)
    if not tools:
        return "当前权限下没有可用工具。"
    lines = ["可用工具："]
    for name, desc in sorted(tools.items()):
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def _exec_tool_command(agent: Agent, raw_msg: str) -> str | None:
    if not raw_msg.startswith("/"):
        return None

    try:
        parts = shlex.split(raw_msg)
    except ValueError as e:
        return f"命令解析失败: {e}"

    if not parts:
        return "空命令。输入 /help 查看可用命令。"

    cmd = parts[0].lower()
    if cmd == "/help":
        return (
            "命令列表：\n"
            "/help 查看命令\n"
            "/tools 查看可用工具\n"
            "/tool <工具名> [参数...] 调用工具\n"
            "/reset 清空当前会话上下文"
        )
    if cmd == "/tools":
        return _format_tool_list(agent)
    if cmd == "/reset":
        agent.reset_history()
        return "当前会话上下文已清空。"
    if cmd != "/tool":
        return "未知命令。输入 /help 查看可用命令。"

    if len(parts) < 2:
        return "用法: /tool <工具名> [参数...]"

    tool_name = parts[1]
    args = parts[2:]
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        try:
            result = tool_registry.run_tool(tool_name, agent.perm, *args)
        except (ValueError, PermissionError) as e:
            return str(e)
    printed = capture.getvalue().strip()

    chunks: list[str] = []
    if printed:
        chunks.append(printed)
    if result is not None:
        chunks.append(f"返回值: {result}")
    if not chunks:
        chunks.append("工具执行完成。")
    return "\n".join(chunks)


def _collect_entries_for_persistence(agent: Agent) -> list[dict[str, str]]:
    raw_entries: list[dict[str, str]] = []
    for msg in agent.get_conversation_messages():
        role = str(msg.get("role", "")).strip()
        if role == "user":
            user_text = strip_user_meta_block(str(msg.get("content", ""))).strip()
            if not user_text:
                continue
            raw_entries.append({"role": "user", "entry_type": "chat", "content": user_text})
            continue

        if role != "assistant":
            continue

        assistant_text = str(msg.get("content", "")).strip()
        has_tool_calls = bool(msg.get("tool_calls"))
        if has_tool_calls:
            # 工具阶段的过渡文本 + 标记都要保留，保证重载后可见调用痕迹
            if assistant_text:
                raw_entries.append({"role": "assistant", "entry_type": "chat", "content": assistant_text})
            raw_entries.append({"role": "assistant", "entry_type": "tool_marker", "content": "> --调用工具中--"})
            continue

        if not assistant_text:
            continue
        raw_entries.append({"role": "assistant", "entry_type": "chat", "content": assistant_text})
    return raw_entries


def _save_session_record(agent: Agent, session_id: str) -> dict | None:
    entries = _collect_entries_for_persistence(agent)
    if not entries:
        return None
    try:
        return _record_store.save_session_entries(session_id, entries, preserve_non_chat=True)
    except Exception as e:
        _logger.warning("save session record failed: session=%s err=%s", session_id, e)
        log_exception("web.save_session_record", e, session_id=session_id)
        return None


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML_HOME


@app.get("/chat", response_class=HTMLResponse)
async def chat_page_legacy():
    return RedirectResponse(url="/", status_code=307)


@app.get("/app", response_class=HTMLResponse)
async def chat_page():
    return _HTML_CHAT


@app.post("/chat")
async def chat(req: ChatRequest):
    sid = _normalize_session_id(req.session_id)
    try:
        agent = _get_agent(sid)
        command_reply = _exec_tool_command(agent, req.message.strip())
        if command_reply is not None:
            return JSONResponse({"reply": command_reply})
        llm_message = _build_user_message_with_meta(
            req.message,
            meta_user_name=req.meta_user_name,
            meta_user_id=req.meta_user_id,
            meta_user_perm=req.meta_user_perm,
            meta_time_iso=req.meta_time_iso,
            meta_time_text=req.meta_time_text,
        )
        result = agent.chat_with_meta(llm_message)
        _save_session_record(agent, sid)
        return JSONResponse(
            {
                "reply": result.get("reply", ""),
                "tool_trace": result.get("tool_trace", []),
                "tool_steps": result.get("tool_steps", 0),
            }
        )
    except Exception as e:
        log_exception("web.chat", e, session_id=sid)
        return JSONResponse({"detail": "chat failed"}, status_code=500)


@app.get("/chat/stream")
async def chat_stream(
    message: str,
    session_id: str = "default",
    meta_user_name: str | None = None,
    meta_user_id: str | None = None,
    meta_user_perm: str | None = None,
    meta_time_iso: str | None = None,
    meta_time_text: str | None = None,
):
    sid = _normalize_session_id(session_id)
    agent = _get_agent(sid)
    command_reply = _exec_tool_command(agent, message.strip())
    if command_reply is not None:
        chunks = [
            _sse_event("delta", {"content": command_reply}),
            _sse_event("done", {"reply": command_reply, "tool_trace": [], "tool_steps": 0}),
        ]
        return HTMLResponse("".join(chunks), media_type="text/event-stream")

    llm_message = _build_user_message_with_meta(
        message,
        meta_user_name=meta_user_name,
        meta_user_id=meta_user_id,
        meta_user_perm=meta_user_perm,
        meta_time_iso=meta_time_iso,
        meta_time_text=meta_time_text,
    )

    def event_iter():
        saw_done = False
        try:
            for event in agent.stream_chat_events(llm_message):
                et = event.get("type", "")
                if et == "delta":
                    yield _sse_event("delta", {"content": event.get("content", "")})
                elif et == "reset":
                    yield _sse_event("reset", {})
                elif et == "done":
                    saw_done = True
                    yield _sse_event(
                        "done",
                        {
                            "reply": event.get("reply", ""),
                            "tool_trace": event.get("tool_trace", []),
                            "tool_steps": int(event.get("tool_steps", 0)),
                        },
                    )
            # 注意：Agent 在生成器结束后才会把 final history_delta 写回 history。
            # 必须在 for 循环结束后再保存，才能包含最后一条 assistant 回复。
            if saw_done:
                _save_session_record(agent, sid)
        except Exception as e:
            log_exception("web.chat_stream", e, session_id=sid)
            yield _sse_event("error", {"message": str(e)})

    from starlette.responses import StreamingResponse
    return StreamingResponse(event_iter(), media_type="text/event-stream")


@app.post("/reset")
async def reset(req: ResetRequest):
    sid = _normalize_session_id(req.session_id)
    agent = _get_agent(sid)
    agent.reset_history()
    return JSONResponse({"ok": True})


@app.post("/tools")
async def tools(req: ToolsRequest):
    agent = _get_agent(_normalize_session_id(req.session_id))
    return JSONResponse({"tools": tool_registry.list_tools(agent.perm)})


@app.post("/session/events")
async def session_events(req: SessionEventsRequest):
    sid = _normalize_session_id(req.session_id)
    entries = req.entries if isinstance(req.entries, list) else []
    try:
        saved = _record_store.append_session_entries(sid, entries)
    except RecordStoreError as e:
        log_exception("web.session_events.validation", e, session_id=sid)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        _logger.warning("append session events failed: session=%s err=%s", sid, e)
        log_exception("web.session_events", e, session_id=sid)
        return JSONResponse({"ok": False, "error": "append failed"}, status_code=500)

    # 按用户要求：系统小气泡/终端轨迹也进入模型上下文
    existed = sid in _sessions
    agent = _get_agent(sid)
    context_entries: list[dict] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        entry_type = str(item.get("entry_type", "")).strip()
        if entry_type in {"notice", "tool_marker", "terminal"}:
            context_entries.append(item)
    if existed:
        _append_entries_to_agent(agent, context_entries)

    saved_record = saved or {}
    return JSONResponse(
        {
            "ok": True,
            "session_id": sid,
            "record": {
                "record_id": saved_record.get("record_id", ""),
                "created_at": saved_record.get("created_at", ""),
                "updated_at": saved_record.get("updated_at", ""),
                "message_count": saved_record.get("message_count", 0),
            },
        }
    )


@app.get("/records")
async def list_records(limit: int = 50, offset: int = 0, q: str = ""):
    payload = _record_store.list_records(limit=limit, offset=offset, query=q)
    return JSONResponse(payload)


@app.get("/records/session")
async def session_record(session_id: str = "default"):
    sid = _normalize_session_id(session_id)
    payload = _record_store.load_latest_for_session(sid)
    if not payload:
        return JSONResponse({"found": False, "session_id": sid})

    agent = _get_agent(sid)
    if not agent.get_conversation_messages():
        msgs = _entries_to_agent_messages(payload.get("entries", []))
        agent.replace_conversation(msgs)

    return JSONResponse(
        {
            "found": True,
            "session_id": sid,
            "record": {
                "record_id": payload.get("record_id", ""),
                "created_at": payload.get("created_at", ""),
                "updated_at": payload.get("updated_at", ""),
                "message_count": payload.get("message_count", 0),
            },
            "entries": payload.get("entries", []),
        }
    )


@app.post("/records/import")
async def import_record(req: ImportRecordRequest):
    sid = _normalize_session_id(req.session_id)
    try:
        payload = _record_store.load_record(req.record_id)
    except RecordStoreError as e:
        log_exception("web.import_record.validation", e, session_id=sid, record_id=req.record_id)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        log_exception("web.import_record", e, session_id=sid, record_id=req.record_id)
        return JSONResponse({"ok": False, "error": "import failed"}, status_code=500)

    entries = payload.get("entries", [])
    msgs = _entries_to_agent_messages(entries)
    agent = _get_agent(sid)
    agent.replace_conversation(msgs)
    saved = _record_store.save_session_entries(sid, entries, preserve_non_chat=False)
    saved_record = saved or {
        "record_id": payload.get("record_id", ""),
        "created_at": payload.get("created_at", ""),
        "updated_at": payload.get("updated_at", ""),
        "message_count": payload.get("message_count", 0),
    }

    return JSONResponse(
        {
            "ok": True,
            "session_id": sid,
            "record": {
                "record_id": saved_record.get("record_id", ""),
                "created_at": saved_record.get("created_at", ""),
                "updated_at": saved_record.get("updated_at", ""),
                "message_count": saved_record.get("message_count", 0),
            },
            "entries": entries,
        }
    )


@app.get("/user/profile")
async def user_profile():
    profile = _get_web_profile()
    return JSONResponse(
        {
            "name": profile.name,
            "uid": profile.uid,
            "perm": profile.perm,
            "perm_label": profile.perm_label,
            "token": profile.token,
        }
    )


@app.get("/model")
async def get_model():
    return JSONResponse(
        {
            "current_model": _client.model,
            "available_models": list(_MODEL_CHOICES),
        }
    )


@app.post("/model")
async def switch_model(req: ModelSwitchRequest):
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("TindaAgent.Web.server:app", host="0.0.0.0", port=8000, reload=True)
