from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from typing import Any

from openai import OpenAI

from TindaAgent.Process.AI.client import (
    LLMClient,
    _parse_json,
    _result_has_pending_confirmation,
    _tool_failed,
    record_llm_request,
    record_llm_response_usage,
)
from TindaAgent.Process.AI.providers import (
    load_llm_provider_config,
    public_provider,
    remove_model,
    resolve_api_key,
    save_llm_provider_config,
    upsert_model,
    upsert_provider,
)
from TindaAgent.Process.Observability import audit_event
from TindaAgent.Tool import tool as tool_registry

_THIS_FILE = __file__


def _mask_api_key(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return "***" if text else ""
    return f"{text[:4]}...{text[-4:]}"


def _json_request(url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout: int = 25) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


def _join_url(base_url: str, path: str, *, default_path: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    raw_path = str(path or "").strip() or default_path
    if raw_path.startswith("http://") or raw_path.startswith("https://"):
        return raw_path
    return base + "/" + raw_path.lstrip("/")


def _api_root_from_base_url(base_url: str) -> str:
    base = str(base_url or "https://api.deepseek.com").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base or "https://api.deepseek.com"


def _choice_response(content: str = "", reasoning: str = "", usage: dict[str, Any] | None = None) -> Any:
    message = SimpleNamespace(content=content, reasoning_content=reasoning)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage)


def _openai_http_response(data: dict[str, Any]) -> Any:
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    content = ""
    reasoning = ""
    if choices:
        msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        if isinstance(msg, dict):
            content = str(msg.get("content") or "")
            reasoning = str(msg.get("reasoning_content") or "")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return _choice_response(content, reasoning=reasoning, usage=usage)


def _build_done(reply: str, working_msgs: list[dict], base_len: int, steps: int, tool_trace: list[dict]) -> dict:
    return {
        "reply": reply,
        "history_delta": working_msgs[base_len:],
        "tool_steps": steps,
        "tool_trace": tool_trace,
    }


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


def _normalize_tool_call(*, call_id: str, function_name: str, function_arguments: Any, fallback_id: str) -> dict:
    if isinstance(function_arguments, str):
        args = function_arguments
    else:
        args = json.dumps(function_arguments if isinstance(function_arguments, dict) else {}, ensure_ascii=False)
    return {
        "id": call_id or fallback_id,
        "type": "function",
        "function": {
            "name": function_name or "",
            "arguments": args or "{}",
        },
    }


def _tool_call_preview(call: dict, *, fallback_id: str) -> dict:
    fn = call.get("function", {}) if isinstance(call, dict) and isinstance(call.get("function"), dict) else {}
    args = _parse_json(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments")
    return {
        "agent_tool": str(fn.get("name", "") or ""),
        "tool_call_id": str(call.get("id", "") or fallback_id),
        "arguments": args if isinstance(args, dict) else {},
    }


class MultiProviderToolClient:
    """Provider-aware tool loop with OpenAI-shaped history output."""

    def __init__(self, dispatcher: "LlmDispatcher") -> None:
        self._dispatcher = dispatcher

    @property
    def model(self) -> str:
        return self._dispatcher.current_model

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        return self._dispatcher.chat(messages, temperature=temperature)

    def chat_with_tools(
        self,
        messages: list[dict],
        user_perm: int,
        temperature: float = 0.7,
        max_tool_steps: int = 6,
    ) -> dict:
        provider = self._dispatcher.current_provider
        row = self._dispatcher._provider(provider)
        if str(row.get("adapter", "openai_compatible")) == "openai_compatible":
            return self._dispatcher.get_client(provider=provider).chat_with_tools(
                messages,
                user_perm=user_perm,
                temperature=temperature,
                max_tool_steps=max_tool_steps,
            )
        base_len = len(messages)
        reply, delta, steps, trace = self._process_tool_loop(
            messages,
            user_perm,
            temperature,
            max_tool_steps,
            base_len,
            "MultiProviderToolClient.chat_with_tools",
        )
        return {"reply": reply, "history_delta": delta, "tool_steps": steps, "tool_trace": trace}

    def stream_chat_with_tools(
        self,
        messages: list[dict],
        user_perm: int,
        temperature: float = 0.7,
        max_tool_steps: int = 6,
    ):
        provider = self._dispatcher.current_provider
        row = self._dispatcher._provider(provider)
        if str(row.get("adapter", "openai_compatible")) == "openai_compatible":
            yield from self._dispatcher.get_client(provider=provider).stream_chat_with_tools(
                messages,
                user_perm=user_perm,
                temperature=temperature,
                max_tool_steps=max_tool_steps,
            )
            return
        base_len = len(messages)
        reply, working_msgs, steps, trace = self._process_tool_loop(
            messages,
            user_perm,
            temperature,
            max_tool_steps,
            base_len,
            "MultiProviderToolClient.stream_chat_with_tools",
            return_working=True,
        )
        yield {"type": "delta", "content": reply}
        yield {"type": "done", **_build_done(reply, working_msgs, base_len, steps, trace)}

    def _process_tool_loop(
        self,
        messages: list[dict],
        user_perm: int,
        temperature: float,
        max_tool_steps: int,
        base_len: int,
        func: str,
        *,
        return_working: bool = False,
    ):
        msgs = [m.copy() if isinstance(m, dict) else m for m in messages]
        trace: list[dict] = []
        steps = 0
        while True:
            force_finalize = steps >= max_tool_steps - 1 and len(trace) > 0
            text, calls, reasoning, usage = self._provider_tool_completion(
                msgs,
                user_perm=user_perm,
                temperature=temperature,
                force_finalize=force_finalize,
                func=func,
            )
            if not calls:
                msg_out = {"role": "assistant", "content": text}
                if reasoning:
                    msg_out["reasoning_content"] = reasoning
                msgs.append(msg_out)
                if return_working:
                    return text, msgs, steps, trace
                return text, msgs[base_len:], steps, trace

            normalized_calls = []
            for idx, call in enumerate(calls):
                normalized_calls.append(_normalize_tool_call(
                    call_id=str(call.get("id", "") or f"call_{steps}_{idx}"),
                    function_name=str(call.get("name", "") or ""),
                    function_arguments=call.get("arguments", {}),
                    fallback_id=f"call_{steps}_{idx}",
                ))
            msg_out = {"role": "assistant", "content": text, "tool_calls": normalized_calls}
            if reasoning:
                msg_out["reasoning_content"] = reasoning
            msgs.append(msg_out)
            step_trace, has_pending_confirmation = self._run_tools(normalized_calls, user_perm, msgs, trace, func)
            steps += 1
            if step_trace and all(_tool_failed(s) for s in step_trace):
                continue
            if has_pending_confirmation:
                pending_reply = text if str(text or "").strip() else ""
                if return_working:
                    return pending_reply, msgs, steps, trace
                return pending_reply, msgs[base_len:], steps, trace
            if steps >= max_tool_steps:
                fallback = _build_tool_limit_fallback(trace)
                msgs.append({"role": "assistant", "content": fallback})
                if return_working:
                    return fallback, msgs, steps, trace
                return fallback, msgs[base_len:], steps, trace

    def _run_tools(self, tool_calls: list[dict], user_perm: int, msgs: list[dict], trace: list[dict], func: str) -> tuple[list[dict], bool]:
        steps: list[dict] = []
        has_pending_confirmation = False
        for call in tool_calls:
            fn = call.get("function", {}) if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name", "") or "")
            args = _parse_json(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments")
            if not isinstance(args, dict):
                args = {}
            model_id = str(call.get("id", "") or "")
            event_id = audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_start tool={name}",
                                   extra={"tool_name": name, "has_arguments": bool(args), "model_id": model_id})
            call_id = f"tc_{event_id:010d}"
            raw = tool_registry.run_agent_tool(name, user_perm, args, call_id=call_id)
            parsed = _parse_json(raw)
            user_safe = parsed if isinstance(parsed, dict) else {"ok": True, "output": raw}
            model_content = raw
            if isinstance(user_safe, dict):
                user_safe.setdefault("call_id", call_id)
                if _result_has_pending_confirmation(user_safe):
                    has_pending_confirmation = True
                if user_safe.get("error_code") == "permission_denied" and user_safe.get("expose_to_user") is False:
                    model_view = dict(user_safe)
                    model_view["error"] = user_safe.get("llm_message") or user_safe.get("error") or "permission denied"
                    model_content = json.dumps(model_view, ensure_ascii=False)
                    safe = dict(user_safe)
                    safe["error"] = user_safe.get("user_message") or "该工具当前不可用，请尝试其它方式。"
                    for key in ("llm_message", "missing_perm_labels", "required_perm_labels",
                                "required_perm_bits", "user_perm", "user_perm_labels"):
                        safe.pop(key, None)
                    user_safe = safe
            trace.append({"agent_tool": name, "call_id": call_id, "tool_call_id": model_id,
                          "arguments": args, "result": user_safe, "raw_result": raw})
            steps.append({"agent_tool": name, "call_id": call_id, "tool_call_id": model_id,
                          "arguments": args, "result": user_safe, "raw_result": raw})
            msgs.append({"role": "tool", "tool_call_id": model_id, "name": name, "content": model_content})
            audit_event("TOOL_EXECUTE", "ai", func, _THIS_FILE, f"tool_done tool={name}",
                        extra={"tool_name": name, "ok": True, "call_id": call_id})
        return steps, has_pending_confirmation

    def _provider_tool_completion(
        self,
        msgs: list[dict],
        *,
        user_perm: int,
        temperature: float,
        force_finalize: bool,
        func: str,
    ) -> tuple[str, list[dict], str, Any]:
        provider = self._dispatcher._provider(self._dispatcher.current_provider)
        adapter = str(provider.get("adapter", "openai_compatible"))
        if adapter == "anthropic_messages":
            return self._anthropic_tool_completion(provider, msgs, user_perm=user_perm, temperature=temperature,
                                                   force_finalize=force_finalize, func=func)
        if adapter == "google_generate_content":
            return self._google_tool_completion(provider, msgs, user_perm=user_perm, temperature=temperature,
                                                force_finalize=force_finalize, func=func)
        raise ValueError(f"unsupported tool adapter: {adapter}")

    def _anthropic_tool_completion(
        self,
        provider: dict[str, Any],
        msgs: list[dict],
        *,
        user_perm: int,
        temperature: float,
        force_finalize: bool,
        func: str,
    ) -> tuple[str, list[dict], str, Any]:
        api_key = resolve_api_key(provider)
        if not api_key:
            raise ValueError("缺少 Anthropic API Key")
        tools = [] if force_finalize else self._anthropic_tools(user_perm)
        body = self._anthropic_messages_body(provider, msgs, temperature=temperature, tools=tools)
        payload = {"provider": provider.get("key"), "adapter_payload": body, "model": body.get("model")}
        record_llm_request(payload, source=func, stream=False)
        data = _json_request(
            _join_url(str(provider.get("base_url") or "https://api.anthropic.com"), str(provider.get("chat_path") or "/v1/messages"), default_path="/v1/messages"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": str(provider.get("anthropic_version") or "2023-06-01"),
            },
            payload=body,
            timeout=45,
        )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        record_llm_response_usage(usage)
        text_parts: list[str] = []
        calls: list[dict] = []
        for item in data.get("content", []) if isinstance(data.get("content"), list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text_parts.append(str(item.get("text") or ""))
            elif item.get("type") == "tool_use":
                calls.append({
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "arguments": item.get("input") if isinstance(item.get("input"), dict) else {},
                })
        return "\n".join(x for x in text_parts if x).strip(), calls, "", usage

    def _google_tool_completion(
        self,
        provider: dict[str, Any],
        msgs: list[dict],
        *,
        user_perm: int,
        temperature: float,
        force_finalize: bool,
        func: str,
    ) -> tuple[str, list[dict], str, Any]:
        api_key = resolve_api_key(provider)
        if not api_key:
            raise ValueError("缺少 Google/Gemini API Key")
        body = self._google_messages_body(provider, msgs, user_perm=user_perm, temperature=temperature,
                                          force_finalize=force_finalize)
        model = str(provider.get("current_model") or provider.get("default_model") or "")
        payload = {"provider": provider.get("key"), "adapter_payload": body, "model": model}
        record_llm_request(payload, source=func, stream=False)
        path = str(provider.get("chat_path") or "/models/{model}:generateContent").replace("{model}", urllib.parse.quote(model, safe=""))
        data = _json_request(
            _join_url(str(provider.get("base_url") or "https://generativelanguage.googleapis.com/v1beta"), path, default_path="/models/{model}:generateContent"),
            headers={"x-goog-api-key": api_key},
            payload=body,
            timeout=45,
        )
        usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        record_llm_response_usage(usage)
        text_parts: list[str] = []
        calls: list[dict] = []
        candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", []) if isinstance(candidates[0], dict) else []
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict):
                    continue
                if "text" in part:
                    text_parts.append(str(part.get("text") or ""))
                fc = part.get("functionCall")
                if isinstance(fc, dict):
                    calls.append({
                        "id": str(fc.get("id") or fc.get("name") or f"google_call_{len(calls)}"),
                        "name": str(fc.get("name") or ""),
                        "arguments": fc.get("args") if isinstance(fc.get("args"), dict) else {},
                    })
        return "\n".join(x for x in text_parts if x).strip(), calls, "", usage

    def _anthropic_messages_body(self, provider: dict[str, Any], msgs: list[dict], *, temperature: float, tools: list[dict]) -> dict[str, Any]:
        system_parts: list[str] = []
        out_messages: list[dict[str, Any]] = []
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "") or "")
            if role == "system":
                text = LlmDispatcher._content_to_text(msg.get("content"))
                if text:
                    system_parts.append(text)
            elif role in {"user", "assistant"}:
                content = self._anthropic_content_from_message(msg)
                if content:
                    out_messages.append({"role": role, "content": content})
            elif role == "tool":
                out_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": str(msg.get("tool_call_id", "")),
                        "content": str(msg.get("content", "")),
                    }],
                })
        body: dict[str, Any] = {
            "model": str(provider.get("current_model") or provider.get("default_model") or ""),
            "messages": out_messages,
            "max_tokens": 1200,
            "temperature": float(temperature),
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if tools:
            body["tools"] = tools
        return body

    def _anthropic_content_from_message(self, msg: dict) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        text = LlmDispatcher._content_to_text(msg.get("content"))
        if text:
            parts.append({"type": "text", "text": text})
        for call in msg.get("tool_calls", []) if isinstance(msg.get("tool_calls"), list) else []:
            fn = call.get("function", {}) if isinstance(call, dict) and isinstance(call.get("function"), dict) else {}
            args = _parse_json(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments")
            parts.append({
                "type": "tool_use",
                "id": str(call.get("id", "")),
                "name": str(fn.get("name", "")),
                "input": args if isinstance(args, dict) else {},
            })
        return parts

    def _google_messages_body(
        self,
        provider: dict[str, Any],
        msgs: list[dict],
        *,
        user_perm: int,
        temperature: float,
        force_finalize: bool,
    ) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "") or "")
            if role == "system":
                text = LlmDispatcher._content_to_text(msg.get("content"))
                if text:
                    contents.append({"role": "user", "parts": [{"text": text}]})
                continue
            if role == "tool":
                tool_name = str(msg.get("name") or msg.get("tool_name") or msg.get("tool_call_id", "tool"))
                contents.append({
                    "role": "function",
                    "parts": [{
                        "functionResponse": {
                            "name": tool_name,
                            "response": {"result": str(msg.get("content", ""))},
                        },
                    }],
                })
                continue
            parts: list[dict[str, Any]] = []
            text = LlmDispatcher._content_to_text(msg.get("content"))
            if text:
                parts.append({"text": text})
            for call in msg.get("tool_calls", []) if isinstance(msg.get("tool_calls"), list) else []:
                fn = call.get("function", {}) if isinstance(call, dict) and isinstance(call.get("function"), dict) else {}
                args = _parse_json(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments")
                parts.append({
                    "functionCall": {
                        "name": str(fn.get("name", "")),
                        "args": args if isinstance(args, dict) else {},
                    },
                })
            if parts:
                contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": float(temperature),
                "maxOutputTokens": 1200,
            },
        }
        if not force_finalize:
            body["tools"] = [{"functionDeclarations": self._google_function_declarations(user_perm)}]
        return body

    @staticmethod
    def _anthropic_tools(user_perm: int) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for schema in tool_registry.build_agent_tool_schemas(user_perm):
            fn = schema.get("function", {}) if isinstance(schema, dict) else {}
            tools.append({
                "name": str(fn.get("name", "")),
                "description": str(fn.get("description", "")),
                "input_schema": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object"},
            })
        return tools

    @staticmethod
    def _google_function_declarations(user_perm: int) -> list[dict[str, Any]]:
        declarations: list[dict[str, Any]] = []
        for schema in tool_registry.build_agent_tool_schemas(user_perm):
            fn = schema.get("function", {}) if isinstance(schema, dict) else {}
            parameters = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object"}
            declarations.append({
                "name": str(fn.get("name", "")),
                "description": str(fn.get("description", "")),
                "parameters": parameters,
            })
        return declarations


