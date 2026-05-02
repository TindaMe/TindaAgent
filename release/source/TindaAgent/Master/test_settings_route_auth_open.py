import unittest

from TindaAgent.Web import server


class SettingsRouteAuthOpenTests(unittest.TestCase):
    def test_settings_path_is_auth_open(self) -> None:
        self.assertIn("/settings", server._AUTH_OPEN_PATHS)


if __name__ == "__main__":
    unittest.main()
