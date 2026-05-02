from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


class StopShTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.stop_sh = cls.repo_root / "stop.sh"
        if not cls.stop_sh.exists():
            raise unittest.SkipTest("stop.sh not found")

    def _run(self, argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        # Ensure tests are deterministic and not polluted by user shell state.
        merged_env["TINDA_ACTIVE_PORTS"] = ""
        merged_env["TINDA_PORTS_INCLUDE_ENV"] = "1"
        if env:
            merged_env.update(env)
        return subprocess.run(
            ["bash", str(self.stop_sh), *argv],
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
        self.assertIn("--port <port>", str(out.stdout))

    def test_invalid_port(self) -> None:
        out = self._run(["--port", "abc"])
        self.assertEqual(int(out.returncode), 2)
        self.assertIn("invalid port", str(out.stdout))

    def test_list_tracked_ports_from_file(self) -> None:
        self.ports_file.write_text("8010\n", encoding="utf-8")
        out = self._run(["--list"], env={"TINDA_ACTIVE_PORTS": ""})
        self.assertEqual(int(out.returncode), 0)
        self.assertIn("port 8010", str(out.stdout))

    def test_stop_by_port_removes_from_file(self) -> None:
        self.ports_file.write_text("8010\n8011\n", encoding="utf-8")
        out = self._run(["--port", "8010"], env={"TINDA_ACTIVE_PORTS": "8010 8011"})
        self.assertEqual(int(out.returncode), 0)
        data = self.ports_file.read_text(encoding="utf-8")
        self.assertIn("wsl:8011", data)
        self.assertNotIn("8010", data)

    def test_list_handles_crlf_ports_file(self) -> None:
        self.ports_file.write_bytes(b"8010\r\n8011\r\n")
        out = self._run(["--list"], env={"TINDA_ACTIVE_PORTS": ""})
        self.assertEqual(int(out.returncode), 0)
        self.assertIn("port 8010", str(out.stdout))
        self.assertIn("port 8011", str(out.stdout))

    def test_list_marks_foreign_env_records(self) -> None:
        self.ports_file.write_text("wsl:8010\nwindows:8011\n", encoding="utf-8")
        out = self._run(["--list"], env={"TINDA_ACTIVE_PORTS": ""})
        self.assertEqual(int(out.returncode), 0)
        self.assertIn("port 8010", str(out.stdout))
        self.assertIn("foreign-env(windows)", str(out.stdout))


if __name__ == "__main__":
    unittest.main()
