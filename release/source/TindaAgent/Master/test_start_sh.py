from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class StartShTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.start_sh = cls.repo_root / "start.sh"
        if not cls.start_sh.exists():
            raise unittest.SkipTest("start.sh not found")

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.start_sh), *argv],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    def test_start_sh_passes_syntax(self) -> None:
        out = subprocess.run(
            ["bash", "-n", str(self.start_sh)],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(int(out.returncode), 0)

    def test_start_sh_help(self) -> None:
        out = self._run(["--help"])
        self.assertEqual(int(out.returncode), 0)
        self.assertIn("Usage:", str(out.stdout))

    def test_start_sh_invalid_port(self) -> None:
        out = self._run(["abc", "0"])
        self.assertEqual(int(out.returncode), 2)
        self.assertIn("invalid port", str(out.stdout))


if __name__ == "__main__":
    unittest.main()
