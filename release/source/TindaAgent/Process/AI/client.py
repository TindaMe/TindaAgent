import os
import json
import html
import re
from pathlib import Path
from typing import Any, Iterator
from openai import OpenAI
try:
    from openai import BadRequestError
except ImportError:
    BadRequestError = None
from dotenv import load_dotenv
from TindaAgent.Tool import tool as tool_registry
from TindaAgent.Process.Observability import audit_event

_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)
_THIS_FILE = str(Path(__file__).resolve())


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

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None) -> None:
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        if not self.api_key:
            raise ValueError("缺少 DEEPSEEK_API_KEY，请在 .env 中配置")
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        # 思考模式拉到最大
        self._thinking = {"type": "enabled"}
        self._extra_body = {"thinking": self._thinking}

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat", _THIS_FILE, "llm_chat_start",
                     extra={"model": self.model, "messages_count": len(messages), "temperature": temperature})
        try:
            resp = self._client.chat.completions.create(
                model=self.model, messages=messages, temperature=temperature,
                extra_body=self._extra_body)
            text = resp.choices[0].message.content
            audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat", _THIS_FILE, "llm_chat_done",
                         extra={"model": self.model, "ok": True, "reply_len": len(text or "")})
            return text
        except Exception as e:
            audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.chat", _THIS_FILE, f"llm_chat_failed err={e}",
                         extra={"model": self.model, "ok": False, "error": str(e)})
            raise

    def _run_tools(self, tool_calls: list[dict], user_perm: int,
                   msgs: list[dict], trace: list[dict], func: str) -> tuple[list[dict], bool]:
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
            msgs.append({"role": "tool", "tool_call_id": call["id"], "content": model_content})
            audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_done tool={name}",
                         extra={"tool_name": name, "ok": True, "call_id": call_id})
        return steps, has_pending_confirmation

    def _process_tool_loop(self, messages: list[dict], user_perm: int, temperature: float,
                           max_tool_steps: int, stream: bool, base_len: int,
                           func: str) -> tuple[str, list[dict], int, list[dict]]:
        """核心工具调用循环（同步与流式共用）。"""
        msgs = [m.copy() for m in messages]
        tools = tool_registry.build_agent_tool_schemas(user_perm)
        steps = 0
        trace = []

        while True:
            force_finalize = steps >= max_tool_steps - 1 and len(trace) > 0
            resp = self._client.chat.completions.create(
                model=self.model, messages=msgs, temperature=temperature,
                tools=tools, tool_choice="none" if force_finalize else "auto",
                extra_body=self._extra_body)
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

            msg_out = _attach_reasoning(
                {"role": "assistant", "content": content_for_history, "tool_calls": calls}, rc)
            msgs.append(msg_out)
            step_trace, has_pending_confirmation = self._run_tools(calls, user_perm, msgs, trace, func)

            steps += 1
            # 所有工具均失败 → 继续循环，让 LLM 根据 tool role 的错误信息自行决定下一步
            if step_trace and all(_tool_failed(s) for s in step_trace):
                continue
            if has_pending_confirmation:
                pending_reply = content_for_history if str(content_for_history or "").strip() else ""
                return pending_reply, msgs[base_len:], steps, trace
            if steps >= max_tool_steps:
                fallback = _build_tool_limit_fallback(trace)
                msgs.append({"role": "assistant", "content": fallback})
                return fallback, msgs[base_len:], steps, trace

    def chat_with_tools(self, messages: list[dict], user_perm: int,
                        temperature: float = 0.7, max_tool_steps: int = 6) -> dict:
        """支持工具调用的对话，自动纠正模型假调用。"""
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
            try:
                stream = self._client.chat.completions.create(
                    model=self.model, messages=msgs, temperature=temperature,
                    tools=tools, tool_choice="none" if force_finalize else "auto",
                    stream=True,
                    extra_body=self._extra_body)
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

            for chunk in stream:
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

            calls = []
            if not force_finalize:
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
            else:
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
            step_trace, has_pending_confirmation = self._run_tools(
                calls,
                user_perm,
                msgs,
                trace,
                "LLMClient.stream_chat_with_tools",
            )
            if step_trace:
                yield {"type": "tool_step", "trace": step_trace}

            steps += 1
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
            if steps >= max_tool_steps:
                fallback = _build_tool_limit_fallback(trace)
                msgs.append({"role": "assistant", "content": fallback})
                yield {"type": "delta", "content": fallback}
                yield {"type": "done", **(_build_done(fallback, msgs, base_len, steps, trace))}
                audit_event("SYSTEM_EXECUTE", "ai", "LLMClient.stream_chat_with_tools", _THIS_FILE,
                             "stream_reach_max_steps", extra={"model": self.model, "tool_steps": steps,
                                                              "tool_trace_len": len(trace)})
                return
