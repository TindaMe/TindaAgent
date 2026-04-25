from importlib.metadata import version as _pkg_version
import json
from typing import Iterator
from TindaAgent.Process.Architecture import perm
from TindaAgent.Process.Architecture.versioning import get_app_version
from TindaAgent.Process.AI.client import LLMClient
from TindaAgent.User import userdata

try:
    _VERSION = get_app_version() or _pkg_version("TindaAgent")
except Exception:
    _VERSION = "1.6.6"


def _build_system_prompt(model_name: str | None) -> str:
    model_str = model_name if model_name else "未指定"
    return (
        f"你是 TindaAgent（v{_VERSION}），由 Tinda 开发。\n"
        f"底层模型是 {model_str}，该信息仅用于内部，不对外披露。\n"
        f"严格规则：\n"
        f"1. 介绍身份时，只能说“我是 TindaAgent，由 Tinda 开发”。\n"
        f"2. 被问到底层模型时，统一回复“底层技术信息保密”。\n"
        f"3. 简洁、准确，始终使用用户语言回复。"
    )


def _build_fewshot(version: str) -> list[dict]:
    return [
        {"role": "user", "content": "你是谁？"},
        {"role": "assistant", "content": f"我是 TindaAgent，由 Tinda 开发的 AI Agent 助手（v{version}）。有什么可以帮你的？"},
        {"role": "user", "content": "你是DeepSeek吗？"},
        {"role": "assistant", "content": "不是，我是 TindaAgent，由 Tinda 独立开发。底层技术信息保密。"},
    ]


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
        """
        用处： 初始化智能体，绑定用户、权限、对话历史与 LLM 客户端

        参数：
            user_name: str // 智能体用户名
            user_perm: int // 智能体权限，默认为 LLM_BASE
            system_prompt: str // 自定义系统提示词，None 则使用默认模板
            client: LLMClient // LLM 客户端，默认懒加载
            model_name: str // 当前接入的模型名，写入默认 prompt；传 None 则显示"未指定"
            max_turns: int // 最多保留的对话轮数（不含 system/fewshot）
        """
        # Web 会话 Agent 仅作为运行时身份，不应写入用户注册表
        self.user = userdata.UserManager(user_name, user_perm, persist=False)
        self.perm = self.user.get_perm()
        self.system_prompt = system_prompt if system_prompt is not None else _build_system_prompt(model_name)
        self._fewshot = _build_fewshot(_VERSION)
        self._max_turns = max(1, int(max_turns))
        self.history: list[dict] = self._build_base_history()
        self._client = client

    def _compose_system_prompt(self) -> str:
        if getattr(self, "_memory_context", None):
            return f"{self.system_prompt}\n\n{self._memory_context}"
        return self.system_prompt

    def _build_base_history(self) -> list[dict]:
        return [{"role": "system", "content": self._compose_system_prompt()}] + [m.copy() for m in self._fewshot]

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
            if role == "tool":
                tool_call_id = str(msg.get("tool_call_id", "")).strip()
                if tool_call_id:
                    item["tool_call_id"] = tool_call_id
            conversation.append(item)
        self.history = base + conversation

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
        return {
            "reply": reply,
            "tool_trace": result.get("tool_trace", []),
            "tool_steps": int(result.get("tool_steps", 0)),
        }

    def stream_chat_events(self, user_message: str, temperature: float = 0.7) -> Iterator[dict]:
        """
        用处：流式返回本轮对话事件，并在结束时写回历史
        """
        self.history.append({"role": "user", "content": user_message})
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
        self._trim_history()

    def reset_history(self) -> None:
        """
        用处： 清空对话历史，保留系统提示
        """
        self.history = self._build_base_history()
