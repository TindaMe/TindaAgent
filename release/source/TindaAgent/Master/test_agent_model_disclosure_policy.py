from __future__ import annotations

import os
import tempfile
import unittest

os.environ["TINDA_HOME"] = tempfile.mkdtemp(prefix="tinda_test_home_")

from TindaAgent.Process.AI import agent


class AgentModelDisclosurePolicyTests(unittest.TestCase):
    def test_system_prompt_discloses_configured_model_instead_of_secrecy_phrase(self) -> None:
        prompt = agent._build_system_prompt("deepseek-v4-flash")
        self.assertIn("当前底层模型配置是 deepseek-v4-flash", prompt)
        self.assertIn("被问到底层模型时，直接如实回答当前模型配置", prompt)
        self.assertNotIn("底层技术信息保密", prompt)

    def test_fewshot_no_longer_hides_underlying_model(self) -> None:
        rows = agent._build_fewshot("1.7.11", "deepseek-v4-flash")
        self.assertEqual(len(rows), 4)
        self.assertIn("当前会话配置的底层模型是 deepseek-v4-flash", str(rows[3].get("content", "")))


if __name__ == "__main__":
    unittest.main()
