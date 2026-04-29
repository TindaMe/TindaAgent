import functools
import contextlib
import io
import json
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Callable
from TindaAgent.Process.Architecture import perm
from TindaAgent.Process.Architecture.paths import get_memory_file
from TindaAgent.Process.Observability import audit_event
from TindaAgent.Process.Architecture.paths import get_log_root, get_legacy_log_root
from TindaAgent.Process.Security import terminal_policy
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

AGENT_LIST_TOOLS_NAME = "list_available_tools"
AGENT_CALL_TOOL_NAME = "call_backend_tool"

MAX_TEXT_LEN = 8000
DEFAULT_TIMEZONE = "Asia/Shanghai"
MEMORY_MAX_ITEMS = 500
MEMORY_MAX_DATA_LEN = 2000
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


def _split_sentences(text: str) -> list[str]:
    parts = [x.strip() for x in re.split(r"[。！？!?]\s*|\n+", text) if x.strip()]
    if len(parts) <= 1:
        parts = [x.strip() for x in re.split(r"[；;，,]\s*", text) if x.strip()]
    return parts


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
        _invalidate_tool_caches()
        import inspect as _inspect
        if "_caller_perm" in _inspect.signature(func).parameters:
            _caller_perm_funcs.add(id(func))

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
        raise ValueError(f"工具 {tool_name} 未注册")

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
        raise PermissionDeniedError(f"调用 {tool_name} 权限不足", payload=payload)

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
        if id(tool_info["func"]) in _caller_perm_funcs:
            kwargs["_caller_perm"] = int(user_perm)
        result = tool_info["func"](*args, **kwargs)
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


_all_tools_cache: dict[str, dict] | None = None
_schema_cache: dict[int, list[dict]] = {}
_caller_perm_funcs: set[int] = set()  # id(func) for tools that accept _caller_perm


def _invalidate_tool_caches() -> None:
    global _all_tools_cache, _schema_cache
    _all_tools_cache = None
    _schema_cache.clear()


def list_tools(user_perm: int | None = None) -> dict[str, str]:
    global _all_tools_cache
    if _all_tools_cache is None:
        _all_tools_cache = {**SYSTEM_TOOL, **SPARE_TOOL}
    result: dict[str, str] = {}
    for name, info in _all_tools_cache.items():
        if user_perm is None or (user_perm & info["perm"]) == info["perm"]:
            result[name] = info["des"]
    return result


