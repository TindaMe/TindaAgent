from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


class StatusShTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.status_sh = cls.repo_root / "status.sh"
        if not cls.status_sh.exists():
            raise unittest.SkipTest("status.sh not found")

    def _run(self, argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        merged_env["TINDA_ACTIVE_PORTS"] = ""
        merged_env["TINDA_PORTS_INCLUDE_ENV"] = "1"
        if env:
            merged_env.update(env)
        return subprocess.run(
            ["bash", str(self.status_sh), *argv],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            env=merged_env,
        )

    def setUp(self) -> None:
        self.ports_file = self.repo_root / ".tinda_ports.list"
        self.ports_file.write_text("", encoding="utf-8")

    def test_help(self) -> None:
        out = self._run(["--help"])
        self.assertEqual(int(out.returncode), 0)
        self.assertIn("status.sh --show", str(out.stdout))

    def test_invalid_arg(self) -> None:
        out = self._run(["--bad"])
        self.assertEqual(int(out.returncode), 2)
        self.assertIn("unknown arg", str(out.stdout))

    def test_show_reads_tracked_ports(self) -> None:
        self.ports_file.write_text("8000\n8001\n", encoding="utf-8")
        out = self._run(["--show"], env={"TINDA_ACTIVE_PORTS": "8001 8002"})
        self.assertEqual(int(out.returncode), 0)
        # legacy ports are only treated as local when local ownership can be confirmed
        self.assertIn("[status] tracked: 8001 8002", str(out.stdout))
        self.assertIn("[status] tracked-foreign:", str(out.stdout))

    def test_show_reads_crlf_tracked_ports(self) -> None:
        self.ports_file.write_bytes(b"8000\r\n8001\r\n")
        out = self._run(["--show"], env={"TINDA_ACTIVE_PORTS": ""})
        self.assertEqual(int(out.returncode), 0)
        self.assertIn("[status] tracked: none", str(out.stdout))
        self.assertIn("[status] tracked-foreign: legacy:8000 legacy:8001", str(out.stdout))


if __name__ == "__main__":
    unittest.main()
