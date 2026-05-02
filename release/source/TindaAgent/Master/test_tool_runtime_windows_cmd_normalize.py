from __future__ import annotations

import unittest

from TindaAgent.Web.tool_runtime import ToolRuntimeManager


class ToolRuntimeWindowsNormalizeTests(unittest.TestCase):
    def test_normalize_run_terminal_cmd_with_windows_path(self) -> None:
        raw = '/tool run_terminal cmd="type \\"E:\\\\Python\\\\release\\\\source\\\\TindaAgent\\\\docs\\\\CHANGELOG.md\\""'
        out = ToolRuntimeManager._normalize_tool_invocation_for_windows(raw)
        self.assertTrue(out.startswith('/tool run_terminal cmd="'))
        self.assertIn("CHANGELOG.md", out)
        self.assertIn("\\\\", out)

    def test_normalize_non_run_terminal_keeps_original(self) -> None:
        raw = '/tool echo "hello world"'
        out = ToolRuntimeManager._normalize_tool_invocation_for_windows(raw)
        self.assertEqual(out, raw)

    def test_normalize_run_terminal_positional_windows_path(self) -> None:
        raw = '/tool run_terminal type "E:\\Python\\release\\source\\TindaAgent\\docs\\CHANGELOG.md"'
        out = ToolRuntimeManager._normalize_tool_invocation_for_windows(raw)
        self.assertTrue(out.startswith('/tool run_terminal cmd="type '))
        self.assertIn("CHANGELOG.md", out)


if __name__ == "__main__":
    unittest.main()
