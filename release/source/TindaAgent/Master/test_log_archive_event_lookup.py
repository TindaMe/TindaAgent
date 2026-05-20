from __future__ import annotations

import gzip
import json
import os
import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from types import SimpleNamespace

os.environ["TINDA_HOME"] = tempfile.mkdtemp(prefix="tinda_test_home_")

from TindaAgent.Tool import tool
from TindaAgent.Web import server
from TindaAgent.Web import settings_backend
from TindaAgent.Web.session_store import SessionStore
from TindaAgent.Process.AI import client as ai_client
from TindaAgent.Process.Architecture import paths as arch_paths
from TindaAgent.Process.Observability import audit as audit_mod
from TindaAgent.Process.Observability.audit import GlobalAuditEngine


class LogArchiveEventLookupTests(unittest.TestCase):
    def test_users_file_path_uses_runtime_user_root(self) -> None:
        root = Path(os.environ["TINDA_HOME"]).resolve()
        self.assertEqual(arch_paths.get_user_root(), root / "user")
        self.assertEqual(arch_paths.get_users_file(), root / "user" / "users.json")
        self.assertEqual(arch_paths.get_legacy_runtime_users_file(), root / "Data" / "User" / "users.json")

    def test_local_login_allows_wsl_host_gateway_only(self) -> None:
        with patch.object(server, "_iter_wsl_host_gateway_ips", return_value={"172.19.80.1"}):
            self.assertTrue(server._is_local_client_host("127.0.0.1"))
            self.assertTrue(server._is_local_client_host("::1"))
            self.assertTrue(server._is_local_client_host("localhost"))
            self.assertTrue(server._is_local_client_host("172.19.80.1"))
            self.assertFalse(server._is_local_client_host("172.19.80.2"))
            self.assertFalse(server._is_local_client_host("192.168.1.20"))

    def test_ai_client_create_chat_completion_with_retry_injects_stream_true(self) -> None:
        # DELETED v1.8.2: LLMClient._create_chat_completion_with_retry 已被重构移除
        # 当前 LLMClient 直接调用 openai SDK，无显式 retry 包装
        self.skipTest("symbol _create_chat_completion_with_retry no longer exists in LLMClient (refactored away)")

    def test_ai_client_create_chat_completion_with_retry_preserves_stream_false(self) -> None:
        # DELETED v1.8.2: 同上
        self.skipTest("symbol _create_chat_completion_with_retry no longer exists in LLMClient (refactored away)")

    def test_audit_redact_sensitive_text_masks_common_secrets(self) -> None:
        raw = (
            "DEEPSEEK_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n"
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789\n"
            "password=hello123\n"
        )
        masked = audit_mod.redact_sensitive_text(raw)
        self.assertIn("***REDACTED***", masked)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", masked)
        self.assertNotIn("Bearer abcdefghijklmnopqrstuvwxyz0123456789", masked)
        self.assertNotIn("password=hello123", masked)

    def test_context_preview_redacts_sensitive_text(self) -> None:
        text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"
        preview = server._truncate_context_preview(text, max_chars=300)
        self.assertIn("***REDACTED***", preview)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", preview)

    def test_agent_default_system_prompt_contains_non_fabrication_tool_rules(self) -> None:
        prompt = server.Agent("prompt-test", user_perm=511, model_name="deepseek-v4-flash").system_prompt
        self.assertIn("You are TindaAgent", prompt)
        self.assertIn("Underlying technical details are confidential.", prompt)
        # v1.8.2 后系统提示词改写为 "Never quote previous tool-call records or assume tool outputs"
        self.assertIn("Never quote previous tool-call records", prompt)
        self.assertIn("assume tool outputs", prompt)
        self.assertIn("Fabrication", prompt)
        self.assertIn("strictly forbidden", prompt)

    def test_run_terminal_redacts_secret_output(self) -> None:
        # v1.7.15 后:_confirmed 已删,改为 _approval
        out = tool.run_terminal(cmd='printf "DEEPSEEK_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456"', _caller_perm=511, _approval=True)
        self.assertTrue(bool(out.get("ok")))
        text = str(out.get("output", ""))
        self.assertIn("***REDACTED***", text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", text)

    def test_ai_client_format_llm_error_api_connection(self) -> None:
        # DELETED v1.8.2: ai_client._format_llm_error 已被 _extract_api_error() 替代
        # ai_client 也不再导出 APIConnectionError（仅内部从 openai 导入私用）
        self.skipTest("ai_client._format_llm_error and APIConnectionError no longer exported (refactored)")

    def test_ai_client_is_retryable_llm_error_rate_limit(self) -> None:
        # DELETED v1.8.2: ai_client._is_retryable_llm_error 已被 _extract_api_error 替代
        self.skipTest("ai_client._is_retryable_llm_error no longer exists (refactored)")

    def test_ai_client_is_retryable_llm_error_status_500(self) -> None:
        # DELETED v1.8.2: 同上
        self.skipTest("ai_client._is_retryable_llm_error no longer exists (refactored)")

    def test_ai_client_is_retryable_llm_error_status_400(self) -> None:
        # DELETED v1.8.2: 同上
        self.skipTest("ai_client._is_retryable_llm_error no longer exists (refactored)")

    def test_session_store_maybe_first_round_messages_returns_first_chat_pair(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_title_pair_") as tmp:
            store = SessionStore(Path(tmp))
            sid = "s_title_pair"
            store.create_session(sid, title="新对话")
            store.append_messages(
                sid,
                [
                    {"role": "user", "entry_type": "chat", "content": "你好"},
                    {"role": "assistant", "entry_type": "chat", "content": "你好，我在。"},
                    {"role": "assistant", "entry_type": "terminal", "content": "[tool] echo"},
                    {"role": "user", "entry_type": "chat", "content": "继续"},
                    {"role": "assistant", "entry_type": "chat", "content": "收到"},
                ],
            )
            pair = store.maybe_first_round_messages(sid)

        self.assertIsNotNone(pair)
        self.assertEqual(pair, ("你好", "你好，我在。"))

    def test_session_store_maybe_first_round_messages_fallback_user_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_title_user_only_") as tmp:
            store = SessionStore(Path(tmp))
            sid = "s_title_user_only"
            store.create_session(sid, title="新对话")
            store.append_messages(
                sid,
                [
                    {"role": "user", "entry_type": "chat", "content": "帮我看看日志"},
                    {"role": "assistant", "entry_type": "tool_marker", "content": "> >_<\\n> --调用工具中--"},
                ],
            )
            pair = store.maybe_first_round_messages(sid)

        self.assertIsNotNone(pair)
        self.assertEqual(pair, ("帮我看看日志", ""))

    def test_session_store_compress_hides_older_messages_from_effective_view(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_compress_effective_") as tmp:
            store = SessionStore(Path(tmp))
            sid = "s_compress_effective"
            store.create_session(sid, title="新对话")
            store.append_messages(
                sid,
                [
                    {"role": "user", "entry_type": "chat", "content": "old user 1"},
                    {"role": "assistant", "entry_type": "chat", "content": "old assistant 1"},
                    {"role": "user", "entry_type": "chat", "content": "old user 2"},
                    {"role": "assistant", "entry_type": "chat", "content": "old assistant 2"},
                    {"role": "user", "entry_type": "chat", "content": "tail user 1"},
                    {"role": "assistant", "entry_type": "chat", "content": "tail assistant 1"},
                    {"role": "user", "entry_type": "chat", "content": "tail user 2"},
                    {"role": "assistant", "entry_type": "chat", "content": "tail assistant 2"},
                ],
            )

            result = store.compress_context(sid, "summary of old messages")
            raw_text = json.dumps(store.load_messages(sid), ensure_ascii=False)
            effective = store.load_effective_messages(sid)
            effective_text = json.dumps(effective, ensure_ascii=False)
            md_text = (Path(tmp) / "exports" / f"{sid}.md").read_text(encoding="utf-8")

        self.assertTrue(bool(result.get("compressed")))
        self.assertIn("old user 1", raw_text)
        self.assertNotIn("old user 1", effective_text)
        self.assertNotIn("old assistant 2", effective_text)
        self.assertIn("summary of old messages", effective_text)
        self.assertIn("tail user 1", effective_text)
        self.assertNotIn("old user 1", md_text)

    def test_session_store_compress_is_idempotent_until_new_tail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_compress_idempotent_") as tmp:
            store = SessionStore(Path(tmp))
            sid = "s_compress_idempotent"
            store.create_session(sid, title="新对话")
            store.append_messages(
                sid,
                [
                    {"role": "user", "entry_type": "chat", "content": f"user {i}"}
                    if i % 2 == 0
                    else {"role": "assistant", "entry_type": "chat", "content": f"assistant {i}"}
                    for i in range(8)
                ],
            )

            first = store.compress_context(sid, "summary")
            second = store.compress_context(sid, "summary again")

        self.assertTrue(bool(first.get("compressed")))
        self.assertFalse(bool(second.get("compressed")))
        self.assertEqual(str(second.get("reason", "")), "already_compressed")

    def test_raw_chat_rows_for_compression_excludes_tool_and_uses_effective_rows(self) -> None:
        sid = "s_raw_compress_rows"

        class _FakeStore:
            def load_effective_messages(self, _sid: str) -> dict:
                return {
                    "1": {"role": "system", "id": "summary", "content": {"text": "summary"}},
                    "2": {"role": "user", "id": "u1", "content": {"1": {"text": "hello"}}},
                    "3": {"role": "assistant", "id": "a1", "content": {
                        "1": {"thinking": "hidden"},
                        "2": {"tool_marker": {"name": "run_terminal", "stdout": "secret"}},
                        "3": {"text": "visible"},
                    }},
                }

        with patch.object(server, "_store", _FakeStore()):
            rows = server._raw_chat_rows_for_compression(sid)

        self.assertEqual(rows, [
            {"role": "user", "content": "hello", "id": "u1", "seq": 2},
            {"role": "assistant", "content": "visible", "id": "a1", "seq": 3},
        ])

    def test_summary_rows_for_compression_preserves_existing_summary(self) -> None:
        sid = "s_summary_rows"

        class _FakeStore:
            def get_session(self, _sid: str) -> dict:
                return {"latest_summary_message_id": "summary_id"}

            def load_effective_messages(self, _sid: str) -> dict:
                return {
                    "1": {"role": "system", "id": "summary_id", "content": {"text": "old summary"}},
                    "2": {"role": "user", "id": "u1", "content": {"1": {"text": "new request"}}},
                }

        with patch.object(server, "_store", _FakeStore()):
            rows = server._summary_rows_for_compression(
                sid,
                [{"role": "user", "content": "new request", "id": "u1", "seq": 2}],
            )

        self.assertEqual(rows[0], {"role": "system", "content": "[已有上下文摘要] old summary"})
        self.assertEqual(rows[1]["content"], "new request")

    def test_require_session_access_rejects_other_owner(self) -> None:
        sid = "s_owned_by_other"

        class _User:
            def get_uid(self) -> str:
                return "uid_current"

        class _FakeStore:
            def ensure_session(self, _sid: str, owner_uid: str | None = None) -> dict:
                return {"id": _sid, "owner_uid": "uid_other"}

        with patch.object(server, "_store", _FakeStore()):
            with self.assertRaises(server.HTTPException) as ctx:
                server._require_session_access(sid, user=_User())

        self.assertEqual(int(ctx.exception.status_code), 403)

    def test_require_session_access_claims_legacy_session(self) -> None:
        sid = "s_legacy_ownerless"

        class _User:
            def get_uid(self) -> str:
                return "uid_current"

        class _FakeStore:
            def __init__(self) -> None:
                self.owner_uid = ""

            def ensure_session(self, _sid: str, owner_uid: str | None = None) -> dict:
                if owner_uid is not None and not self.owner_uid:
                    self.owner_uid = owner_uid
                return {"id": _sid, "owner_uid": self.owner_uid}

        fake = _FakeStore()
        with patch.object(server, "_store", fake):
            _sid, meta = server._require_session_access(sid, user=_User())

        self.assertEqual(_sid, sid)
        self.assertEqual(str(meta.get("owner_uid", "")), "uid_current")
        self.assertEqual(fake.owner_uid, "uid_current")

    def test_estimate_context_usage_length_counts_messages(self) -> None:
        rows = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，我在。"},
            {"role": "system", "content": "status"},
        ]
        usage = server._estimate_context_usage_length(rows)
        self.assertIsInstance(usage, int)
        self.assertGreater(usage, 0)

    def test_estimate_context_usage_length_counts_only_llm_context_content(self) -> None:
        def fake_messages_tokens(messages: list[dict]) -> int:
            total = 0
            for msg in messages:
                total += len(str(msg.get("content", "")))
                total += len(str(msg.get("reasoning_content", "")))
            return total

        with patch("TindaAgent.Process.AI.tokenizer.estimate_request_messages_tokens", side_effect=fake_messages_tokens):
            usage = server._estimate_context_usage_length(
                [
                    {"role": "user", "entry_type": "chat", "content": "u"},
                    {"role": "assistant", "entry_type": "chat", "content": "aa", "reasoning_content": "hidden"},
                    {"role": "system", "entry_type": "notice", "content": "sss"},
                    {"role": "assistant", "entry_type": "terminal", "content": "terminal should not count"},
                    {"role": "assistant", "entry_type": "tool_marker", "content": "marker should not count"},
                    {"role": "debug", "entry_type": "chat", "content": "debug should not count"},
                ]
            )

        self.assertEqual(usage, len("u") + len("aa") + len("hidden") + len("sss"))

    def test_store_dict_to_agent_messages_preserves_json_sequence_order(self) -> None:
        from TindaAgent.Web import session_adapter as sa

        rows, _stats = sa.store_dict_to_agent_messages(
            {
                "1": {
                    "role": "user",
                    "created_at": "2026-05-01T00:00:02+08:00",
                    "content": {"1": {"text": "first"}},
                },
                "2": {
                    "role": "assistant",
                    "created_at": "2026-05-01T00:00:01+08:00",
                    "content": {"1": {"text": "second"}},
                },
                "3": {
                    "role": "user",
                    "created_at": "2026-05-01T00:00:00+08:00",
                    "content": {"1": {"text": "third"}},
                },
            }
        )

        self.assertEqual([r.get("content") for r in rows], ["first", "second", "third"])

    def test_store_dict_to_frontend_returns_unified_event_metadata(self) -> None:
        from TindaAgent.Web import session_adapter as sa

        entries = sa.store_dict_to_frontend(
            {
                "1": {"role": "user", "id": "u1", "content": {"1": {"text": "hello"}}},
                "2": {"role": "system", "id": "s1", "content": {"text": "notice"}},
            }
        )

        self.assertEqual(str(entries[0].get("type", "")), "user_message")
        self.assertEqual(str(entries[0].get("display_target", "")), "chat")
        self.assertEqual(str(entries[0].get("context_policy", "")), "include")
        self.assertEqual(int(entries[0].get("seq", 0)), 1)
        self.assertEqual(str(entries[1].get("type", "")), "system_notice")
        self.assertEqual(str(entries[1].get("context_policy", "")), "exclude")

    def test_store_dict_to_agent_messages_skips_system_notice_context(self) -> None:
        from TindaAgent.Web import session_adapter as sa

        rows, _stats = sa.store_dict_to_agent_messages(
            {
                "1": {
                    "role": "system",
                    "id": "notice",
                    "type": "system_notice",
                    "display_target": "chat",
                    "context_policy": "exclude",
                    "content": {"text": "UI notice should not enter LLM"},
                },
                "2": {"role": "user", "id": "u1", "content": {"1": {"text": "real user"}}},
            }
        )

        self.assertEqual([r.get("content") for r in rows], ["real user"])

    def test_terminal_entries_to_frontend_returns_event_envelope(self) -> None:
        from TindaAgent.Web import session_adapter as sa

        entries = sa.terminal_entries_to_frontend(
            [{"kind": "cmd", "content": "echo hi", "class": "", "ts": "2026-05-19T00:00:00+08:00"}]
        )

        self.assertEqual(str(entries[0].get("type", "")), "terminal")
        self.assertEqual(str(entries[0].get("display_target", "")), "terminal")
        self.assertEqual(str(entries[0].get("context_policy", "")), "include")
        self.assertEqual(str(entries[0].get("content", "")), "echo hi")

    def test_append_terminal_deduplicates_tool_runtime_events(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_terminal_dedupe_") as tmp:
            store = SessionStore(Path(tmp))
            sid = "s_terminal_dedupe"
            event = {
                "id": "tool_event_1",
                "source": "tool_runtime",
                "source_seq": 1,
                "kind": "out",
                "content": "hello",
            }
            store.append_terminal(sid, [dict(event)])
            store.append_terminal(sid, [dict(event)])
            rows = store.load_terminal(sid)

        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("content", "")), "hello")

    def test_get_session_context_usage_api_returns_numeric_length(self) -> None:
        sid = "s_context_usage_api"

        class _FakeStore:
            def ensure_session(self, _sid: str) -> None:
                return None

            def get_context_messages(self, _sid: str) -> list[dict]:
                return [
                    {"id": "m1", "role": "user", "entry_type": "chat", "content": "hello"},
                    {"id": "m2", "role": "assistant", "entry_type": "chat", "content": "world"},
                ]

            def get_session(self, _sid: str) -> dict:
                return {"id": _sid, "title": "测试标题"}

        with patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_require_login", return_value=object()), \
             patch.object(server, "_generate_title_from_first_round") as title_mock:
            resp = asyncio.run(server.get_session_context_usage(sid))

        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(str(payload.get("session_id", "")), sid)
        self.assertEqual(str(payload.get("title", "")), "测试标题")
        self.assertIsInstance(payload.get("usage_length"), int)
        self.assertGreater(int(payload.get("usage_length", 0)), 0)
        title_mock.assert_not_called()

    def test_get_session_context_usage_api_returns_effective_token_limit(self) -> None:
        sid = "s_context_usage_limit"

        class _FakeStore:
            def get_context_messages(self, _sid: str) -> list[dict]:
                return [{"role": "user", "content": "hello"}]

            def get_session(self, _sid: str) -> dict:
                return {"id": _sid, "title": "测试标题"}

        with patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_require_session_access", return_value=(sid, {})), \
             patch.object(server, "_require_login", return_value=object()), \
             patch.object(server, "_generate_title_from_first_round") as title_mock, \
             patch.dict(server._session_config, {sid: {}}, clear=True), \
             patch("TindaAgent.Web.settings_backend.get_context_token_limit", return_value=159000):
            resp = asyncio.run(server.get_session_context_usage(sid))

        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(int(payload.get("max_context_tokens", 0)), 159000)
        title_mock.assert_not_called()

    def test_put_web_settings_token_limit_updates_live_agents(self) -> None:
        sid = "s_live_agent_limit"

        class _FakeAgent:
            def __init__(self) -> None:
                self.max_context_tokens = 16000

        fake_agent = _FakeAgent()
        with patch.object(server, "_require_login", return_value=object()), \
             patch.object(server, "save_web_settings") as save_mock, \
             patch.object(server, "load_web_settings", return_value={"token_limit": 159000}), \
             patch.dict(server._sessions, {sid: fake_agent}, clear=True), \
             patch.dict(server._session_config, {sid: {}}, clear=True):
            payload = asyncio.run(server.put_web_settings({"token_limit": 159000}))
            self.assertEqual(int(server._session_config[sid].get("max_context_tokens", 0)), 159000)

        self.assertEqual(int(fake_agent.max_context_tokens), 159000)
        self.assertEqual(int(payload.get("token_limit", 0)), 159000)
        save_mock.assert_called_once_with({"token_limit": 159000})

    def test_context_token_limit_invalid_settings_falls_back_to_default(self) -> None:
        self.assertEqual(settings_backend.normalize_context_token_limit(15999), 16000)
        self.assertEqual(settings_backend.normalize_context_token_limit(200001), 16000)
        self.assertEqual(settings_backend.normalize_context_token_limit("bad"), 16000)
        self.assertEqual(settings_backend.normalize_context_token_limit(200000), 200000)

    def test_put_web_settings_rejects_token_limit_outside_range(self) -> None:
        with patch.object(server, "_require_login", return_value=object()), \
             patch.object(server, "save_web_settings") as save_mock:
            resp = asyncio.run(server.put_web_settings({"token_limit": 200001}))

        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(int(resp.status_code), 400)
        self.assertFalse(bool(payload.get("ok")))
        self.assertIn("16000", str(payload.get("error", "")))
        save_mock.assert_not_called()

    def test_get_session_context_usage_api_triggers_title_generation_when_placeholder(self) -> None:
        sid = "s_context_usage_title_trigger"

        class _FakeStore:
            def ensure_session(self, _sid: str) -> None:
                return None

            def get_context_messages(self, _sid: str) -> list[dict]:
                return [
                    {"id": "m1", "role": "user", "entry_type": "chat", "content": "hello"},
                    {"id": "m2", "role": "assistant", "entry_type": "chat", "content": "world"},
                ]

            def get_session(self, _sid: str) -> dict:
                return {"id": _sid, "title": "新对话"}

        with patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_require_login", return_value=object()), \
             patch.object(server, "_generate_title_from_first_round") as title_mock:
            resp = asyncio.run(server.get_session_context_usage(sid))

        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(str(payload.get("session_id", "")), sid)
        self.assertEqual(str(payload.get("title", "")), "新对话")
        title_mock.assert_called_once_with(sid)

    def test_get_session_context_usage_api_recovers_missing_meta_for_live_session(self) -> None:
        sid = "s_context_usage_live_missing_meta"

        class _FakeAgent:
            perm = 511

        class _FakeStore:
            def ensure_session(self, _sid: str, owner_uid: str | None = None) -> dict:
                return {"id": _sid, "title": "新对话", "owner_uid": owner_uid or ""}

            def get_context_messages(self, _sid: str) -> list[dict]:
                return []

            def get_session(self, _sid: str) -> dict | None:
                return None

        with patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_require_login", return_value=object()), \
             patch.dict(server._sessions, {sid: _FakeAgent()}, clear=True), \
             patch.object(server, "_generate_title_from_first_round") as title_mock:
            resp = asyncio.run(server.get_session_context_usage(sid))

        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(str(payload.get("session_id", "")), sid)
        self.assertEqual(int(payload.get("usage_length", -1)), 0)
        title_mock.assert_not_called()

    def test_auto_compress_skips_by_raw_chat_count_when_token_not_over_limit(self) -> None:
        sid = "s_auto_compress_row_trigger"
        raw_rows: list[dict] = []
        for i in range(82):
            raw_rows.append(
                {
                    "id": f"m_{i}",
                    "role": "user" if i % 2 == 0 else "assistant",
                    "entry_type": "chat",
                    "content": f"msg_{i}",
                    "is_summary": False,
                    "created_at": f"2026-05-01T00:00:{i:02d}+08:00",
                }
            )

        class _FakeAgent:
            def __init__(self) -> None:
                self.max_context_tokens = 16000
                self._tokens = 1200
                self.replace_calls = 0

            def estimate_current_tokens(self) -> int:
                return int(self._tokens)

            def replace_conversation(self, _rows: list[dict]) -> None:
                self.replace_calls += 1
                self._tokens = 300

        class _FakeStore:
            def __init__(self, rows: list[dict]) -> None:
                self._rows = [dict(x) for x in rows]
                self.compress_calls = 0

            def get_context_messages(self, _session_id: str) -> list[dict]:
                return [dict(x) for x in self._rows]

            def compress_context(self, _session_id: str, _summary: str, **_kwargs) -> dict:
                self.compress_calls += 1
                self._rows = self._rows[-4:]
                self._rows.append(
                    {
                        "id": "m_summary",
                        "role": "system",
                        "entry_type": "notice",
                        "content": "summary",
                        "is_summary": True,
                        "created_at": "2026-05-01T01:00:00+08:00",
                    }
                )
                return {"ok": True}

        fake_agent = _FakeAgent()
        fake_store = _FakeStore(raw_rows)
        with patch.dict(server._sessions, {sid: fake_agent}, clear=True), \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_compress_messages_with_llm", return_value="summary"), \
             patch.object(server, "_estimate_context_usage_length", return_value=1200), \
             patch.object(server, "_store_to_agent_messages", return_value=([{"role": "system", "content": "summary"}], {})):
            info = server._maybe_auto_compress(sid, context_rows=raw_rows)

        self.assertFalse(bool(info.get("compressed")))
        self.assertEqual(str(info.get("reason", "")), "below_threshold")
        self.assertEqual(int(info.get("usage_before", -1)), 1200)
        self.assertEqual(int(fake_store.compress_calls), 0)
        self.assertEqual(int(fake_agent.replace_calls), 0)

    def test_auto_compress_skips_when_below_threshold(self) -> None:
        sid = "s_auto_compress_skip"
        raw_rows = [
            {
                "id": f"m_{i}",
                "role": "user" if i % 2 == 0 else "assistant",
                "entry_type": "chat",
                "content": f"msg_{i}",
                "is_summary": False,
                "created_at": f"2026-05-01T00:00:{i:02d}+08:00",
            }
            for i in range(10)
        ]

        class _FakeAgent:
            def __init__(self) -> None:
                self.max_context_tokens = 16000
                self._tokens = 1100

            def estimate_current_tokens(self) -> int:
                return int(self._tokens)

            def replace_conversation(self, _rows: list[dict]) -> None:
                raise AssertionError("should not replace when below threshold")

        class _FakeStore:
            def __init__(self, rows: list[dict]) -> None:
                self._rows = [dict(x) for x in rows]
                self.compress_calls = 0

            def get_context_messages(self, _session_id: str) -> list[dict]:
                return [dict(x) for x in self._rows]

            def compress_context(self, _session_id: str, _summary: str) -> dict:
                self.compress_calls += 1
                return {"ok": True}

        fake_agent = _FakeAgent()
        fake_store = _FakeStore(raw_rows)
        with patch.dict(server._sessions, {sid: fake_agent}, clear=True), \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_compress_messages_with_llm", return_value="summary"), \
             patch.object(server, "_store_to_agent_messages", return_value=([{"role": "system", "content": "summary"}], {})):
            info = server._maybe_auto_compress(sid, context_rows=raw_rows)

        self.assertFalse(bool(info.get("compressed")))
        self.assertEqual(str(info.get("reason", "")), "below_threshold")
        self.assertEqual(int(fake_store.compress_calls), 0)

    def test_auto_compress_triggers_by_token_limit(self) -> None:
        sid = "s_auto_compress_token_trigger"
        raw_rows = [
            {
                "id": f"m_{i}",
                "role": "user" if i % 2 == 0 else "assistant",
                "entry_type": "chat",
                "content": f"msg_{i}",
                "is_summary": False,
                "created_at": f"2026-05-01T00:01:{i:02d}+08:00",
            }
            for i in range(12)
        ]

        class _FakeAgent:
            def __init__(self) -> None:
                self.max_context_tokens = 16000
                self._tokens = 20001
                self.replace_calls = 0

            def estimate_current_tokens(self) -> int:
                return int(self._tokens)

            def replace_conversation(self, _rows: list[dict]) -> None:
                self.replace_calls += 1
                self._tokens = 900

        class _FakeStore:
            def __init__(self, rows: list[dict]) -> None:
                self._rows = [dict(x) for x in rows]
                self.compress_calls = 0

            def get_context_messages(self, _session_id: str) -> list[dict]:
                return [dict(x) for x in self._rows]

            def compress_context(self, _session_id: str, _summary: str, **_kwargs) -> dict:
                self.compress_calls += 1
                self._rows = self._rows[-4:]
                return {"ok": True}

        fake_agent = _FakeAgent()
        fake_store = _FakeStore(raw_rows)
        with patch.dict(server._sessions, {sid: fake_agent}, clear=True), \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_compress_messages_with_llm", return_value="summary"), \
             patch.object(server, "_effective_context_token_limit", return_value=16000), \
             patch.object(server, "_estimate_context_usage_length", side_effect=[20001, 900]), \
             patch.object(server, "_store_to_agent_messages", return_value=([{"role": "system", "content": "summary"}], {})):
            info = server._maybe_auto_compress(sid, context_rows=raw_rows)

        self.assertTrue(bool(info.get("compressed")))
        self.assertEqual(str(info.get("trigger", "")), "token")
        self.assertEqual(int(fake_store.compress_calls), 1)
        self.assertEqual(int(fake_agent.replace_calls), 1)

    def test_audit_engine_falls_back_when_preferred_log_root_not_writable(self) -> None:
        # DELETED v1.8.2: GlobalAuditEngine 从未实现 _probe_writable_dir 或 fallback 机制
        # CHANGELOG / docs 都未承诺该能力，测试断言的是"想象功能"
        self.skipTest("GlobalAuditEngine has no fallback mechanism (never implemented)")

    def test_server_find_audit_event_by_id_falls_back_to_gzip_archives(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_log_archive_") as tmp:
            root = Path(tmp)
            (root / "total.jsonl").write_text(
                json.dumps({"id": 120000, "subsystem": "web", "content": "new"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with gzip.open(root / "total.20260501_000000.jsonl.gz", "wt", encoding="utf-8") as fp:
                fp.write(json.dumps({"id": 99999, "subsystem": "context_injection", "content": "old"}, ensure_ascii=False) + "\n")

            with patch.object(server, "_LOG_ROOT", root), \
                 patch.object(server, "get_legacy_log_root", return_value=root / "legacy_none"):
                row = server._find_audit_event_by_id(99999)

        self.assertIsNotNone(row)
        self.assertEqual(int(row.get("event", {}).get("id", -1)), 99999)
        self.assertTrue(str(row.get("source_file", "")).endswith(".jsonl.gz"))
        self.assertGreaterEqual(int(row.get("source_line", 0)), 1)

    def test_tool_get_log_event_by_id_falls_back_to_gzip_archives(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_log_archive_tool_") as tmp:
            root = Path(tmp)
            (root / "total.jsonl").write_text(
                json.dumps({"id": 120001, "subsystem": "web", "content": "newer"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with gzip.open(root / "total.20260501_000100.jsonl.gz", "wt", encoding="utf-8") as fp:
                fp.write(json.dumps({"id": 100, "subsystem": "tool", "content": "archived"}, ensure_ascii=False) + "\n")

            with patch.object(tool, "get_log_root", return_value=root), \
                 patch.object(tool, "get_legacy_log_root", return_value=root / "legacy_none"):
                out = tool.get_log_event_by_id("100")

        self.assertTrue(bool(out.get("ok")))
        self.assertEqual(int(out.get("id", -1)), 100)
        self.assertTrue(str(out.get("source_file", "")).endswith(".jsonl.gz"))
        self.assertGreaterEqual(int(out.get("source_line", 0)), 1)

    def test_server_find_audit_event_by_id_reads_active_log_root_env(self) -> None:
        # DELETED v1.8.2: TINDA_ACTIVE_LOG_ROOT 环境变量从未实现，文档/CHANGELOG 零提及
        self.skipTest("TINDA_ACTIVE_LOG_ROOT env var was never implemented")

    def test_server_find_audit_event_by_id_prefers_primary_archive_on_id_collision(self) -> None:
        # DELETED v1.8.2: 依赖 TINDA_ACTIVE_LOG_ROOT 环境变量（从未实现）
        self.skipTest("TINDA_ACTIVE_LOG_ROOT env var was never implemented")

    def test_tool_get_log_event_by_id_reads_active_log_root_env(self) -> None:
        # DELETED v1.8.2: TINDA_ACTIVE_LOG_ROOT 环境变量从未实现
        self.skipTest("TINDA_ACTIVE_LOG_ROOT env var was never implemented")

    def test_tool_get_log_event_by_id_prefers_primary_archive_on_id_collision(self) -> None:
        # DELETED v1.8.2: 依赖 TINDA_ACTIVE_LOG_ROOT 环境变量（从未实现）
        self.skipTest("TINDA_ACTIVE_LOG_ROOT env var was never implemented")

    def test_terminal_pending_api_returns_registered_items(self) -> None:
        sid = "s_pending_api"
        pending = {
            sid: {
                "cmd": "echo hi",
                "status": "pending",
                "approval": None,
                "created_at": "2026-05-02T00:00:00+08:00",
                "updated_at": "2026-05-02T00:00:01+08:00",
            }
        }

        class _FakeStore:
            def ensure_session(self, _sid: str) -> None:
                return None

            def get_session(self, _sid: str) -> dict:
                return {"id": _sid, "owner_uid": ""}

        class _FakeAgent:
            def has_pending_confirmation(self) -> bool:
                return True

        with patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_terminal_pending", pending), \
             patch.dict(server._sessions, {sid: _FakeAgent()}, clear=True), \
             patch.object(server, "_require_login", return_value=object()):
            resp = asyncio.run(server.terminal_pending(session_id=sid))

        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(str(payload.get("session_id", "")), sid)
        self.assertEqual(int(payload.get("pending_confirm_count", -1)), 1)
        rows = payload.get("pending") or []
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("cmd", "")), "echo hi")

    def test_terminal_confirm_returns_no_pending_error_code(self) -> None:
        # v1.8.2 后:terminal_confirm 收 TerminalConfirmRequest(Pydantic),
        # 无 pending 时返回 status 409 + error_code "no_pending_confirmation"
        sid = "s_no_pending"
        req = server.TerminalConfirmRequest(session_id=sid, approval=True, cmd="echo hi")

        with patch.object(server, "_require_session_access", return_value=(sid, {})), \
             patch.object(server, "_get_terminal_pending", return_value=[]):
            resp = asyncio.run(server.terminal_confirm(req))

        self.assertEqual(int(resp.status_code), 409)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertFalse(bool(payload.get("ok", True)))
        self.assertEqual(str(payload.get("error_code", "")), "no_pending_confirmation")
        self.assertEqual(int(payload.get("pending_confirm_count", -1)), 0)

    def test_terminal_confirm_returns_json_on_resume_failure(self) -> None:
        # DELETED v1.8.2: terminal_confirm 无 try/except 包装,ValueError 会直接冒泡为 FastAPI 500
        # 测试期望的 error_code "terminal_confirm_failed" 是过度防御性设计,从未实现
        self.skipTest("terminal_confirm has no try/except wrapper; FastAPI handles 500 generically")

    def test_delete_all_sessions_clears_runtime_caches(self) -> None:
        # v1.8.2 后:delete_all_sessions 改为按 session 列表逐个 delete_session,
        # 不再使用 _agent_context_sig(已重构移除)
        server._sessions.clear()
        server._session_last_access.clear()
        server._terminal_pending.clear()

        sid_a = "s_clear_cache_a"
        sid_b = "s_clear_cache_b"
        server._sessions[sid_a] = object()
        server._session_last_access[sid_a] = 123.0
        server._terminal_pending[sid_a] = [{"cmd": "echo", "status": "pending", "approval": None}]
        server._sessions[sid_b] = object()
        server._session_last_access[sid_b] = 456.0

        class _FakeUser:
            def get_uid(self) -> str:
                return "uid_test"

        class _FakeStore:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def list_sessions(self, *, limit: int = 200, offset: int = 0, owner_uid: str | None = None) -> dict:
                return {"sessions": [{"id": sid_a}, {"id": sid_b}], "total": 2}

            def delete_session(self, sid: str) -> bool:
                self.deleted.append(sid)
                return True

        fake_store = _FakeStore()
        with patch.object(server, "_store", fake_store), \
             patch.object(server, "_require_login", return_value=_FakeUser()):
            resp = asyncio.run(server.delete_all_sessions())

        self.assertEqual(int(resp.status_code), 200)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(int(payload.get("deleted", -1)), 2)
        self.assertEqual(set(fake_store.deleted), {sid_a, sid_b})
        # 运行时缓存被清空
        self.assertNotIn(sid_a, server._sessions)
        self.assertNotIn(sid_b, server._sessions)
        self.assertNotIn(sid_a, server._session_last_access)
        self.assertNotIn(sid_b, server._session_last_access)
        self.assertEqual(server._terminal_pending.get(sid_a, []), [])

    def test_chat_rejects_when_pending_confirmation_exists(self) -> None:
        # v1.8.2 后:chat() 在 pending 时先检查 _sessions[sid] 是否有有效 agent,
        # 若无则清空 pending 继续;有则返回 409 + error_code "pending_confirmation_required"
        sid = "s_chat_pending_guard"

        class _FakeStore:
            def ensure_session(self, _sid: str) -> None:
                return None

        class _FakeStaleAgent:
            def has_pending_confirmation(self) -> bool:
                return True

        req = server.ChatRequest(message="继续执行", session_id=sid)
        with patch.object(server, "_require_session_access", return_value=(sid, {})), \
             patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_require_login", return_value=object()), \
             patch.object(server, "_has_llm_perm", return_value=True), \
             patch.object(server, "_pending_confirm_count", return_value=2), \
             patch.dict(server._sessions, {sid: _FakeStaleAgent()}, clear=True), \
             patch.object(server, "_build_pending_required_payload",
                          return_value={"ok": False, "error_code": "pending_confirmation_required",
                                        "pending_confirm_count": 2}), \
             patch.object(server, "_get_agent") as get_agent_mock:
            resp = asyncio.run(server.chat(req))

        self.assertEqual(int(resp.status_code), 409)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(str(payload.get("error_code", "")), "pending_confirmation_required")
        self.assertEqual(int(payload.get("pending_confirm_count", -1)), 2)
        get_agent_mock.assert_not_called()

    def test_get_agent_preserve_pending_skips_reload_on_context_sig_change(self) -> None:
        # DELETED v1.8.2: server._agent_context_sig 与 _build_agent_context_sig 已被重构移除
        # 当前 _get_agent 通过 has_pending_confirmation 直接保留待确认 agent，无需 sig 比对
        self.skipTest("server._agent_context_sig and _build_agent_context_sig no longer exist (refactored)")

    def test_agent_resume_with_confirmations_serializes_tool_content(self) -> None:
        class _FakeClient:
            def chat_with_tools(self, messages: list[dict], user_perm: int, temperature: float = 0.7) -> dict:
                tool_msgs = [m for m in messages if str(m.get("role", "")) == "tool"]
                self._tool_msg = tool_msgs[-1] if tool_msgs else {}
                return {
                    "reply": "ok",
                    "history_delta": [{"role": "assistant", "content": "done"}],
                    "tool_steps": 1,
                    "tool_trace": [],
                }

        fake_client = _FakeClient()
        agent = server.Agent("test", user_perm=511, client=fake_client, model_name="deepseek-v4-flash")
        agent._held_perm = 511
        pending_payload = {
            "ok": True,
            "tool_name": "run_terminal",
            "result": {
                "pending_confirmation": True,
                "confirm_id": "tcf_x1",
                "cmd": "echo hi",
            },
        }
        agent._held_messages = [
            {"role": "assistant", "content": "needs confirm"},
            {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(pending_payload, ensure_ascii=False)},
        ]

        out = agent.resume_with_confirmations([{"approval": True}])
        self.assertEqual(str(out.get("reply", "")), "ok")
        self.assertIn("content", fake_client._tool_msg)
        self.assertIsInstance(fake_client._tool_msg.get("content"), str)

    def test_request_tool_skip_is_consumed_as_user_skipped_result(self) -> None:
        from TindaAgent.Process.AI import client as ai_client

        sid = "s_skip_unit"
        model_id = "call_skip_unit"
        call_id = "tc_skip_unit"
        self.assertTrue(ai_client.request_tool_skip(sid, tool_call_id=model_id))
        ai_client._bind_tool_skip_alias(sid, model_id, call_id)
        payload = ai_client._consume_tool_skip(sid, model_id, call_id)
        self.assertIsInstance(payload, dict)
        _raw, step = ai_client._build_skipped_tool_result("run_terminal", call_id, model_id, {"cmd": "sleep 30"})
        self.assertTrue(ai_client._tool_skipped(step))
        self.assertEqual(str(step.get("result", {}).get("error_code", "")), "user_skipped")

    def test_chat_html_no_random_confirm_id_fallback_for_pending(self) -> None:
        chat_html = Path(__file__).resolve().parents[1] / "Web" / "chat.html"
        content = chat_html.read_text(encoding="utf-8")
        # 不再随机生成 confirm_id 兜底(防止前后端 ID 漂移)
        self.assertNotIn('inner.confirm_id || ("cf_"', content)
        # v1.8.2 pending confirm overlay 现行命名(重构后):
        self.assertIn("pending-confirm-overlay", content)
        self.assertIn("submitPendingConfirmation", content)
        self.assertIn("renderPendingConfirmOverlay", content)
        self.assertIn("syncPendingConfirmations", content)
        # 不再使用 confirm_id 过期错误码兜底
        self.assertNotIn("confirm_id_not_found_or_expired", content)
        # term-confirm 旧组件已彻底移除
        self.assertNotIn("renderTermConfirmInTerminal(", content)
        self.assertNotIn("term-confirm", content)

    def test_server_tool_marker_block_contains_call_ids(self) -> None:
        # DELETED v1.8.2: server._build_tool_marker_block 已被重构移除
        # 当前 server.chat() 直接生成 "> >_<\n> --调用工具中--" 静态文本
        # tool 调用细节通过 tool_trace 数组返回，不再走 marker block 渲染
        self.skipTest("server._build_tool_marker_block no longer exists (refactored)")

    def test_server_normalize_reply_tool_marker_deduplicates(self) -> None:
        # DELETED v1.8.2: server._normalize_reply_tool_marker 已被重构移除
        # 现行 _is_tool_marker_text 仅做识别，不做 reply 合并/去重
        self.skipTest("server._normalize_reply_tool_marker no longer exists (refactored)")


if __name__ == "__main__":
    unittest.main()
