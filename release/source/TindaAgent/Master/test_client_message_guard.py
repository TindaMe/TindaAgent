from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["TINDA_HOME"] = tempfile.mkdtemp(prefix="tinda_test_home_")

from TindaAgent.Process.AI import client


class ClientMessageGuardTests(unittest.TestCase):
    def test_require_user_last_message_accepts_valid_input(self) -> None:
        with patch.object(client, "audit_event", return_value=1) as mock_audit:
            client._require_user_last_message(
                [
                    {"role": "system", "content": "ctx"},
                    {"role": "user", "content": "hello"},
                ],
                func="unit_test",
            )
        mock_audit.assert_not_called()

    def test_require_user_last_message_rejects_non_user_last_role(self) -> None:
        with patch.object(client, "audit_event", return_value=1) as mock_audit:
            with self.assertRaises(ValueError):
                client._require_user_last_message(
                    [
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "world"},
                    ],
                    func="unit_test",
                )
        self.assertEqual(mock_audit.call_count, 1)


if __name__ == "__main__":
    unittest.main()
