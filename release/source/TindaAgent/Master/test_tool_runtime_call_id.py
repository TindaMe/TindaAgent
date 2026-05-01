from __future__ import annotations

import unittest
from unittest.mock import patch

from TindaAgent.Web.tool_runtime import ToolRuntimeManager


class ToolRuntimeCallIdTests(unittest.TestCase):
    def test_submit_tool_command_returns_reserved_call_id(self) -> None:
        mgr = ToolRuntimeManager()
        try:
            with patch("TindaAgent.Web.tool_runtime.audit_event", return_value=321):
                job = mgr.submit_command("s_tool_callid", "/tool echo hello", 511)
            self.assertEqual(str(job.get("call_id", "")), "tc_0000000321")
        finally:
            mgr.stop_session("s_tool_callid")

    def test_submit_non_tool_command_has_empty_call_id(self) -> None:
        mgr = ToolRuntimeManager()
        try:
            with patch("TindaAgent.Web.tool_runtime.audit_event", return_value=321):
                job = mgr.submit_command("s_help_callid", "/help", 511)
            self.assertEqual(str(job.get("call_id", "")), "")
        finally:
            mgr.stop_session("s_help_callid")


if __name__ == "__main__":
    unittest.main()