def build_agent_tool_schemas(user_perm: int) -> list[dict[str, Any]]:
    cached = _schema_cache.get(user_perm)
    if cached is not None:
        return cached
    tools = list_tools(user_perm)
    tool_hint = ", ".join(sorted(tools.keys())) if tools else "无"
    schemas: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": AGENT_LIST_TOOLS_NAME,
                "description": "列出当前会话可调用的后端工具及用途。",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": AGENT_CALL_TOOL_NAME,
                "description": (
                    "调用一个后端工具。优先传字符串参数；短文本任务不必强制调用工具。"
                    f"当前可用工具: {tool_hint}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool_name": {"type": "string", "description": "目标工具名称"},
                        "args": {"type": "array", "description": "传给工具的位置参数列表（字符串）", "items": {"type": "string"}, "default": []},
                        "kwargs": {"type": "object", "description": "传给工具的具名参数（值会按字符串处理）", "additionalProperties": {"type": "string"}, "default": {}},
                    },
                    "required": ["tool_name"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    _schema_cache[user_perm] = schemas
    return schemas


def run_agent_tool(
    agent_tool_name: str,
    user_perm: int,
    arguments: dict[str, Any] | None,
    *,
    call_id: str | None = None,
) -> str:
    """
    用处：执行模型可见的代理工具（统一入口，返回 JSON 字符串）
    """
    payload = arguments if isinstance(arguments, dict) else {}
    call_id_text = str(call_id or "").strip()
    audit_event(
        op_type="TOOL_EXECUTE",
        subsystem="tool",
        func="run_agent_tool",
        file_path=_THIS_FILE,
        content=f"agent_tool_dispatch agent_tool={agent_tool_name}",
        extra={
            "agent_tool_name": str(agent_tool_name),
            "user_perm": int(user_perm),
            "has_arguments": isinstance(arguments, dict),
            "call_id": call_id_text,
        },
    )

    if agent_tool_name == AGENT_LIST_TOOLS_NAME:
        audit_event(
            op_type="TOOL_READ",
            subsystem="tool",
            func="run_agent_tool",
            file_path=_THIS_FILE,
            content="agent_list_tools",
            extra={"user_perm": int(user_perm)},
        )
        out = {"ok": True, "tools": list_tools(user_perm)}
        if call_id_text:
            out["call_id"] = call_id_text
        return json.dumps(out, ensure_ascii=False)

    # 兼容两种调用形态：
    # 1) 标准封装：agent_tool_name=call_backend_tool，真实工具名在 payload.tool_name
    # 2) 直接调用：agent_tool_name=echo/get_current_time/...，参数直接放在 payload
    if agent_tool_name == AGENT_CALL_TOOL_NAME:
        tool_name = str(payload.get("tool_name", "")).strip()
        if not tool_name:
            audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="tool",
                func="run_agent_tool",
                file_path=_THIS_FILE,
                content="agent_call_tool_missing_tool_name",
                extra={"ok": False, "user_perm": int(user_perm)},
            )
            out = {"ok": False, "error": "tool_name 不能为空"}
            if call_id_text:
                out["call_id"] = call_id_text
            return json.dumps(out, ensure_ascii=False)
    else:
        tool_name = str(agent_tool_name or "").strip()
        if not find_tool(tool_name):
            audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="tool",
                func="run_agent_tool",
                file_path=_THIS_FILE,
                content=f"agent_tool_not_supported tool={agent_tool_name}",
                extra={"ok": False, "user_perm": int(user_perm)},
            )
            out = {"ok": False, "error": f"不支持的 agent 工具: {agent_tool_name}"}
            if call_id_text:
                out["call_id"] = call_id_text
            return json.dumps(out, ensure_ascii=False)

    raw_args = payload.get("args", [])
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list):
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool",
            func="run_agent_tool",
            file_path=_THIS_FILE,
            content=f"agent_tool_invalid_args tool={tool_name}",
            extra={"ok": False, "user_perm": int(user_perm)},
        )
        out = {"ok": False, "tool_name": tool_name, "error": "args 必须是数组"}
        if call_id_text:
            out["call_id"] = call_id_text
        return json.dumps(out, ensure_ascii=False)
    raw_kwargs = payload.get("kwargs", {})
    if raw_kwargs is None:
        raw_kwargs = {}
    if not isinstance(raw_kwargs, dict):
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool",
            func="run_agent_tool",
            file_path=_THIS_FILE,
            content=f"agent_tool_invalid_kwargs tool={tool_name}",
            extra={"ok": False, "user_perm": int(user_perm)},
        )
        out = {"ok": False, "tool_name": tool_name, "error": "kwargs 必须是对象"}
        if call_id_text:
            out["call_id"] = call_id_text
        return json.dumps(out, ensure_ascii=False)

    call_args = [str(x) for x in raw_args]
    call_kwargs: dict[str, str] = {}
    for key, value in raw_kwargs.items():
        clean_key = str(key).strip()
        if not clean_key:
            audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="tool",
                func="run_agent_tool",
                file_path=_THIS_FILE,
                content=f"agent_tool_empty_kwarg_key tool={tool_name}",
                extra={"ok": False, "user_perm": int(user_perm)},
            )
            out = {"ok": False, "tool_name": tool_name, "error": "kwargs 的键不能为空"}
            if call_id_text:
                out["call_id"] = call_id_text
            return json.dumps(out, ensure_ascii=False)
        call_kwargs[clean_key] = str(value)

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
    payload: dict[str, Any] = {"ok": True, "tool_name": tool_name}
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
            "ok": True,
            "user_perm": int(user_perm),
            "has_stdout": bool(printed),
            "has_result": result is not None,
        },
    )
    return json.dumps(payload, ensure_ascii=False, default=str)


@tool(perm.PUBLIC_EXECUTE, "按行输出文本到工具 stdout（可传多段）", must=True)
def echo(*content_list: str) -> None:
    """
    多次打印内容
    """
    for content in content_list:
        print(content)


@tool(perm.PUBLIC_READ, "获取 Tinda 的个人简介（供模型了解用户背景）", must=True)
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


@tool(perm.PUBLIC_READ, "获取当前时间（支持传入 tz 时区，如 Asia/Shanghai）", must=True)
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


@tool(perm.PUBLIC_READ, "摘要长文本（输入 text，可选 max_sentences=1-8，兼容 sentences/n_sentences）", must=True)
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


@tool(perm.PUBLIC_READ, "提取关键词（输入 text，可选 top_k=1-20，兼容 n_keywords）", must=True)
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


