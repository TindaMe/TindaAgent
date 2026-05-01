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

    def ensure_session(self, _sid: str) -> None:
        return None

    def load_messages(self, _sid: str) -> list[dict]:
        return list(self.rows)


class ChatPendingTerminalConfirmGuardTests(unittest.TestCase):
    def test_chat_rejects_new_message_when_terminal_confirm_pending(self) -> None:
        sid = "s_pending_guard_chat"
        pending_rows = [
            {
                "id": "tcf_pending_1",
                "role": "user",
                "entry_type": "terminal_confirm",
                "turn_id": "turn_pending_1",
                "content": json.dumps({"cmd": "whoami", "status": "pending"}, ensure_ascii=False),
            }
        ]
        req = server.ChatRequest(message="继续聊天", session_id=sid)

        with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
             patch.object(server, "_has_llm_perm", return_value=True), \
             patch.object(server, "_store", _FakeStore(pending_rows)), \
             patch.object(server, "_get_agent", return_value=SimpleNamespace(history=[])):
            response = asyncio.run(server.chat(req))

        self.assertEqual(getattr(response, "status_code", 0), 409)
        data = json.loads(response.body.decode("utf-8"))
        self.assertIn("待确认终端命令", str(data.get("error", "")))
        self.assertEqual(int(data.get("pending_confirm_count", 0)), 1)
        self.assertEqual(len(data.get("pending_confirmations", [])), 1)

    def test_chat_stream_rejects_new_message_when_terminal_confirm_pending(self) -> None:
        sid = "s_pending_guard_stream"
        pending_rows = [
            {
                "id": "tcf_pending_2",
                "role": "user",
                "entry_type": "terminal_confirm",
                "turn_id": "turn_pending_2",
                "content": json.dumps({"cmd": "pwd", "status": "pending"}, ensure_ascii=False),
            }
        ]

        with patch.object(server, "_require_login", return_value=_DummyUser(511)), \
             patch.object(server, "_has_llm_perm", return_value=True), \
             patch.object(server, "_store", _FakeStore(pending_rows)), \
             patch.object(server, "_get_agent", return_value=SimpleNamespace(history=[])):
            response = asyncio.run(
                server.chat_stream(
                    message="继续",
                    session_id=sid,
                )
            )

        body = response.body.decode("utf-8")
        self.assertIn("event: error", body)
        self.assertIn("待确认终端命令", body)
        self.assertIn("pending_confirm_count", body)


if __name__ == "__main__":
    unittest.main()
