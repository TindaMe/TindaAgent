#!/usr/bin/env python3
"""TindaAgent CLI — prompt_toolkit 交互界面（Tab补全 + 箭头选择 + 流式）。

用法:
    python -m TindaAgent.CLI.main [--model deepseek-v4-flash] [--session <id>]
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML

from TindaAgent.Process.AI.agent import Agent
from TindaAgent.Process.AI.client import LLMClient
from TindaAgent.Process.Architecture import perm
from TindaAgent.Process.Security.terminal_policy import is_bypass_enabled
from TindaAgent.CLI.display import (
    sanitize_messages,
    print_trace,
    ask_confirm,
    _find_pending_in_result,
    stream_print,
    c,
)
from TindaAgent.CLI.session_manager import SessionManager
from TindaAgent.CLI import settings
from TindaAgent.Process.AI.agent import _VERSION  # noqa — CLI 版本号

USER_PERM = perm.PUBLIC_ALL | perm.TOOL_ALL
COMMANDS = ["/help", "/sessions", "/session", "/new", "/delete", "/reset", "/model", "/last", "/version", "/quit"]
MODELS = ["deepseek-v4-flash", "deepseek-chat", "deepseek-v4-pro", "deepseek-reasoner"]

# ── ANSI 简写 ──
B = "\033[1m"
D = "\033[2m"
R = "\033[0m"
G = "\033[32m"
C = "\033[36m"
Y = "\033[33m"
RD = "\033[31m"
BL = "\033[34m"


def _header(client: "CLI") -> str:
    sid = client.session_id[:16] if client.session_id else "新会话"
    return (f" {C}{B}TindaAgent{R} {D}v{_VERSION}{R}  {D}{client.client.model}{R}  "
            f"{D}sid:{sid}{R}  {D}/help{R}")


def _truncate_title(title: str, width: int = 14) -> str:
    """截断标题到固定显示宽度，CJK算2宽，超出加…"""
    tw = 0
    result = []
    for c in str(title or "新对话"):
        cp = ord(c)
        # CJK + 全角 + 中文标点
        cw = 2 if cp > 0x7F else 1  # 非ASCII全部算2宽（简化处理）
        if tw + cw > width - 1:
            result.append("…")
            tw += 1
            break
        result.append(c)
        tw += cw
    pad = width - tw
    return "".join(result) + " " * max(0, pad)


# ── 选择器 ──

def select_from_list(ps: PromptSession, title: str, items: list[tuple[str, str, str]],
                     *, s_tab_label: str = "", s_tab_value: str = "") -> str | None:
    """↑↓ 箭头选择 + Enter 确认 + Shift+Tab 快捷键。"""
    if not items:
        return None

    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.formatted_text import HTML

    n = len(items)
    idx = [0]

    def _render():
        lines = [f" <b>{title}</b>  <dim>↑↓ Enter=确认 Esc=取消</dim>"]
        if s_tab_value:
            lines.append(f" <dim>Shift+Tab</dim>  {s_tab_label}")
        lines.append("")
        for i, (key, label, desc) in enumerate(items):
            prefix = " <ansigreen>▶</ansigreen>" if i == idx[0] else "  "
            lbl = f"<ansigreen><b>{label}</b></ansigreen>" if i == idx[0] else label
            lines.append(f"{prefix} {lbl}  <dim>{desc}</dim>")
        return HTML("\n".join(lines))

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        idx[0] = (idx[0] - 1) % n

    @kb.add("down")
    def _(event):
        idx[0] = (idx[0] + 1) % n

    @kb.add("enter")
    def _(event):
        event.app.exit(result=items[idx[0]][0])

    @kb.add("escape")
    def _(event):
        event.app.exit(result=None)

    @kb.add("c-c")
    def _(event):
        event.app.exit(result=None)

    if s_tab_value:
        @kb.add("s-tab")
        def _(event):
            event.app.exit(result=s_tab_value)

    content = Window(content=FormattedTextControl(_render), always_hide_cursor=True)
    app = Application(layout=Layout(HSplit([content])), key_bindings=kb, full_screen=False,
                      erase_when_done=False)

    return app.run()


# ── Tab 补全 ──

class CLICompleter(Completer):
    def __init__(self, cli: "CLI") -> None:
        self.cli = cli

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if text.startswith("/"):
            for cmd in COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text))
        elif text.startswith("/model "):
            prefix = text[len("/model "):]
            for m in MODELS:
                if m.startswith(prefix):
                    yield Completion(m, start_position=-len(prefix))
        elif text.startswith("/session "):
            prefix = text[len("/session "):]
            for row in self.cli.sessions.list_sessions(limit=20):
                sid = row.get("id", "")
                if sid.startswith(prefix):
                    yield Completion(sid, start_position=-len(prefix))


# ── CLI 主类 ──

class CLI:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.client = LLMClient()
        if args.model:
            self.client.model = args.model
        elif settings.get_model():
            self.client.model = settings.get_model()
        self.sessions = SessionManager()
        self.session_id = ""
        self._init_session()
        self.agent = Agent(
            user_name="cli-user",
            user_perm=getattr(args, "perm", USER_PERM),
            client=self.client,
            model_name=self.client.model,
            max_turns=30,
        )
        if self.session_id:
            self._load_session_context()

    def _init_session(self) -> None:
        """延迟创建会话：首次发消息时才落盘。"""
        if self.args.session:
            self.session_id = str(self.args.session).strip()
            self.sessions.ensure_session(self.session_id)
            settings.set_last_session(self.session_id)
        else:
            self.session_id = ""  # 待首次消息时创建

    def _load_session_context(self) -> None:
        rows = self.sessions.get_messages(self.session_id)
        if rows:
            from TindaAgent.Web.server import _store_to_agent_messages
            agent_rows, _ = _store_to_agent_messages(rows)
            self.agent.replace_conversation(agent_rows)

    def _now_iso(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _save_turn(self, user_text: str, reply_text: str) -> None:
        items = [
            {"id": f"m_{uuid.uuid4().hex[:16]}", "role": "user", "content": user_text,
             "entry_type": "chat", "created_at": self._now_iso(), "is_summary": False},
            {"id": f"m_{uuid.uuid4().hex[:16]}", "role": "assistant", "content": reply_text,
             "entry_type": "chat", "created_at": self._now_iso(), "is_summary": False},
        ]
        self.sessions.append_messages(self.session_id, items)
        self._maybe_generate_title(user_text, reply_text)

    def _maybe_generate_title(self, user_text: str, reply_text: str) -> None:
        """首轮对话后异步生成标题（复用 web 端逻辑）。"""
        meta = self.sessions.get_session(self.session_id) or {}
        if str(meta.get("title", "")).strip() not in ("", "新对话"):
            return
        # 只在 exactly 第一条 user+assistant 消息时触发
        rows = self.sessions.get_messages(self.session_id)
        user_msgs = [m for m in rows if m.get("role") == "user" and m.get("entry_type") == "chat"]
        asst_msgs = [m for m in rows if m.get("role") == "assistant" and m.get("entry_type") == "chat"]
        if len(user_msgs) != 1 or len(asst_msgs) != 1:
            return

        import threading
        def _run():
            try:
                prompt = (
                    "Generate a concise title (max 15 chars) for this conversation. "
                    "Return ONLY the title text, no quotes, no explanation.\n\n"
                    f"User: {user_text}\n"
                    f"Assistant: {reply_text}"
                )
                title_client = LLMClient(model="deepseek-v4-flash")
                title = title_client.chat([
                    {"role": "system", "content": "You are a conversation title generator."},
                    {"role": "user", "content": prompt},
                ], temperature=0.3)
                clean_title = str(title or "").strip().strip("\"'")
                if clean_title:
                    self.sessions.store.set_session_title(self.session_id, clean_title[:15])
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    # ── 对话 ──

    def do_chat(self, user_input: str) -> dict:
        sanitize_messages(self.agent.history)
        if self.args.no_stream:
            result = self.agent.chat_with_meta(user_input, temperature=0.7)
            sanitize_messages(self.agent.history)
            return result

        self.agent._held_messages = None
        self.agent.history.append({"role": "user", "content": user_input})
        sanitize_messages(self.agent.history)

        final: dict | None = None
        printed = False
        for event in self.agent.stream_chat_events(user_input, temperature=0.7):
            t = event.get("type", "")
            if t == "delta":
                if not printed:
                    print(f" {BL}│{R} ", end="")
                    printed = True
                stream_print(event.get("content", ""))
            elif t == "tool_step":
                if printed:
                    print()
                print_trace(event.get("trace", []))
                printed = False
            elif t == "done":
                final = event

        sanitize_messages(self.agent.history)
        if final is None:
            final = {"reply": "", "tool_trace": [], "tool_steps": 0}
        if printed:
            print()
        return {
            "reply": final.get("reply", ""),
            "tool_trace": final.get("tool_trace", []),
            "tool_steps": int(final.get("tool_steps", 0)),
            "pending_confirmation": self.agent.has_pending_confirmation(),
        }

    def do_resume(self, approval: bool) -> dict:
        if self.agent._held_messages:
            sanitize_messages(self.agent._held_messages)
        result = self.agent.resume_with_confirmations([{"approval": approval}])
        sanitize_messages(self.agent.history)
        return result

    # ── 会话切换 ──

    def _switch_session(self, sid: str) -> None:
        meta = self.sessions.get_session(sid)
        if not meta:
            print(f"  {RD}会话不存在{R}: {sid}")
            return
        self.session_id = sid
        self.agent.reset_history()
        self._load_session_context()
        settings.set_last_session(sid)
        print(f"  {G}已切换{R}: {sid} ({meta.get('title', '?')})")

    def _delete_all_sessions(self) -> None:
        rows = self.sessions.list_sessions(limit=1000)
        count = 0
        for r in rows:
            sid = r.get("id", "")
            if sid and sid != self.session_id:
                self.sessions.delete_session(sid)
                count += 1
        print(f"  {G}已删除 {count} 个会话{R}（当前会话保留）")

    def _session_action(self, ps: PromptSession, sid: str) -> None:
        """选中会话后的二级菜单：打开/删除。"""
        meta = self.sessions.get_session(sid)
        title = meta.get("title", "?") if meta else "?"
        action = select_from_list(ps, f"会话 {sid[:20]} ({title})",
            [("open", "打开", "切换到此会话"),
             ("delete", "删除", "删除此会话")],
            extra_tip="")
        if action == "open":
            self._switch_session(sid)
        elif action == "delete":
            if sid == self.session_id:
                print(f"  {RD}不能删除当前会话{R}")
            else:
                self.sessions.delete_session(sid)
                print(f"  {G}已删除{R}: {sid}")

    # ── 命令处理 ──

    def _handle_command(self, raw: str, ps: PromptSession) -> bool:
        parts = raw.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            print(f" {D}bye{R}")
            return True

        elif cmd == "/help":
            print(f"""
 {B}TindaAgent CLI{R}
 {C}/help{R}              帮助
 {C}/sessions{R}          列出会话
 {C}/session [id]{R}      选择会话 → 打开/删除  {D}(↑↓){R}
 {C}/new [标题]{R}        新建会话
 {C}/delete [id]{R}       删除会话  {D}(↑↓){R}
 {C}/reset{R}             清空对话
 {C}/last{R}              载入上次会话并显示历史
 {C}/model [name]{R}      查看/切换模型  {D}(↑↓ 选择){R}
 {C}/version{R}           版本信息
 {C}/quit{R}              退出
 {D}Tab 补全 / 命令   ↑↓ 历史输入{R}
