from importlib.metadata import version as _pkg_version
import json
from typing import Callable, Iterator
from TindaAgent.Process.Architecture import perm
from TindaAgent.Process.Architecture.versioning import get_app_version
from TindaAgent.Process.AI.client import LLMClient, _trace_has_pending_confirmation
from TindaAgent.Process.AI.tokenizer import estimate_messages_tokens
from TindaAgent.User import userdata

try:
    _VERSION = get_app_version() or _pkg_version("TindaAgent")
except Exception:
    _VERSION = "1.7.16"


def _build_system_prompt(model_name: str | None) -> str:
    model_str = model_name if model_name else "unspecified"
    return (
        f"You are TindaAgent (v{_VERSION}), developed by Tinda.\n"
        f"The underlying model is {model_str}. This is internal-only and must not be disclosed publicly.\n"
        f"\n"
        f"Identity examples (these are NOT conversation history):\n"
        f"- Q: Who are you? -> A: I am TindaAgent, an AI agent assistant developed by Tinda (v{_VERSION}). How can I help?\n"
        f"- Q: Are you DeepSeek? -> A: No, I am TindaAgent, independently developed by Tinda. Underlying technical details are confidential.\n"
        f"\n"
        f"Strict rules:\n"
        f"1. When introducing yourself, only say: \"I am TindaAgent, developed by Tinda.\"\n"
        f"2. If asked about the underlying model, always reply: \"Underlying technical details are confidential.\"\n"
        f"3. Be concise and accurate. Always respond in the same language as the user.\n"
        f"4. You are a powerful agent assistant with access to tools. Use them when needed.\n"
        f"5. TOOL CALLS MUST use the native tool_calls / function-calling API. Never describe, simulate, or fabricate tool invocations in your text response. If you need a tool, emit a real tool_call. If you cannot use tools for a request, explain why in natural language — do not pretend to have executed one.\n"
        f"6. Never quote previous tool-call records or assume tool outputs. Everything must be based on actual tool results.\n"
        f"7. Fabrication of any kind is strictly forbidden.\n"
        f"8. For complex tasks, use note= to describe each step. Chain related commands with && ; | || in a single run_terminal call when appropriate."
    )


