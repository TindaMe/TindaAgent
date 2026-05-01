from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
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


class _FakeStoreForAgent:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def get_session(self, _sid: str) -> dict:
        return {}

    def get_context_messages(self, _sid: str) -> list[dict]:
        return list(self._rows)


class _FakeStoreForChat:
    def ensure_session(self, _sid: str) -> None:
        return None

    def load_messages(self, _sid: str) -> list[dict]:
        return []


class _FakeAgentForChat:
    def __init__(self) -> None:
        self.history: list[dict] = [{"role": "system", "content": "base-system"}]

    def chat_with_meta(self, user_message: str) -> dict:
        return {
            "reply": f"echo::{user_message[:12]}",
            "tool_trace": [],
            "tool_steps": 0,
        }


class ContextInjectionLoggingTests(unittest.TestCase):
    def _decode(self, response) -> dict:
        return json.loads(response.body.decode("utf-8"))

    def test_summarize_context_injection_messages_tracks_roles_and_legacy_payloads(self) -> None:
        llm_json = server._build_llm_system_input_json(
            input_role="user",
            entry_type="chat",
            content="x" * 420,
            message_id="m1",
            created_at="2026-05-01T00:00:00+08:00",
        )
        rows = [
            {"role": "system", "entry_type": "notice", "content": "plain-system"},
            {"role": "system", "content": llm_json},
            {"role": "assistant", "entry_type": "chat", "content": "a1"},
            {"role": "assistant", "entry_type": "chat", "content": "a2"},
            {"role": "assistant", "entry_type": "chat", "content": "a3"},
            {"role": "assistant", "entry_type": "chat", "content": "a4"},
        ]

        summary = server._summarize_context_injection_messages(rows, max_preview_items=4)

        self.assertEqual(summary.get("message_count"), 6)
        self.assertEqual(summary.get("legacy_json_payload_count"), 1)
        self.assertEqual(summary.get("json_payload_count"), 1)
        self.assertEqual(summary.get("preview_count"), 4)
        self.assertEqual(summary.get("preview_omitted_count"), 2)
        self.assertEqual(summary.get("role_counts", {}).get("assistant"), 4)
        self.assertEqual(summary.get("legacy_input_role_counts", {}).get("user"), 1)
        self.assertEqual(summary.get("entry_type_counts", {}).get("chat"), 5)

        previews = summary.get("preview", [])
        self.assertEqual(len(previews), 4)
        json_preview = next((p for p in previews if p.get("is_legacy_json_payload") is True), None)
        self.assertIsNotNone(json_preview)
        self.assertTrue(str(json_preview.get("content_preview", "")).endswith("..."))

    def test_audit_context_injection_uses_dedicated_subsystem(self) -> None:
        with patch.object(server, "audit_event", return_value=123) as mock_audit_event:
            eid = server._audit_context_injection(
                "chat.request",
                "s_audit_context",
                [{"role": "user", "content": "hello"}],
                extra={"stream": False},
            )

        self.assertEqual(eid, 123)
        kwargs = mock_audit_event.call_args.kwargs
        self.assertEqual(kwargs.get("subsystem"), "context_injection")
        self.assertEqual(kwargs.get("func"), "_audit_context_injection")
        self.assertEqual(kwargs.get("op_type"), "SYSTEM_EXECUTE")
        extra = kwargs.get("extra", {})
        self.assertEqual(extra.get("phase"), "chat.request")
        self.assertEqual(extra.get("session_id"), "s_audit_context")
        self.assertEqual(extra.get("message_count"), 1)
        self.assertIs(extra.get("stream"), False)

    def test_get_agent_rebuild_context_emits_context_injection_audit(self) -> None:
        sid = "s_ctx_rebuild"
        rows = [
            {
                "id": "m1",
                "role": "assistant",
                "entry_type": "chat",
                "content": "history line",
                "created_at": "2026-05-01T00:00:00+08:00",
            }
        ]
        fake_store = _FakeStoreForAgent(rows)

        old_sessions = dict(server._sessions)
        old_access = dict(server._session_last_access)
        try:
            server._sessions.clear()
            server._session_last_access.clear()
            with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
                 patch.object(server, "_store", fake_store), \
                 patch.object(server, "_audit_web", return_value=0), \
                 patch.object(server, "_maybe_auto_compress", return_value=None), \
                 patch.object(server, "_audit_context_injection", return_value=0) as mock_ctx_audit:
                agent = server._get_agent(sid)

            self.assertIsNotNone(agent)
            self.assertEqual(mock_ctx_audit.call_count, 1)
            args = mock_ctx_audit.call_args.args
            self.assertEqual(args[0], "get_agent.rebuild_context")
            self.assertEqual(args[1], sid)
            rebuilt_messages = args[2]
            self.assertTrue(isinstance(rebuilt_messages, list) and len(rebuilt_messages) >= 1)
            self.assertEqual(str(rebuilt_messages[0].get("role", "")), "assistant")
            self.assertEqual(str(rebuilt_messages[0].get("content", "")), "history line")
        finally:
            server._sessions.clear()
            server._sessions.update(old_sessions)
            server._session_last_access.clear()
            server._session_last_access.update(old_access)

    def test_chat_emits_context_injection_audit_with_user_openai_message(self) -> None:
        req = server.ChatRequest(message="hello context log", session_id="s_chat_ctx")
        fake_agent = _FakeAgentForChat()

        with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
             patch.object(server, "_has_llm_perm", return_value=True), \
             patch.object(server, "_store", _FakeStoreForChat()), \
             patch.object(server, "_get_agent", return_value=fake_agent), \
             patch.object(server, "_save_chat_messages", return_value=None), \
             patch.object(server, "_generate_title_from_first_round", return_value=None), \
             patch.object(server, "_audit_context_injection", return_value=0) as mock_ctx_audit:
            response = asyncio.run(server.chat(req))

        data = self._decode(response)
        self.assertIn("reply", data)
        self.assertEqual(mock_ctx_audit.call_count, 1)
        args = mock_ctx_audit.call_args.args
        self.assertEqual(args[0], "chat.request")
        self.assertEqual(args[1], "s_chat_ctx")
        snapshot = args[2]
        self.assertGreaterEqual(len(snapshot), 2)
        last = snapshot[-1]
        self.assertEqual(last.get("role"), "user")
        self.assertEqual(str(last.get("content", "")), "hello context log")


if __name__ == "__main__":
    unittest.main()
