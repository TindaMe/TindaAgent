from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import doctor


class DoctorCoreTests(unittest.TestCase):
    def test_parse_ports_text_crlf_and_delimiters(self) -> None:
        text = "8000\r\n8001,8002;8002 bad 0 70000"
        ports = doctor._parse_ports_text(text)
        self.assertEqual(ports, [8000, 8001, 8002])

    def test_http_probe_handles_unreachable(self) -> None:
        ok, status, _url = doctor._http_probe(65530, path="/chat", timeout_sec=0.1)
        self.assertFalse(ok)
        self.assertIsNone(status)

    def test_read_tracked_ports_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_doctor_ports_") as tmp:
            root = Path(tmp)
            (root / ".tinda_ports.list").write_bytes(b"8010\r\n8011\n")
            got = doctor._read_tracked_ports(root)
            self.assertEqual(got, [8010, 8011])

    def test_to_windows_path_passthrough_non_wsl(self) -> None:
        path = Path("/tmp/abc")
        with patch("doctor.os.name", "posix"), patch("doctor._is_wsl", return_value=False):
            self.assertEqual(doctor._to_windows_path(path), str(path))

    def test_to_windows_path_from_wsl(self) -> None:
        path = Path("/mnt/e/Python/release/source")
        with patch("doctor.os.name", "posix"), patch("doctor._is_wsl", return_value=True), patch(
            "doctor._run", return_value=(0, "E:\\Python\\release\\source\n")
        ):
            self.assertEqual(doctor._to_windows_path(path), "E:\\Python\\release\\source")


if __name__ == "__main__":
    unittest.main()
