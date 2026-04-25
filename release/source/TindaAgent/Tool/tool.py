import functools
import contextlib
import io
import json
import re
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Callable
from TindaAgent.Process.Architecture import perm
from TindaAgent.log.error_logger import log_exception

# 系统工具注册表 {func_name: {"des": str, "perm": int, "func": function}}
SYSTEM_TOOL: dict[str, dict[str, Any]] = {}

# 备用工具注册表 {func_name: {"des": str, "perm": int, "func": function}}
SPARE_TOOL: dict[str, dict[str, Any]] = {}

AGENT_LIST_TOOLS_NAME = "list_available_tools"
AGENT_CALL_TOOL_NAME = "call_backend_tool"

MAX_TEXT_LEN = 8000
DEFAULT_TIMEZONE = "Asia/Shanghai"
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

        tool_info = {
            "des": tool_des,
            "perm": tool_perm,
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
        raise ValueError(f"工具 {tool_name} 未注册")

    required = tool_info["perm"]
    if (user_perm & required) != required:
        raise PermissionError(f"调用 {tool_name} 权限不足")

    return tool_info["func"](*args, **kwargs)


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
    """
    用处：构建供模型调用的工具 schema（OpenAI Chat Completions tools 格式）
    """
    tools = list_tools(user_perm)
    tool_hint = ", ".join(sorted(tools.keys())) if tools else "无"

    schemas: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": AGENT_LIST_TOOLS_NAME,
                "description": "列出当前会话可调用的后端工具及用途。",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
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
                        "tool_name": {
                            "type": "string",
                            "description": "目标工具名称",
                        },
                        "args": {
                            "type": "array",
                            "description": "传给工具的位置参数列表（字符串）",
                            "items": {"type": "string"},
                            "default": [],
                        },
                        "kwargs": {
                            "type": "object",
                            "description": "传给工具的具名参数（值会按字符串处理）",
                            "additionalProperties": {"type": "string"},
                            "default": {},
                        },
                    },
                    "required": ["tool_name"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    return schemas


def run_agent_tool(agent_tool_name: str, user_perm: int, arguments: dict[str, Any] | None) -> str:
    """
    用处：执行模型可见的代理工具（统一入口，返回 JSON 字符串）
    """
    payload = arguments if isinstance(arguments, dict) else {}

    if agent_tool_name == AGENT_LIST_TOOLS_NAME:
        return json.dumps(
            {"ok": True, "tools": list_tools(user_perm)},
            ensure_ascii=False,
        )

    if agent_tool_name != AGENT_CALL_TOOL_NAME:
        return json.dumps(
            {"ok": False, "error": f"不支持的 agent 工具: {agent_tool_name}"},
            ensure_ascii=False,
        )

    tool_name = str(payload.get("tool_name", "")).strip()
    if not tool_name:
        return json.dumps(
            {"ok": False, "error": "tool_name 不能为空"},
            ensure_ascii=False,
        )

    raw_args = payload.get("args", [])
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list):
        return json.dumps(
            {"ok": False, "tool_name": tool_name, "error": "args 必须是数组"},
            ensure_ascii=False,
        )
    raw_kwargs = payload.get("kwargs", {})
    if raw_kwargs is None:
        raw_kwargs = {}
    if not isinstance(raw_kwargs, dict):
        return json.dumps(
            {"ok": False, "tool_name": tool_name, "error": "kwargs 必须是对象"},
            ensure_ascii=False,
        )

    call_args = [str(x) for x in raw_args]
    call_kwargs: dict[str, str] = {}
    for key, value in raw_kwargs.items():
        clean_key = str(key).strip()
        if not clean_key:
            return json.dumps(
                {"ok": False, "tool_name": tool_name, "error": "kwargs 的键不能为空"},
                ensure_ascii=False,
            )
        call_kwargs[clean_key] = str(value)

    capture = io.StringIO()
    try:
        with contextlib.redirect_stdout(capture):
            result = run_tool(tool_name, user_perm, *call_args, **call_kwargs)
    except (ValueError, PermissionError) as e:
        log_exception(
            "tool.run_agent_tool.validation",
            e,
            agent_tool_name=agent_tool_name,
            tool_name=tool_name,
        )
        return json.dumps(
            {"ok": False, "tool_name": tool_name, "error": str(e)},
            ensure_ascii=False,
        )
    except Exception as e:
        log_exception(
            "tool.run_agent_tool",
            e,
            agent_tool_name=agent_tool_name,
            tool_name=tool_name,
        )
        return json.dumps(
            {"ok": False, "tool_name": tool_name, "error": f"执行异常: {e}"},
            ensure_ascii=False,
        )

    printed = capture.getvalue().strip()
    payload: dict[str, Any] = {"ok": True, "tool_name": tool_name}
    if printed:
        payload["stdout"] = printed
    if result is not None:
        payload["result"] = result
    if not printed and result is None:
        payload["result"] = "工具执行完成"
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


