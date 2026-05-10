#!/usr/bin/env python3
"""使用 Agent.stream_chat_events 真实工作流 + 新格式会话记录"""

import json
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from TindaAgent.Process.AI.agent import Agent
from TindaAgent.Process.Architecture import perm
from TindaAgent.Process.Observability.audit import get_audit_engine
from TindaAgent.User import userdata

OUTPUT = Path("/mnt/e/Test/session_output.json")

_USER = userdata.UserManager("Tinda", perm.PUBLIC_ALL, persist=False)


def _next_id() -> str:
    return datetime.now().strftime(f"%Y-%-m-%-d-{get_audit_engine().next_id()}")


class SessionRecorder:
    def __init__(self):
        self.records: dict[str, dict] = {}
        self._msg_seq = 0

    def _next_key(self) -> str:
        self._msg_seq += 1
        return str(self._msg_seq)

    def _next_id(self) -> str:
        self._id_seq += 1
        return _fmt_id(self._id_seq)

    def add_user(self, text: str, *, raw: bool = False) -> None:
        content = {"user": text} if raw else {"text": text}
        self.records[self._next_key()] = {
            "role": "user", "id": _next_id(), "content": content,
        }

    def add_system(self, text: str) -> None:
        self.records[self._next_key()] = {
            "role": "system", "id": _next_id(), "content": {"text": text},
        }

    def add_agent_response(self, events: list[dict]) -> None:
        """将 Agent.stream_chat_events 产生的事件转为逻辑子步骤"""
        thinking_parts: list[str] = []
        text_parts: list[str] = []
        tool_results: list[dict] = []
        has_reset = False
        first_text_phase = True

        for ev in events:
            t = ev.get("type", "")

            if t == "delta":
                content = ev.get("content", "")
                if not has_reset and first_text_phase:
                    thinking_parts.append(content)
                else:
                    text_parts.append(content)

            elif t == "reset":
                has_reset = True
                first_text_phase = False

            elif t == "tool_step":
                for tr in ev.get("trace", []):
                    name = tr.get("agent_tool", "unknown")
                    call_id = tr.get("call_id", "").lstrip("tc_")
                    args = tr.get("arguments", {})
                    result = tr.get("result", {})
                    ok = result.get("ok", False) if isinstance(result, dict) else False
                    # stdin: 工具入参摘要
                    stdin = ""
                    if isinstance(args, dict):
                        stdin = args.get("cmd") or args.get("text") or args.get("key") or json.dumps(args, ensure_ascii=False)
                    # stdout: 工具输出
                    stdout = ""
                    if isinstance(result, dict):
                        stdout = result.get("stdout") or result.get("output") or str(result.get("result", ""))
                    if not stdout and not isinstance(result, dict):
                        stdout = str(result)
                    tool_results.append({
                        "tool_name": name,
                        "ok": ok,
                        "stdin": str(stdin)[:500],
                        "stdout": str(stdout)[:500],
                        "call_id": call_id,
                    })

            elif t == "done":
                pass

        # 聚合
        substeps: dict[str, dict] = {}
        n = 0

        thinking = "".join(thinking_parts).strip()
        if has_reset and thinking:
            n += 1
            substeps[str(n)] = {"thinking": thinking}

        for tr in tool_results:
            n += 1
            substeps[str(n)] = {"tool_marker": tr}

        text = "".join(text_parts).strip()
        if not has_reset:
            text = thinking + text  # 无工具，thinking 合并到 text
            thinking = ""

        if text:
            n += 1
            substeps[str(n)] = {"text": text}

        self.records[self._next_key()] = {
            "role": "assistant",
            "id": _next_id(),
            "content": substeps if substeps else {"text": ""},
        }

    def save(self) -> Path:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved → {OUTPUT}")
        return OUTPUT


def collect_events(agent: Agent, user_message: str) -> list[dict]:
    """收集所有流式事件"""
    events: list[dict] = []
    for ev in agent.stream_chat_events(user_message):
        events.append(ev)
    return events


def run():
    agent = Agent(
        user_name="Tinda",
        user_perm=perm.PUBLIC_ALL,
        model_name="deepseek-v4-flash",
        max_turns=30,
    )
    rec = SessionRecorder()

    # ── 第一轮 ──
    print("=== 第一轮: 自我介绍 ===")
    rec.add_user("你好，请介绍一下你自己", raw=True)
    events = collect_events(agent, "你好，请介绍一下你自己")
    rec.add_agent_response(events)
    print(f"  events: {len(events)}, substeps: {len(rec.records[str(rec._msg_seq)]['content'])}")

    # ── 第二轮 ──
    print("=== 第二轮: 执行命令 ===")
    rec.add_user("帮我执行命令 echo Hello TindaAgent && date")
    events2 = collect_events(agent, "帮我执行命令 echo Hello TindaAgent && date")
    rec.add_agent_response(events2)
    print(f"  events: {len(events2)}, substeps: {len(rec.records[str(rec._msg_seq)]['content'])}")

    # ── 第三轮: system ──
    print("=== 第三轮: system 注入 ===")
    rec.add_system("[上下文压缩] 已压缩历史消息")

    rec.save()


if __name__ == "__main__":
    run()
