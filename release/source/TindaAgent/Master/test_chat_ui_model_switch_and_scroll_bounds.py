from __future__ import annotations

from pathlib import Path
import unittest


class ChatUiModelSwitchAndScrollBoundsTests(unittest.TestCase):
    def test_model_switch_uses_rebindable_dom_references(self) -> None:
        chat_html = Path(__file__).resolve().parents[1] / "Web" / "chat.html"
        text = chat_html.read_text(encoding="utf-8")

        self.assertIn("let modelSwitchBtnEl = document.getElementById(\"modelSwitchBtn\");", text)
        self.assertIn("let modelPanelEl = document.getElementById(\"modelPanel\");", text)
        self.assertIn("if (newModel) modelSwitchBtnEl = newModel;", text)
        self.assertIn("if (newPanel) modelPanelEl = newPanel;", text)

    def test_messages_scroll_is_hard_clamped_without_rubber_band(self) -> None:
        chat_html = Path(__file__).resolve().parents[1] / "Web" / "chat.html"
        text = chat_html.read_text(encoding="utf-8")

        self.assertIn("overscroll-behavior-y: none;", text)
        self.assertIn("function clampMessagesScrollWithinBounds()", text)
        self.assertNotIn("triggerRubberBand(", text)
        self.assertNotIn("rubber-bottom", text)
        self.assertNotIn("rubber-top", text)


if __name__ == "__main__":
    unittest.main()
