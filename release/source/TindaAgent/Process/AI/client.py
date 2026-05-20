import os
import json
import html
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterator
from openai import OpenAI
try:
    from openai import BadRequestError
except ImportError:
    BadRequestError = None
from dotenv import load_dotenv
from TindaAgent.Tool import tool as tool_registry
from TindaAgent.Process.Architecture.paths import get_log_root
from TindaAgent.Process.Observability import audit_event

_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)
_THIS_FILE = str(Path(__file__).resolve())
_LLM_REQUEST_LOG = Path(os.getenv("TINDA_LLM_REQUEST_LOG", str(get_log_root() / "llm_request.jsonl")))
MAX_TOOL_STEPS_LIMIT = 900
_last_llm_request_row: dict[str, Any] | None = None
_TOOL_SKIP_LOCK = threading.RLock()
_TOOL_SKIP_REQUESTS: dict[str, dict[str, Any]] = {}


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_json(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _coerce_float(value: Any, default: float | None = None, *,
                  min_value: float | None = None, max_value: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except Exception:
        return default
    if min_value is not None and parsed < min_value:
        return default
    if max_value is not None and parsed > max_value:
        return default
    return parsed


def _coerce_int(value: Any, default: int | None = None, *,
                min_value: int | None = None, max_value: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except Exception:
        return default
    if min_value is not None and parsed < min_value:
        return default
    if max_value is not None and parsed > max_value:
        return default
    return parsed


def request_tool_skip(session_id: str, tool_call_id: str = "", call_id: str = "") -> bool:
    sid = str(session_id or "").strip()
    model_id = str(tool_call_id or "").strip()
    internal_id = str(call_id or "").strip()
    if not sid or (not model_id and not internal_id):
        return False
    key = model_id or internal_id
    with _TOOL_SKIP_LOCK:
        bucket = _TOOL_SKIP_REQUESTS.setdefault(sid, {})
        bucket[key] = {"tool_call_id": model_id, "call_id": internal_id, "ts": time.time()}
        if model_id and internal_id:
            bucket[internal_id] = bucket[key]
    try:
        tool_registry.skip_running_tool(internal_id or model_id)
    except Exception:
        pass
    return True


def _bind_tool_skip_alias(session_id: str, tool_call_id: str, call_id: str) -> None:
    sid = str(session_id or "").strip()
    model_id = str(tool_call_id or "").strip()
    internal_id = str(call_id or "").strip()
    if not sid or not model_id or not internal_id:
        return
    with _TOOL_SKIP_LOCK:
        bucket = _TOOL_SKIP_REQUESTS.get(sid, {})
        payload = bucket.get(model_id) or bucket.get(internal_id)
        if isinstance(payload, dict):
            payload["tool_call_id"] = model_id
            payload["call_id"] = internal_id
            bucket[model_id] = payload
            bucket[internal_id] = payload
    if _peek_tool_skip(sid, model_id, internal_id):
        try:
            tool_registry.skip_running_tool(internal_id)
        except Exception:
            pass


def _consume_tool_skip(session_id: str, tool_call_id: str = "", call_id: str = "") -> dict[str, Any] | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    keys = [str(tool_call_id or "").strip(), str(call_id or "").strip()]
    with _TOOL_SKIP_LOCK:
        bucket = _TOOL_SKIP_REQUESTS.get(sid, {})
        for key in keys:
            if key and key in bucket:
                payload = bucket.pop(key)
                for other in keys:
                    if other:
                        bucket.pop(other, None)
                if not bucket:
                    _TOOL_SKIP_REQUESTS.pop(sid, None)
                return payload
    return None


def _peek_tool_skip(session_id: str, tool_call_id: str = "", call_id: str = "") -> bool:
    sid = str(session_id or "").strip()
    if not sid:
        return False
    keys = {str(tool_call_id or "").strip(), str(call_id or "").strip()}
    with _TOOL_SKIP_LOCK:
        bucket = _TOOL_SKIP_REQUESTS.get(sid, {})
        return any(key and key in bucket for key in keys)


def _build_skipped_tool_result(name: str, call_id: str, model_id: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    payload = {
        "ok": False,
        "tool_name": str(name or "unknown"),
        "call_id": str(call_id or ""),
        "error": "用户已跳过该工具调用",
        "error_code": "user_skipped",
        "status": "skipped",
        "pending_confirmation": False,
    }
    raw = json.dumps(payload, ensure_ascii=False)
    step = {
        "agent_tool": str(name or "unknown"),
        "call_id": str(call_id or ""),
        "tool_call_id": str(model_id or ""),
        "arguments": args if isinstance(args, dict) else {},
        "result": dict(payload),
        "raw_result": raw,
    }
    return raw, step


def _tool_result_message(model_id: str, name: str, content: str) -> dict[str, Any]:
    """Build a tool role message compatible with OpenAI-shaped chat history."""
    message: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": str(model_id or ""),
        "content": str(content or ""),
    }
    if name:
        message["name"] = str(name)
    return message


def _extract_api_error(exc: Exception) -> str:
    """Extract the real error message from an OpenAI SDK exception."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", {})
        if isinstance(err, dict):
            msg = str(err.get("message", "") or "")
            if msg:
                return msg
        return str(body)
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            import json as _json
            data = _json.loads(resp.text) if hasattr(resp, "text") else _json.loads(resp.content)
            if isinstance(data, dict):
                err = data.get("error", {})
                if isinstance(err, dict):
                    msg = str(err.get("message", "") or "")
                    if msg:
                        return msg
                return str(data)
        except Exception:
            pass
    return str(exc)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _estimate_chars(value: Any) -> int:
    try:
        return len(json.dumps(_json_safe(value), ensure_ascii=False))
    except Exception:
        return len(str(value or ""))


_SDK_ONLY_REQUEST_KEYS = {
    "timeout",
    "extra_headers",
    "extra_query",
}


def _request_body_from_sdk_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe_payload = _json_safe(payload)
    if not isinstance(safe_payload, dict):
        return {}
    body = {k: v for k, v in safe_payload.items() if k != "extra_body" and k not in _SDK_ONLY_REQUEST_KEYS}
    extra_body = safe_payload.get("extra_body")
    if isinstance(extra_body, dict):
        body.update(extra_body)
    return body


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return _json_safe(usage) if isinstance(_json_safe(usage), dict) else {}
    for attr in ("model_dump", "dict"):
        fn = getattr(usage, attr, None)
        if callable(fn):
            try:
                data = fn()
                return data if isinstance(data, dict) else {}
            except Exception:
                pass
    data: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        val = getattr(usage, key, None)
        if val is not None:
            data[key] = val
    return _json_safe(data) if data else {}


def _apply_response_usage(row: dict[str, Any], usage: Any) -> dict[str, Any]:
    usage_dict = _usage_to_dict(usage)
    if not usage_dict:
        return row
    row["response_usage"] = usage_dict
    prompt_tokens = usage_dict.get("prompt_tokens")
    if isinstance(prompt_tokens, int):
        row["request_tokens"] = int(prompt_tokens)
        token_usage = row.get("token_usage") if isinstance(row.get("token_usage"), dict) else {}
        token_usage["total"] = int(prompt_tokens)
        token_usage["source"] = "api_usage"
        row["token_usage"] = token_usage
    total_tokens = usage_dict.get("total_tokens")
    if isinstance(total_tokens, int):
        row["total_tokens"] = int(total_tokens)
    completion_tokens = usage_dict.get("completion_tokens")
    if isinstance(completion_tokens, int):
        row["completion_tokens"] = int(completion_tokens)
    return row


def _append_llm_request_row(row: dict[str, Any]) -> None:
    _LLM_REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _LLM_REQUEST_LOG.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_llm_request(payload: dict[str, Any], *, source: str, stream: bool = False, response_usage: Any = None) -> dict[str, Any] | None:
    global _last_llm_request_row
    try:
        sdk_payload = _json_safe(payload)
        request_body = _request_body_from_sdk_payload(payload)
        messages = request_body.get("messages") if isinstance(request_body, dict) else []
        tools = request_body.get("tools") if isinstance(request_body, dict) else []
        try:
            from TindaAgent.Process.AI.tokenizer import estimate_request_token_usage
            token_usage = estimate_request_token_usage(request_body)
        except Exception:
            token_usage = {"total": 0, "messages": 0, "tools": 0, "tokenizer": {"engine": "unknown"}}
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": str(source),
            "stream": bool(stream),
            "model": str(request_body.get("model", "")) if isinstance(request_body, dict) else "",
            "message_count": len(messages) if isinstance(messages, list) else 0,
            "tool_count": len(tools) if isinstance(tools, list) else 0,
            "payload_chars": _estimate_chars(request_body),
            "request_tokens": int(token_usage.get("total", 0) or 0),
            "payload_tokens": int(token_usage.get("payload", 0) or 0),
            "token_usage": token_usage,
            "payload": request_body,
            "sdk_kwargs": sdk_payload,
        }
        _apply_response_usage(row, response_usage)
        _last_llm_request_row = row
        _append_llm_request_row(row)
        return row
    except Exception as e:
        audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.record_llm_request", _THIS_FILE,
                    f"record_llm_request_failed err={e}", extra={"ok": False, "error": str(e)})
        return None


def record_llm_response_usage(usage: Any) -> None:
    try:
        if _last_llm_request_row is None:
            return
        row = dict(_last_llm_request_row)
        _apply_response_usage(row, usage)
        if row is _last_llm_request_row or "response_usage" not in row:
            return
        row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        row["usage_update"] = True
        _append_llm_request_row(row)
    except Exception as e:
        audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.record_llm_response_usage", _THIS_FILE,
                    f"record_llm_response_usage_failed err={e}", extra={"ok": False, "error": str(e)})


_record_llm_request = record_llm_request


def _concat_reasoning(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or "")
            else:
                parts.append(str(item))
        return "".join(parts)
    if isinstance(raw, dict):
        return raw.get("text") or raw.get("content") or json.dumps(raw, ensure_ascii=False)
    return str(raw)


def _normalize_tool_call(*, call_id, call_type, function_name, function_arguments, fallback_id) -> dict:
    return {
        "id": call_id or fallback_id,
        "type": call_type or "function",
        "function": {
            "name": function_name or "",
            "arguments": function_arguments or "{}",
        },
    }


def _tool_call_preview(call: dict, *, fallback_id: str) -> dict:
    if not isinstance(call, dict):
        call = {}
    fn = call.get("function", {}) if isinstance(call.get("function"), dict) else {}
    name = str(fn.get("name", "") or "")
    args = _parse_args(fn.get("arguments", ""))
    model_id = str(call.get("id", "") or fallback_id)
    return {
        "agent_tool": name,
        "tool_call_id": model_id,
        "arguments": args,
    }


def _attach_reasoning(msg: dict, text: str) -> dict:
    if text:
        msg["reasoning_content"] = text
    return msg


def _build_done(reply, working_msgs, base_len, steps, tool_trace) -> dict:
    return {
        "reply": reply,
        "history_delta": working_msgs[base_len:],
        "tool_steps": steps,
        "tool_trace": tool_trace,
    }


_DSML_TOOL_CALLS_BLOCK_RE = re.compile(
    r"\s*<[^>]*(?:tool[_\-\u2581]?calls|toolcalls)[^>]*>.*?</[^>]*(?:tool[_\-\u2581]?calls|toolcalls)[^>]*>\s*",
    flags=re.DOTALL | re.IGNORECASE,
)
_DSML_INVOKE_BLOCK_RE = re.compile(
    r"\s*<[^>]*invoke[^>]*name\s*=\s*(['\"])(.*?)\1[^>]*>.*?</[^>]*invoke[^>]*>\s*",
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


def _safe_stream_emit_end(content: str) -> int:
    """Hold a short partial tag tail so split DSML tags never leak as deltas."""
    text = str(content or "")
    if not text:
        return 0
    start = _find_tool_protocol_start(text)
    if start >= 0:
        return start
    last_lt = text.rfind("<")
    last_gt = text.rfind(">")
    if last_lt > last_gt and len(text) - last_lt <= 240:
        return last_lt
    return len(text)


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
    """Return True when assistant text contains known tool-call protocol residue."""
    text = str(content or "")
    if not text:
        return False
    if not _has_tool_protocol_marker(text):
        return False
    if _DSML_TOOL_CALLS_BLOCK_RE.search(text) or _DSML_INVOKE_BLOCK_RE.search(text):
        return True
    return "invoke" in text.lower()


def _truncate_from_protocol_start(content: str) -> str:
    text = str(content or "")
    start = _find_tool_protocol_start(text)
    if start >= 0:
        return text[:start]
    return text


def strip_tool_protocol_artifacts(content: str) -> str:
    """
    Remove known DeepSeek/DSML tool-call protocol blocks from user-visible text.

    Native tool_calls stay authoritative. This function only prevents fallback
    protocol text from leaking into chat bubbles, logs and session records.
    """
    text = str(content or "")
    if not text:
        return ""
    if not _has_tool_protocol_marker(text):
        return text
    cleaned = _DSML_TOOL_CALLS_BLOCK_RE.sub("\n", text)
    cleaned = _DSML_INVOKE_BLOCK_RE.sub("\n", cleaned)
    cleaned = _DSML_TOOL_CALLS_TAG_RE.sub("\n", cleaned)
    if _find_tool_protocol_start(cleaned) >= 0:
        cleaned = _truncate_from_protocol_start(cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _detect_dsml_tool_calls(content: str, *, prefix: str) -> list[dict]:
    """从文本中提取 DSML 格式的工具调用（DeepSeek 偶发行为兼容）"""
    text = content or ""
    lower = text.lower()
    if "invoke" not in lower or not _has_tool_protocol_marker(text):
        return []

    blocks = re.findall(
        r"<[^>]*invoke[^>]*name\s*=\s*(['\"])(.*?)\1[^>]*>(.*?)</[^>]*invoke[^>]*>",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not blocks:
        return []

    result = []
    for idx, (_quote, name, body) in enumerate(blocks):
        params = {}
        for _p_quote, p_name, p_value in re.findall(
            r"<[^>]*parameter[^>]*name\s*=\s*(['\"])(.*?)\1[^>]*>(.*?)</[^>]*parameter[^>]*>",
            body,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            params[p_name.strip()] = p_value.strip()

        fn = html.unescape(name).strip()
        if not fn:
            continue
        if tool_registry.find_tool(fn) is None:
            continue

        result.append({
            "id": f"{prefix}_{idx}",
            "type": "function",
            "function": {
                "name": fn,
                "arguments": json.dumps(
                    {html.unescape(str(k)): html.unescape(str(v)) for k, v in params.items()},
                    ensure_ascii=False,
                ),
            },
        })
    return result


def _build_tool_limit_fallback(tool_trace: list[dict]) -> str:
    if not tool_trace:
        return "Maximum tool call iterations reached. Provide your best answer based on the results obtained so far."
    lines = ["Maximum tool call iterations reached. Summarize the results and provide a final answer."]
    for idx, step in enumerate(tool_trace[-4:], start=1):
        name = step.get("agent_tool", "unknown_tool")
        result = step.get("result")
        text = ""
        if isinstance(result, dict):
            if result.get("ok") is False:
                text = str(result.get("error", ""))
            elif result.get("result") is not None:
                text = str(result.get("result"))
            elif result.get("stdout"):
                text = str(result.get("stdout"))
            else:
                text = "completed"
        elif result is not None:
            text = str(result)
        else:
            text = str(step.get("raw_result", ""))
        text = text.replace("\n", " ").strip()
        if len(text) > 140:
            text = text[:140] + "..."
        lines.append(f"{idx}. {name}: {text or 'completed'}")
    return "\n".join(lines)


def _build_tool_limit_system_message(tool_trace: list[dict]) -> dict[str, str]:
    return {
        "role": "system",
        "content": (
            "The configured maximum tool-call iterations has been reached. "
            "Do not call any more tools. Summarize the actual tool results already available "
            "and provide the best final answer. If the task cannot be completed with the "
            "available results, state the limitation clearly.\n\n"
            + _build_tool_limit_fallback(tool_trace)
        ),
    }


def _result_has_pending_confirmation(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("pending_confirmation") is True:
        return True
    inner = payload.get("result")
    return isinstance(inner, dict) and inner.get("pending_confirmation") is True


def _tool_failed(step: dict) -> bool:
    """工具步骤是否执行失败（外层或内层 ok 为 False）。"""
    r = step.get("result", {})
    if isinstance(r, dict):
        if r.get("ok") is False:
            return True
        inner = r.get("result")
        if isinstance(inner, dict) and inner.get("ok") is False:
            return True
    return False


def _tool_skipped(step: dict) -> bool:
    r = step.get("result", {})
    if isinstance(r, dict):
        if r.get("error_code") == "user_skipped" or r.get("status") == "skipped":
            return True
        inner = r.get("result")
        if isinstance(inner, dict):
            return inner.get("error_code") == "user_skipped" or inner.get("status") == "skipped"
    return False


def _tool_error(step: dict) -> str:
    """提取工具步骤的错误信息。"""
    r = step.get("result", {})
    if isinstance(r, dict):
        inner = r.get("result")
        if isinstance(inner, dict):
            return str(inner.get("error", "") or "")
        return str(r.get("error", "") or "")
    return ""


def _trace_has_pending_confirmation(trace: list[dict] | None) -> bool:
    if not isinstance(trace, list):
        return False
    for step in trace:
        if not isinstance(step, dict):
            continue
        if _result_has_pending_confirmation(step.get("result")):
            return True
        raw = step.get("raw_result")
        if isinstance(raw, str) and raw.strip():
            parsed = _parse_json(raw)
            if _result_has_pending_confirmation(parsed):
                return True
    return False


class LLMClient:
    """
    用处：封装 LLM 调用，支持工具循环与自动纠正假调用。
    """

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None,
                 request_params: dict[str, Any] | None = None) -> None:
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        if not self.api_key:
            raise ValueError("缺少 DEEPSEEK_API_KEY，请在 .env 中配置")
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self._temperature = 0.7
        self._top_p: float | None = None
        self._presence_penalty: float | None = None
        self._frequency_penalty: float | None = None
        self._max_tokens: int | None = None
        self._seed: int | None = None
        self._timeout = 25
        self._tool_choice = "auto"
        self._max_tool_steps = 6
        self._reasoning_effort = "max"
        self._thinking = {"type": "enabled"}
        self._extra_body = {"thinking": self._thinking}
        if request_params:
            self.configure_request_params(request_params)

    def configure_request_params(self, params: dict[str, Any] | None = None) -> None:
        """Apply provider-level request defaults to this client."""
        if not isinstance(params, dict):
            return
        parsed_temperature = _coerce_float(
            params.get("temperature"),
            self._temperature,
            min_value=0.0,
            max_value=2.0,
        )
        if parsed_temperature is not None:
            self._temperature = parsed_temperature
        self._top_p = _coerce_float(params.get("top_p"), None, min_value=0.0, max_value=1.0)
        self._presence_penalty = _coerce_float(params.get("presence_penalty"), None,
                                               min_value=-2.0, max_value=2.0)
        self._frequency_penalty = _coerce_float(params.get("frequency_penalty"), None,
                                                min_value=-2.0, max_value=2.0)
        self._max_tokens = _coerce_int(params.get("max_tokens"), None, min_value=1, max_value=200000)
        self._seed = _coerce_int(params.get("seed"), None, min_value=0, max_value=4294967295)
        self._timeout = _coerce_int(params.get("timeout"), self._timeout, min_value=1, max_value=600) or self._timeout
        self._max_tool_steps = _coerce_int(params.get("max_tool_steps"), self._max_tool_steps,
                                           min_value=1, max_value=MAX_TOOL_STEPS_LIMIT) or self._max_tool_steps

        tool_choice = str(params.get("tool_choice") or self._tool_choice or "auto").strip().lower()
        self._tool_choice = tool_choice if tool_choice in {"auto", "none", "required"} else "auto"

        reasoning = str(params.get("reasoning_effort") or "").strip().lower()
        self._reasoning_effort = reasoning if reasoning in {"low", "medium", "high", "max"} else ""

        extra_body = params.get("extra_body") if isinstance(params.get("extra_body"), dict) else {}
        self._extra_body = json.loads(json.dumps(extra_body, ensure_ascii=False)) if extra_body else {}
        thinking_enabled = bool(params.get("thinking_enabled", bool(self._extra_body.get("thinking"))))
        if thinking_enabled:
            self._thinking = {"type": "enabled"}
            self._extra_body["thinking"] = self._thinking
        else:
            self._thinking = {}
            self._extra_body.pop("thinking", None)

    def _effective_temperature(self, temperature: float | None = None) -> float:
        value = _coerce_float(temperature, None, min_value=0.0, max_value=2.0)
        return float(self._temperature if value is None else value)

    def _effective_max_tool_steps(self, value: int | None = None) -> int:
        parsed = _coerce_int(value, None, min_value=1, max_value=MAX_TOOL_STEPS_LIMIT)
        return int(self._max_tool_steps if parsed is None else parsed)

    def _tool_choice_for_request(self, *, force_finalize: bool = False) -> str:
        if force_finalize:
            return "none"
        return self._tool_choice if self._tool_choice in {"auto", "none", "required"} else "auto"

    def _apply_request_params(self, payload: dict[str, Any], *, temperature: float | None = None) -> dict[str, Any]:
        payload["temperature"] = self._effective_temperature(temperature)
        if self._top_p is not None:
            payload.setdefault("top_p", self._top_p)
        if self._presence_penalty is not None:
            payload.setdefault("presence_penalty", self._presence_penalty)
        if self._frequency_penalty is not None:
            payload.setdefault("frequency_penalty", self._frequency_penalty)
        if self._max_tokens is not None:
            payload.setdefault("max_tokens", self._max_tokens)
        if self._seed is not None:
            payload.setdefault("seed", self._seed)
        if self._timeout:
            payload.setdefault("timeout", int(self._timeout))
        if self._reasoning_effort:
            payload.setdefault("reasoning_effort", self._reasoning_effort)
        if self._extra_body:
            payload.setdefault("extra_body", dict(self._extra_body))
        return payload

    def chat(self, messages: list[dict], temperature: float | None = None) -> str:
        effective_temperature = self._effective_temperature(temperature)
        audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat", _THIS_FILE, "llm_chat_start",
                     extra={"model": self.model, "messages_count": len(messages), "temperature": effective_temperature})
        try:
            payload = self._apply_request_params({
                "model": self.model,
                "messages": messages,
            }, temperature=temperature)
            _record_llm_request(payload, source="LLMClient.chat", stream=False)
            resp = self._client.chat.completions.create(**payload)
            record_llm_response_usage(getattr(resp, "usage", None))
            text = resp.choices[0].message.content
            audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat", _THIS_FILE, "llm_chat_done",
                         extra={"model": self.model, "ok": True, "reply_len": len(text or "")})
            return text
        except Exception as e:
            audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat", _THIS_FILE, f"llm_chat_failed err={e}",
                         extra={"model": self.model, "ok": False, "error": str(e)})
            raise

    def _run_tools(self, tool_calls: list[dict], user_perm: int,
                   msgs: list[dict], trace: list[dict], func: str,
                   session_id: str = "") -> tuple[list[dict], bool]:
        """执行工具调用，写入消息历史与轨迹。"""
        steps = []
        has_pending_confirmation = False
        for call in tool_calls:
            name = call["function"]["name"]
            args = _parse_args(call["function"].get("arguments", ""))
            model_id = call.get("id", "")
            event_id = audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_start tool={name}",
                                    extra={"tool_name": name, "has_arguments": bool(args), "model_id": model_id})
            call_id = f"tc_{event_id:010d}"
            _bind_tool_skip_alias(session_id, model_id, call_id)
            if _consume_tool_skip(session_id, model_id, call_id):
                raw, skipped_step = _build_skipped_tool_result(name, call_id, model_id, args)
                trace.append(skipped_step)
                steps.append(skipped_step)
                msgs.append(_tool_result_message(model_id, name, raw))
                continue
            raw = tool_registry.run_agent_tool(name, user_perm, args, call_id=call_id)
            parsed = _parse_json(raw)

            user_safe = parsed
            model_content = raw
            if isinstance(parsed, dict):
                parsed.setdefault("call_id", call_id)
                user_safe.setdefault("call_id", call_id)
                if _result_has_pending_confirmation(parsed):
                    has_pending_confirmation = True
                if parsed.get("error_code") == "permission_denied" and parsed.get("expose_to_user") is False:
                    model_view = dict(parsed)
                    model_view["error"] = parsed.get("llm_message") or parsed.get("error") or "permission denied"
                    model_content = json.dumps(model_view, ensure_ascii=False)

                    safe = dict(parsed)
                    safe["error"] = parsed.get("user_message") or "该工具当前不可用，请尝试其它方式。"
                    for k in ("llm_message", "missing_perm_labels", "required_perm_labels",
                              "required_perm_bits", "user_perm", "user_perm_labels"):
                        safe.pop(k, None)
                    user_safe = safe
            if _result_has_pending_confirmation(user_safe):
                has_pending_confirmation = True

            trace.append({"agent_tool": name, "call_id": call_id, "tool_call_id": model_id,
                          "arguments": args, "result": user_safe, "raw_result": raw})
            steps.append({"agent_tool": name, "call_id": call_id, "tool_call_id": model_id,
                          "arguments": args, "result": user_safe, "raw_result": raw})
            msgs.append(_tool_result_message(model_id, name, model_content))
            audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_done tool={name}",
                         extra={"tool_name": name, "ok": True, "call_id": call_id})
        return steps, has_pending_confirmation

    def _run_tools_iter(
            self,
            tool_calls: list[dict],
            user_perm: int,
            msgs: list[dict],
            trace: list[dict],
            func: str,
            *,
            heartbeat_interval: float = 1.0,
            session_id: str = "",
    ) -> Iterator[dict]:
        """Run blocking tools in a worker thread and emit heartbeats while waiting."""
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        current: dict[str, Any] = {"name": "", "tool_call_id": "", "call_id": "", "arguments": {}}
        abandoned: set[tuple[str, str]] = set()
        state_lock = threading.RLock()
        stop_worker = threading.Event()

        def worker() -> None:
            try:
                steps = []
                has_pending_confirmation = False
                for call in tool_calls:
                    if stop_worker.is_set():
                        break
                    name = call["function"]["name"]
                    args = _parse_args(call["function"].get("arguments", ""))
                    model_id = call.get("id", "")
                    with state_lock:
                        current.update({"name": name, "tool_call_id": model_id, "call_id": "", "arguments": args})
                    event_id = audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_start tool={name}",
                                            extra={"tool_name": name, "has_arguments": bool(args), "model_id": model_id})
                    call_id = f"tc_{event_id:010d}"
                    with state_lock:
                        current["call_id"] = call_id
                    _bind_tool_skip_alias(session_id, model_id, call_id)
                    if _consume_tool_skip(session_id, model_id, call_id):
                        raw, skipped_step = _build_skipped_tool_result(name, call_id, model_id, args)
                        trace.append(skipped_step)
                        steps.append(skipped_step)
                        msgs.append(_tool_result_message(model_id, name, raw))
                        audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_skipped_before_run tool={name}",
                                    extra={"tool_name": name, "call_id": call_id, "model_id": model_id})
                        continue
                    raw = tool_registry.run_agent_tool(name, user_perm, args, call_id=call_id)
                    with state_lock:
                        is_abandoned = (model_id, call_id) in abandoned
                    if is_abandoned:
                        audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_result_abandoned_after_skip tool={name}",
                                    extra={"tool_name": name, "call_id": call_id, "model_id": model_id})
                        break
                    if _consume_tool_skip(session_id, model_id, call_id):
                        raw, skipped_step = _build_skipped_tool_result(name, call_id, model_id, args)
                        trace.append(skipped_step)
                        steps.append(skipped_step)
                        msgs.append(_tool_result_message(model_id, name, raw))
                        audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_skipped_after_run tool={name}",
                                    extra={"tool_name": name, "call_id": call_id, "model_id": model_id})
                        continue
                    parsed = _parse_json(raw)

                    user_safe = parsed
                    model_content = raw
                    if isinstance(parsed, dict):
                        parsed.setdefault("call_id", call_id)
                        user_safe.setdefault("call_id", call_id)
                        if _result_has_pending_confirmation(parsed):
                            has_pending_confirmation = True
                        if parsed.get("error_code") == "permission_denied" and parsed.get("expose_to_user") is False:
                            model_view = dict(parsed)
                            model_view["error"] = parsed.get("llm_message") or parsed.get("error") or "permission denied"
                            model_content = json.dumps(model_view, ensure_ascii=False)

                            safe = dict(parsed)
                            safe["error"] = parsed.get("user_message") or "该工具当前不可用，请尝试其它方式。"
                            for k in ("llm_message", "missing_perm_labels", "required_perm_labels",
                                      "required_perm_bits", "user_perm", "user_perm_labels"):
                                safe.pop(k, None)
                            user_safe = safe
                    if _result_has_pending_confirmation(user_safe):
                        has_pending_confirmation = True

                    trace.append({"agent_tool": name, "call_id": call_id, "tool_call_id": model_id,
                                  "arguments": args, "result": user_safe, "raw_result": raw})
                    steps.append({"agent_tool": name, "call_id": call_id, "tool_call_id": model_id,
                                  "arguments": args, "result": user_safe, "raw_result": raw})
                    msgs.append(_tool_result_message(model_id, name, model_content))
                    audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_done tool={name}",
                                extra={"tool_name": name, "ok": True, "call_id": call_id})
                with state_lock:
                    current.clear()
                if not stop_worker.is_set():
                    q.put({"type": "result", "steps": steps, "pending": has_pending_confirmation})
            except BaseException as exc:
                with state_lock:
                    current.clear()
                if not stop_worker.is_set():
                    q.put({"type": "error", "error": exc})

        thread = threading.Thread(target=worker, daemon=True, name="tinda-llm-tool-runner")
        thread.start()
        started = time.monotonic()
        while True:
            try:
                item = q.get(timeout=max(0.2, float(heartbeat_interval)))
            except queue.Empty:
                with state_lock:
                    cur = dict(current)
                cur["skippable"] = bool(
                    cur.get("tool_call_id")
                    or cur.get("call_id")
                    or cur.get("name")
                )
                cur["skip_requested"] = _peek_tool_skip(
                    session_id,
                    str(cur.get("tool_call_id", "") or ""),
                    str(cur.get("call_id", "") or ""),
                )
                if cur["skip_requested"]:
                    name = str(cur.get("name", "") or "unknown")
                    model_id = str(cur.get("tool_call_id", "") or "")
                    call_id = str(cur.get("call_id", "") or "")
                    args = cur.get("arguments") if isinstance(cur.get("arguments"), dict) else {}
                    if not call_id:
                        continue
                    _consume_tool_skip(session_id, model_id, call_id)
                    raw, skipped_step = _build_skipped_tool_result(name, call_id, model_id, args)
                    with state_lock:
                        abandoned.add((model_id, call_id))
                    stop_worker.set()
                    try:
                        tool_registry.skip_running_tool(call_id)
                    except Exception:
                        pass
                    trace.append(skipped_step)
                    msgs.append(_tool_result_message(model_id, name, raw))
                    audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_skipped_during_run tool={name}",
                                extra={"tool_name": name, "call_id": call_id, "model_id": model_id})
                    yield {
                        "type": "tool_result",
                        "steps": [skipped_step],
                        "pending": False,
                    }
                    return
                yield {
                    "type": "tool_heartbeat",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "tool": cur,
                }
                continue
            if item.get("type") == "error":
                raise item["error"]
            yield {
                "type": "tool_result",
                "steps": item.get("steps", []),
                "pending": bool(item.get("pending", False)),
            }
            return

    def _process_tool_loop(self, messages: list[dict], user_perm: int, temperature: float | None,
                           max_tool_steps: int | None, stream: bool, base_len: int,
                           func: str, *, session_id: str = "") -> tuple[str, list[dict], int, list[dict]]:
        """核心工具调用循环（同步与流式共用）。"""
        msgs = [m.copy() for m in messages]
        tools = tool_registry.build_agent_tool_schemas(user_perm)
        steps = 0
        trace = []
        max_tool_steps = self._effective_max_tool_steps(max_tool_steps)

        while True:
            payload = self._apply_request_params({
                "model": self.model,
                "tools": tools,
                "tool_choice": self._tool_choice_for_request(force_finalize=False),
                "messages": msgs,
            }, temperature=temperature)
            _record_llm_request(payload, source=func, stream=stream)
            resp = self._client.chat.completions.create(**payload)
            record_llm_response_usage(getattr(resp, "usage", None))
            msg = resp.choices[0].message
            rc = _concat_reasoning(getattr(msg, "reasoning_content", None))
            content = msg.content or ""

            calls = [_normalize_tool_call(
                call_id=getattr(c, "id", None), call_type=getattr(c, "type", None),
                function_name=getattr(c.function, "name", ""),
                function_arguments=getattr(c.function, "arguments", None),
                fallback_id=f"call_{steps}_{i}")
                for i, c in enumerate(msg.tool_calls or [])]
            content_for_history = strip_tool_protocol_artifacts(content)
            if not calls:
                calls = _detect_dsml_tool_calls(content, prefix=f"dsml_{steps}")
                if calls:
                    content_for_history = strip_tool_protocol_artifacts(content)

            if not calls:
                clean_content = strip_tool_protocol_artifacts(content)
                msg_out = _attach_reasoning({"role": "assistant", "content": clean_content}, rc)
                msgs.append(msg_out)
                return clean_content, msgs[base_len:], steps, trace

            if steps >= max_tool_steps:
                msgs.append(_build_tool_limit_system_message(trace))
                final_payload = self._apply_request_params({
                    "model": self.model,
                    "tools": tools,
                    "tool_choice": self._tool_choice_for_request(force_finalize=True),
                    "messages": msgs,
                }, temperature=temperature)
                _record_llm_request(final_payload, source=f"{func}.tool_limit_finalize", stream=stream)
                final_resp = self._client.chat.completions.create(**final_payload)
                record_llm_response_usage(getattr(final_resp, "usage", None))
                final_msg = final_resp.choices[0].message
                final_rc = _concat_reasoning(getattr(final_msg, "reasoning_content", None))
                final_content = strip_tool_protocol_artifacts(final_msg.content or "")
                if not final_content:
                    final_content = _build_tool_limit_fallback(trace)
                msg_out = _attach_reasoning({"role": "assistant", "content": final_content}, final_rc)
                msgs.append(msg_out)
                return final_content, msgs[base_len:], steps, trace

            msg_out = _attach_reasoning(
                {"role": "assistant", "content": content_for_history, "tool_calls": calls}, rc)
            msgs.append(msg_out)
            step_trace, has_pending_confirmation = self._run_tools(
                calls,
                user_perm,
                msgs,
                trace,
                func,
                session_id=session_id,
            )

            steps += 1
            if any(_tool_skipped(s) for s in step_trace):
                continue
            # 所有工具均失败 → 继续循环，让 LLM 根据 tool role 的错误信息自行决定下一步
            if step_trace and all(_tool_failed(s) for s in step_trace):
                continue
            if has_pending_confirmation:
                pending_reply = content_for_history if str(content_for_history or "").strip() else ""
                return pending_reply, msgs[base_len:], steps, trace

    def chat_with_tools(self, messages: list[dict], user_perm: int,
                        temperature: float | None = None, max_tool_steps: int | None = None,
                        session_id: str = "") -> dict:
        """支持工具调用的对话，自动纠正模型假调用。"""
        base_len = len(messages)
        reply, delta, steps, trace = self._process_tool_loop(
            messages, user_perm, temperature, max_tool_steps, False, base_len, "LLMClient.chat_with_tools",
            session_id=session_id)
        audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat_with_tools", _THIS_FILE, "chat_with_tools_done",
                     extra={"model": self.model, "tool_steps": steps, "tool_trace_len": len(trace),
                            "reply_len": len(reply)})
        return {"reply": reply, "history_delta": delta, "tool_steps": steps, "tool_trace": trace}

    def stream_chat_with_tools(self, messages: list[dict], user_perm: int,
                               temperature: float | None = None, max_tool_steps: int | None = None,
                               session_id: str = "") -> Iterator[dict]:
        """流式对话（支持工具调用循环），自动纠正模型假调用。"""
        msgs = [m.copy() for m in messages]
        tools = tool_registry.build_agent_tool_schemas(user_perm)
        base_len = len(msgs)
        steps = 0
        trace = []
        max_tool_steps = self._effective_max_tool_steps(max_tool_steps)
        audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.stream_chat_with_tools", _THIS_FILE,
                     "stream_start", extra={"model": self.model, "messages_count": len(messages),
                                            "user_perm": user_perm, "max_tool_steps": max_tool_steps})

        while True:
            try:
                payload = self._apply_request_params({
                    "model": self.model,
                    "tools": tools,
                    "tool_choice": self._tool_choice_for_request(force_finalize=False),
                    "messages": msgs,
                    "stream": True,
                }, temperature=temperature)
                _record_llm_request(payload, source="LLMClient.stream_chat_with_tools", stream=True)
                stream = self._client.chat.completions.create(**payload)
            except Exception as e:
                detail = _extract_api_error(e)
                audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.stream_chat_with_tools", _THIS_FILE,
                             f"stream_fail model={self.model} err={detail}", extra={"model": self.model,
                             "ok": False, "error": detail, "messages_count": len(msgs), "steps": steps})
                raise RuntimeError(detail) from e
            content_parts = []
            reasoning_parts = []
            tool_calls_map = {}
            stream_visible_sent = 0
            stream_protocol_started = False
            response_usage = None

            for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    response_usage = chunk_usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue

                part = getattr(delta, "content", None)
                if part:
                    content_parts.append(part)
                    content_so_far = "".join(content_parts)
                    if not stream_protocol_started:
                        emit_end = _safe_stream_emit_end(content_so_far)
                        if emit_end > stream_visible_sent:
                            yield {"type": "delta", "content": content_so_far[stream_visible_sent:emit_end]}
                            stream_visible_sent = emit_end
                        if _find_tool_protocol_start(content_so_far) >= 0:
                            stream_protocol_started = True
                rp = _concat_reasoning(getattr(delta, "reasoning_content", None))
                if rp:
                    reasoning_parts.append(rp)
                    yield {"type": "reasoning_delta", "content": rp}

                for tc in (getattr(delta, "tool_calls", None) or []):
                    idx = getattr(tc, "index", 0)
                    entry = tool_calls_map.setdefault(idx, {"id": "", "type": "function",
                                                            "function": {"name": "", "arguments": ""}})
                    if getattr(tc, "id", None):
                        entry["id"] = tc.id
                    if getattr(tc, "type", None):
                        entry["type"] = tc.type
                    fn = getattr(tc, "function", None)
                    if fn:
                        if getattr(fn, "name", None):
                            entry["function"]["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            entry["function"]["arguments"] += fn.arguments

            content = "".join(content_parts)
            rc = "".join(reasoning_parts)
            if response_usage is not None:
                record_llm_response_usage(response_usage)

            calls = []
            for key in sorted(tool_calls_map):
                c = tool_calls_map[key]
                calls.append(_normalize_tool_call(
                    call_id=c.get("id"), call_type=c.get("type"),
                    function_name=c.get("function", {}).get("name"),
                    function_arguments=c.get("function", {}).get("arguments"),
                    fallback_id=f"call_{steps}_{key}"))
            content_for_history = strip_tool_protocol_artifacts(content)
            if not calls:
                calls = _detect_dsml_tool_calls(content, prefix=f"dsml_{steps}")
                if calls:
                    content_for_history = strip_tool_protocol_artifacts(content)

            if not calls:
                clean_content = strip_tool_protocol_artifacts(content)
                if stream_visible_sent < len(content):
                    if has_tool_protocol_artifacts(content) or stream_protocol_started:
                        yield {"type": "replace_segment", "content": clean_content}
                    else:
                        yield {"type": "delta", "content": content[stream_visible_sent:]}
                msg_out = _attach_reasoning({"role": "assistant", "content": clean_content}, rc)
                msgs.append(msg_out)
                audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.stream_chat_with_tools", _THIS_FILE,
                             "stream_done", extra={"model": self.model, "tool_steps": steps,
                                                   "tool_trace_len": len(trace), "reply_len": len(clean_content)})
                yield {"type": "done", **(_build_done(clean_content, msgs, base_len, steps, trace))}
                return

            if steps >= max_tool_steps:
                if stream_visible_sent < len(content):
                    yield {"type": "replace_segment", "content": content_for_history}
                msgs.append(_build_tool_limit_system_message(trace))
                final_payload = self._apply_request_params({
                    "model": self.model,
                    "tools": tools,
                    "tool_choice": self._tool_choice_for_request(force_finalize=True),
                    "messages": msgs,
                }, temperature=temperature)
                _record_llm_request(final_payload, source="LLMClient.stream_chat_with_tools.tool_limit_finalize", stream=False)
                final_resp = self._client.chat.completions.create(**final_payload)
                record_llm_response_usage(getattr(final_resp, "usage", None))
                final_msg = final_resp.choices[0].message
                final_rc = _concat_reasoning(getattr(final_msg, "reasoning_content", None))
                final_content = strip_tool_protocol_artifacts(final_msg.content or "")
                if not final_content:
                    final_content = _build_tool_limit_fallback(trace)
                msg_out = _attach_reasoning({"role": "assistant", "content": final_content}, final_rc)
                msgs.append(msg_out)
                yield {"type": "delta", "content": final_content}
                yield {"type": "done", **(_build_done(final_content, msgs, base_len, steps, trace))}
                audit_event(
                    "SYSTEM_EXECUTE",
                    "ai",
                    "LLMClient.stream_chat_with_tools",
                    _THIS_FILE,
                    "stream_reach_max_steps_before_tool_execution",
                    extra={"model": self.model, "tool_steps": steps, "tool_trace_len": len(trace)},
                )
                return

            yield {"type": "replace_segment", "content": content_for_history}
            yield {
                "type": "tool_call_start",
                "calls": [
                    _tool_call_preview(call, fallback_id=f"call_{steps}_{idx}")
                    for idx, call in enumerate(calls)
                ],
            }
            msg_out = _attach_reasoning(
                {"role": "assistant", "content": content_for_history, "tool_calls": calls}, rc)
            msgs.append(msg_out)
            step_trace = []
            has_pending_confirmation = False
            for tool_event in self._run_tools_iter(
                    calls,
                    user_perm,
                    msgs,
                    trace,
                    "LLMClient.stream_chat_with_tools",
                    session_id=session_id,
            ):
                if tool_event.get("type") == "tool_heartbeat":
                    yield {
                        "type": "tool_heartbeat",
                        "elapsed_ms": int(tool_event.get("elapsed_ms", 0) or 0),
                        "tool": tool_event.get("tool", {}),
                    }
                    continue
                if tool_event.get("type") == "tool_result":
                    step_trace = tool_event.get("steps", [])
                    has_pending_confirmation = bool(tool_event.get("pending", False))
            if step_trace:
                yield {"type": "tool_step", "trace": step_trace}

            steps += 1
            if any(_tool_skipped(s) for s in step_trace):
                audit_event(
                    "SYSTEM_EXECUTE",
                    "ai",
                    "LLMClient.stream_chat_with_tools",
                    _THIS_FILE,
                    "stream_tool_skipped",
                    extra={"model": self.model, "tool_steps": steps, "tool_trace_len": len(trace)},
                )
                continue
            if step_trace and all(_tool_failed(s) for s in step_trace):
                continue
            if has_pending_confirmation:
                pending_reply = content_for_history if str(content_for_history or "").strip() else ""
                yield {"type": "done", **(_build_done(pending_reply, msgs, base_len, steps, trace))}
                audit_event(
                    "SYSTEM_EXECUTE",
                    "ai",
                    "LLMClient.stream_chat_with_tools",
                    _THIS_FILE,
                    "stream_paused_pending_confirmation",
                    extra={"model": self.model, "tool_steps": steps, "tool_trace_len": len(trace)},
                )
                return
