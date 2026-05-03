from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import normalize_tool_markers as ntm


class NormalizeToolMarkersTests(unittest.TestCase):
    def test_normalize_rows_rewrites_legacy_marker_and_keeps_call_id(self) -> None:
        rows = [
            {
                "id": "m1",
                "role": "assistant",
                "entry_type": "chat",
                "content": "好的，重新查。\n\n> --调用工具中--\n\n> --调用工具中--",
            },
            {
                "id": "m2",
                "role": "assistant",
                "entry_type": "terminal",
                "content": "[tool] call_backend_tool #tc_1234567890 {\n  \"tool_name\": \"run_terminal\"\n}",
            },
        ]
        out, changed = ntm._normalize_rows(rows)
        self.assertEqual(changed, 1)
        content = str(out[0].get("content", ""))
        self.assertIn("> >_<", content)
        self.assertIn("> --工具调用中--", content)
        self.assertIn("> --call_id: tc_1234567890--", content)
        self.assertEqual(content.count("--工具调用中--"), 1)

    def test_iter_target_files_by_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_marker_norm_") as tmp:
            root = Path(tmp)
            msgs = root / "Sessions" / "messages"
            msgs.mkdir(parents=True, exist_ok=True)
            f1 = msgs / "s_a.jsonl"
            f2 = msgs / "s_b.jsonl"
            f1.write_text("", encoding="utf-8")
            f2.write_text("", encoding="utf-8")
            only = ntm._iter_target_files(msgs, "s_a")
            self.assertEqual([x.name for x in only], ["s_a.jsonl"])
            all_rows = ntm._iter_target_files(msgs, "")
            self.assertEqual(sorted(x.name for x in all_rows), ["s_a.jsonl", "s_b.jsonl"])

    def test_extract_call_ids_for_turn_stops_at_next_user_chat(self) -> None:
        rows = [
            {"role": "assistant", "entry_type": "chat", "content": "A\n> --调用工具中--"},
            {"role": "assistant", "entry_type": "terminal", "content": "[tool] run #tc_1 {}"},
            {"role": "assistant", "entry_type": "terminal", "content": "\"ok\": true,"},
            {"role": "assistant", "entry_type": "terminal", "content": "tool: run_terminal #tc_1"},
            {"role": "user", "entry_type": "chat", "content": "next round"},
            {"role": "assistant", "entry_type": "terminal", "content": "[tool] run #tc_2 {}"},
        ]
        ids = ntm._extract_call_ids_for_turn(rows, 0)
        self.assertEqual(ids, ["tc_1"])

    def test_extract_call_ids_for_turn_skips_failed_tool_attempt(self) -> None:
        rows = [
            {"role": "assistant", "entry_type": "chat", "content": "B\n> --调用工具中--"},
            {"role": "assistant", "entry_type": "terminal", "content": "[tool] run_terminal #tc_bad {\n  \"cmd\": \"uname -a\"\n}"},
            {"role": "assistant", "entry_type": "terminal", "content": "\"ok\": false,"},
            {"role": "assistant", "entry_type": "terminal", "content": "────────────────"},
            {"role": "assistant", "entry_type": "terminal", "content": "[tool] call_backend_tool #tc_good {\n  \"tool_name\": \"run_terminal\"\n}"},
            {"role": "assistant", "entry_type": "terminal", "content": "\"ok\": true,"},
            {"role": "assistant", "entry_type": "terminal", "content": "────────────────"},
        ]
        ids = ntm._extract_call_ids_for_turn(rows, 0)
        self.assertEqual(ids, ["tc_good"])


if __name__ == "__main__":
    unittest.main()
