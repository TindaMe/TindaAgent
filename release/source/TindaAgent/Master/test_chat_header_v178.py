from __future__ import annotations

import unittest
from pathlib import Path


class ChatHeaderV178Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.chat_html = cls.repo_root / "TindaAgent" / "Web" / "chat.html"
        if not cls.chat_html.exists():
            raise unittest.SkipTest("chat.html not found")
        cls.content = cls.chat_html.read_text(encoding="utf-8")

    def test_header_uses_account_popup_not_select_switch(self) -> None:
        self.assertIn('id="accountBtn"', self.content)
        self.assertIn('id="accountPopup"', self.content)
        self.assertIn('id="accountList"', self.content)
        self.assertNotIn('id="headerUserSwitchSelect"', self.content)
        self.assertNotIn('id="headerUserSwitchBtn"', self.content)

    def test_header_uses_dynamic_quick_buttons_container(self) -> None:
        self.assertIn('id="quickBtns"', self.content)
        self.assertIn('id="quickSep"', self.content)
        self.assertIn('const QUICK_BUTTON_DEFS = {', self.content)
        self.assertIn("function renderQuickButtons()", self.content)
        self.assertIn('href="/settings"', self.content)

    def test_status_pill_format_kept_online_session_context(self) -> None:
        self.assertIn('在线 <span class="status-sep">·</span> 新会话 <span class="status-sep">·</span> 0', self.content)
        self.assertIn("function renderHeaderStatus()", self.content)
        self.assertIn("context-usage", self.content)


if __name__ == "__main__":
    unittest.main()
