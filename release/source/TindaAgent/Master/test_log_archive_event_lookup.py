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
        client = ai_client.LLMClient(api_key="test", base_url="https://example.com", model="deepseek-v4-flash")

        class _FakeCompletions:
            def __init__(self) -> None:
                self.last_req = None

            def create(self, **req):
                self.last_req = dict(req)
                return SimpleNamespace(ok=True)

        fake = _FakeCompletions()
        client._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))

        out = client._create_chat_completion_with_retry(
            func="test.stream",
            stream=True,
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.1,
        )
        self.assertTrue(bool(getattr(out, "ok", False)))
        self.assertIsNotNone(fake.last_req)
        self.assertTrue(bool(fake.last_req.get("stream") is True))

    def test_ai_client_create_chat_completion_with_retry_preserves_stream_false(self) -> None:
        client = ai_client.LLMClient(api_key="test", base_url="https://example.com", model="deepseek-v4-flash")

        class _FakeCompletions:
            def __init__(self) -> None:
                self.last_req = None

            def create(self, **req):
                self.last_req = dict(req)
                return SimpleNamespace(ok=True)

        fake = _FakeCompletions()
        client._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))

        out = client._create_chat_completion_with_retry(
            func="test.non_stream",
            stream=False,
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.1,
        )
        self.assertTrue(bool(getattr(out, "ok", False)))
        self.assertIsNotNone(fake.last_req)
        self.assertTrue(bool(fake.last_req.get("stream") is False))

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
        self.assertIn("must not directly quote previous tool-call records", prompt)
        self.assertIn("must not assume tool outputs", prompt)
        self.assertIn("fabrication is strictly forbidden", prompt)

    def test_run_terminal_redacts_secret_output(self) -> None:
        out = tool.run_terminal(cmd='printf "DEEPSEEK_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456"', _caller_perm=511, _confirmed=True)
        self.assertTrue(bool(out.get("ok")))
        text = str(out.get("output", ""))
        self.assertIn("***REDACTED***", text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", text)

    def test_ai_client_format_llm_error_api_connection(self) -> None:
        req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        err = ai_client.APIConnectionError(request=req)
        code, user_message = ai_client._format_llm_error(err)
        self.assertEqual(code, "upstream_connection_error")
        self.assertIn("连接失败", user_message)

    def test_ai_client_is_retryable_llm_error_rate_limit(self) -> None:
        req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        resp = httpx.Response(429, request=req)
        err = ai_client.RateLimitError("rate limit", response=resp, body={"error": "rate limit"})
        self.assertTrue(bool(ai_client._is_retryable_llm_error(err)))

    def test_ai_client_is_retryable_llm_error_status_500(self) -> None:
        req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        resp = httpx.Response(500, request=req)
        err = ai_client.APIStatusError("server error", response=resp, body=None)
        self.assertTrue(bool(ai_client._is_retryable_llm_error(err)))

    def test_ai_client_is_retryable_llm_error_status_400(self) -> None:
        req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        resp = httpx.Response(400, request=req)
        err = ai_client.APIStatusError("bad request", response=resp, body=None)
        self.assertFalse(bool(ai_client._is_retryable_llm_error(err)))

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
        with patch("TindaAgent.Process.AI.tokenizer.estimate_tokens", side_effect=lambda text: len(str(text))):
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

        self.assertEqual(usage, len("u") + len("aa") + len("sss"))

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

    def test_auto_compress_triggers_by_raw_chat_count_when_token_not_over_limit(self) -> None:
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

            def compress_context(self, _session_id: str, _summary: str) -> dict:
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
             patch.object(server, "_store_to_agent_messages", return_value=([{"role": "system", "content": "summary"}], {})):
            info = server._maybe_auto_compress(sid, context_rows=raw_rows)

        self.assertTrue(bool(info.get("compressed")))
        self.assertEqual(str(info.get("trigger", "")), "raw_chat_count")
        self.assertEqual(int(info.get("estimated_tokens_before", -1)), 1200)
        self.assertEqual(int(info.get("estimated_tokens_after", -1)), 300)
        self.assertEqual(int(fake_store.compress_calls), 1)
        self.assertEqual(int(fake_agent.replace_calls), 1)

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

            def compress_context(self, _session_id: str, _summary: str) -> dict:
                self.compress_calls += 1
                self._rows = self._rows[-4:]
                return {"ok": True}

        fake_agent = _FakeAgent()
        fake_store = _FakeStore(raw_rows)
        with patch.dict(server._sessions, {sid: fake_agent}, clear=True), \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_compress_messages_with_llm", return_value="summary"), \
             patch.object(server, "_store_to_agent_messages", return_value=([{"role": "system", "content": "summary"}], {})):
            info = server._maybe_auto_compress(sid, context_rows=raw_rows)

        self.assertTrue(bool(info.get("compressed")))
        self.assertEqual(str(info.get("trigger", "")), "token")
        self.assertEqual(int(fake_store.compress_calls), 1)
        self.assertEqual(int(fake_agent.replace_calls), 1)

    def test_audit_engine_falls_back_when_preferred_log_root_not_writable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_audit_fallback_") as tmp:
            preferred = Path(tmp) / "readonly_mount" / "log"
            fake_home = Path(tmp) / "home"
            expected_root = (fake_home / ".tinda" / "agent" / "log").resolve()
            preferred.mkdir(parents=True, exist_ok=True)
            (preferred / "id_counter.txt").write_text("4321\n", encoding="utf-8")
            os.environ.pop("TINDA_ACTIVE_LOG_ROOT", None)
            call_count = {"value": 0}

            def _fake_probe(path: Path) -> tuple[bool, str]:
                call_count["value"] += 1
                if call_count["value"] == 1:
                    return False, "Read-only file system"
                path.mkdir(parents=True, exist_ok=True)
                return True, ""

            with patch("pathlib.Path.home", return_value=fake_home), \
                 patch.object(GlobalAuditEngine, "_probe_writable_dir", side_effect=_fake_probe):
                engine = GlobalAuditEngine(log_root=preferred)

            self.assertEqual(engine._files.root, expected_root)
            self.assertEqual(int(engine._current_id), 4321)
            self.assertEqual(os.getenv("TINDA_ACTIVE_LOG_ROOT", ""), str(expected_root))
            self.assertTrue(engine._files.error_text.exists())
            err_text = engine._files.error_text.read_text(encoding="utf-8")
            self.assertIn("log_root_fallback", err_text)
            os.environ.pop("TINDA_ACTIVE_LOG_ROOT", None)

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
        with tempfile.TemporaryDirectory(prefix="tinda_log_active_root_server_") as tmp:
            root = Path(tmp)
            primary = root / "primary"
            active = root / "active"
            primary.mkdir(parents=True, exist_ok=True)
            active.mkdir(parents=True, exist_ok=True)
            (active / "total.jsonl").write_text(
                json.dumps({"id": 777, "subsystem": "context_injection", "content": "active_root"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with patch.object(server, "_LOG_ROOT", primary), \
                 patch.object(server, "get_log_root", return_value=primary), \
                 patch.object(server, "get_legacy_log_root", return_value=root / "legacy_none"), \
                 patch.dict(os.environ, {"TINDA_ACTIVE_LOG_ROOT": str(active)}, clear=False):
                row = server._find_audit_event_by_id(777)

        self.assertIsNotNone(row)
        self.assertEqual(int(row.get("event", {}).get("id", -1)), 777)
        self.assertEqual(str(row.get("source_file", "")), "total.jsonl")

    def test_server_find_audit_event_by_id_prefers_primary_archive_on_id_collision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_log_active_root_server_collision_") as tmp:
            root = Path(tmp)
            primary = root / "primary"
            active = root / "active"
            primary.mkdir(parents=True, exist_ok=True)
            active.mkdir(parents=True, exist_ok=True)
            with gzip.open(primary / "total.20260501_111111.jsonl.gz", "wt", encoding="utf-8") as fp:
                fp.write(json.dumps({"id": 555, "content": "from_primary_archive"}, ensure_ascii=False) + "\n")
            (active / "total.jsonl").write_text(
                json.dumps({"id": 555, "content": "from_active_total"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with patch.object(server, "_LOG_ROOT", primary), \
                 patch.object(server, "get_log_root", return_value=primary), \
                 patch.object(server, "get_legacy_log_root", return_value=root / "legacy_none"), \
                 patch.dict(os.environ, {"TINDA_ACTIVE_LOG_ROOT": str(active)}, clear=False):
                row = server._find_audit_event_by_id(555)

        self.assertIsNotNone(row)
        self.assertTrue(str(row.get("source_file", "")).endswith(".jsonl.gz"))
        self.assertEqual(str(row.get("event", {}).get("content", "")), "from_primary_archive")

    def test_tool_get_log_event_by_id_reads_active_log_root_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_log_active_root_tool_") as tmp:
            root = Path(tmp)
            primary = root / "primary"
            active = root / "active"
            primary.mkdir(parents=True, exist_ok=True)
            active.mkdir(parents=True, exist_ok=True)
            (active / "total.jsonl").write_text(
                json.dumps({"id": 888, "subsystem": "tool", "content": "active_root"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with patch.object(tool, "get_log_root", return_value=primary), \
                 patch.object(tool, "get_legacy_log_root", return_value=root / "legacy_none"), \
                 patch.dict(os.environ, {"TINDA_ACTIVE_LOG_ROOT": str(active)}, clear=False):
                out = tool.get_log_event_by_id("888")

        self.assertTrue(bool(out.get("ok")))
        self.assertEqual(int(out.get("id", -1)), 888)
        self.assertEqual(str(out.get("source_file", "")), "total.jsonl")

    def test_tool_get_log_event_by_id_prefers_primary_archive_on_id_collision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_log_active_root_tool_collision_") as tmp:
            root = Path(tmp)
            primary = root / "primary"
            active = root / "active"
            primary.mkdir(parents=True, exist_ok=True)
            active.mkdir(parents=True, exist_ok=True)
            with gzip.open(primary / "total.20260501_222222.jsonl.gz", "wt", encoding="utf-8") as fp:
                fp.write(json.dumps({"id": 556, "content": "from_primary_archive"}, ensure_ascii=False) + "\n")
            (active / "total.jsonl").write_text(
                json.dumps({"id": 556, "content": "from_active_total"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with patch.object(tool, "get_log_root", return_value=primary), \
                 patch.object(tool, "get_legacy_log_root", return_value=root / "legacy_none"), \
                 patch.dict(os.environ, {"TINDA_ACTIVE_LOG_ROOT": str(active)}, clear=False):
                out = tool.get_log_event_by_id("556")

        self.assertTrue(bool(out.get("ok")))
        self.assertTrue(str(out.get("source_file", "")).endswith(".jsonl.gz"))
        self.assertEqual(str(out.get("event", {}).get("content", "")), "from_primary_archive")

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
        sid = "s_no_pending"

        class _Req:
            async def json(self):
                return {
                    "session_id": sid,
                    "approval": True,
                    "cmd": "echo hi",
                }

        class _FakeStore:
            def ensure_session(self, _sid: str) -> None:
                return None

        with patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_terminal_pending", {}), \
             patch.object(server, "_require_login", return_value=object()):
            resp = asyncio.run(server.terminal_confirm(_Req()))

        self.assertEqual(int(resp.status_code), 400)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertFalse(bool(payload.get("ok", True)))
        self.assertEqual(str(payload.get("error_code", "")), "no_pending_for_session")
        self.assertEqual(int(payload.get("pending_confirm_count", -1)), 0)

    def test_terminal_confirm_returns_json_on_resume_failure(self) -> None:
        sid = "s_confirm_runtime_fail"
        pending = {
            sid: {
                "cmd": "echo real",
                "status": "pending",
                "approval": None,
                "created_at": "2026-05-02T00:00:00+08:00",
                "updated_at": "2026-05-02T00:00:01+08:00",
            }
        }

        class _Req:
            async def json(self):
                return {
                    "session_id": sid,
                    "approval": True,
                    "cmd": "echo real",
                }

        class _FakeStore:
            def ensure_session(self, _sid: str) -> None:
                return None

        class _FakeAgent:
            def has_pending_confirmation(self) -> bool:
                return True

            def resume_with_confirmations(self, _decisions: list[dict]) -> dict:
                raise ValueError("bad request from upstream")

        with patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_terminal_pending", pending), \
             patch.object(server, "_require_login", return_value=object()), \
             patch.object(server, "_get_agent", return_value=_FakeAgent()):
            resp = asyncio.run(server.terminal_confirm(_Req()))

        self.assertEqual(int(resp.status_code), 500)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertFalse(bool(payload.get("ok", True)))
        self.assertEqual(str(payload.get("error_code", "")), "terminal_confirm_failed")

    def test_delete_all_sessions_clears_runtime_caches(self) -> None:
        server._sessions.clear()
        server._session_last_access.clear()
        server._agent_context_sig.clear()
        server._terminal_pending.clear()

        server._sessions["s_a"] = object()
        server._session_last_access["s_a"] = 123.0
        server._agent_context_sig["s_a"] = "sig"
        server._terminal_pending["s_a"] = {"cmd": "echo", "status": "pending", "approval": None}

        class _FakeStore:
            def __init__(self) -> None:
                self.cleared = False

            def clear_all(self) -> None:
                self.cleared = True

        fake_store = _FakeStore()

        with patch.object(server, "_store", fake_store), \
             patch.object(server, "_require_login", return_value=object()):
            resp = asyncio.run(server.delete_all_sessions())

        self.assertEqual(int(resp.status_code), 200)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertTrue(bool(payload.get("cleared")))
        self.assertTrue(bool(fake_store.cleared))
        self.assertEqual(server._sessions, {})
        self.assertEqual(server._session_last_access, {})
        self.assertEqual(server._agent_context_sig, {})
        self.assertEqual(server._terminal_pending, {})

    def test_chat_rejects_when_pending_confirmation_exists(self) -> None:
        sid = "s_chat_pending_guard"

        class _FakeStore:
            def ensure_session(self, _sid: str) -> None:
                return None

        req = server.ChatRequest(message="继续执行", session_id=sid)
        with patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_require_login", return_value=object()), \
             patch.object(server, "_has_llm_perm", return_value=True), \
             patch.object(server, "_pending_confirm_count", return_value=2), \
             patch.object(server, "_get_agent") as get_agent_mock:
            resp = asyncio.run(server.chat(req))

        self.assertEqual(int(resp.status_code), 409)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(str(payload.get("error_code", "")), "pending_confirmation_required")
        self.assertEqual(int(payload.get("pending_confirm_count", -1)), 2)
        get_agent_mock.assert_not_called()

    def test_get_agent_preserve_pending_skips_reload_on_context_sig_change(self) -> None:
        sid = "s_preserve_pending_agent"

        class _FakeCurrentUser:
            def get_perm(self) -> int:
                return 7

        class _FakeAgent:
            def __init__(self) -> None:
                self.perm = 7
                self.replace_calls = 0

            def has_pending_confirmation(self) -> bool:
                return True

            def replace_conversation(self, _rows: list[dict]) -> None:
                self.replace_calls += 1

        class _FakeStore:
            def get_context_messages(self, _sid: str) -> list[dict]:
                return [{"id": "m1", "role": "user", "entry_type": "chat", "content": "hello"}]

        fake_agent = _FakeAgent()
        sig_after = ""
        with patch.dict(server._sessions, {sid: fake_agent}, clear=True), \
             patch.dict(server._agent_context_sig, {sid: "old_sig"}, clear=True), \
             patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_store_to_agent_messages", return_value=([{"role": "user", "content": "hello"}], {})), \
             patch.object(server, "_build_agent_context_sig", return_value="new_sig"), \
             patch.object(server, "_require_login", return_value=_FakeCurrentUser()):
            out_agent = server._get_agent(sid, preserve_pending=True)
            sig_after = str(server._agent_context_sig.get(sid, ""))

        self.assertIs(out_agent, fake_agent)
        self.assertEqual(int(fake_agent.replace_calls), 0)
        self.assertEqual(sig_after, "old_sig")

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

    def test_chat_html_no_random_confirm_id_fallback_for_pending(self) -> None:
        chat_html = Path(__file__).resolve().parents[1] / "Web" / "chat.html"
        content = chat_html.read_text(encoding="utf-8")
        self.assertNotIn('inner.confirm_id || ("cf_"', content)
        self.assertIn("pending-confirm-overlay", content)
        self.assertIn("submitPendingConfirmAction", content)
        self.assertIn("upsertPendingConfirmQueue", content)
        self.assertIn("renderPendingConfirmModal", content)
        self.assertIn("syncPendingConfirmations", content)
        self.assertIn("parseJsonSafe", content)
        self.assertIn("no_pending_for_session", content)
        self.assertNotIn("confirm_id_not_found_or_expired", content)
        self.assertIn("--工具调用中--", content)
        self.assertNotIn("renderTermConfirmInTerminal(", content)
        self.assertNotIn("term-confirm", content)

    def test_server_tool_marker_block_contains_call_ids(self) -> None:
        trace = [
            {
                "call_id": "tc_1001",
                "agent_tool": "call_backend_tool",
                "result": {"ok": True, "call_id": "tc_1001", "result": {"ok": True}},
            },
            {
                "tool_call_id": "tool_abc",
                "agent_tool": "call_backend_tool",
                "result": {"ok": True, "result": {"ok": True}},
            },
            {
                "agent_tool": "call_backend_tool",
                "result": {"ok": True, "call_id": "tc_1003", "result": {"ok": True}},
            },
            {
                "agent_tool": "run_terminal",
                "call_id": "tc_bad",
                "result": {"ok": False, "error": "cmd 不能为空", "result": {"ok": False}},
            },
        ]
        marker = server._build_tool_marker_block(trace)
        self.assertIn("> >_<", marker)
        self.assertIn("> --工具调用中--", marker)
        self.assertIn("> --call_id: tc_1001--", marker)
        self.assertIn("> --call_id: tool_abc--", marker)
        self.assertIn("> --call_id: tc_1003--", marker)
        self.assertNotIn("tc_bad", marker)

    def test_server_normalize_reply_tool_marker_deduplicates(self) -> None:
        raw = "好的，重新查。\n\n> --调用工具中--\n\n> --工具调用中--\n> --call_id: old--"
        trace = [
            {
                "call_id": "tc_2001",
                "agent_tool": "call_backend_tool",
                "result": {"ok": True, "result": {"ok": True}},
            }
        ]
        out = server._normalize_reply_tool_marker(raw, trace)
        self.assertIn("好的，重新查。", out)
        self.assertEqual(out.count("--工具调用中--"), 1)
        self.assertIn("> --call_id: tc_2001--", out)


if __name__ == "__main__":
    unittest.main()
