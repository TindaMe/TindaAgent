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
    def __init__(self) -> None:
        self.appended_rows: list[dict] = []

    def ensure_session(self, _sid: str) -> None:
        return None

    def load_messages(self, _sid: str) -> list[dict]:
        return []

    def append_messages(self, _sid: str, rows: list[dict]) -> list[dict]:
        self.appended_rows.extend(rows)
        return rows


class _FakeAgentWithEmbeddedMarker:
    def __init__(self) -> None:
        self.history: list[dict] = []

    def chat_with_meta(self, _user_message: str) -> dict:
        return {
            "reply": "好的\n\n> >_<\n> --调用工具中--",
            "tool_steps": 1,
            "tool_trace": [
                {
                    "agent_tool": "run_terminal",
                    "result": {"ok": True, "tool_name": "run_terminal", "call_id": "tc_0000004242", "result": "ok"},
                }
            ],
        }


class ChatToolMarkerCallIdTests(unittest.TestCase):
    def _decode_json_response(self, response) -> dict:
        return json.loads(response.body.decode("utf-8"))

    def test_chat_slash_command_tool_marker_contains_call_id(self) -> None:
        fake_store = _FakeStore()
        req = server.ChatRequest(message="/tool echo hi", session_id="s_tool_marker_callid")
        fake_job = {
            "job_id": "j_test_1",
            "session_id": "s_tool_marker_callid",
            "status": "queued",
            "created_at": "2026-05-01T00:00:00+08:00",
            "call_id": "tc_0000001234",
        }
        with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
             patch.object(server, "_has_llm_perm", return_value=True), \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_get_agent", return_value=SimpleNamespace(history=[])), \
             patch.object(server, "_get_web_profile", return_value=SimpleNamespace(perm=511)), \
             patch.object(server._tool_runtime, "submit_command", return_value=fake_job):
            response = asyncio.run(server.chat(req))

        data = self._decode_json_response(response)
        self.assertTrue(bool(data.get("tool_async")))
        self.assertIn("> --call_id: tc_0000001234--", str(data.get("reply", "")))
        marker_rows = [r for r in fake_store.appended_rows if str(r.get("entry_type", "")) == "tool_marker"]
        self.assertEqual(len(marker_rows), 1)
        self.assertIn("> --call_id: tc_0000001234--", str(marker_rows[0].get("content", "")))

    def test_chat_stream_slash_command_tool_marker_contains_call_id(self) -> None:
        fake_store = _FakeStore()
        fake_job = {
            "job_id": "j_test_2",
            "session_id": "s_tool_marker_stream",
            "status": "queued",
            "created_at": "2026-05-01T00:00:00+08:00",
            "call_id": "tc_0000005678",
        }
        with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
             patch.object(server, "_has_llm_perm", return_value=True), \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_get_agent", return_value=SimpleNamespace(history=[])), \
             patch.object(server, "_get_web_profile", return_value=SimpleNamespace(perm=511)), \
             patch.object(server._tool_runtime, "submit_command", return_value=fake_job):
            response = asyncio.run(
                server.chat_stream(
                    message="/tool echo hi",
                    session_id="s_tool_marker_stream",
                )
            )

        body = response.body.decode("utf-8")
        self.assertIn("> --call_id: tc_0000005678--", body)
        marker_rows = [r for r in fake_store.appended_rows if str(r.get("entry_type", "")) == "tool_marker"]
        self.assertEqual(len(marker_rows), 1)
        self.assertIn("> --call_id: tc_0000005678--", str(marker_rows[0].get("content", "")))

    def test_chat_regular_reply_embedded_tool_marker_injects_call_id(self) -> None:
        req = server.ChatRequest(message="执行一下工具", session_id="s_tool_marker_embedded")
        fake_agent = _FakeAgentWithEmbeddedMarker()
        captured: dict = {}

        def _fake_save(_sid: str, _user_text: str, assistant_text: str, **kwargs):
            captured["assistant_text"] = assistant_text
            captured["tool_trace"] = kwargs.get("tool_trace", [])
            return kwargs.get("turn_id") or "turn_test_embedded"

        with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
             patch.object(server, "_has_llm_perm", return_value=True), \
             patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_get_agent", return_value=fake_agent), \
             patch.object(server, "_save_chat_messages", side_effect=_fake_save), \
             patch.object(server, "_generate_title_from_first_round", return_value=None):
            response = asyncio.run(server.chat(req))

        data = self._decode_json_response(response)
        self.assertIn("> --call_id: tc_0000004242--", str(data.get("reply", "")))
        self.assertIn("> --call_id: tc_0000004242--", str(captured.get("assistant_text", "")))
        self.assertEqual(len(captured.get("tool_trace", [])), 1)


if __name__ == "__main__":
    unittest.main()
