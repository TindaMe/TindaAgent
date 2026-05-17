import functools
import contextlib
import io
import json
import os
import re
import subprocess
import shutil
import gzip
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
    for name, info in {**SYSTEM_TOOL, **SPARE_TOOL}.items():
        if user_perm is None or (user_perm & info["perm"]) == info["perm"]:
            result[name] = info["des"]
    return result


def build_agent_tool_schemas(user_perm: int) -> list[dict[str, Any]]:
    """Build OpenAI tool schemas — each tool exposed directly, params from signature."""
    import inspect

    tools = list_tools(user_perm)
    schemas: list[dict[str, Any]] = []

    for tool_name, tool_desc in sorted(tools.items()):
        info = find_tool(tool_name)
        properties: dict[str, Any] = {}
        required: list[str] = []

        if info:
            has_var_positional = False
            try:
                sig = inspect.signature(info["func"])
                for pname, param in sig.parameters.items():
                    if pname.startswith("_") or pname in ("call_id", "command"):
                        continue  # 内部参数/别名，不暴露给 LLM
                    if param.kind == inspect.Parameter.VAR_POSITIONAL:
                        has_var_positional = True
                        continue
                    if param.kind == inspect.Parameter.VAR_KEYWORD:
                        continue
                    ptype = "string"
                    if param.annotation is not inspect.Parameter.empty:
                        a = param.annotation
                        if a is int: ptype = "integer"
                        elif a is bool: ptype = "boolean"
                    properties[pname] = {"type": ptype}
                    if param.default is inspect.Parameter.empty:
                        required.append(pname)
                # 变长位置参数工具（如 echo）暴露 text 属性
                if has_var_positional and not properties:
                    properties["text"] = {"type": "string"}
            except Exception:
                pass

        schema_params: dict[str, Any] = {"type": "object"}
        if properties:
            schema_params["properties"] = properties
            schema_params["required"] = required
        schemas.append({
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_desc,
                "parameters": schema_params,
            },
        })
    return schemas


def run_agent_tool(
    agent_tool_name: str,
    user_perm: int,
    arguments: dict[str, Any] | None,
    *,
    call_id: str | None = None,
) -> str:
    """
    Execute a tool on behalf of the LLM agent. Returns JSON string.
    agent_tool_name is the registered tool name. arguments are treated as kwargs.
    """
    payload = arguments if isinstance(arguments, dict) else {}
    call_id_text = str(call_id or "").strip()

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

    # All other LLM arguments are kwargs — filter internal params
    call_kwargs: dict[str, str] = {}
    for key, value in payload.items():
        clean_key = str(key).strip()
        if not clean_key or clean_key.startswith("_"):
            continue
        if clean_key == "args":
            continue  # already handled above
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


@tool(perm.PUBLIC_EXECUTE, "Print text to tool stdout (param: text)", must=True)
def echo(text: str = "", *content_list: str) -> None:
    """Print text to stdout."""
    if text:
        print(text)
    for content in content_list:
        print(content)


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


@tool(perm.TOOL_EXECUTE | perm.PUBLIC_EXECUTE, "Execute a shell command in terminal. Parameters: cmd=command string, supports multiline bash/heredoc; note=purpose (max 80 chars); timeout=seconds (default 30); cwd=working dir (optional). System operations (rm/mv/chmod etc) require SYSTEM_EXECUTE permission.", must=True)
def run_terminal(
    cmd: str = "",
    timeout: int = 30,
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

    try:
        work_dir = str(cwd).strip() if cwd else None
        if work_dir and not Path(work_dir).is_dir():
            cwd_info = f" (cwd 不存在，已用当前目录)"
            work_dir = None
        exec_cwd = work_dir or os.getcwd()
        shell_path = shutil.which("bash") or "/bin/sh"
        result = subprocess.run(
            command,
            shell=True,
            executable=shell_path,
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout), 120)),
            cwd=work_dir,
            env={**_safe_env(), "PYTHONUNBUFFERED": "1"},
        )
        stdout_raw = result.stdout or ""
        stderr_raw = result.stderr or ""
        out = stdout_raw + stderr_raw
        if len(out) > 8000:
            out = out[:8000] + "\n...(output truncated)"
        safe_stdout = redact_sensitive_text(stdout_raw)
        safe_stderr = redact_sensitive_text(stderr_raw)
        safe_output = redact_sensitive_text(out.strip() or "(no output)")
        ret = {
            "ok": result.returncode == 0,
            "success": result.returncode == 0,
            "cmd": command,
            "note": note_text,
            "cwd": exec_cwd,
            "shell": shell_path,
            "stdout": safe_stdout,
            "stderr": safe_stderr,
            "returncode": result.returncode,
            "output": safe_output,
            "pending_confirmation": False,
            "approval": True if approval is True else approval,
        }
        if result.returncode != 0:
            ret["error"] = f"Command failed with exit code {result.returncode}"
        if cwd_info:
            ret["cwd_note"] = cwd_info
        return ret
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Command timed out (>{timeout}s): {command[:120]}",
                "cmd": command, "note": note_text}
    except Exception as e:
        return {"ok": False, "error": str(e), "cmd": command[:120], "note": note_text}


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
