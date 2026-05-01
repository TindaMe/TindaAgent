from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ["TINDA_HOME"] = tempfile.mkdtemp(prefix="tinda_test_home_")

from TindaAgent.Tool import tool


class RunTerminalTests(unittest.TestCase):
    def test_run_terminal_accepts_command_alias_and_reports_cwd(self) -> None:
        with patch.object(tool.terminal_policy, "check_blacklist", return_value=[]), \
             patch.object(tool.terminal_policy, "detect_system_operations", return_value=[]), \
             patch.object(tool.terminal_policy, "is_bypass_enabled", return_value=True), \
             patch("subprocess.run", return_value=SimpleNamespace(stdout="hello\n", stderr="", returncode=0)):
            result = tool.run_terminal(command="echo hello", cwd="/tmp")

        self.assertTrue(result["ok"])
        self.assertEqual(result["cmd"], "echo hello")
        self.assertEqual(result["cwd"], "/tmp")
        self.assertEqual(result["returncode"], 0)
        self.assertIn("hello", result["output"])

    def test_run_terminal_falls_back_to_process_cwd_when_target_dir_invalid(self) -> None:
        with patch.object(tool.terminal_policy, "check_blacklist", return_value=[]), \
             patch.object(tool.terminal_policy, "detect_system_operations", return_value=[]), \
             patch.object(tool.terminal_policy, "is_bypass_enabled", return_value=True), \
             patch.object(tool.os, "getcwd", return_value="/virtual/cwd"), \
             patch("subprocess.run", return_value=SimpleNamespace(stdout="", stderr="", returncode=0)):
            result = tool.run_terminal(command="pwd", cwd="/path/not/exists")

        self.assertTrue(result["ok"])
        self.assertEqual(result["cwd"], "/virtual/cwd")
        self.assertEqual(result["cmd"], "pwd")


if __name__ == "__main__":
    unittest.main()
