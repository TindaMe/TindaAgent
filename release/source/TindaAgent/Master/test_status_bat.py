from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class StatusBatTests(unittest.TestCase):
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
            timeout=30,
        )

    def test_status_bat_help(self) -> None:
        out = self._run_cmd("status.bat --help")
        self.assertEqual(int(out.returncode), 0)
        self.assertIn("status.bat --show", str(out.stdout))

    def test_status_bat_show_no_crash(self) -> None:
        out = self._run_cmd("echo 8000> .tinda_ports.list & status.bat --show")
        self.assertEqual(int(out.returncode), 0)
        self.assertIn("[status] tracked:", str(out.stdout))


if __name__ == "__main__":
    unittest.main()
