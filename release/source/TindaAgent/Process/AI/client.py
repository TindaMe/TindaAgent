import os
import json
import re
import time
from pathlib import Path
from typing import Any, Iterator
from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError, RateLimitError
from dotenv import load_dotenv
from TindaAgent.Tool import tool as tool_registry
from TindaAgent.Process.Observability import audit_event

_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)
_THIS_FILE = str(Path(__file__).resolve())
_LLM_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _is_retryable_llm_error(err: Exception) -> bool:
    if isinstance(err, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    if isinstance(err, APIStatusError):
        try:
            status = int(getattr(err, "status_code", 0) or 0)
        except Exception:
            status = 0
        return status in _LLM_RETRYABLE_STATUS
    return False


def _format_llm_error(err: Exception) -> tuple[str, str]:
    """
    返回 (error_code, user_message)
    """
    if isinstance(err, APIConnectionError):
        return "upstream_connection_error", "模型服务连接失败，请稍后重试。"
    if isinstance(err, APITimeoutError):
        return "upstream_timeout", "模型服务响应超时，请稍后重试。"
    if isinstance(err, RateLimitError):
        return "upstream_rate_limited", "模型服务限流，请稍后重试。"
    if isinstance(err, APIStatusError):
        try:
            status = int(getattr(err, "status_code", 0) or 0)
        except Exception:
            status = 0
        if status == 401:
            return "upstream_auth_failed", "模型服务鉴权失败，请检查 API Key。"
        if status == 403:
            return "upstream_forbidden", "模型服务拒绝访问，请检查账号权限。"
        if status == 404:
            return "upstream_model_not_found", "模型配置不存在，请检查模型名称。"
        if status == 429:
            return "upstream_rate_limited", "模型服务限流，请稍后重试。"
        if status in {500, 502, 503, 504}:
            return "upstream_server_error", "模型服务暂时不可用，请稍后重试。"
        return "upstream_http_error", f"模型服务返回异常状态码：{status}。"
    return "llm_runtime_error", f"模型调用失败：{type(err).__name__}。"


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


def _trace_has_pending_confirmation(trace: list[dict]) -> bool:
    for step in trace:
        r = step.get("result")
        if isinstance(r, dict):
            inner = r.get("result")
            if isinstance(inner, dict) and inner.get("pending_confirmation") is True:
                return True
    return False


def _detect_dsml_tool_calls(content: str, *, prefix: str) -> list[dict]:
    """从文本中提取 DSML 格式的工具调用（DeepSeek 偶发行为兼容）"""
    text = content or ""
    lower = text.lower()
    if "invoke" not in lower:
        return []

    blocks = re.findall(
        r'<[^>]*invoke[^>]*name="([^"]+)"[^>]*>(.*?)</[^>]*invoke>',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not blocks:
        return []

    result = []
    for idx, (name, body) in enumerate(blocks):
        params = {}
        for p_name, p_value in re.findall(
            r'<[^>]*parameter[^>]*name="([^"]+)"[^>]*>(.*?)</[^>]*parameter>',
            body,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            params[p_name.strip()] = p_value.strip()

        fn = name.strip()
        if not fn:
            continue

        if fn == "call_backend_tool":
            args = {"tool_name": params.get("tool_name", "")}
            raw_args = params.get("args", "")
            if raw_args:
                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, list):
                        args["args"] = parsed
                except Exception:
                    pass
            raw_kwargs = params.get("kwargs", "")
            if raw_kwargs:
                try:
                    parsed = json.loads(raw_kwargs)
                    if isinstance(parsed, dict):
                        args["kwargs"] = parsed
                except Exception:
                    pass
            result.append({
                "id": f"{prefix}_{idx}",
                "type": "function",
                "function": {"name": "call_backend_tool", "arguments": json.dumps(args, ensure_ascii=False)},
            })
        else:
            result.append({
                "id": f"{prefix}_{idx}",
                "type": "function",
                "function": {"name": fn, "arguments": json.dumps(params, ensure_ascii=False)},
            })
    return result


def _build_tool_limit_fallback(tool_trace: list[dict]) -> str:
    if not tool_trace:
        return "本轮工具调用次数达到上限，已停止继续调用。"
    lines = ["本轮工具调用较多，已停止继续调用。已获取信息摘要："]
    for idx, step in enumerate(tool_trace[-4:], start=1):
        name = step.get("agent_tool", "unknown_tool")
        result = step.get("result")
        text = ""
        if isinstance(result, dict):
            if result.get("ok") is False:
                text = str(result.get("error", "执行失败"))
            elif result.get("result") is not None:
                text = str(result.get("result"))
            elif result.get("stdout"):
                text = str(result.get("stdout"))
            else:
                text = "调用完成"
        elif result is not None:
            text = str(result)
        else:
            text = str(step.get("raw_result", ""))
        text = text.replace("\n", " ").strip()
        if len(text) > 140:
            text = text[:140] + "..."
        lines.append(f"{idx}. {name}: {text or '调用完成'}")
    lines.append("如果你希望更精准结果，请缩小问题范围后再试。")
    return "\n".join(lines)


def _require_user_last_message(messages: list[dict], *, func: str) -> None:
    rows = messages if isinstance(messages, list) else []
    count = len(rows)
    if count <= 0:
        audit_event(
            "SYSTEM_EXECUTE",
            "ai",
            func,
            _THIS_FILE,
            "invalid_messages_empty",
            extra={"ok": False, "reason": "empty_messages"},
        )
        raise ValueError("messages empty: last role must be user")

    last = rows[-1] if isinstance(rows[-1], dict) else {}
    role = str(last.get("role", "")).strip().lower()
    if role != "user":
        audit_event(
            "SYSTEM_EXECUTE",
            "ai",
            func,
            _THIS_FILE,
            "invalid_messages_last_role",
            extra={"ok": False, "reason": "last_role_not_user", "last_role": role, "messages_count": count},
        )
        raise ValueError(f"invalid messages: last role must be user, got '{role or 'unknown'}'")


class LLMClient:
    """
    用处：封装 LLM 调用，支持工具循环与自动纠正假调用。
    """

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None) -> None:
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        self.max_retries = max(0, int(os.getenv("DEEPSEEK_MAX_RETRIES", "2")))
        self.retry_backoff_sec = max(0.05, float(os.getenv("DEEPSEEK_RETRY_BACKOFF_SEC", "0.6")))
        if not self.api_key:
            raise ValueError("缺少 DEEPSEEK_API_KEY，请在 .env 中配置")
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _create_chat_completion_with_retry(self, *, func: str, stream: bool, **req):
        max_attempts = 1 + int(self.max_retries)
        attempt = 0
        last_err: Exception | None = None
        if "stream" not in req:
            req["stream"] = bool(stream)
        while attempt < max_attempts:
            attempt += 1
            try:
                return self._client.chat.completions.create(**req)
            except Exception as e:
                last_err = e
                retryable = _is_retryable_llm_error(e)
                will_retry = retryable and attempt < max_attempts
                audit_event(
                    "SYSTEM_EXECUTE",
                    "ai",
                    func,
                    _THIS_FILE,
                    "llm_request_error",
                    extra={
                        "model": self.model,
                        "stream": bool(stream),
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "retryable": bool(retryable),
                        "will_retry": bool(will_retry),
                        "error_type": type(e).__name__,
                        "error": str(e),
                    },
                )
                if not will_retry:
                    break
                sleep_s = self.retry_backoff_sec * (2 ** (attempt - 1))
                time.sleep(sleep_s)
        if last_err is not None:
            raise last_err
        raise RuntimeError("llm request failed without exception")

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat", _THIS_FILE, "llm_chat_start",
                     extra={"model": self.model, "messages_count": len(messages), "temperature": temperature})
        try:
            resp = self._create_chat_completion_with_retry(
                func="LLMClient.chat",
                stream=False,
                model=self.model, messages=messages, temperature=temperature)
            text = resp.choices[0].message.content
            audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat", _THIS_FILE, "llm_chat_done",
                         extra={"model": self.model, "ok": True, "reply_len": len(text or "")})
            return text
        except Exception as e:
            code, user_message = _format_llm_error(e)
            audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat", _THIS_FILE, f"llm_chat_failed code={code}",
                         extra={"model": self.model, "ok": False, "error": str(e), "error_code": code})
            setattr(e, "tinda_error_code", code)
            setattr(e, "tinda_user_message", user_message)
            raise

    def _run_tools(self, tool_calls: list[dict], user_perm: int,
                   msgs: list[dict], trace: list[dict], func: str) -> list[dict]:
        """执行工具调用，写入消息历史与轨迹。"""
        steps = []
        for call in tool_calls:
            name = call["function"]["name"]
            args = _parse_args(call["function"].get("arguments", ""))
            model_id = call.get("id", "")
            event_id = audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_start tool={name}",
                                    extra={"tool_name": name, "has_arguments": bool(args), "model_id": model_id})
            call_id = f"tc_{event_id:010d}"
            raw = tool_registry.run_agent_tool(name, user_perm, args, call_id=call_id)
            parsed = _parse_json(raw)

            user_safe = parsed
            model_content = raw
            if isinstance(parsed, dict):
                parsed.setdefault("call_id", call_id)
                user_safe.setdefault("call_id", call_id)
                if parsed.get("error_code") == "permission_denied" and parsed.get("expose_to_user") is False:
                    model_view = dict(parsed)
                    model_view["error"] = parsed.get("llm_message") or parsed.get("error") or "权限不足"
                    model_content = json.dumps(model_view, ensure_ascii=False)

                    safe = dict(parsed)
                    safe["error"] = parsed.get("user_message") or "该工具当前不可用，请尝试其它方式。"
                    for k in ("llm_message", "missing_perm_labels", "required_perm_labels",
                              "required_perm_bits", "user_perm", "user_perm_labels"):
                        safe.pop(k, None)
                    user_safe = safe

            trace.append({"agent_tool": name, "call_id": call_id, "tool_call_id": model_id,
                          "arguments": args, "result": user_safe, "raw_result": raw})
            steps.append({"agent_tool": name, "call_id": call_id, "tool_call_id": model_id,
                          "arguments": args, "result": user_safe, "raw_result": raw})
            msgs.append({"role": "tool", "tool_call_id": call["id"], "content": model_content})
            audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_done tool={name}",
                         extra={"tool_name": name, "ok": True, "call_id": call_id})
        return steps

    def _process_tool_loop(self, messages: list[dict], user_perm: int, temperature: float,
                           max_tool_steps: int, stream: bool, base_len: int,
                           func: str) -> tuple[str, list[dict], int, list[dict]]:
        """核心工具调用循环（同步与流式共用）。"""
        msgs = [m.copy() for m in messages]
        tools = tool_registry.build_agent_tool_schemas(user_perm)
        steps = 0
        trace = []
        allow_tools = int(max_tool_steps) > 0

        while True:
            force_finalize = allow_tools and steps >= max_tool_steps - 1 and len(trace) > 0
            req: dict[str, Any] = {
                "model": self.model,
                "messages": msgs,
                "temperature": temperature,
            }
            if allow_tools:
                req["tools"] = tools
                req["tool_choice"] = "none" if force_finalize else "auto"
            resp = self._create_chat_completion_with_retry(
                func=func,
                stream=False,
                **req,
            )
            msg = resp.choices[0].message
            rc = _concat_reasoning(getattr(msg, "reasoning_content", None))
            content = msg.content or ""

            calls = []
            if allow_tools:
                calls = [_normalize_tool_call(
                    call_id=getattr(c, "id", None), call_type=getattr(c, "type", None),
                    function_name=getattr(c.function, "name", ""),
                    function_arguments=getattr(c.function, "arguments", None),
                    fallback_id=f"call_{steps}_{i}")
                    for i, c in enumerate(msg.tool_calls or [])]
                if not calls:
                    calls = _detect_dsml_tool_calls(content, prefix=f"dsml_{steps}")

            if not calls:
                msg_out = _attach_reasoning({"role": "assistant", "content": content}, rc)
                msgs.append(msg_out)
                return content, msgs[base_len:], steps, trace

            msg_out = _attach_reasoning(
                {"role": "assistant", "content": content, "tool_calls": calls}, rc)
            msgs.append(msg_out)
            self._run_tools(calls, user_perm, msgs, trace, func)
            steps += 1

            if _trace_has_pending_confirmation(trace):
                return "", msgs[base_len:], steps, trace

            if steps >= max_tool_steps:
                fallback = _build_tool_limit_fallback(trace)
                msgs.append({"role": "assistant", "content": fallback})
                return fallback, msgs[base_len:], steps, trace

    def chat_with_tools(self, messages: list[dict], user_perm: int,
                        temperature: float = 0.7, max_tool_steps: int = 6) -> dict:
        """支持工具调用的对话，自动纠正模型假调用。"""
        _require_user_last_message(messages, func="LLMClient.chat_with_tools")
        base_len = len(messages)
        reply, delta, steps, trace = self._process_tool_loop(
            messages, user_perm, temperature, max_tool_steps, False, base_len, "LLMClient.chat_with_tools")
        audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat_with_tools", _THIS_FILE, "chat_with_tools_done",
                     extra={"model": self.model, "tool_steps": steps, "tool_trace_len": len(trace),
                            "reply_len": len(reply)})
        return {"reply": reply, "history_delta": delta, "tool_steps": steps, "tool_trace": trace}

    def stream_chat_with_tools(self, messages: list[dict], user_perm: int,
                               temperature: float = 0.7, max_tool_steps: int = 6) -> Iterator[dict]:
        """流式对话（支持工具调用循环），自动纠正模型假调用。"""
        _require_user_last_message(messages, func="LLMClient.stream_chat_with_tools")
        msgs = [m.copy() for m in messages]
        tools = tool_registry.build_agent_tool_schemas(user_perm)
        base_len = len(msgs)
        steps = 0
        trace = []
        audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.stream_chat_with_tools", _THIS_FILE,
                     "stream_start", extra={"model": self.model, "messages_count": len(messages),
                                            "user_perm": user_perm, "max_tool_steps": max_tool_steps})

        while True:
            force_finalize = steps >= max_tool_steps - 1 and len(trace) > 0
            stream = self._create_chat_completion_with_retry(
                func="LLMClient.stream_chat_with_tools",
                stream=True,
                model=self.model, messages=msgs, temperature=temperature,
                tools=tools, tool_choice="none" if force_finalize else "auto",
            )
            content_parts = []
            reasoning_parts = []
            tool_calls_map = {}

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue

                part = getattr(delta, "content", None)
                if part:
                    content_parts.append(part)
                    yield {"type": "delta", "content": part}
                rp = _concat_reasoning(getattr(delta, "reasoning_content", None))
                if rp:
                    reasoning_parts.append(rp)

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

            calls = []
            if not force_finalize:
                for key in sorted(tool_calls_map):
                    c = tool_calls_map[key]
                    calls.append(_normalize_tool_call(
                        call_id=c.get("id"), call_type=c.get("type"),
                        function_name=c.get("function", {}).get("name"),
                        function_arguments=c.get("function", {}).get("arguments"),
                        fallback_id=f"call_{steps}_{key}"))
                if not calls:
                    calls = _detect_dsml_tool_calls(content, prefix=f"dsml_{steps}")

            if not calls:
                msg_out = _attach_reasoning({"role": "assistant", "content": content}, rc)
                msgs.append(msg_out)
                audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.stream_chat_with_tools", _THIS_FILE,
                             "stream_done", extra={"model": self.model, "tool_steps": steps,
                                                   "tool_trace_len": len(trace), "reply_len": len(content)})
                yield {"type": "done", **(_build_done(content, msgs, base_len, steps, trace))}
                return

            yield {"type": "reset"}
            msg_out = _attach_reasoning(
                {"role": "assistant", "content": content, "tool_calls": calls}, rc)
            msgs.append(msg_out)
            step_trace = self._run_tools(calls, user_perm, msgs, trace, "LLMClient.stream_chat_with_tools")
            if step_trace:
                yield {"type": "tool_step", "trace": step_trace}

            steps += 1

            if _trace_has_pending_confirmation(trace):
                audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.stream_chat_with_tools", _THIS_FILE,
                             "stream_pending_confirmation",
                             extra={"model": self.model, "tool_steps": steps, "tool_trace_len": len(trace)})
                yield {"type": "done", **(_build_done("", msgs, base_len, steps, trace))}
                return

            if steps >= max_tool_steps:
                fallback = _build_tool_limit_fallback(trace)
                msgs.append({"role": "assistant", "content": fallback})
                yield {"type": "delta", "content": fallback}
                yield {"type": "done", **(_build_done(fallback, msgs, base_len, steps, trace))}
                audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.stream_chat_with_tools", _THIS_FILE,
                             "stream_reach_max_steps", extra={"model": self.model, "tool_steps": steps,
                                                              "tool_trace_len": len(trace)})
                return