@tool(perm.PUBLIC_READ, "摘要长文本（输入 text，可选 max_sentences=1-8，兼容 sentences）", must=True)
def summarize_text(text: str, max_sentences: str = "3", sentences: str | None = None) -> str:
    """
    对输入文本做轻量摘要，返回压缩后的关键信息
    """
    clean_text = _normalize_text(text)
    if not clean_text:
        return "输入为空，无法摘要。"

    # 兼容模型偶发传参：sentences
    limit_raw = sentences if sentences is not None else max_sentences
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


@tool(perm.PUBLIC_READ, "提取关键词（输入 text，可选 top_k=1-20）", must=True)
def extract_keywords(text: str, top_k: str = "8") -> list[str]:
    """
    从文本中抽取高频关键词，便于检索与标签化
    """
    clean_text = _normalize_text(text)
    if not clean_text:
        return []

    limit = _parse_int(top_k, default=8, minimum=1, maximum=20)
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", clean_text.lower())
    if not tokens:
        return []

    filtered = [tok for tok in tokens if tok not in STOPWORDS and not tok.isdigit()]
    if not filtered:
        return []

    freq = Counter(filtered)
    return [word for word, _ in freq.most_common(limit)]


@tool(perm.PUBLIC_READ, "分类用户意图（问答/计划/写作/代码/闲聊等）", must=True)
def classify_intent(text: str) -> dict[str, str]:
    """
    使用规则对输入文本进行意图分类
    """
    clean_text = _normalize_text(text, max_len=2000)
    if not clean_text:
        return {"intent": "unknown", "confidence": "low", "reason": "输入为空"}

    rules = [
        ("coding", ["代码", "bug", "报错", "接口", "函数", "脚本", "python", "js", "修复"]),
        ("planning", ["计划", "方案", "步骤", "roadmap", "排期", "实现"]),
        ("writing", ["润色", "文案", "简介", "邮件", "翻译", "总结", "改写"]),
        ("qa", ["是什么", "为什么", "如何", "区别", "解释", "介绍", "怎么"]),
        ("chat", ["你好", "在吗", "谢谢", "哈哈", "聊聊", "随便"]),
    ]

    lower_text = clean_text.lower()
    scored = [(intent, sum(lower_text.count(k) for k in keywords)) for intent, keywords in rules]
    intent, best_score = max(scored, key=lambda x: x[1])

    if best_score <= 0:
        return {"intent": "general", "confidence": "low", "reason": "未匹配到显著关键词"}

    sorted_scores = sorted(scored, key=lambda x: x[1], reverse=True)
    runner_up = sorted_scores[1][1] if len(sorted_scores) > 1 else 0
    confidence = "high" if best_score >= runner_up + 2 else "medium"
    return {
        "intent": intent,
        "confidence": confidence,
        "reason": f"命中关键词得分 {best_score}",
    }


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
