from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from TindaAgent.Web.session_store import SessionStore


class SessionStoreTurnIdTests(unittest.TestCase):
    def test_append_and_load_messages_keep_turn_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_session_store_") as tmp:
            store = SessionStore(Path(tmp))
            store.ensure_session("s_turn")
            store.append_messages(
                "s_turn",
                [
                    {"role": "user", "entry_type": "chat", "content": "u1", "turn_id": "turn_abc"},
                    {"role": "assistant", "entry_type": "chat", "content": "a1", "turn_id": "turn_abc"},
                ],
            )
            rows = store.load_messages("s_turn")

        self.assertEqual(len(rows), 2)
        self.assertEqual(str(rows[0].get("turn_id", "")), "turn_abc")
        self.assertEqual(str(rows[1].get("turn_id", "")), "turn_abc")

    def test_normalize_turn_id_removes_invalid_chars(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_session_store_") as tmp:
            store = SessionStore(Path(tmp))
            store.ensure_session("s_turn_norm")
            store.append_messages(
                "s_turn_norm",
                [
                    {
                        "role": "assistant",
                        "entry_type": "chat",
                        "content": "hello",
                        "turn_id": "turn a/?1",
                    }
                ],
            )
            rows = store.load_messages("s_turn_norm")

        self.assertEqual(len(rows), 1)
        tid = str(rows[0].get("turn_id", ""))
        self.assertTrue(bool(tid))
        self.assertNotIn(" ", tid)
        self.assertNotIn("/", tid)
        self.assertNotIn("?", tid)


if __name__ == "__main__":
    unittest.main()
