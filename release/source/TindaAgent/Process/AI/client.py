import os
import json
import re
from pathlib import Path
from typing import Any, Iterator
from openai import OpenAI
from dotenv import load_dotenv
from TindaAgent.Tool import tool as tool_registry
from TindaAgent.Process.Observability import audit_event

# .env 位于 TindaAgent 包根目录
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)
_THIS_FILE = str(Path(__file__).resolve())


class LLMClient:
    """
    用处： 封装 LLM 调用，未来换厂商/接 LiteLLM 只改这一个文件
    """

    def __init__(
        self,
        api_key: str = None,
        base_url: str = None,
        model: str = None,
    ) -> None:
        """
        用处： 初始化 LLM 客户端，默认读取环境变量

        参数：
            api_key: str // API 密钥，默认读 DEEPSEEK_API_KEY
            base_url: str // 接口地址，默认读 DEEPSEEK_BASE_URL
            model: str // 模型名，默认读 DEEPSEEK_MODEL
        """
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

        if not self.api_key:
            raise ValueError("缺少 DEEPSEEK_API_KEY，请在 .env 中配置")

        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        """
        用处： 发起一次对话请求，返回模型回复文本

        参数：
            messages: list[dict] // OpenAI 格式的消息列表
            temperature: float // 采样温度，0-2

        返回：
            str // 模型回复内容
        """
        audit_event(
            op_type="SYSTEM_EXECUTE",
            subsystem="ai",
            func="LLMClient.chat",
            file_path=_THIS_FILE,
            content="llm_chat_start",
            extra={"model": self.model, "messages_count": len(messages), "temperature": float(temperature)},
        )
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
            )
            text = response.choices[0].message.content
            audit_event(
                op_type="SYSTEM_EXECUTE",
                subsystem="ai",
                func="LLMClient.chat",
                file_path=_THIS_FILE,
                content="llm_chat_done",
                extra={"model": self.model, "ok": True, "reply_len": len(str(text or ""))},
            )
            return text
        except Exception as e:
            audit_event(
                op_type="SYSTEM_EXECUTE",
                subsystem="ai",
                func="LLMClient.chat",
                file_path=_THIS_FILE,
                content=f"llm_chat_failed err={e}",
                extra={"model": self.model, "ok": False, "error": str(e)},
            )
            raise

    @staticmethod
    def _parse_tool_arguments(raw_arguments: str | None) -> dict[str, Any]:
        if not raw_arguments:
            return {}
        try:
            data = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            return data
        return {}

    @staticmethod
    def _parse_json(raw_text: str | None) -> Any:
        if raw_text is None:
            return None
        try:
            return json.loads(raw_text)
        except (TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _normalize_reasoning_content(raw: Any) -> str:
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, list):
            chunks: list[str] = []
            for item in raw:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                    if text:
                        chunks.append(str(text))
                else:
                    chunks.append(str(item))
            return "".join(chunks)
        if isinstance(raw, dict):
            text = raw.get("text") or raw.get("content")
            if text is not None:
                return str(text)
            return json.dumps(raw, ensure_ascii=False)
        return str(raw)

    @staticmethod
    def _attach_reasoning_content(message: dict[str, Any], reasoning_content: str) -> dict[str, Any]:
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        return message

    @staticmethod
    def _build_done_payload(
        reply: str,
        working_messages: list[dict[str, Any]],
        base_len: int,
        steps: int,
        tool_trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "reply": reply,
            "history_delta": working_messages[base_len:],
            "tool_steps": steps,
            "tool_trace": tool_trace,
        }

    @staticmethod
    def _normalize_tool_call(
        *,
        call_id: str | None,
        call_type: str | None,
        function_name: str | None,
        function_arguments: str | None,
        fallback_id: str,
    ) -> dict[str, Any]:
        return {
            "id": str(call_id or "").strip() or fallback_id,
            "type": str(call_type or "function").strip() or "function",
            "function": {
                "name": str(function_name or "").strip(),
                "arguments": function_arguments or "{}",
            },
        }

    @staticmethod
    def _extract_dsml_tool_calls(content: str | None, *, fallback_prefix: str) -> list[dict[str, Any]]:
        """
        兼容 DeepSeek 偶发把 tool call 以 DSML 文本吐在 content 中的场景。
        """
        text = str(content or "")
        lower = text.lower()
        if "tool_calls" not in lower and "invoke" not in lower:
            return []

        invoke_blocks = re.findall(
            r"<[^>]*invoke[^>]*name=\"([^\"]+)\"[^>]*>(.*?)</[^>]*invoke>",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not invoke_blocks:
            return []

        normalized: list[dict[str, Any]] = []
        for idx, (invoke_name, body) in enumerate(invoke_blocks):
            params: dict[str, str] = {}
            for p_name, p_value in re.findall(
                r"<[^>]*parameter[^>]*name=\"([^\"]+)\"[^>]*>(.*?)</[^>]*parameter>",
                body,
                flags=re.DOTALL | re.IGNORECASE,
            ):
                params[str(p_name).strip()] = str(p_value).strip()

            fn_name = str(invoke_name or "").strip()
            if not fn_name:
                continue

            # call_backend_tool 需要参数打包成 JSON arguments
            if fn_name == "call_backend_tool":
                tool_name = str(params.get("tool_name", "")).strip()
                args_val = params.get("args", "")
                kwargs_val = params.get("kwargs", "")
                arguments: dict[str, Any] = {"tool_name": tool_name}
                if args_val:
                    try:
                        parsed_args = json.loads(args_val)
                        if isinstance(parsed_args, list):
                            arguments["args"] = parsed_args
                    except Exception:
                        pass
                if kwargs_val:
                    try:
                        parsed_kwargs = json.loads(kwargs_val)
                        if isinstance(parsed_kwargs, dict):
                            arguments["kwargs"] = parsed_kwargs
                    except Exception:
                        pass
                normalized.append(
                    {
                        "id": f"{fallback_prefix}_{idx}",
                        "type": "function",
                        "function": {
                            "name": "call_backend_tool",
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                )
                continue

            # 其他 invoke 当作普通 function call，按参数字典传
            normalized.append(
                {
                    "id": f"{fallback_prefix}_{idx}",
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": json.dumps(params, ensure_ascii=False),
                    },
                }
            )

        return normalized

    def _run_normalized_tool_calls(
        self,
        *,
        normalized_tool_calls: list[dict[str, Any]],
        user_perm: int,
        working_messages: list[dict[str, Any]],
        tool_trace: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        executed_steps: list[dict[str, Any]] = []
        for call in normalized_tool_calls:
            tool_name = str(call["function"].get("name", ""))
            raw_arguments = call["function"].get("arguments", "")
            parsed_args = self._parse_tool_arguments(raw_arguments)
            model_call_id = str(call.get("id", "") or "")
            start_event_id = audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="ai",
                func="LLMClient._run_normalized_tool_calls",
                file_path=_THIS_FILE,
                content=f"llm_tool_call_start tool={tool_name}",
                extra={
                    "tool_name": tool_name,
                    "has_arguments": bool(parsed_args),
                    "model_tool_call_id": model_call_id,
                },
            )
            call_id = f"tc_{int(start_event_id):010d}"
            tool_result = tool_registry.run_agent_tool(
                tool_name,
                user_perm,
                parsed_args,
                call_id=call_id,
            )
            parsed_result = self._parse_json(tool_result)
            user_safe_result = parsed_result
            model_tool_content = tool_result
            if isinstance(user_safe_result, dict):
                user_safe_result.setdefault("call_id", call_id)
            if isinstance(parsed_result, dict):
                parsed_result.setdefault("call_id", call_id)
                error_code = str(parsed_result.get("error_code", "") or "")
                if error_code == "permission_denied" and parsed_result.get("expose_to_user") is False:
                    # 模型侧保留详细缺权信息，避免反复盲调同一受限工具。
                    model_view = dict(parsed_result)
                    model_view["error"] = str(
                        parsed_result.get("llm_message")
                        or parsed_result.get("error")
                        or "权限不足"
                    )
                    model_tool_content = json.dumps(model_view, ensure_ascii=False)

                    safe = dict(parsed_result)
                    safe["error"] = str(parsed_result.get("user_message") or "该工具当前不可用，请尝试其它方式。")
                    safe.pop("llm_message", None)
                    safe.pop("missing_perm_labels", None)
                    safe.pop("required_perm_labels", None)
                    safe.pop("required_perm_bits", None)
                    safe.pop("user_perm", None)
                    safe.pop("user_perm_labels", None)
                    user_safe_result = safe
            tool_trace.append(
                {
                    "agent_tool": tool_name,
                    "call_id": call_id,
                    "tool_call_id": model_call_id,
                    "arguments": parsed_args,
                    "result": user_safe_result,
                    "raw_result": tool_result,
                }
            )
            executed_steps.append(
                {
                    "agent_tool": tool_name,
                    "call_id": call_id,
                    "tool_call_id": model_call_id,
                    "arguments": parsed_args,
                    "result": user_safe_result,
                    "raw_result": tool_result,
                }
            )
            working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": model_tool_content,
                }
            )
            audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="ai",
                func="LLMClient._run_normalized_tool_calls",
                file_path=_THIS_FILE,
                content=f"llm_tool_call_done tool={tool_name} call_id={call_id}",
                extra={
                    "tool_name": tool_name,
                    "ok": True,
                    "call_id": call_id,
                    "model_tool_call_id": model_call_id,
                },
            )
        return executed_steps

    def _append_assistant_tool_message(
        self,
        *,
        working_messages: list[dict[str, Any]],
        content: str,
        reasoning_content: str,
        normalized_tool_calls: list[dict[str, Any]],
    ) -> None:
        assistant_tool_msg: dict[str, Any] = self._attach_reasoning_content(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": [],
            },
            reasoning_content,
        )
        assistant_tool_msg["tool_calls"].extend(normalized_tool_calls)
        working_messages.append(assistant_tool_msg)

    @staticmethod
    def _build_tool_limit_fallback(tool_trace: list[dict[str, Any]]) -> str:
        """
        工具调用过多时，输出可读汇总，避免只返回生硬上限提示。
        """
        if not tool_trace:
            return "本轮工具调用次数达到上限，已停止继续调用。"

        lines = ["本轮工具调用较多，已停止继续调用。已获取信息摘要："]
        for idx, step in enumerate(tool_trace[-4:], start=1):
            name = str(step.get("agent_tool", "") or "unknown_tool")
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

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        user_perm: int,
        temperature: float = 0.7,
        max_tool_steps: int = 6,
    ) -> dict[str, Any]:
        """
        用处：支持模型工具调用的对话请求

        返回：
            {
                "reply": str,               # 最终 assistant 文本回复
                "history_delta": list[dict],# 本轮新增到历史的消息（assistant/tool 等）
                "tool_steps": int,          # 实际工具循环次数
                "tool_trace": list[dict],   # 本轮工具调用轨迹
            }
        """
        working_messages: list[dict[str, Any]] = [m.copy() for m in messages]
        tools = tool_registry.build_agent_tool_schemas(user_perm)
        base_len = len(working_messages)
        steps = 0
        tool_trace: list[dict[str, Any]] = []
        audit_event(
            op_type="SYSTEM_EXECUTE",
            subsystem="ai",
            func="LLMClient.chat_with_tools",
            file_path=_THIS_FILE,
            content="chat_with_tools_start",
            extra={
                "model": self.model,
                "messages_count": len(messages),
                "user_perm": int(user_perm),
                "max_tool_steps": int(max_tool_steps),
            },
        )

        while True:
            # 接近上限时，强制模型基于现有工具结果作答，避免继续 tool-call 循环
            force_finalize = steps >= max_tool_steps - 1 and len(tool_trace) > 0
            response = self._client.chat.completions.create(
                model=self.model,
                messages=working_messages,
                temperature=temperature,
                tools=tools,
                tool_choice="none" if force_finalize else "auto",
            )
            msg = response.choices[0].message
            reasoning_content = self._normalize_reasoning_content(getattr(msg, "reasoning_content", None))
            normalized_tool_calls: list[dict[str, Any]] = []
            if not force_finalize:
                for idx, call in enumerate(msg.tool_calls or []):
                    normalized_tool_calls.append(
                        self._normalize_tool_call(
                            call_id=getattr(call, "id", None),
                            call_type=getattr(call, "type", None),
                            function_name=getattr(call.function, "name", ""),
                            function_arguments=getattr(call.function, "arguments", None),
                            fallback_id=f"call_{steps}_{idx}",
                        )
                    )
                # 兼容 DSML 文本工具调用（msg.tool_calls 为空但 content 内含 invoke）
                if not normalized_tool_calls:
                    normalized_tool_calls = self._extract_dsml_tool_calls(
                        msg.content or "",
                        fallback_prefix=f"dsml_{steps}",
                    )

            if not normalized_tool_calls:
                assistant_msg = self._attach_reasoning_content(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                    },
                    reasoning_content,
                )
                working_messages.append(assistant_msg)
                audit_event(
                    op_type="SYSTEM_EXECUTE",
                    subsystem="ai",
                    func="LLMClient.chat_with_tools",
                    file_path=_THIS_FILE,
                    content="chat_with_tools_done",
                    extra={
                        "model": self.model,
                        "tool_steps": steps,
                        "tool_trace_len": len(tool_trace),
                        "reply_len": len(str(assistant_msg.get("content", ""))),
                    },
                )
                return self._build_done_payload(
                    assistant_msg["content"],
                    working_messages,
                    base_len,
                    steps,
                    tool_trace,
                )

            self._append_assistant_tool_message(
                working_messages=working_messages,
                content=msg.content or "",
                reasoning_content=reasoning_content,
                normalized_tool_calls=normalized_tool_calls,
            )
            self._run_normalized_tool_calls(
                normalized_tool_calls=normalized_tool_calls,
                user_perm=user_perm,
                working_messages=working_messages,
                tool_trace=tool_trace,
            )

            steps += 1
            if steps >= max_tool_steps:
                fallback_text = self._build_tool_limit_fallback(tool_trace)
                fallback_msg = {
                    "role": "assistant",
                    "content": fallback_text,
                }
                working_messages.append(fallback_msg)
                audit_event(
                    op_type="SYSTEM_EXECUTE",
                    subsystem="ai",
                    func="LLMClient.chat_with_tools",
                    file_path=_THIS_FILE,
                    content="chat_with_tools_reach_max_steps",
                    extra={
                        "model": self.model,
                        "tool_steps": steps,
                        "tool_trace_len": len(tool_trace),
                    },
                )
                return self._build_done_payload(
                    fallback_text,
                    working_messages,
                    base_len,
                    steps,
                    tool_trace,
                )

    def stream_chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        user_perm: int,
        temperature: float = 0.7,
        max_tool_steps: int = 6,
    ) -> Iterator[dict[str, Any]]:
        """
        用处：流式对话（支持工具调用循环）

        事件：
            {"type":"delta","content":str}
            {"type":"reset"}  # 本轮出现 tool_call，清空临时文本
            {"type":"done","reply":str,"history_delta":list,"tool_trace":list,"tool_steps":int}
        """
        working_messages: list[dict[str, Any]] = [m.copy() for m in messages]
        tools = tool_registry.build_agent_tool_schemas(user_perm)
        base_len = len(working_messages)
        steps = 0
        tool_trace: list[dict[str, Any]] = []
        audit_event(
            op_type="SYSTEM_EXECUTE",
            subsystem="ai",
            func="LLMClient.stream_chat_with_tools",
            file_path=_THIS_FILE,
            content="stream_chat_with_tools_start",
            extra={
                "model": self.model,
                "messages_count": len(messages),
                "user_perm": int(user_perm),
                "max_tool_steps": int(max_tool_steps),
            },
        )

        while True:
            # 接近上限时，强制模型基于现有工具结果作答，避免继续 tool-call 循环
            force_finalize = steps >= max_tool_steps - 1 and len(tool_trace) > 0
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=working_messages,
                temperature=temperature,
                tools=tools,
                tool_choice="none" if force_finalize else "auto",
                stream=True,
            )

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls_map: dict[int, dict[str, Any]] = {}

            for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                part = getattr(delta, "content", None)
                if part:
                    content_parts.append(part)
                    yield {"type": "delta", "content": part}
                reasoning_part = self._normalize_reasoning_content(getattr(delta, "reasoning_content", None))
                if reasoning_part:
                    reasoning_parts.append(reasoning_part)

                delta_tool_calls = getattr(delta, "tool_calls", None) or []
                for tc in delta_tool_calls:
                    idx = getattr(tc, "index", 0)
                    entry = tool_calls_map.setdefault(
                        idx,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    tc_id = getattr(tc, "id", None)
                    if tc_id:
                        entry["id"] = tc_id
                    tc_type = getattr(tc, "type", None)
                    if tc_type:
                        entry["type"] = tc_type

                    tc_function = getattr(tc, "function", None)
                    if tc_function is not None:
                        tc_name = getattr(tc_function, "name", None)
                        if tc_name:
                            entry["function"]["name"] = tc_name
                        tc_args = getattr(tc_function, "arguments", None)
                        if tc_args:
                            entry["function"]["arguments"] += tc_args

            round_content = "".join(content_parts)
            round_reasoning = "".join(reasoning_parts)
            ordered_tool_calls: list[dict[str, Any]] = []
            if not force_finalize:
                for idx, key in enumerate(sorted(tool_calls_map.keys())):
                    call = tool_calls_map[key]
                    ordered_tool_calls.append(
                        self._normalize_tool_call(
                            call_id=call.get("id"),
                            call_type=call.get("type"),
                            function_name=call.get("function", {}).get("name"),
                            function_arguments=call.get("function", {}).get("arguments"),
                            fallback_id=f"call_{steps}_{idx}",
                        )
                    )
                # 兼容流式下 DSML 文本工具调用（无 delta.tool_calls）
                if not ordered_tool_calls:
                    ordered_tool_calls = self._extract_dsml_tool_calls(
                        round_content,
                        fallback_prefix=f"dsml_{steps}",
                    )

            if not ordered_tool_calls:
                assistant_msg = self._attach_reasoning_content(
                    {"role": "assistant", "content": round_content},
                    round_reasoning,
                )
                working_messages.append(assistant_msg)
                audit_event(
                    op_type="SYSTEM_EXECUTE",
                    subsystem="ai",
                    func="LLMClient.stream_chat_with_tools",
                    file_path=_THIS_FILE,
                    content="stream_chat_with_tools_done",
                    extra={
                        "model": self.model,
                        "tool_steps": steps,
                        "tool_trace_len": len(tool_trace),
                        "reply_len": len(round_content),
                    },
                )
                yield {
                    "type": "done",
                    **self._build_done_payload(
                        round_content,
                        working_messages,
                        base_len,
                        steps,
                        tool_trace,
                    ),
                }
                return

            # 只要进入 tool_call 轮次就发 reset，前端据此追加工具调用标记。
            # 不能依赖 round_content，否则某些“纯工具轮”会完全看不到调用痕迹。
            yield {"type": "reset"}

            self._append_assistant_tool_message(
                working_messages=working_messages,
                content=round_content,
                reasoning_content=round_reasoning,
                normalized_tool_calls=ordered_tool_calls,
            )
            step_trace = self._run_normalized_tool_calls(
                normalized_tool_calls=ordered_tool_calls,
                user_perm=user_perm,
                working_messages=working_messages,
                tool_trace=tool_trace,
            )
            if step_trace:
                yield {"type": "tool_step", "trace": step_trace}

            steps += 1
            if steps >= max_tool_steps:
                fallback_text = self._build_tool_limit_fallback(tool_trace)
                working_messages.append({"role": "assistant", "content": fallback_text})
                yield {"type": "delta", "content": fallback_text}
                yield {
                    "type": "done",
                    **self._build_done_payload(
                        fallback_text,
                        working_messages,
                        base_len,
                        steps,
                        tool_trace,
                    ),
                }
                audit_event(
                    op_type="SYSTEM_EXECUTE",
                    subsystem="ai",
                    func="LLMClient.stream_chat_with_tools",
                    file_path=_THIS_FILE,
                    content="stream_chat_with_tools_reach_max_steps",
                    extra={
                        "model": self.model,
                        "tool_steps": steps,
                        "tool_trace_len": len(tool_trace),
                    },
                )
                return
