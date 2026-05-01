from __future__ import annotations

import os
import tempfile
import unittest

os.environ["TINDA_HOME"] = tempfile.mkdtemp(prefix="tinda_test_home_")

from TindaAgent.Process.AI.agent import Agent


class AgentContextRoleTests(unittest.TestCase):
    def test_replace_conversation_accepts_system_messages(self) -> None:
        agent = Agent("test_user", user_perm=511, model_name="deepseek-v4-flash")
        agent.replace_conversation(
            [
                {"role": "system", "content": "{\"format\":\"tinda_llm_json_input\",\"input_role\":\"system\"}"},
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
            ]
        )
        conv = agent.get_conversation_messages()
        self.assertEqual(len(conv), 3)
        self.assertEqual(conv[0]["role"], "system")
        self.assertEqual(conv[1]["role"], "user")
        self.assertEqual(conv[2]["role"], "assistant")

    def test_refresh_model_identity_updates_prompt_and_keeps_conversation(self) -> None:
        agent = Agent("test_user", user_perm=511, model_name="deepseek-v4-flash")
        agent.replace_conversation(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ]
        )
        before = agent.get_conversation_messages()
        self.assertIn("deepseek-v4-flash", agent.system_prompt)

        agent.refresh_model_identity("deepseek-v4-pro")

        after = agent.get_conversation_messages()
        self.assertEqual(before, after)
        self.assertIn("deepseek-v4-pro", agent.system_prompt)


if __name__ == "__main__":
    unittest.main()
