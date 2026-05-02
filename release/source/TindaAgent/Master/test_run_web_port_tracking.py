from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_web


class RunWebPortTrackingTests(unittest.TestCase):
    def test_parse_ports_text_dedup_and_filter(self) -> None:
        text = "8000,8001;8001\nabc 65536 0 -1 9000 \"\""
        ports = run_web._parse_ports_text(text)
        self.assertEqual(ports, [8000, 8001, 9000])

    def test_update_tracked_port_add_then_remove(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_ports_") as tmp:
            ports_file = Path(tmp) / ".tinda_ports.list"
            with patch.dict(os.environ, {}, clear=True):
                with patch("run_web._sync_windows_ports_env", return_value=None) as sync_mock:
                    run_web._update_tracked_port(8000, add=True, file_path=ports_file, sync_windows_env=True, env_tag="windows")
                    run_web._update_tracked_port(8001, add=True, file_path=ports_file, sync_windows_env=True, env_tag="windows")
                    run_web._update_tracked_port(8000, add=False, file_path=ports_file, sync_windows_env=True, env_tag="windows")

                self.assertTrue(ports_file.exists())
                data = ports_file.read_text(encoding="utf-8")
                self.assertEqual(data.strip(), "windows:8001")
                self.assertEqual(str(os.environ.get("TINDA_ACTIVE_PORTS", "")).strip(), "8001")
                # add/add/remove => three sync attempts
                self.assertEqual(int(sync_mock.call_count), 3)

    def test_load_tracked_ports_merges_file_and_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_ports_merge_") as tmp:
            ports_file = Path(tmp) / ".tinda_ports.list"
            ports_file.write_text("windows:8000\nwsl:8002\nlegacy:8010\n", encoding="utf-8")
            ports = run_web._load_tracked_ports(file_path=ports_file, env_value="8001 8002", env_tag="windows")
            self.assertEqual(ports, [8000, 8002, 8010, 8001])

    def test_load_tracked_ports_filters_foreign_when_requested(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_ports_filter_") as tmp:
            ports_file = Path(tmp) / ".tinda_ports.list"
            ports_file.write_text("windows:8000\nwsl:8001\nlegacy:8002\n", encoding="utf-8")
            ports = run_web._load_tracked_ports(
                file_path=ports_file,
                env_value="",
                env_tag="windows",
                include_foreign=False,
                include_legacy=True,
            )
            self.assertEqual(ports, [8000, 8002])


if __name__ == "__main__":
    unittest.main()
