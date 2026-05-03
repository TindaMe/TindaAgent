#!/usr/bin/env python3
"""
TindaAgent run_terminal 全链路终端脚本。
最小链路：用户输入 → LLM 调用工具 → 挂起确认 → 终端输入 1/0 → 继续执行。

用法: python test_run_terminal_chain.py
"""

import sys

sys.path.insert(0, "/mnt/e/Python/release/source")

from TindaAgent.Process.AI.agent import Agent
from TindaAgent.Process.AI.client import LLMClient
from TindaAgent.Process.Architecture import perm
from TindaAgent.Process.Security.terminal_policy import is_bypass_enabled

USER_PERM = perm.PUBLIC_ALL | perm.TOOL_ALL  # 缺 SYSTEM_EXECUTE，确保挂起确认


def sanitize_messages(history: list[dict]) -> None:
    """清理所有 lone surrogate 字符。

    不碰 reasoning_content —— DeepSeek V4 thinking 模式要求原样回传，
    连空字符串都不能改成 null/None。
    """
    def clean_str(s: str) -> str:
        return "".join(c for c in s if ord(c) not in range(0xD800, 0xE000))

    def walk(val):
        if isinstance(val, str):
            return clean_str(val)
        if isinstance(val, dict):
            return {k: walk(v) for k, v in val.items()}
        if isinstance(val, list):
            return [walk(v) for v in val]
        return val

    for m in history:
        for k in list(m.keys()):
            m[k] = walk(m[k])


def _check_surrogates(history: list[dict]) -> None:
    """调试：检查 history 中是否还有 surrogate 残留，并试编码。"""
    import json
    for i, m in enumerate(history):
        s = str(m)
        for j, c in enumerate(s):
            if 0xD800 <= ord(c) <= 0xDFFF:
                print(f"  [DEBUG] surrogate U+{ord(c):04X} 在 history[{i}] role={m.get('role','?')} pos={j} content={str(m.get('content',''))[:60]}")
                return
    # 试编码
    try:
        body = json.dumps(history, ensure_ascii=False)
        body.encode("utf-8")
    except Exception as e:
        print(f"  [DEBUG] json.dumps 后 encode 失败: {e}")
        # 定位具体位置
        for i, m in enumerate(history):
            try:
                json.dumps(m, ensure_ascii=False).encode("utf-8")
            except Exception:
                print(f"  [DEBUG]   问题在 history[{i}] role={m.get('role','?')}")
                break


def do_chat(agent: Agent, user_input: str) -> dict:
    """发起对话，内部清理 reasoning_content + lone surrogate 防止 API 拒绝。"""
    sanitize_messages(agent.history)
    _check_surrogates(agent.history)
    result = agent.chat_with_meta(user_input, temperature=0.7)
    sanitize_messages(agent.history)
    return result


def do_resume(agent: Agent, approval: bool) -> dict:
    """恢复挂起的确认。同时清理 _held_messages 快照中的 surrogate。"""
    if agent._held_messages:
        sanitize_messages(agent._held_messages)
    result = agent.resume_with_confirmations([{"approval": approval}])
    sanitize_messages(agent.history)
    return result


def _find_pending_in_result(r: dict) -> dict | None:
    """在 trace result 中找 pending_confirmation（可能在嵌套的 result.result 里）"""
    for cand in (r, r.get("result") if isinstance(r.get("result"), dict) else None):
        if isinstance(cand, dict) and cand.get("pending_confirmation"):
            return cand
    return None


def print_trace(tool_trace: list[dict]) -> None:
    for step in tool_trace:
        name = step.get("agent_tool", "?")
        r = step.get("result", {})
        if isinstance(r, dict):
            pending = _find_pending_in_result(r)
            if pending:
                note = pending.get("note", "")
                cmd = pending.get("cmd", "?")[:80]
                if note:
                    print(f"  [tool] {name} → ⏸  {note}")
                    print(f"           命令: {cmd}")
                else:
                    print(f"  [tool] {name} → ⏸  {cmd}")
                continue
            if r.get("ok") is False:
                print(f"  [tool] {name} → ✗ {r.get('error', '?')[:80]}")
                continue
        print(f"  [tool] {name} → ✓")