class Agent:
    def __init__(
        self,
        user_name: str,
        user_perm: int = perm.LLM_BASE,
        system_prompt: str = None,
        client: LLMClient = None,
        model_name: str = None,
        max_turns: int = 12,
    ) -> None:
        self.user = userdata.UserManager(user_name, user_perm, persist=False)
        self.perm = self.user.get_perm()
        self.system_prompt = system_prompt if system_prompt is not None else _build_system_prompt(model_name)
        self._max_turns = max(1, int(max_turns))
        self.history: list[dict] = self._build_base_history()
        self._client = client
        self._held_messages: list[dict] | None = None
        self._held_perm: int = 0
        self._context_logger: Callable[[list[dict], str], None] | None = None
        self.max_context_tokens: int = 16000
        self._tokens: int = 0
        self._refresh_tokens()

    def _compose_system_prompt(self) -> str:
        if getattr(self, "_memory_context", None):
            return f"{self.system_prompt}\n\n{self._memory_context}"
        return self.system_prompt

    def _build_base_history(self) -> list[dict]:
        return [{"role": "system", "content": self._compose_system_prompt()}]

    def _refresh_tokens(self) -> None:
        self._tokens = int(estimate_messages_tokens(self.history))

    def estimate_current_tokens(self) -> int:
        return int(self._tokens)

    def set_memory_context(self, memory_payload: dict) -> None:
        """
        用处：设置每轮请求前注入的记忆上下文，并重建当前基座消息。
        """
        payload = memory_payload if isinstance(memory_payload, dict) else {"version": 1, "items": []}
        memory_json = json.dumps(payload, ensure_ascii=False)
        self._memory_context = (
            "[MEMORY_POLICY]\n"
            "你可自行判断是否调用 save_memory 写入长期记忆。仅写入长期有价值、稳定、可复用的信息；"
            "闲聊、一次性任务过程、临时情绪不应写入。\n"
            "[MEMORY_CONTEXT_JSON]\n"
            f"{memory_json}"
        )
        conv = self.get_conversation_messages()
        self.replace_conversation(conv)

    def _trim_history(self) -> None:
        """
        用处：限制历史长度，降低 token 开销并避免长上下文漂移
        """
        base = self._build_base_history()
        base_len = len(base)
        if len(self.history) <= base_len:
            return

        conversation = self.history[base_len:]
        user_indexes = [i for i, m in enumerate(conversation) if m.get("role") == "user"]
        if len(user_indexes) <= self._max_turns:
            return

        start_idx = user_indexes[-self._max_turns]
        self.history = base + conversation[start_idx:]
        self._refresh_tokens()

    def get_conversation_messages(self) -> list[dict]:
        """
        用处：导出当前对话消息（不含 system/fewshot 基座）
        """
        base_len = len(self._build_base_history())
        return [m.copy() for m in self.history[base_len:]]

    def replace_conversation(self, messages: list[dict]) -> None:
        """
        用处：用外部消息替换当前对话（保留 system/fewshot 基座）
        """
        # 外部会话回灌会刷新对话上下文，挂起确认状态必须失效以避免跨上下文误执行。
        self._held_messages = None
        self._held_perm = 0
        base = self._build_base_history()
        conversation: list[dict] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "")).strip()
            if role not in {"user", "assistant", "tool"}:
                continue
            content = str(msg.get("content", ""))
            if role in {"user", "assistant"} and not content.strip():
                continue
            item = {"role": role, "content": content}
            if role == "assistant":
                rc = msg.get("reasoning_content")
                if rc is not None:
                    item["reasoning_content"] = rc
            if role == "tool":
                tool_call_id = str(msg.get("tool_call_id", "")).strip()
                if tool_call_id:
                    item["tool_call_id"] = tool_call_id
            conversation.append(item)
        self.history = base + conversation
        self._refresh_tokens()

    def _ensure_client(self) -> LLMClient:
        """懒加载 LLM 客户端"""
        if self._client is None:
            self._client = LLMClient()
        return self._client

    def check_perm(self, task: str) -> bool:
        """
        用处： 检查用户是否有执行任务的权限

        参数：
            task: str // 任务的名称
        """
        task_perm = perm.PermManager.task_dict.get(task)
        if task_perm is None:
            return False
        if (self.perm & task_perm) == task_perm:
            return True
        return False

    def chat(self, user_message: str, temperature: float = 0.7) -> str:
        """
        用处： 发起一次多轮对话，自动维护历史

        参数：
            user_message: str // 用户输入
            temperature: float // 采样温度

        返回：
            str // 模型回复
        """
        self.history.append({"role": "user", "content": user_message})
        result = self._ensure_client().chat_with_tools(
            self.history,
            user_perm=self.perm,
            temperature=temperature,
        )
        delta = result.get("history_delta", [])
        if delta:
            self.history.extend(delta)
        reply = str(result.get("reply", ""))
        self._trim_history()
        return reply

    def chat_with_meta(self, user_message: str, temperature: float = 0.7) -> dict:
        """
        用处：发起对话并返回回复 + 工具轨迹元信息（给 Web 层调试展示）
        """
        self._held_messages = None
        self.history.append({"role": "user", "content": user_message})
        if self._context_logger is not None:
            try:
                self._context_logger(self.history, "llm_request")
            except Exception:
                pass
        result = self._ensure_client().chat_with_tools(
            self.history,
            user_perm=self.perm,
            temperature=temperature,
        )
        delta = result.get("history_delta", [])
        if delta:
            self.history.extend(delta)
        reply = str(result.get("reply", ""))
        trace = result.get("tool_trace", [])
        steps = int(result.get("tool_steps", 0))

        if _trace_has_pending_confirmation(trace):
            self._held_messages = [m.copy() for m in self.history]
            self._held_perm = int(self.perm)

        self._trim_history()
        if self._context_logger is not None:
            try:
                self._context_logger(self.history, "llm_response")
            except Exception:
                pass
        return {
            "reply": reply,
            "tool_trace": trace,
            "tool_steps": steps,
            "pending_confirmation": self._held_messages is not None,
        }

    def stream_chat_events(self, user_message: str, temperature: float = 0.7) -> Iterator[dict]:
        """
        用处：流式返回本轮对话事件，并在结束时写回历史
        """
        self._held_messages = None
        self.history.append({"role": "user", "content": user_message})
        if self._context_logger is not None:
            try:
                self._context_logger(self.history, "llm_request")
            except Exception:
                pass
        final_result: dict | None = None

        for event in self._ensure_client().stream_chat_with_tools(
            self.history,
            user_perm=self.perm,
            temperature=temperature,
        ):
            if event.get("type") == "done":
                final_result = event
            yield event

        if final_result is None:
            final_result = {
                "reply": "",
                "history_delta": [{"role": "assistant", "content": ""}],
                "tool_trace": [],
                "tool_steps": 0,
            }

        delta = final_result.get("history_delta", [])
        if delta:
            self.history.extend(delta)

        trace = final_result.get("tool_trace", [])
        if _trace_has_pending_confirmation(trace):
            self._held_messages = [m.copy() for m in self.history]
            self._held_perm = int(self.perm)

        self._trim_history()
        if self._context_logger is not None:
            try:
                self._context_logger(self.history, "llm_response")
            except Exception:
                pass

    def has_pending_confirmation(self) -> bool:
        return self._held_messages is not None

    def resume_with_confirmations(self, decisions: list[dict]) -> dict:
        """
        用处：用户对挂起的终端命令做出决策后，重新执行工具并恢复 LLM 循环。
        decisions: [{"confirm_id": "tcf_xxx", "action": "allow"|"deny"}, ...]
        """
        if not self._held_messages:
            raise RuntimeError("没有挂起的确认请求")
        msgs = [m.copy() for m in self._held_messages]
        self._held_messages = None

        # 最小确认链路：按首条 decision 的 approval（或 action）决策
        approval: bool | None = None
        if isinstance(decisions, list) and decisions:
            first = decisions[0] if isinstance(decisions[0], dict) else {}
            if isinstance(first.get("approval"), bool):
                approval = bool(first.get("approval"))
            else:
                act = str(first.get("action", "")).strip().lower()
                if act in {"allow", "deny"}:
                    approval = act == "allow"

        # 兼容旧结构：将 decisions 按 confirm_id 索引
        decision_map: dict[str, str] = {}
        for d in decisions:
            cid = str(d.get("confirm_id", "")).strip()
            act = str(d.get("action", "deny")).strip().lower()
            if cid:
                decision_map[cid] = "allow" if act == "allow" else "deny"

        # 在 msgs 中找到 tool 消息里含 pending_confirmation 的，重新执行并替换
        import json as _json
        for idx, m in enumerate(msgs):
            if m.get("role") != "tool":
                continue
            content = str(m.get("content", ""))
            try:
                parsed = _json.loads(content)
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue
            inner = parsed.get("result")
            if not isinstance(inner, dict) or not inner.get("pending_confirmation"):
                continue
            cid = str(inner.get("confirm_id", ""))
            action = decision_map.get(cid, "deny")
            cmd = str(inner.get("cmd", ""))
            resolved_approval = approval if isinstance(approval, bool) else (action == "allow")
            from TindaAgent.Tool.tool import run_terminal
            new_result = run_terminal(cmd=cmd, _caller_perm=self._held_perm, _approval=resolved_approval, call_id=cid)
            msgs[idx] = {
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", ""),
                # OpenAI/DeepSeek 兼容接口要求 tool message content 为字符串。
                "content": _json.dumps(new_result, ensure_ascii=False),
            }

        # 检查是否有命令被拒绝执行
        any_denied = False
        for m in msgs:
            if m.get("role") != "tool":
                continue
            try:
                parsed = _json.loads(str(m.get("content", "{}")))
            except Exception:
                continue
            if isinstance(parsed, dict) and parsed.get("error_code") == "user_denied":
                any_denied = True
                break
        if any_denied:
            msgs.append({"role": "system", "content": "The user denied the terminal command execution. You MUST inform the user that the command was not executed and ask if they need anything else. Do NOT call the same or similar tools again unless the user explicitly requests it."})
        else:
            msgs.append({"role": "system", "content": "The terminal command above has been executed per your request. You MUST now respond to the user in natural language: describe what was executed, show the key results, and ask if they need anything else. Do NOT call more tools unless the user explicitly asks for another action."})
        result = self._ensure_client().chat_with_tools(
            msgs,
            user_perm=self._held_perm,
            temperature=0.7,
        )
        msgs.pop()
        delta = result.get("history_delta", [])
        if delta:
            self.history = msgs
            self.history.extend(delta)
        else:
            self.history = msgs
        reply = str(result.get("reply", "")).strip()
        # 如果 LLM 仍然没回复，插入一条基于工具结果的摘要
        if not reply and delta:
            last_tool = None
            for m in reversed(delta):
                if m.get("role") == "tool":
                    try:
                        last_tool = __import__("json").loads(str(m.get("content", "{}")))
                    except Exception:
                        pass
                    break
            if isinstance(last_tool, dict) and last_tool.get("ok") and last_tool.get("output"):
                reply = f"Command executed. Output:\n{str(last_tool.get('output', ''))[:500]}"
            elif isinstance(last_tool, dict) and last_tool.get("ok") is False:
                reply = f"Command failed: {last_tool.get('error', 'unknown error')}"
        trace = result.get("tool_trace", [])
        steps = int(result.get("tool_steps", 0))
        if _trace_has_pending_confirmation(trace):
            self._held_messages = [m.copy() for m in self.history]
            self._held_perm = int(self.perm)
        self._trim_history()
        return {
            "reply": reply,
            "tool_trace": trace,
            "tool_steps": steps,
            "pending_confirmation": self._held_messages is not None,
        }

    def reset_history(self) -> None:
        """
        用处： 清空对话历史，保留系统提示
        """
        self.history = self._build_base_history()
        self._held_messages = None
        self._refresh_tokens()
