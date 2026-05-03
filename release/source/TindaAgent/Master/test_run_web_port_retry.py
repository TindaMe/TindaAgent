from __future__ import annotations

import socket
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import run_web


class RunWebPortRetryTests(unittest.TestCase):
    def test_to_local_visit_host_maps_wildcard_to_localhost(self) -> None:
        self.assertEqual(run_web._to_local_visit_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(run_web._to_local_visit_host("::"), "127.0.0.1")
        self.assertEqual(run_web._to_local_visit_host("127.0.0.1"), "127.0.0.1")

    def test_reload_flag_defaults_to_false(self) -> None:
        parser = run_web.argparse.ArgumentParser()
        parser.add_argument("--reload", action="store_true")
        args = parser.parse_args([])
        self.assertFalse(bool(args.reload))

    def test_reload_flag_enabled_when_passed(self) -> None:
        parser = run_web.argparse.ArgumentParser()
        parser.add_argument("--reload", action="store_true")
        args = parser.parse_args(["--reload"])
        self.assertTrue(bool(args.reload))

    def test_is_port_bindable_false_when_occupied(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        port = int(probe.getsockname()[1])
        try:
            self.assertFalse(bool(run_web._is_port_bindable("127.0.0.1", port)))
        finally:
            probe.close()

    def test_pick_port_with_retry_finds_next_port(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        base_port = int(probe.getsockname()[1])
        try:
            selected, offset = run_web._pick_port_with_retry("127.0.0.1", base_port, 5)
            self.assertGreaterEqual(selected, base_port + 1)
            self.assertGreaterEqual(offset, 1)
            self.assertEqual(selected, base_port + offset)
        finally:
            probe.close()

    def test_pick_port_with_retry_raises_when_no_retry(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        base_port = int(probe.getsockname()[1])
        try:
            with self.assertRaises(RuntimeError):
                run_web._pick_port_with_retry("127.0.0.1", base_port, 0)
        finally:
            probe.close()

    def test_is_port_bindable_returns_false_when_cross_env_busy(self) -> None:
        with patch("run_web._is_port_bindable_local", return_value=True), \
             patch("run_web._is_port_in_use_cross_env", return_value=True):
            self.assertFalse(bool(run_web._is_port_bindable("127.0.0.1", 8000)))

    def test_is_port_bindable_returns_true_when_local_and_cross_env_free(self) -> None:
        with patch("run_web._is_port_bindable_local", return_value=True), \
             patch("run_web._is_port_in_use_cross_env", return_value=False):
            self.assertTrue(bool(run_web._is_port_bindable("127.0.0.1", 8000)))

    def test_is_port_in_use_on_wsl_side_reads_tracking_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_wsl_side_") as tmp:
            ports_file = Path(tmp) / ".tinda_ports.list"
            ports_file.write_text("8000\n", encoding="utf-8")
            with patch("run_web._ports_file_path", return_value=ports_file):
                self.assertTrue(bool(run_web._is_port_in_use_on_wsl_side(8000)))
                self.assertFalse(bool(run_web._is_port_in_use_on_wsl_side(8001)))

    def test_pick_port_waits_for_base_port_before_increment(self) -> None:
        checks = {"n": 0}

        def _fake_bindable(_host: str, port: int) -> bool:
            checks["n"] += 1
            # First check at base port fails, second succeeds after wait loop.
            if int(port) == 8000 and checks["n"] >= 2:
                return True
            return False

        with patch("run_web._is_port_bindable", side_effect=_fake_bindable), \
             patch("run_web.time.sleep", return_value=None):
            selected, offset = run_web._pick_port_with_retry(
                "127.0.0.1",
                8000,
                5,
                first_port_wait_ms=300,
                first_port_poll_ms=50,
            )
        self.assertEqual(selected, 8000)
        self.assertEqual(offset, 0)

    def test_pick_port_increments_when_base_not_released_in_wait_window(self) -> None:
        def _fake_bindable(_host: str, port: int) -> bool:
            return int(port) == 8001

        with patch("run_web._is_port_bindable", side_effect=_fake_bindable), \
             patch("run_web.time.sleep", return_value=None):
            selected, offset = run_web._pick_port_with_retry(
                "127.0.0.1",
                8000,
                5,
                first_port_wait_ms=120,
                first_port_poll_ms=60,
            )
        self.assertEqual(selected, 8001)
        self.assertEqual(offset, 1)


if __name__ == "__main__":
    unittest.main()
