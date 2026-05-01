from __future__ import annotations

from pathlib import Path
import unittest


class ChatTurnGroupingTests(unittest.TestCase):
    def test_chat_ui_has_turn_grouping_helpers(self) -> None:
        chat_html = Path(__file__).resolve().parents[1] / "Web" / "chat.html"
        text = chat_html.read_text(encoding="utf-8")

        self.assertIn("const assistantTurnBubbleById = new Map();", text)
        self.assertIn("function upsertAssistantTurnBubble(", text)
        self.assertIn("const turnId = normalizeTurnId(entry?.turn_id || \"\");", text)
        self.assertIn("turnId: data?.turn_id", text)
        self.assertIn("bubble.dataset.turnId = tid;", text)


if __name__ == "__main__":
    unittest.main()