""")

        elif cmd == "/sessions":
            rows = self.sessions.list_sessions(limit=20)
            if not rows:
                print(f"  {D}暂无会话{R}")
            else:
                for i, row in enumerate(rows, 1):
                    sid = row.get("id", "")
                    title = row.get("title", "?")
                    updated = str(row.get("updated_at", ""))[:19]
                    m = f" {G}←{R}" if sid == self.session_id else ""
                    t = _truncate_title(title, 14)
                    print(f"  {C}{i}{R}. {t}  {sid}  {D}{updated}{R}{m}")

        elif cmd == "/session":
            if arg:
                self._switch_session(arg.strip())
            else:
                rows = self.sessions.list_sessions(limit=20)
                if not rows:
                    print(f"  {D}暂无会话{R}")
                    return False
                print(f"  {D}当前: {R}{G}{self.session_id}{R}")
                items = [(r.get("id", ""), r.get("id", ""),
                          f"{_truncate_title(r.get('title','?'),14)}  {r.get('id','')}  {str(r.get('updated_at',''))[:19]}")
                         for r in rows]
                selected = select_from_list(ps, "选择会话", items,
                    s_tab_label="[x] 删除全部会话",
                    s_tab_value="__delete_all__")
                if selected == "__delete_all__":
                    self._delete_all_sessions()
                elif selected:
                    self._session_action(ps, selected)

        elif cmd == "/new":
            title = arg.strip() or "新对话"
            self.session_id = self.sessions.create_session(title)
            self.agent.reset_history()
            settings.set_last_session(self.session_id)
            print(f"  {G}新建{R}: {self.session_id} ({title})")

        elif cmd == "/delete":
            if not arg:
                rows = self.sessions.list_sessions(limit=20)
                if not rows:
                    print(f"  {D}暂无会话{R}")
                    return False
                items = [(r.get("id", ""), r.get("id", ""),
                          f"{_truncate_title(r.get('title','?'),14)}  {r.get('id','')}  {str(r.get('updated_at',''))[:19]}")
                         for r in rows if r.get("id") != self.session_id]
                selected = select_from_list(ps, "删除会话", items,
                    s_tab_label="[x] 删除全部会话",
                    s_tab_value="__delete_all__")
                if selected == "__delete_all__":
                    self._delete_all_sessions()
                elif selected:
                    self.sessions.delete_session(selected)
                    print(f"  {G}已删除{R}: {selected}")
            else:
                self._session_action(ps, arg.strip())

        elif cmd == "/last":
            sid = settings.get_last_session()
            if not sid:
                print(f"  {D}没有上次会话记录{R}")
            else:
                self._switch_session(sid)
                rows = self.sessions.get_messages(sid)
                if rows:
                    print(f"\n {D}── 上次会话 {sid} ({len(rows)} 条消息) ──{R}\n")
                    for item in rows:
                        role = item.get("role", "?")
                        content = str(item.get("content", ""))
                        entry_type = str(item.get("entry_type", "chat"))
                        if entry_type not in ("chat",):
                            continue
                        if not content.strip():
                            continue
                        ts = str(item.get("created_at", ""))[:19]
                        if role == "user":
                            print(f" {D}{ts}{R}")
                            print(f" {G}{B}You{R} {G}▶{R} {content}")
                            print()
                        elif role == "assistant":
                            print(f" {BL}│{R} {content}")
                            print()
                    print(f" {D}── 结束 ──{R}\n")
                else:
                    print(f"  {D}会话为空{R}")

        elif cmd == "/version":
            print(f"  {B}TindaAgent{R} {G}v{_VERSION}{R}  model: {D}{self.client.model}{R}  perm: {Y}{USER_PERM}{R}")
            try:
                from TindaAgent.Process.Versioning import get_version_manager
                remote = get_version_manager().list_remote_releases()
                latest = remote.get("latest_verified", "")
                if latest and str(latest).lstrip("v") != str(_VERSION).lstrip("v"):
                    print(f"  {Y}新版本可用: v{latest}{R}  {D}pip install --upgrade tindaagent{R}")
                elif latest:
                    print(f"  {D}已是最新版本{R}")
            except Exception:
                pass

        elif cmd == "/reset":
            self.agent.reset_history()
            print(f"  {G}对话已清空{R}")

        elif cmd == "/model":
            items = [
                ("deepseek-v4-flash", "deepseek-v4-flash", "快速经济（默认）"),
                ("deepseek-chat", "deepseek-chat", "V4 Flash 非思考模式"),
                ("deepseek-v4-pro", "deepseek-v4-pro", "Pro 版"),
                ("deepseek-reasoner", "deepseek-reasoner", "V4 Flash 思考模式"),
            ]
            if arg:
                self.client.model = arg.strip()
                settings.set_model(self.client.model)
                print(f"  {G}已切换{R}: {self.client.model}")
            else:
                print(f"  {D}当前: {R}{Y}{self.client.model}{R}")
                selected = select_from_list(ps, "选择模型", items)
                if selected:
                    self.client.model = selected
                    settings.set_model(selected)
                    print(f"  {G}已切换{R}: {self.client.model}")

        else:
            print(f"  {Y}未知命令{R}: {cmd}  (/help)")

        print()
        return False

    # ── 主循环 ──

    def run(self) -> None:
        print(f"\n {_header(self)}\n")

        def _check_update():
            try:
                from TindaAgent.Process.Versioning import get_version_manager
                remote = get_version_manager().list_remote_releases()
                latest = remote.get("latest_verified", "")
                if latest and str(latest).lstrip("v") != str(_VERSION).lstrip("v"):
                    print(f" {Y}新版本 v{latest} 可用 — pip install --upgrade tindaagent{R}\n")
            except Exception:
                pass
        import threading
        threading.Thread(target=_check_update, daemon=True).start()

        p = PromptSession(
            history=InMemoryHistory(),
            completer=CLICompleter(self),
            style=Style.from_dict({"prompt": "fg:#89b4fa bold", "": "fg:#cdd6f4"}),
            message=HTML(f"<b>You</b> <dim>»</dim> "),
        )

        while True:
            try:
                raw = p.prompt().strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n {D}bye{R}")
                break
            if not raw:
                continue
            if raw.startswith("/"):
                if self._handle_command(raw, p):
                    break
                continue

            # 延迟创建：首次发消息时才落盘会话
            if not self.session_id:
                self.session_id = self.sessions.create_session("新对话")
                settings.set_last_session(self.session_id)

            print()

            try:
                result = self.do_chat(raw)
            except Exception as e:
                print(f" {RD}✗{R} {e}")
                continue

            reply = str(result.get("reply", ""))
            tool_trace = result.get("tool_trace", [])
            tool_steps = int(result.get("tool_steps", 0))
            pending = bool(result.get("pending_confirmation", False))

            print_trace(tool_trace)

            while pending:
                pcmd = pnote = ""
                for step in tool_trace:
                    r = step.get("result", {})
                    if isinstance(r, dict):
                        pf = _find_pending_in_result(r)
                        if pf:
                            pcmd = pf.get("cmd", "")
                            pnote = pf.get("note", "")
                            break
                approval = ask_confirm(cmd=pcmd, note=pnote)
                try:
                    resume_result = self.do_resume(approval)
                except Exception as e:
                    print(f" {RD}✗{R} {e}")
                    break
                reply = str(resume_result.get("reply", ""))
                tool_trace = resume_result.get("tool_trace", [])
                tool_steps = int(resume_result.get("tool_steps", 0))
                pending = bool(resume_result.get("pending_confirmation", False))
                print_trace(tool_trace)
                # 确认后立即输出回复（无论流式与否），避免回复丢失
                if reply.strip():
                    print(f"\n {BL}│{R} {reply}\n")
                elif not pending:
                    print(f" {D}(命令已执行，等待回复...){R}")
                if pending:
                    print(f" {Y}…还有待确认命令{R}")
                    continue

            if not self.args.no_stream and reply.strip():
                print()
            if self.args.no_stream:
                if reply.strip():
                    print(f" {BL}│{R} {reply}")
                else:
                    print(f" {D}(无文本回复, {tool_steps} 个工具){R}")

            self._save_turn(raw, reply)
            print()


def main():
    parser = argparse.ArgumentParser(description="TindaAgent CLI")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--session", type=str, default=None)
    parser.add_argument("--list-sessions", action="store_true")
    parser.add_argument("--delete-session", type=str, default=None)
    parser.add_argument("--perm", type=int, default=USER_PERM)
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()

    cli = CLI(args)

    if args.list_sessions:
        for row in cli.sessions.list_sessions():
            print(f"{row.get('id', '')}  {row.get('title', '')[:30]}  {row.get('updated_at', '')}")
        return
    if args.delete_session:
        cli.sessions.delete_session(args.delete_session)
        print(f"deleted {args.delete_session}")
        return

    cli.run()


if __name__ == "__main__":
    main()
