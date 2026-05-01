from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ["TINDA_HOME"] = tempfile.mkdtemp(prefix="tinda_test_home_")

from TindaAgent.Web import server


class _DummyUser:
    def __init__(self, perm_value: int = 511) -> None:
        self._perm = int(perm_value)

    def get_perm(self) -> int:
        return self._perm

    def get_uid(self) -> str:
        return "u_test"


class _FakeStore:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.write_calls = 0
        self.appended_rows: list[dict] = []

    def load_messages(self, _sid: str) -> list[dict]:
        return self.rows

    def _write_messages(self, _sid: str, rows: list[dict]) -> None:
        self.rows = rows
        self.write_calls += 1

    def append_messages(self, _sid: str, rows: list[dict]) -> list[dict]:
        self.appended_rows.extend(rows)
        return rows


class _FakeAgent:
    def __init__(self) -> None:
        self.history: list[dict] = [{"role": "user", "content": "ctx"}]
        self.trim_calls = 0

    def _trim_history(self) -> None:
        self.trim_calls += 1


class _FakeClient:
    def __init__(self, reply: str, tool_trace: list[dict] | None = None, history_delta: list[dict] | None = None) -> None:
        self.reply = reply
        self.tool_trace = tool_trace or []
        self.history_delta = history_delta or []
        self.calls: list[dict] = []

    def chat_with_tools(self, history: list[dict], user_perm: int, max_tool_steps: int = 0) -> dict:
        last = history[-1] if history else {}
        self.calls.append(
            {
                "history_len": len(history),
                "user_perm": int(user_perm),
                "max_tool_steps": int(max_tool_steps),
                "last_role": str(last.get("role", "")) if isinstance(last, dict) else "",
                "last_content": str(last.get("content", "")) if isinstance(last, dict) else "",
            }
        )
        return {
            "reply": self.reply,
            "tool_trace": self.tool_trace,
            "history_delta": list(self.history_delta),
        }


