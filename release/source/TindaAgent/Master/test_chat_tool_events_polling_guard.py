from __future__ import annotations

from pathlib import Path
import unittest


class ChatToolEventsPollingGuardTests(unittest.TestCase):
    def test_tool_events_polling_has_throttle_and_auto_pause_guards(self) -> None:
        chat_html = Path(__file__).resolve().parents[1] / "Web" / "chat.html"
        text = chat_html.read_text(encoding="utf-8")

        self.assertIn("let toolPollInFlight = false;", text)
        self.assertIn("const TOOL_EVENTS_ERROR_REPORT_INTERVAL_MS = 15000;", text)
        self.assertIn("const TOOL_EVENTS_ERROR_AUTO_PAUSE_STREAK = 12;", text)
        self.assertIn("if (toolPollInFlight) return;", text)
        self.assertIn("toolPollInFlight = true;", text)
        self.assertIn("const isFirstPersistentTransient = isTransient && toolEventsFetchErrorStreak === 3;", text)
        self.assertIn("toolEventsFetchErrorStreak >= TOOL_EVENTS_ERROR_AUTO_PAUSE_STREAK", text)
        self.assertIn("stopToolPolling();", text)
        self.assertIn("toolPollPausedByError = false;", text)


if __name__ == "__main__":
    unittest.main()
