from __future__ import annotations

from pathlib import Path
import unittest


class ChatTerminalConfirmLockingTests(unittest.TestCase):
    def test_chat_ui_locks_input_while_terminal_confirm_pending(self) -> None:
        chat_html = Path(__file__).resolve().parents[1] / "Web" / "chat.html"
        text = chat_html.read_text(encoding="utf-8")

        self.assertIn("const INPUT_PLACEHOLDER_CONFIRM_LOCK = \"存在待确认终端命令，请先在终端全部允许/拒绝\";", text)
        self.assertIn("function setTerminalConfirmLock(active, pendingCount = 0)", text)
        self.assertIn("function syncTerminalConfirmLockFromDom()", text)
        self.assertIn("if (syncTerminalConfirmLockFromDom() > 0)", text)
        self.assertIn("data?.awaiting_other_confirmations", text)
        self.assertIn("pending_confirm_count", text)


if __name__ == "__main__":
    unittest.main()
