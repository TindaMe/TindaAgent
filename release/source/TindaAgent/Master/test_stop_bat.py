from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class StopBatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.cmd_exe = shutil.which("cmd.exe")

    def _run_cmd(self, command: str) -> subprocess.CompletedProcess[str]:
        if not self.cmd_exe:
            self.skipTest("cmd.exe not available")
        return subprocess.run(
            [self.cmd_exe, "/c", command],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    def test_stop_bat_help(self) -> None:
        out = self._run_cmd("stop.bat --help")
        self.assertEqual(int(out.returncode), 0)
        self.assertIn("--port <port>", str(out.stdout))
        self.assertIn("--all", str(out.stdout))

    def test_stop_bat_invalid_port(self) -> None:
        out = self._run_cmd("stop.bat --port abc")
        self.assertEqual(int(out.returncode), 2)
        self.assertIn("invalid port", str(out.stdout))

    def test_stop_bat_all_no_crash(self) -> None:
        out = self._run_cmd("echo 8000> .tinda_ports.list & stop.bat --all")
        self.assertEqual(int(out.returncode), 0)
        self.assertIn("[stop]", str(out.stdout))


if __name__ == "__main__":
    unittest.main()