@tool(perm.PUBLIC_READ, "读取 Tinda 资料片段（key: full/bio/project/contact/slogan）", must=True)
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
        return f"不支持的 key: {key}。可用 key: {valid_keys}"

    if target == "full":
        return (
            f"{PROFILE_SNIPPETS['bio']}\n"
            f"{PROFILE_SNIPPETS['project']}\n"
            f"{PROFILE_SNIPPETS['contact']}\n"
            "——\n"
            f"{PROFILE_SNIPPETS['slogan']}"
        )
    return PROFILE_SNIPPETS[target]


@tool(perm.PUBLIC_READ, "读取全局记忆 JSON（time/data）", must=True)
def read_memories() -> dict[str, Any]:
    """
    读取全局记忆；损坏时自动回退到空结构
    """
    return _load_memory_payload()


@tool(perm.PUBLIC_WRITE, "写入一条全局记忆（参数: data, 可选 time）", must=True)
def save_memory(data: str, time: str = "") -> dict[str, Any]:
    """
    写入一条长期记忆，自动更新 updated_at
    """
    content = str(data or "").strip()
    if not content:
        raise ValueError("data 不能为空")
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


@tool(perm.PUBLIC_WRITE, "删除全局记忆（按包含文本匹配 data，参数: contains）", must=True)
def delete_memory(contains: str) -> dict[str, Any]:
    """
    按子串匹配删除记忆，便于人工清理错误记忆
    """
    keyword = str(contains or "").strip()
    if not keyword:
        raise ValueError("contains 不能为空")

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


@tool(perm.USER_ADMIN, "空工具（仅满权限可调用，用于权限验证）", must=True)
def admin_noop() -> dict[str, Any]:
    """
    用于权限系统联调：该工具不执行任何业务逻辑，仅返回固定结果。
    """
    return {"ok": True, "message": "admin_noop executed"}


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
    paths: list[Path] = []
    primary = get_log_root() / "total.jsonl"
    if primary.exists() and primary.is_file():
        paths.append(primary)
    legacy = get_legacy_log_root() / "total.jsonl"
    if legacy.exists() and legacy.is_file():
        try:
            if legacy.resolve() != primary.resolve():
                paths.append(legacy)
        except Exception:
            paths.append(legacy)
    return paths


@tool(perm.PUBLIC_READ, "按审计ID查询日志事件（id 支持纯数字或 tc_ 前缀）", must=True)
def get_log_event_by_id(id: str) -> dict[str, Any]:
    """
    根据审计事件 ID 查询 total.jsonl 中的原始事件。
    """
    parsed_id = _parse_event_id(id)
    if parsed_id is None:
        raise ValueError("id 无效，支持纯数字或 tc_前缀")

    for path in _iter_total_jsonl_candidates():
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


@tool(perm.TOOL_EXECUTE | perm.PUBLIC_EXECUTE, "在终端执行一条 shell 命令（参数: cmd=命令字符串, timeout=超时秒数默认30, cwd=工作目录可选）。系统级操作(rm/mv/chmod等)需额外SYSTEM权限。", must=True)
def run_terminal(cmd: str, timeout: int = 30, cwd: str | None = None, _caller_perm: int = 0) -> dict[str, Any]:
    command = str(cmd or "").strip()
    if not command:
        return {"ok": False, "error": "cmd 不能为空"}

    blacklisted = terminal_policy.check_blacklist(command)
    if blacklisted:
        return {"ok": False, "error": f"命令被黑名单拦截: {', '.join(blacklisted)}"}

    sys_ops = terminal_policy.detect_system_operations(command)
    if sys_ops and (_caller_perm & perm.SYSTEM_EXECUTE) != perm.SYSTEM_EXECUTE:
        return {"ok": False, "error": f"命令涉及系统操作({', '.join(sys_ops)})，需要 SYSTEM_EXECUTE 权限，当前权限不足"}

    try:
        work_dir = str(cwd).strip() if cwd else None
        if work_dir and not Path(work_dir).is_dir():
            work_dir = None
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout), 120)),
            cwd=work_dir,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        out = (result.stdout or "") + (result.stderr or "")
        if len(out) > 8000:
            out = out[:8000] + "\n...(output truncated)"
        return {
            "ok": True,
            "cmd": command,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "returncode": result.returncode,
            "output": out.strip() or "(no output)",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"命令超时（>{timeout}s）: {command[:120]}"}
    except Exception as e:
        return {"ok": False, "error": str(e), "cmd": command[:120]}