class ServerTerminalConfirmTests(unittest.TestCase):
    def _decode(self, response) -> dict:
        return json.loads(response.body.decode("utf-8"))

    def test_build_user_message_with_meta_returns_plain_text_with_meta_block(self) -> None:
        msg = server._build_user_message_with_meta(
            "hello",
            meta_user_name="Tinda",
            meta_user_id="u1",
            meta_user_perm="PUBLIC_EXECUTE",
            meta_time_iso="2026-05-01T12:00:00+08:00",
            meta_time_text="2026年05月01日 12:00:00",
        )
        self.assertIn("hello", msg)
        self.assertIn("[USER_META]", msg)
        self.assertIn("uid=u1", msg)
        self.assertEqual(server._strip_user_meta_block(msg), "hello")

    def test_strip_tool_marker_noise_removes_call_id_marker_line(self) -> None:
        raw = "前文\n\n> >_<\n> --调用工具中--\n> --call_id: tc_123456--\n\n后文"
        cleaned = server._strip_tool_marker_noise(raw)
        self.assertEqual(cleaned, "前文\n\n后文")

    def test_inject_tool_call_ids_into_marker_text_inserts_real_call_id(self) -> None:
        raw = "前文\n> >_<\n> --调用工具中--\n后文"
        trace = [
            {
                "agent_tool": "run_terminal",
                "result": {"ok": True, "call_id": "tc_0000099999", "result": "ok"},
            }
        ]
        out = server._inject_tool_call_ids_into_marker_text(raw, trace)
        self.assertIn("> --call_id: tc_0000099999--", out)
        self.assertIn("前文", out)
        self.assertIn("后文", out)

    def test_inject_tool_call_ids_into_marker_text_does_not_duplicate_existing_call_id_line(self) -> None:
        raw = "前文\n> >_<\n> --调用工具中--\n> --call_id: tc_existing--\n后文"
        trace = [
            {
                "agent_tool": "run_terminal",
                "result": {"ok": True, "call_id": "tc_new", "result": "ok"},
            }
        ]
        out = server._inject_tool_call_ids_into_marker_text(raw, trace)
        self.assertEqual(out.count("call_id:"), 1)
        self.assertIn("> --call_id: tc_existing--", out)
        self.assertNotIn("tc_new", out)

    def test_store_to_agent_messages_emits_openai_role_messages(self) -> None:
        rows = [
            {"role": "system", "entry_type": "notice", "content": "[系统摘要] 只给模型的内部摘要"},
            {"role": "assistant", "entry_type": "chat", "content": "[系统摘要] 旧记录"},
            {"role": "user", "entry_type": "chat", "content": "/tools"},
            {"role": "assistant", "entry_type": "terminal", "terminal_kind": "out", "content": "tool output line"},
        ]
        out, stats = server._store_to_agent_messages(rows)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0].get("role"), "system")
        self.assertEqual(out[0].get("content"), "只给模型的内部摘要")
        self.assertEqual(out[1].get("role"), "assistant")
        self.assertEqual(out[1].get("content"), "旧记录")
        self.assertEqual(out[2].get("role"), "system")
        self.assertEqual(out[2].get("entry_type"), "terminal")
        self.assertGreaterEqual(stats["skipped_tool_cmd"], 1)

    def test_save_chat_messages_tool_marker_contains_real_call_ids(self) -> None:
        fake_store = _FakeStore([])
        trace = [
            {
                "agent_tool": "alpha",
                "call_id": "tc_000001",
                "result": {"ok": True, "tool_name": "alpha", "result": "ok"},
            },
            {
                "agent_tool": "beta",
                "tool_call_id": "model_call_1",
                "result": {"ok": True, "tool_name": "beta", "call_id": "tc_000002", "result": "ok"},
            },
            {
                "agent_tool": "dup",
                "call_id": "tc_000001",
                "result": {"ok": True, "tool_name": "dup", "result": "ok"},
            },
        ]
        with patch.object(server, "_store", fake_store), patch.object(server, "_audit_web", return_value=0):
            turn_id = server._save_chat_messages("s_call_ids", "u", "a", tool_marker=True, tool_trace=trace)

        marker_rows = [r for r in fake_store.appended_rows if r.get("entry_type") == "tool_marker"]
        self.assertEqual(len(marker_rows), 1)
        marker = str(marker_rows[0].get("content", ""))
        self.assertIn("> --call_id: tc_000001--", marker)
        self.assertIn("> --call_id: tc_000002--", marker)
        self.assertNotIn("<call_id>", marker)
        self.assertEqual(marker.count("call_id:"), 2)
        self.assertTrue(str(turn_id).startswith("turn_"))
        row_turn_ids = {str(r.get("turn_id", "")) for r in fake_store.appended_rows}
        self.assertEqual(row_turn_ids, {str(turn_id)})

    def test_store_to_agent_messages_caps_terminal_context_rows(self) -> None:
        rows = []
        for i in range(server._LLM_TERMINAL_CONTEXT_MAX_ROWS + 5):
            rows.append(
                {
                    "id": f"m_term_{i}",
                    "role": "assistant",
                    "entry_type": "terminal",
                    "terminal_kind": "out",
                    "content": f"line_{i}",
                    "created_at": f"2026-05-01T00:00:{i % 60:02d}+08:00",
                }
            )
        out, stats = server._store_to_agent_messages(rows)
        self.assertEqual(len(out), server._LLM_TERMINAL_CONTEXT_MAX_ROWS)
        self.assertEqual(stats["dropped_terminal"], 5)
        self.assertEqual(str(out[0].get("content", "")), "line_5")

    def test_terminal_confirm_allow_continues_llm_loop(self) -> None:
        sid = "s_confirm_allow"
        cid = "c_confirm_allow"
        rows = [
            {
                "id": cid,
                "role": "user",
                "entry_type": "terminal_confirm",
                "content": json.dumps({"cmd": "echo hi", "status": "pending"}, ensure_ascii=False),
            }
        ]
        fake_store = _FakeStore(rows)
        fake_agent = _FakeAgent()
        fake_client = _FakeClient(
            reply="确认后续写完成",
            tool_trace=[],
            history_delta=[{"role": "assistant", "content": "delta"}],
        )
        req = server.TerminalConfirmRequest(session_id=sid, confirm_id=cid, action="allow", cmd="echo hi")

        with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_get_agent", return_value=fake_agent), \
             patch.object(server, "_client", fake_client), \
             patch("subprocess.run", return_value=SimpleNamespace(stdout="ok\n", stderr="", returncode=0)):
            resp = asyncio.run(server.terminal_confirm(req))

        data = self._decode(resp)
        self.assertTrue(data["ok"])
        self.assertTrue(data["executed"])
        self.assertTrue(str(data.get("turn_id", "")).startswith("turn_"))
        self.assertEqual(data["reply"], "确认后续写完成")
        self.assertEqual(len(fake_client.calls), 1)
        self.assertEqual(fake_client.calls[0]["max_tool_steps"], 0)
        self.assertEqual(fake_client.calls[0]["last_role"], "user")
        followup_text = str(fake_client.calls[0]["last_content"])
        self.assertIn("[TERMINAL_FOLLOWUP]", followup_text)
        self.assertIn("命令: echo hi", followup_text)
        self.assertIn("返回码: 0", followup_text)
        self.assertEqual(fake_agent.trim_calls, 1)
        self.assertEqual(fake_agent.history[-1], {"role": "assistant", "content": "delta"})
        self.assertEqual(fake_store.write_calls, 1)
        assistant_rows = [r for r in fake_store.appended_rows if r.get("entry_type") == "chat" and r.get("role") == "assistant"]
        self.assertTrue(bool(assistant_rows))
        self.assertEqual(str(assistant_rows[0].get("turn_id", "")), str(data.get("turn_id", "")))

    def test_terminal_confirm_allow_off_topic_reply_falls_back_to_command_summary(self) -> None:
        sid = "s_confirm_off_topic"
        cid = "c_confirm_off_topic"
        rows = [
            {
                "id": "u_before",
                "role": "user",
                "entry_type": "chat",
                "content": "我让你执行ping",
            },
            {
                "id": cid,
                "role": "user",
                "entry_type": "terminal_confirm",
                "content": json.dumps({"cmd": "ping 8.8.8.8", "status": "pending"}, ensure_ascii=False),
            },
        ]
        fake_store = _FakeStore(rows)
        fake_agent = _FakeAgent()
        fake_client = _FakeClient(reply="底层技术信息保密。", tool_trace=[], history_delta=[])
        req = server.TerminalConfirmRequest(session_id=sid, confirm_id=cid, action="allow", cmd="ping 8.8.8.8")

        with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_get_agent", return_value=fake_agent), \
             patch.object(server, "_client", fake_client), \
             patch("subprocess.run", return_value=SimpleNamespace(stdout="ping ok\n", stderr="", returncode=0)):
            resp = asyncio.run(server.terminal_confirm(req))

        data = self._decode(resp)
        self.assertTrue(data["ok"])
        self.assertTrue(data["executed"])
        self.assertIn("命令 `ping 8.8.8.8` 已执行", data["reply"])
        self.assertIn("输出结果", data["reply"])

    def test_terminal_confirm_multi_pending_waits_until_all_confirmed_then_continue_once(self) -> None:
        sid = "s_confirm_multi"
        turn_id = "turn_multi_1"
        c1 = "c_multi_1"
        c2 = "c_multi_2"
        rows = [
            {
                "id": "u_multi_before",
                "role": "user",
                "entry_type": "chat",
                "content": "请执行两条终端命令并汇总",
                "turn_id": turn_id,
            },
            {
                "id": c1,
                "role": "user",
                "entry_type": "terminal_confirm",
                "content": json.dumps({"cmd": "echo first", "status": "pending"}, ensure_ascii=False),
                "turn_id": turn_id,
            },
            {
                "id": c2,
                "role": "user",
                "entry_type": "terminal_confirm",
                "content": json.dumps({"cmd": "echo second", "status": "pending"}, ensure_ascii=False),
                "turn_id": turn_id,
            },
        ]
        fake_store = _FakeStore(rows)
        fake_agent = _FakeAgent()
        fake_client = _FakeClient(reply="批量续写完成", tool_trace=[], history_delta=[])
        req1 = server.TerminalConfirmRequest(session_id=sid, confirm_id=c1, action="allow", cmd="echo first")
        req2 = server.TerminalConfirmRequest(session_id=sid, confirm_id=c2, action="deny", cmd="echo second")

        with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_get_agent", return_value=fake_agent), \
             patch.object(server, "_client", fake_client), \
             patch("subprocess.run", return_value=SimpleNamespace(stdout="first\n", stderr="", returncode=0)) as mock_run:
            resp1 = asyncio.run(server.terminal_confirm(req1))
            resp2 = asyncio.run(server.terminal_confirm(req2))

        data1 = self._decode(resp1)
        data2 = self._decode(resp2)
        self.assertTrue(data1["ok"])
        self.assertTrue(data1["awaiting_other_confirmations"])
        self.assertEqual(int(data1.get("pending_confirm_count", 0)), 1)
        self.assertEqual(str(data1.get("reply", "")), "")
        self.assertTrue(data1.get("executed"))
        self.assertTrue(data2["ok"])
        self.assertFalse(bool(data2.get("awaiting_other_confirmations")))
        self.assertEqual(int(data2.get("pending_confirm_count", 0)), 0)
        self.assertTrue(bool(data2.get("is_batch_confirm")))
        self.assertEqual(int(data2.get("resolved_confirm_count", 0)), 2)
        self.assertEqual(str(data2.get("reply", "")), "批量续写完成")
        self.assertEqual(len(fake_client.calls), 1)
        self.assertIn("[TERMINAL_FOLLOWUP_BATCH]", str(fake_client.calls[0].get("last_content", "")))
        self.assertIn("echo first", str(fake_client.calls[0].get("last_content", "")))
        self.assertIn("echo second", str(fake_client.calls[0].get("last_content", "")))
        self.assertEqual(mock_run.call_count, 1)

    def test_terminal_confirm_idempotent_replay_does_not_reexecute(self) -> None:
        sid = "s_confirm_idempotent"
        cid = "c_confirm_idempotent"
        rows = [
            {
                "id": cid,
                "role": "user",
                "entry_type": "terminal_confirm",
                "content": json.dumps(
                    {
                        "cmd": "echo hi",
                        "status": "allowed",
                        "action": "allow",
                        "executed": True,
                        "output": "cached output",
                        "returncode": 0,
                    },
                    ensure_ascii=False,
                ),
            }
        ]
        fake_store = _FakeStore(rows)
        fake_client = _FakeClient(reply="should_not_be_called")
        req = server.TerminalConfirmRequest(session_id=sid, confirm_id=cid, action="allow", cmd="echo hi")

        with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_client", fake_client), \
             patch("subprocess.run") as mock_subprocess_run:
            resp = asyncio.run(server.terminal_confirm(req))

        data = self._decode(resp)
        self.assertTrue(data["ok"])
        self.assertTrue(data["already_processed"])
        self.assertIn("已执行命令", data["reply"])
        self.assertEqual(len(fake_client.calls), 0)
        self.assertEqual(fake_store.write_calls, 0)
        self.assertEqual(fake_store.appended_rows, [])
        mock_subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