class LlmDispatcher:
    """统一 LLM 调用入口。

    主聊天仍复用 LLMClient 的 OpenAI-compatible 工具流。
    模型检测和模型数据面板通过 provider registry 选择专属 adapter。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self._bootstrap_api_key = api_key
        self._bootstrap_base_url = base_url
        self._bootstrap_model = default_model
        self._primary_client: LLMClient | None = None
        self._tool_client = MultiProviderToolClient(self)
        self._clients: dict[tuple[str, str, str], LLMClient] = {}
        self.reload()

    def reload(self) -> None:
        self._config = load_llm_provider_config()
        self._current_provider = str(self._config.get("current_provider") or "deepseek")
        deepseek = self._provider("deepseek")
        if self._bootstrap_api_key:
            deepseek["api_key"] = self._bootstrap_api_key
        if self._bootstrap_base_url:
            deepseek["base_url"] = self._bootstrap_base_url
        if self._bootstrap_model:
            deepseek["current_model"] = self._bootstrap_model
        self.api_key = resolve_api_key(deepseek)
        self.base_url = str(deepseek.get("base_url") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
        current = self._provider(self._current_provider)
        self._current_model = str(current.get("current_model") or current.get("default_model") or "")
        if not self._current_model:
            self._current_model = str(deepseek.get("current_model") or deepseek.get("default_model") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"))
        self._deepseek_model = str(deepseek.get("current_model") or deepseek.get("default_model") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"))
        if self._primary_client is not None:
            self._primary_client.api_key = self.api_key
            self._primary_client.base_url = self.base_url
            self._primary_client.model = self._deepseek_model

    @staticmethod
    def normalize_provider(raw: object = None) -> str:
        text = str(raw or "").strip().lower()
        if not text:
            return "deepseek"
        if text in {"deepseek", "dpsk", "ds"}:
            return "deepseek"
        return text

    @property
    def current_model(self) -> str:
        return self._current_model

    @property
    def current_provider(self) -> str:
        return self._current_provider

    @property
    def primary_client(self) -> LLMClient:
        if self._primary_client is None:
            self._primary_client = LLMClient(
                api_key=self.api_key,
                base_url=self.base_url,
                model=self._deepseek_model,
            )
        return self._primary_client

    @property
    def tool_client(self) -> MultiProviderToolClient:
        return self._tool_client

    @property
    def deepseek_api_root(self) -> str:
        return _api_root_from_base_url(self.base_url)

    def _provider(self, provider: str = "deepseek") -> dict[str, Any]:
        key = self.normalize_provider(provider)
        providers = self._config.get("providers") if isinstance(self._config.get("providers"), dict) else {}
        if key not in providers:
            raise ValueError(f"unsupported llm provider: {key}")
        row = providers[key]
        return row if isinstance(row, dict) else {}

    def public_providers(self) -> list[dict[str, Any]]:
        return [
            public_provider(row, current_provider=self._current_provider)
            for row in self._config.get("providers", {}).values()
            if isinstance(row, dict)
        ]

    def available_models(self, provider: str | None = None, *, chat_compatible_only: bool = False) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        providers = self._config.get("providers") if isinstance(self._config.get("providers"), dict) else {}
        provider_keys = [self.normalize_provider(provider)] if provider else list(providers.keys())
        for key in provider_keys:
            row = providers.get(key)
            if not isinstance(row, dict):
                continue
            if chat_compatible_only and str(row.get("adapter", "")) not in {"openai_compatible"}:
                continue
            for model in row.get("models", []) if isinstance(row.get("models"), list) else []:
                model_id = str(model.get("id", "") if isinstance(model, dict) else model).strip()
                if not model_id:
                    continue
                label = str(model.get("label", model_id) if isinstance(model, dict) else model_id)
                rows.append({"id": model_id, "label": label, "provider": key})
        return rows

    def get_client(
        self,
        *,
        model: str | None = None,
        provider: str = "deepseek",
        purpose: str = "chat",
    ) -> LLMClient:
        provider_key = self.normalize_provider(provider)
        row = self._provider(provider_key)
        if str(row.get("adapter", "openai_compatible")) != "openai_compatible":
            raise ValueError(f"provider {provider_key} does not support LLMClient tool chat")
        target_model = str(model or row.get("current_model") or row.get("default_model") or "").strip()
        if not target_model:
            raise ValueError("missing llm model")
        if provider_key == "deepseek" and model is None:
            return self.primary_client
        api_key = resolve_api_key(row)
        base_url = str(row.get("base_url") or "").strip()
        cache_key = (provider_key, base_url, target_model)
        if cache_key not in self._clients:
            self._clients[cache_key] = LLMClient(
                api_key=api_key,
                base_url=base_url,
                model=target_model,
            )
        return self._clients[cache_key]

    def switch_model(self, model: str, *, provider: str = "deepseek") -> LLMClient:
        provider_key = self.normalize_provider(provider)
        row = self._provider(provider_key)
        target = str(model or "").strip()
        if not target:
            raise ValueError("missing llm model")
        row["current_model"] = target
        self._config["current_provider"] = provider_key
        save_llm_provider_config(self._config)
        self.reload()
        if provider_key == "deepseek" and self._primary_client is not None:
            self._primary_client.model = target
        if str(row.get("adapter", "openai_compatible")) == "openai_compatible":
            return self.get_client(model=None if provider_key == "deepseek" else target, provider=provider_key)
        return self.primary_client

    def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        provider: str = "deepseek",
        purpose: str = "chat",
        temperature: float = 0.7,
    ) -> str:
        return self.get_client(model=model, provider=provider, purpose=purpose).chat(messages, temperature=temperature)

    def create_completion(
        self,
        payload: dict[str, Any],
        *,
        provider: str = "deepseek",
        purpose: str = "llm_dispatcher",
        stream: bool = False,
    ) -> Any:
        provider_key = self.normalize_provider(provider)
        row = self._provider(provider_key)
        adapter = str(row.get("adapter", "openai_compatible"))
        request_payload = dict(payload)
        target_model = str(request_payload.get("model") or row.get("current_model") or row.get("default_model") or "").strip()
        if not target_model:
            raise ValueError("missing llm model")
        request_payload["model"] = target_model
        if adapter == "openai_compatible":
            return self._create_openai_compatible(row, request_payload, purpose=purpose, stream=stream)
        if adapter == "anthropic_messages":
            return self._create_anthropic(row, request_payload, purpose=purpose)
        if adapter == "google_generate_content":
            return self._create_google(row, request_payload, purpose=purpose)
        raise ValueError(f"unsupported llm adapter: {adapter}")

    def _create_openai_compatible(self, provider: dict[str, Any], payload: dict[str, Any], *, purpose: str, stream: bool) -> Any:
        api_key = resolve_api_key(provider)
        if not api_key:
            raise ValueError(f"缺少 {provider.get('label') or provider.get('key')} API Key")
        payload = dict(payload)
        if provider.get("reasoning_effort"):
            payload.setdefault("reasoning_effort", provider.get("reasoning_effort"))
        if isinstance(provider.get("extra_body"), dict):
            payload.setdefault("extra_body", provider.get("extra_body"))
        record_llm_request(payload, source=purpose, stream=stream)
        chat_path = str(provider.get("chat_path") or "/chat/completions").strip()
        uses_default_sdk_path = chat_path in {"", "/chat/completions", "chat/completions"}
        if not stream and not uses_default_sdk_path:
            data = _json_request(
                _join_url(str(provider.get("base_url") or "").strip(), chat_path, default_path="/chat/completions"),
                headers={"Authorization": f"Bearer {api_key}"},
                payload=payload,
                timeout=int(payload.get("timeout") or 25),
            )
            resp = _openai_http_response(data)
        elif self.normalize_provider(provider.get("key")) == "deepseek":
            client = self.get_client(model=str(payload.get("model")), provider="deepseek", purpose=purpose)
            resp = client._client.chat.completions.create(**payload)
        else:
            client = OpenAI(api_key=api_key, base_url=str(provider.get("base_url") or "").strip())
            resp = client.chat.completions.create(**payload)
        record_llm_response_usage(getattr(resp, "usage", None))
        return resp

    def _create_anthropic(self, provider: dict[str, Any], payload: dict[str, Any], *, purpose: str) -> Any:
        api_key = resolve_api_key(provider)
        if not api_key:
            raise ValueError("缺少 Anthropic API Key")
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        system_parts: list[str] = []
        body_messages: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "")).strip()
            content = self._content_to_text(msg.get("content"))
            if role == "system":
                if content:
                    system_parts.append(content)
            elif role in {"user", "assistant"}:
                body_messages.append({"role": role, "content": content})
        body = {
            "model": str(payload.get("model")),
            "messages": body_messages,
            "max_tokens": int(payload.get("max_tokens") or 120),
            "temperature": float(payload.get("temperature", 0.2)),
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        record_llm_request({**payload, "provider": provider.get("key"), "adapter_payload": body}, source=purpose, stream=False)
        base = str(provider.get("base_url") or "https://api.anthropic.com").rstrip("/")
        path = str(provider.get("chat_path") or "/v1/messages")
        data = _json_request(
            _join_url(base, path, default_path="/v1/messages"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": str(provider.get("anthropic_version") or "2023-06-01"),
            },
            payload=body,
            timeout=int(payload.get("timeout") or 25),
        )
        text = ""
        for item in data.get("content", []) if isinstance(data.get("content"), list) else []:
            if isinstance(item, dict) and item.get("type") == "text":
                text += str(item.get("text") or "")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        record_llm_response_usage(usage)
        return _choice_response(text, usage=usage)

    def _create_google(self, provider: dict[str, Any], payload: dict[str, Any], *, purpose: str) -> Any:
        api_key = resolve_api_key(provider)
        if not api_key:
            raise ValueError("缺少 Google/Gemini API Key")
        model = str(payload.get("model") or "").strip()
        contents = []
        for msg in payload.get("messages", []) if isinstance(payload.get("messages"), list) else []:
            if not isinstance(msg, dict):
                continue
            role = "model" if msg.get("role") == "assistant" else "user"
            text = self._content_to_text(msg.get("content"))
            if text:
                contents.append({"role": role, "parts": [{"text": text}]})
        body = {
            "contents": contents,
            "generationConfig": {
                "temperature": float(payload.get("temperature", 0.2)),
                "maxOutputTokens": int(payload.get("max_tokens") or 120),
            },
        }
        record_llm_request({**payload, "provider": provider.get("key"), "adapter_payload": body}, source=purpose, stream=False)
        path = str(provider.get("chat_path") or "/models/{model}:generateContent").replace("{model}", urllib.parse.quote(model, safe=""))
        data = _json_request(
            _join_url(str(provider.get("base_url") or "https://generativelanguage.googleapis.com/v1beta"), path, default_path="/models/{model}:generateContent"),
            headers={"x-goog-api-key": api_key},
            payload=body,
            timeout=int(payload.get("timeout") or 25),
        )
        text = ""
        candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", []) if isinstance(candidates[0], dict) else []
            for part in parts if isinstance(parts, list) else []:
                if isinstance(part, dict):
                    text += str(part.get("text") or "")
        usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        record_llm_response_usage(usage)
        return _choice_response(text, usage=usage)

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text") or ""))
                    elif "text" in item:
                        parts.append(str(item.get("text") or ""))
                elif item is not None:
                    parts.append(str(item))
            return "\n".join(x for x in parts if x)
        if content is None:
            return ""
        return str(content)

    def provider_meta(self, provider: str = "deepseek") -> dict[str, Any]:
        return public_provider(self._provider(provider), current_provider=self._current_provider)

    def providers_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "current_provider": self._current_provider,
            "providers": self.public_providers(),
        }

    def upsert_provider(self, data: dict[str, Any]) -> dict[str, Any]:
        self._config = upsert_provider(self._config, data)
        self.reload()
        return self.providers_payload()

    def add_model(self, provider: str, model_id: str, label: str = "") -> dict[str, Any]:
        self._config = upsert_model(self._config, provider, model_id, label)
        self.reload()
        return self.providers_payload()

    def remove_model(self, provider: str, model_id: str) -> dict[str, Any]:
        self._config = remove_model(self._config, provider, model_id)
        self.reload()
        return self.providers_payload()

    def fetch_balance(self, provider: str, *, mask_api_key=_mask_api_key) -> dict[str, Any]:
        provider_key = self.normalize_provider(provider)
        if provider_key != "deepseek":
            return {
                **self.provider_meta(provider_key),
                "ok": False,
                "key_masked": mask_api_key(resolve_api_key(self._provider(provider_key))),
                "error": "当前 provider 暂无余额查询接口",
            }
        return self.fetch_deepseek_balance(mask_api_key=mask_api_key)

    def fetch_deepseek_balance(self, *, mask_api_key=_mask_api_key) -> dict[str, Any]:
        provider = self._provider("deepseek")
        api_key = resolve_api_key(provider)
        if not api_key:
            return {**self.provider_meta("deepseek"), "ok": False, "error": "缺少 DEEPSEEK_API_KEY"}
        api_root = _api_root_from_base_url(str(provider.get("base_url") or self.base_url))
        path = str(provider.get("balance_path") or "/user/balance")
        req = urllib.request.Request(
            api_root + path,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            balance_infos = data.get("balance_infos") if isinstance(data, dict) else []
            if not isinstance(balance_infos, list):
                balance_infos = []
            return {
                **self.provider_meta("deepseek"),
                "ok": True,
                "key_masked": mask_api_key(api_key),
                "is_available": bool(data.get("is_available", False)) if isinstance(data, dict) else False,
                "balance_infos": balance_infos,
                "raw": data,
            }
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
            return {
                **self.provider_meta("deepseek"),
                "ok": False,
                "key_masked": mask_api_key(api_key),
                "status": int(getattr(e, "code", 0) or 0),
                "error": detail or str(e),
            }
        except Exception as e:
            return {
                **self.provider_meta("deepseek"),
                "ok": False,
                "key_masked": mask_api_key(api_key),
                "error": str(e),
            }
