import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv
from openai import OpenAI

from TindaAgent.Tool import tool as tool_registry

# .env 位于 TindaAgent 包根目录
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


class ProviderAdapter(ABC):
    """
    用处：抽象模型供应商适配层，为多模型/多厂商预留统一接口。
    """

    @abstractmethod
    def chat_create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        stream: bool = False,
    ) -> Any:
        """
        发起一次 chat.completions 请求（可流式）。
        """
        raise NotImplementedError


class OpenAICompatibleProviderAdapter(ProviderAdapter):
    """
    用处：适配 OpenAI 兼容接口（当前 DeepSeek 走该适配器）。
    """

    def __init__(self, api_key: str, base_url: str) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def chat_create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        stream: bool = False,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return self._client.chat.completions.create(**kwargs)


class LLMClient:
    """
    用处：封装 LLM 调用，Provider 可替换，业务层接口保持稳定。
    """

    def __init__(
        self,
        api_key: str = None,
        base_url: str = None,
        model: str = None,
        provider: ProviderAdapter | None = None,
    ) -> None:
        """
        参数：
            api_key: 默认读取 DEEPSEEK_API_KEY
            base_url: 默认读取 DEEPSEEK_BASE_URL
            model: 默认读取 DEEPSEEK_MODEL
            provider: 可注入自定义适配器，便于未来多厂商扩展
        """
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

        if not self.api_key:
            raise ValueError("缺少 DEEPSEEK_API_KEY，请在 .env 中配置")

        self._provider = provider or OpenAICompatibleProviderAdapter(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        """
        用处：发起一次对话请求，返回模型回复文本。
        """
        response = self._provider.chat_create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            stream=False,
        )
        return response.choices[0].message.content

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

    def _run_normalized_tool_calls(
        self,
        *,
        normalized_tool_calls: list[dict[str, Any]],
        user_perm: int,
        working_messages: list[dict[str, Any]],
        tool_trace: list[dict[str, Any]],
    ) -> None:
        for call in normalized_tool_calls:
            tool_name = str(call["function"].get("name", ""))
            raw_arguments = call["function"].get("arguments", "")
            parsed_args = self._parse_tool_arguments(raw_arguments)
            tool_result = tool_registry.run_agent_tool(
                tool_name,
                user_perm,
                parsed_args,
            )
            parsed_result = self._parse_json(tool_result)
            tool_trace.append(
                {
                    "agent_tool": tool_name,
                    "arguments": parsed_args,
                    "result": parsed_result,
                    "raw_result": tool_result,
                }
            )
            working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_result,
                }
            )

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
                "reply": str,
                "history_delta": list[dict],
                "tool_steps": int,
                "tool_trace": list[dict],
            }
        """
        working_messages: list[dict[str, Any]] = [m.copy() for m in messages]
        tools = tool_registry.build_agent_tool_schemas(user_perm)
        base_len = len(working_messages)
        steps = 0
        tool_trace: list[dict[str, Any]] = []

        while True:
            force_finalize = steps >= max_tool_steps - 1 and len(tool_trace) > 0
            response = self._provider.chat_create(
                model=self.model,
                messages=working_messages,
                temperature=temperature,
                tools=tools,
                tool_choice="none" if force_finalize else "auto",
                stream=False,
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

            if not normalized_tool_calls:
                assistant_msg = self._attach_reasoning_content(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                    },
                    reasoning_content,
                )
                working_messages.append(assistant_msg)
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
            {"type":"reset"}
            {"type":"done","reply":str,"history_delta":list,"tool_trace":list,"tool_steps":int}
        """
        working_messages: list[dict[str, Any]] = [m.copy() for m in messages]
        tools = tool_registry.build_agent_tool_schemas(user_perm)
        base_len = len(working_messages)
        steps = 0
        tool_trace: list[dict[str, Any]] = []

        while True:
            force_finalize = steps >= max_tool_steps - 1 and len(tool_trace) > 0
            stream = self._provider.chat_create(
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

            if not ordered_tool_calls:
                assistant_msg = self._attach_reasoning_content(
                    {"role": "assistant", "content": round_content},
                    round_reasoning,
                )
                working_messages.append(assistant_msg)
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

            yield {"type": "reset"}

            self._append_assistant_tool_message(
                working_messages=working_messages,
                content=round_content,
                reasoning_content=round_reasoning,
                normalized_tool_calls=ordered_tool_calls,
            )
            self._run_normalized_tool_calls(
                normalized_tool_calls=ordered_tool_calls,
                user_perm=user_perm,
                working_messages=working_messages,
                tool_trace=tool_trace,
            )

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
                return
