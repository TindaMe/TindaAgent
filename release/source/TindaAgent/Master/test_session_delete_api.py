from __future__ import annotations

import asyncio
import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["TINDA_HOME"] = tempfile.mkdtemp(prefix="tinda_test_home_")

from TindaAgent.Web import server
from TindaAgent.Web.tool_runtime import ToolJob, ToolRuntimeManager


class _DummyUser:
    def get_perm(self) -> int:
        return 511

    def get_uid(self) -> str:
        return "u_test"


class _FakeStoreDeleteOk:
    def delete_session(self, _sid: str) -> bool:
        return True


class SessionDeleteApiTests(unittest.TestCase):
    def test_tool_runtime_stop_session_clears_runtime_state(self) -> None:
        mgr = ToolRuntimeManager()
        sid = "s_runtime_stop"
        mgr._queues[sid] = queue.Queue()
        mgr._threads[sid] = threading.Thread(target=lambda: None)
        mgr._events[sid] = [{"seq": 1}]
        mgr._seq[sid] = 1
        now = "2026-05-01T00:00:00+08:00"
        mgr._jobs[sid] = {"j1": ToolJob("j1", sid, "/help", "queued", now, now)}

        result = mgr.stop_session(sid)
        self.assertTrue(result.get("ok"))
        self.assertNotIn(sid, mgr._queues)
        self.assertNotIn(sid, mgr._threads)
        self.assertNotIn(sid, mgr._events)
        self.assertNotIn(sid, mgr._seq)
        self.assertNotIn(sid, mgr._jobs)

    def test_delete_session_still_returns_json_when_runtime_stop_fails(self) -> None:
        sid = "s_delete_runtime_err"
        server._sessions[sid] = object()
        server._session_last_access[sid] = 123.0
        with patch.object(server, "_require_login", return_value=_DummyUser()), \
             patch.object(server, "_store", _FakeStoreDeleteOk()), \
             patch.object(server._tool_runtime, "stop_session", side_effect=RuntimeError("boom")):
            response = asyncio.run(server.delete_session(sid))
        payload = json.loads(response.body.decode("utf-8"))
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("session_id"), sid)

    def test_chat_delete_flow_has_non_json_response_guard(self) -> None:
        chat_html = Path(__file__).resolve().parents[1] / "Web" / "chat.html"
        text = chat_html.read_text(encoding="utf-8")
        self.assertIn("const bodyText = await res.text();", text)
        self.assertIn("data = { ok: false, error: `HTTP ${res.status} ${bodyText.slice(0, 120)}` };", text)


if __name__ == "__main__":
    unittest.main()