def ask_confirm(cmd: str = "", note: str = "") -> bool:
    print()
    if note:
        print(f"  ┌ 备注: {note}")
    print(f"  │ 命令: {cmd[:120]}")
    print("  ╔══════════════════════════════════════╗")
    print("  ║  终端命令请求确认                    ║")
    print("  ║  输入 1 = 允许执行                   ║")
    print("  ║  输入 0 = 拒绝执行                   ║")
    print("  ╚══════════════════════════════════════╝")
    try:
        choice = input("  [1/0] > ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "0"
    return choice == "1"


# ── monkey-patch: 每次 API 调用前清理 messages 中所有 surrogate 和 reasoning_content ──
from openai.resources.chat.completions import Completions

_original_create = Completions.create


def _safe_create(self, *, messages, **kwargs):
    sanitize_messages(messages)
    return _original_create(self, messages=messages, **kwargs)


Completions.create = _safe_create


def main():
    print("=" * 60)
    print("  TindaAgent run_terminal 全链路终端脚本")
    print("  输入消息开始对话，LLM 调用终端命令时会要求确认")
    print("  输入 /quit 退出，/reset 清空对话")
    print("=" * 60)
    print()

    client = LLMClient()
    print(f"[init] 模型: {client.model}  |  权限: {USER_PERM}  |  bypass: {is_bypass_enabled(USER_PERM)}")

    agent = Agent(
        user_name="terminal",
        user_perm=USER_PERM,
        client=client,
        model_name=client.model,
        max_turns=20,
    )
    print(f"[init] Agent 就绪, max_turns={agent._max_turns}")
    print()

    turn = 0
    while True:
        try:
            user_input = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[exit]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("/quit", "/exit", "/q"):
            print("[exit]")
            break
        if user_input.lower() in ("/reset", "/clear"):
            agent.reset_history()
            print("[reset] 对话已清空")
            continue

        turn += 1
        print(f"\n[turn {turn}]")
        print("-" * 40)

        # ── 第一段：发起对话 ──
        try:
            result = do_chat(agent, user_input)
        except Exception as e:
            print(f"  [error] {e}")
            continue

        reply = str(result.get("reply", ""))
        tool_trace = result.get("tool_trace", [])
        tool_steps = int(result.get("tool_steps", 0))
        pending = bool(result.get("pending_confirmation", False))

        print_trace(tool_trace)

        # ── 第二段：挂起确认循环 ──
        while pending:
            pending_cmd = ""
            pending_note = ""
            for step in tool_trace:
                r = step.get("result", {})
                if isinstance(r, dict):
                    p = _find_pending_in_result(r)
                    if p:
                        pending_cmd = p.get("cmd", "")
                        pending_note = p.get("note", "")
                        break
            approval = ask_confirm(cmd=pending_cmd, note=pending_note)
            print(f"  → {'允许' if approval else '拒绝'}执行")

            try:
                resume_result = do_resume(agent, approval)
            except Exception as e:
                print(f"  [error] 恢复失败: {e}")
                break

            reply = str(resume_result.get("reply", ""))
            tool_trace = resume_result.get("tool_trace", [])
            tool_steps = int(resume_result.get("tool_steps", 0))
            pending = bool(resume_result.get("pending_confirmation", False))

            print_trace(tool_trace)

            if pending:
                print("  (还有新的待确认命令...)")
                continue

        # ── 回复 ──
        print("-" * 40)
        if reply.strip():
            print(f"Agent > {reply}")
        else:
            print(f"Agent > (无文本回复, {tool_steps} 个工具已执行)")
        print("-" * 40)
        print()


if __name__ == "__main__":
    main()
