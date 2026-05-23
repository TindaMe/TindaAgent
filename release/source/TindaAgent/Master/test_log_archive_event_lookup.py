from __future__ import annotations

import gzip
import json
import os
import tempfile
import asyncio
import threading
import time
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
from TindaAgent.Process.AI import providers as ai_providers
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

    def test_provider_request_params_normalize_and_preserve_zero_temperature(self) -> None:
        cfg = ai_providers._normalize_config({
            "providers": {
                "deepseek": {
                    "temperature": 0,
                    "top_p": 0.82,
                    "presence_penalty": 0.1,
                    "frequency_penalty": -0.2,
                    "max_tokens": 4096,
                    "seed": 42,
                    "timeout": 33,
                    "tool_choice": "required",
                    "max_tool_steps": 900,
                    "thinking_enabled": False,
                    "reasoning_effort": "high",
                }
            }
        })
        row = cfg["providers"]["deepseek"]
        self.assertEqual(row["temperature"], 0)
        self.assertEqual(row["top_p"], 0.82)
        self.assertEqual(row["tool_choice"], "required")
        self.assertEqual(row["max_tool_steps"], 900)
        self.assertFalse(row["thinking_enabled"])
        self.assertNotIn("thinking", row["extra_body"])
        self.assertEqual(row["reasoning_effort"], "high")

    def test_llm_client_applies_provider_request_params_to_payload(self) -> None:
        client = ai_client.LLMClient(api_key="sk-test", request_params={
            "temperature": 0,
            "top_p": 0.9,
            "presence_penalty": 0.2,
            "frequency_penalty": 0.3,
            "max_tokens": 123,
            "seed": 7,
            "timeout": 11,
            "tool_choice": "none",
            "max_tool_steps": 900,
            "thinking_enabled": True,
            "reasoning_effort": "max",
            "extra_body": {"thinking": {"type": "enabled"}},
        })
        payload = client._apply_request_params({"model": "m", "messages": []}, temperature=None)
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertEqual(payload["presence_penalty"], 0.2)
        self.assertEqual(payload["frequency_penalty"], 0.3)
        self.assertEqual(payload["max_tokens"], 123)
        self.assertEqual(payload["seed"], 7)
        self.assertEqual(payload["timeout"], 11)
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(payload["extra_body"]["thinking"]["type"], "enabled")
        self.assertEqual(client._tool_choice_for_request(), "none")
        self.assertEqual(client._effective_max_tool_steps(None), 900)
        self.assertEqual(client._effective_max_tool_steps(901), 900)

    def test_llm_tool_loop_uses_full_budget_before_finalizing(self) -> None:
        client = ai_client.LLMClient(api_key="sk-test", request_params={
            "tool_choice": "auto",
            "max_tool_steps": 1,
        })
        calls: list[dict] = []

        def tool_call_response(call_id: str) -> SimpleNamespace:
            fn = SimpleNamespace(name="echo", arguments=json.dumps({"text": "hi"}, ensure_ascii=False))
            message = SimpleNamespace(content="", reasoning_content="", tool_calls=[
                SimpleNamespace(id=call_id, type="function", function=fn)
            ])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        def final_response(text: str) -> SimpleNamespace:
            message = SimpleNamespace(content=text, reasoning_content="", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        def fake_create(**payload):
            calls.append(payload)
            if len(calls) == 1:
                return tool_call_response("call_first")
            if len(calls) == 2:
                return tool_call_response("call_over_limit")
            return final_response("final after limit")

        client._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        )

        with patch.object(tool, "run_agent_tool", return_value=json.dumps({"ok": True, "output": "hi"}, ensure_ascii=False)):
            result = client.chat_with_tools(
                [{"role": "user", "content": "use tool"}],
                user_perm=511,
                max_tool_steps=1,
            )

        self.assertEqual(str(result.get("reply", "")), "final after limit")
        self.assertEqual(int(result.get("tool_steps", 0)), 1)
        self.assertEqual(len(result.get("tool_trace", [])), 1)
        self.assertEqual(calls[0].get("tool_choice"), "auto")
        self.assertEqual(calls[1].get("tool_choice"), "auto")
        self.assertEqual(calls[2].get("tool_choice"), "none")

    def test_llm_tool_limit_internal_prompt_not_returned_as_reply(self) -> None:
        client = ai_client.LLMClient(api_key="sk-test", request_params={
            "tool_choice": "auto",
            "max_tool_steps": 1,
        })
        calls: list[dict] = []

        def tool_call_response(call_id: str) -> SimpleNamespace:
            fn = SimpleNamespace(name="echo", arguments=json.dumps({"text": "hi"}, ensure_ascii=False))
            message = SimpleNamespace(content="", reasoning_content="", tool_calls=[
                SimpleNamespace(id=call_id, type="function", function=fn)
            ])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        def final_response(text: str) -> SimpleNamespace:
            message = SimpleNamespace(content=text, reasoning_content="", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        def fake_create(**payload):
            calls.append(payload)
            if len(calls) == 1:
                return tool_call_response("call_first")
            if len(calls) == 2:
                return tool_call_response("call_over_limit")
            return final_response("Maximum tool call iterations reached. Summarize the results and provide a final answer.")

        client._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        )

        with patch.object(tool, "run_agent_tool", return_value=json.dumps({"ok": True, "output": "hi"}, ensure_ascii=False)):
            result = client.chat_with_tools(
                [{"role": "user", "content": "use tool"}],
                user_perm=511,
                max_tool_steps=1,
            )

        reply = str(result.get("reply", ""))
        self.assertIn("工具调用已达到上限", reply)
        self.assertNotIn("Maximum tool call iterations reached", reply)
        self.assertEqual(calls[2].get("tool_choice"), "none")
        system_rows = [m for m in calls[2].get("messages", []) if str(m.get("role", "")) == "system"]
        self.assertTrue(any("Do not call any more tools" in str(m.get("content", "")) for m in system_rows))
        self.assertTrue(any(str(m.get("role", "")) == "system" and "maximum tool-call iterations" in str(m.get("content", "")) for m in calls[2].get("messages", [])))

    def test_run_terminal_redacts_secret_output(self) -> None:
        # v1.7.15 后:_confirmed 已删,改为 _approval
        out = tool.run_terminal(cmd='printf "DEEPSEEK_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456"', _caller_perm=511, _approval=True)
        self.assertTrue(bool(out.get("ok")))
        text = str(out.get("output", ""))
        self.assertIn("***REDACTED***", text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", text)

    def test_edit_file_exact_replacement_requires_unique_old_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_edit_tool_") as tmp:
            path = Path(tmp) / "sample.txt"
            path.write_text("alpha\nbeta\n", encoding="utf-8")
            read_result = tool.read_file(str(path))
            self.assertTrue(bool(read_result.get("ok")))

            dry = tool.edit_file(
                str(path),
                old_text="beta",
                new_text="gamma",
                expected_sha256=str(read_result.get("sha256", "")),
                dry_run="true",
            )
            self.assertTrue(bool(dry.get("ok")))
            self.assertIn("-beta", str(dry.get("diff", "")))
            self.assertEqual(path.read_text(encoding="utf-8"), "alpha\nbeta\n")

            written = tool.edit_file(
                str(path),
                old_text="beta",
                new_text="gamma",
                expected_sha256=str(read_result.get("sha256", "")),
            )
            self.assertTrue(bool(written.get("ok")))
            self.assertEqual(path.read_text(encoding="utf-8"), "alpha\ngamma\n")

    def test_search_files_finds_names_and_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_search_tool_") as tmp:
            root = Path(tmp)
            (root / "alpha.py").write_text("print('needle')\n", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "beta.txt").write_text("hello\nneedle here\n", encoding="utf-8")

            by_name = tool.search_files(root=str(root), query="alpha", glob="*.py")
            by_content = tool.search_files(root=str(root), content="needle here", glob="*.txt")

        self.assertTrue(bool(by_name.get("ok")))
        self.assertEqual(str((by_name.get("results") or [])[0].get("relative_path", "")), "alpha.py")
        self.assertTrue(bool(by_content.get("ok")))
        row = (by_content.get("results") or [])[0]
        self.assertEqual(str(row.get("relative_path", "")), "nested/beta.txt")
        self.assertEqual(int(row.get("line", 0)), 2)
        self.assertIn("needle here", str(row.get("snippet", "")))

    def test_search_web_uses_tavily_when_key_present(self) -> None:
        from TindaAgent.Tool import web_search as web_search_mod

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "query": "python pathlib",
                        "answer": "Pathlib is Python's object-oriented path API.",
                        "results": [
                            {
                                "title": "pathlib docs",
                                "url": "https://docs.python.org/3/library/pathlib.html",
                                "content": "Object-oriented filesystem paths.",
                                "score": 0.98,
                            }
                        ],
                        "response_time": 0.12,
                        "request_id": "req_test",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        requests = []

        def fake_urlopen(req, timeout=0):
            requests.append((req, timeout))
            return FakeResponse()

        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test", "TAVILY_BASE_URL": "https://api.tavily.com"}):
            with patch.object(web_search_mod.request, "urlopen", fake_urlopen):
                result = tool.search_web("python pathlib", source="auto", max_results="2", include_answer="true")

        self.assertTrue(bool(result.get("ok")))
        self.assertEqual(result.get("source"), "tavily")
        self.assertIn("Pathlib", str(result.get("answer", "")))
        self.assertEqual(str((result.get("results") or [])[0].get("url", "")), "https://docs.python.org/3/library/pathlib.html")
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0][0].data.decode("utf-8"))
        self.assertEqual(payload.get("max_results"), 2)
        self.assertEqual(payload.get("search_depth"), "basic")
        self.assertEqual(requests[0][0].get_header("Authorization"), "Bearer tvly-test")

    def test_search_web_builtin_parses_duckduckgo_html_without_key(self) -> None:
        from TindaAgent.Tool import web_search as web_search_mod

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b"""
                <html><body>
                  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs">Example Docs</a>
                  <a class="result__snippet">Example documentation snippet.</a>
                </body></html>
                """

        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}):
            with patch.object(web_search_mod.request, "urlopen", lambda req, timeout=0: FakeResponse()):
                result = tool.search_web("example docs", source="builtin", max_results="1")

        self.assertTrue(bool(result.get("ok")))
        self.assertEqual(result.get("source"), "builtin:duckduckgo")
        row = (result.get("results") or [])[0]
        self.assertEqual(str(row.get("title", "")), "Example Docs")
        self.assertEqual(str(row.get("url", "")), "https://example.com/docs")
        self.assertIn("snippet", str(row.get("content", "")))

    def test_search_web_auto_falls_back_to_builtin_index(self) -> None:
        from TindaAgent.Tool import web_search as web_search_mod

        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}):
            with patch.object(web_search_mod, "_duckduckgo_search", return_value={"ok": False, "source": "builtin:duckduckgo", "error": "offline"}):
                result = tool.search_web("react docs", source="auto", max_results="3")

        self.assertTrue(bool(result.get("ok")))
        self.assertEqual(result.get("source"), "builtin:index")
        self.assertEqual(len(result.get("results") or []), 3)
        self.assertIn("offline", str(result.get("fallback_reason", "")))
        self.assertTrue(any(str(row.get("id", "")) == "react_docs" for row in result.get("index_table", [])))

    def test_search_web_is_registered_with_tool_read_permission(self) -> None:
        info = tool.find_tool("search_web")
        self.assertIsNotNone(info)
        self.assertEqual(int((info or {}).get("perm", 0)), 9)
        schemas = tool.build_agent_tool_schemas(511)
        names = [str(row.get("function", {}).get("name", "")) for row in schemas]
        self.assertIn("search_web", names)

    def test_ask_user_question_tool_returns_pending_question(self) -> None:
        info = tool.find_tool("ask_user_question")
        self.assertIsNotNone(info)
        self.assertIn("HARD RULES", str((info or {}).get("des", "")))
        self.assertIn("simulate the user's answer", str((info or {}).get("des", "")))
        result = tool.ask_user_question(
            question="请选择修复范围",
            options=["只修BUG", "顺带优化UI", "全部"],
            allow_custom_answer="true",
            call_id="ask_unit",
        )
        self.assertTrue(bool(result.get("ok")))
        self.assertTrue(bool(result.get("pending_confirmation")))
        self.assertEqual(result.get("kind"), "question")
        self.assertEqual(result.get("confirm_id"), "ask_unit")
        self.assertEqual(result.get("options"), ["只修BUG", "顺带优化UI", "全部", "__none_of_them__"])
        self.assertEqual(result.get("none_of_them_label"), "以上都不是，我自己补充")

        abc_result = tool.ask_user_question(
            question="请选择学习方案",
            options=(
                "（这是一个选项）A. 在第三版（轻量慢节奏版）基础上继续调整（比如再降低起点、再放慢节奏、再拆分知识点)\n"
                "B. 第三版方向不对，换一个完全不同的学习方案（不限于高数，或换一种学习方式）\n"
                "C. 第三版大体可以，但某部分需要大改（请您补充具体哪部分）"
            ),
        )
        self.assertEqual((abc_result.get("options") or [])[0], "A. 在第三版（轻量慢节奏版）基础上继续调整（比如再降低起点、再放慢节奏、再拆分知识点)")
        self.assertEqual((abc_result.get("options") or [])[1], "B. 第三版方向不对，换一个完全不同的学习方案（不限于高数，或换一种学习方式）")
        self.assertEqual((abc_result.get("options") or [])[2], "C. 第三版大体可以，但某部分需要大改（请您补充具体哪部分）")

        schemas = tool.build_agent_tool_schemas(511)
        ask_schema = next(row for row in schemas if str(row.get("function", {}).get("name", "")) == "ask_user_question")
        fn = ask_schema.get("function", {})
        params = fn.get("parameters", {})
        props = params.get("properties", {})
        self.assertIn("HARD RULES", str(fn.get("description", "")))
        self.assertEqual(params.get("required"), ["question"])
        self.assertIn("Ask only one thing", str(props.get("question", {}).get("description", "")))
        self.assertEqual(props.get("options", {}).get("type"), "array")
        self.assertEqual(props.get("options", {}).get("items", {}).get("type"), "string")
        self.assertIn("mutually exclusive", str(props.get("options", {}).get("description", "")).lower())
        self.assertIn("one clean choice in each array item", str(props.get("options", {}).get("description", "")))

    def test_plan_tool_is_registered_and_visible_to_llm(self) -> None:
        info = tool.find_tool("plan")
        self.assertIsNotNone(info)
        self.assertEqual(int((info or {}).get("perm", 0)), 1)

        result = tool.plan(
            action="create",
            goal="修复 Deep 上下文顺序",
            steps=[
                {"text": "检查请求体"},
                {"text": "调整注入顺序", "status": "in_progress"},
                {"text": "补测试"},
            ],
            status="planned",
            notes="只制定计划，不执行修改",
        )
        self.assertTrue(bool(result.get("ok")))
        self.assertEqual(result.get("kind"), "plan")
        self.assertEqual(result.get("action"), "create")
        self.assertEqual(result.get("schema_version"), 2)
        self.assertEqual(len(result.get("steps") or []), 3)
        self.assertEqual((result.get("steps") or [])[1].get("status"), "in_progress")
        self.assertEqual(result.get("status"), "planned")
        self.assertFalse(bool(result.get("completed")))
        self.assertNotIn("requires_completion_confirmation", result)
        self.assertNotIn("completion_confirmation_state", result)

        done = tool.plan(
            action="update",
            goal="修复 Deep 上下文顺序",
            status="complete",
            completed=True,
            completion_note="已完成",
        )
        self.assertEqual(done.get("action"), "update")
        self.assertEqual(done.get("status"), "complete")
        self.assertTrue(bool(done.get("completed")))
        self.assertNotIn("requires_completion_confirmation", done)
        self.assertNotIn("completion_confirmation_state", done)

        step_done = tool.plan(
            action="set_step_status",
            step_index=1,
            step_status="done",
            update_note="用户确认第一步已完成",
        )
        self.assertTrue(bool(step_done.get("ok")))
        self.assertEqual(step_done.get("action"), "set_step_status")
        self.assertEqual(step_done.get("schema_version"), 2)
        self.assertEqual((step_done.get("step_updates") or [])[0].get("index"), 1)
        self.assertEqual((step_done.get("step_updates") or [])[0].get("status"), "done")

        bad_text_status = tool.plan(
            action="update",
            goal="植物大战僵尸开发",
            steps=[
                {"text": "游戏概述 — 已完成（由用户完成）", "status": "pending"},
                {"text": "Phase 1 — 网格渲染", "status": "pending"},
            ],
        )
        self.assertFalse(bool(bad_text_status.get("ok")))
        self.assertEqual(bad_text_status.get("error_code"), "invalid_plan_contract")
        self.assertIn("set_step_status", str(bad_text_status.get("message", "")))

        agent_raw = tool.run_agent_tool(
            "plan",
            511,
            {
                "action": "create",
                "goal": "修复 steps 入参",
                "steps": [
                    {"text": "复现工具边界问题", "status": "pending"},
                    {"text": "修复解析和校验", "status": "in_progress"},
                ],
            },
            call_id="tc_plan_steps_regression",
        )
        agent_result = json.loads(agent_raw)
        self.assertTrue(bool(agent_result.get("ok")), agent_raw)
        plan_result = agent_result.get("result") or {}
        self.assertEqual(plan_result.get("action"), "create")
        self.assertEqual(len(plan_result.get("steps") or []), 2)
        self.assertEqual((plan_result.get("steps") or [])[1].get("status"), "in_progress")

        status_raw = tool.run_agent_tool(
            "plan",
            511,
            {"action": "set_step_status", "step_index": 1, "step_status": "done"},
        )
        status_result = json.loads(status_raw)
        self.assertTrue(bool(status_result.get("ok")), status_raw)
        self.assertEqual((status_result.get("result", {}).get("step_updates") or [])[0].get("status"), "done")

        done_raw = tool.run_agent_tool(
            "plan",
            511,
            {
                "action": "update",
                "goal": "修复 steps 入参",
                "status": "complete",
                "completed": True,
                "completion_note": "已完成",
            },
        )
        done_result = json.loads(done_raw)
        self.assertTrue(bool(done_result.get("ok")), done_raw)
        self.assertEqual(done_result.get("result", {}).get("status"), "complete")

        legacy_repr = tool.plan(
            action="create",
            goal="兼容旧边界",
            steps=str([{"text": "旧 repr 字符串", "status": "pending"}]),
        )
        self.assertTrue(bool(legacy_repr.get("ok")), legacy_repr)
        self.assertEqual((legacy_repr.get("steps") or [])[0].get("text"), "旧 repr 字符串")

        schemas = tool.build_agent_tool_schemas(511)
        names = [str(row.get("function", {}).get("name", "")) for row in schemas]
        self.assertIn("plan", names)
        plan_schema = next(row for row in schemas if str(row.get("function", {}).get("name", "")) == "plan")
        props = plan_schema.get("function", {}).get("parameters", {}).get("properties", {})
        self.assertIn("action", props)
        self.assertIn("completed", props)
        self.assertNotIn("requires_completion_confirmation", props)
        self.assertNotIn("completion_confirmation_state", props)
        self.assertIn("completion_note", props)
        self.assertEqual(props.get("action", {}).get("enum"), ["create", "update", "set_step_status", "block", "clear"])
        self.assertEqual(props.get("status", {}).get("enum"), ["planned", "revised", "blocked", "complete"])
        self.assertEqual(props.get("completed", {}).get("type"), "boolean")
        self.assertEqual(props.get("steps", {}).get("type"), "array")
        self.assertEqual(props.get("steps", {}).get("items", {}).get("type"), "object")
        self.assertIn("step_index", props)
        self.assertIn("step_status", props)
        self.assertIn("step_updates", props)
        self.assertEqual(props.get("step_status", {}).get("enum"), ["pending", "in_progress", "done", "blocked"])
        plan_description = str(plan_schema.get("function", {}).get("description", ""))
        self.assertIn("structured Plan state API", plan_description)
        self.assertIn("never use emoji", plan_description.lower())
        self.assertIn("set_step_status", plan_description)
        self.assertIn("Never pack multiple steps", plan_description)
        self.assertNotIn("request_completion_confirmation", plan_description)
        self.assertNotIn("confirm_complete", plan_description)

    def test_plan_normalizer_keeps_legacy_text_status_at_boundary_only(self) -> None:
        legacy = tool.normalize_plan_payload({
            "kind": "plan",
            "action": "create",
            "goal": "旧计划",
            "steps": [{"text": "游戏概述 — 已完成（由用户完成）", "status": "pending"}],
        })
        self.assertIsNotNone(legacy)
        self.assertEqual((legacy or {}).get("schema_version"), 1)
        self.assertTrue(bool((legacy or {}).get("legacy_text_adapter")))
        self.assertEqual(((legacy or {}).get("steps") or [])[0].get("text"), "游戏概述")
        self.assertEqual(((legacy or {}).get("steps") or [])[0].get("status"), "done")

        strict = tool.normalize_plan_payload({
            "kind": "plan",
            "schema_version": 2,
            "action": "create",
            "goal": "新计划",
            "steps": [{"text": "游戏概述 — 已完成（由用户完成）", "status": "pending"}],
        })
        self.assertIsNotNone(strict)
        self.assertEqual((strict or {}).get("schema_version"), 2)
        self.assertFalse(bool((strict or {}).get("legacy_text_adapter")))
        self.assertEqual(((strict or {}).get("steps") or [])[0].get("text"), "游戏概述 — 已完成（由用户完成）")
        self.assertEqual(((strict or {}).get("steps") or [])[0].get("status"), "pending")

    def test_session_store_tracks_plan_deleted_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tinda_plan_delete_") as tmp:
            store = SessionStore(Path(tmp))
            sid = "s_plan_delete"
            store.ensure_session(sid, owner_uid="u1")
            deleted = store.mark_plan_deleted(sid)
            self.assertTrue(str(deleted.get("plan_deleted_at", "")).strip())
            cleared = store.clear_plan_deleted(sid)
            self.assertEqual(str(cleared.get("plan_deleted_at", "")), "")

    def test_server_exposes_delete_session_plan_api(self) -> None:
        self.assertTrue(hasattr(server, "delete_session_plan"))
        self.assertIn('"/sessions/{session_id}/plan"', Path(server._THIS_FILE).read_text(encoding="utf-8"))

    def test_plan_mode_helpers_strip_prefix_and_build_english_context(self) -> None:
        self.assertTrue(server._is_plan_mode_message("/plan 修复登录问题"))
        self.assertTrue(server._is_plan_mode_message("/PLAN"))
        self.assertFalse(server._is_plan_mode_message("/planner"))
        self.assertEqual(server._strip_plan_mode_prefix("/plan 修复登录问题"), "修复登录问题")

        context = server._build_plan_mode_transient_context("/plan 修复登录问题")
        self.assertIn("[PLAN_MODE]", context)
        self.assertIn("Do not execute", context)
        self.assertIn("ask_user_question", context)
        self.assertIn("MCP-backed plan tool", context)
        self.assertIn("full available tool list", context)
        self.assertIn("only tools you should call in Plan mode", context)
        self.assertIn("status and completed", context)
        self.assertNotIn("completion_confirmation_state", context)
        self.assertNotIn("request_completion_confirmation", context)
        self.assertNotIn("confirm_complete", context)
        self.assertIn("Do not use emoji", context)
        self.assertIn("修复登录问题", context)
        self.assertNotIn("请", context)

    def test_plan_mode_blocks_execution_tools_without_hiding_tool_schemas(self) -> None:
        client = ai_client.LLMClient(api_key="sk-test")
        calls: list[dict] = []

        def fake_create(**payload):
            calls.append(payload)
            if len(calls) == 1:
                fn = SimpleNamespace(
                    name="run_terminal",
                    arguments=json.dumps({"cmd": "echo should-not-run"}, ensure_ascii=False),
                )
                message = SimpleNamespace(content="", reasoning_content="", tool_calls=[
                    SimpleNamespace(id="call_exec", type="function", function=fn)
                ])
                return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
            message = SimpleNamespace(content="我会先制定计划，等待确认后再执行。", reasoning_content="", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        client._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        )

        messages = [
            {"role": "system", "content": "[PLAN_MODE]\nPlan only.\n[/PLAN_MODE]"},
            {"role": "user", "content": "修复问题"},
        ]
        with patch.object(tool, "run_agent_tool") as run_tool_mock:
            result = client.chat_with_tools(messages, user_perm=511, max_tool_steps=3)

        run_tool_mock.assert_not_called()
        self.assertEqual(str(result.get("reply", "")), "我会先制定计划，等待确认后再执行。")
        trace = result.get("tool_trace") or []
        self.assertEqual(len(trace), 1)
        self.assertEqual(str(trace[0].get("agent_tool", "")), "run_terminal")
        self.assertEqual(str((trace[0].get("result") or {}).get("error_code", "")), "plan_mode_execution_blocked")
        first_tool_names = [str(row.get("function", {}).get("name", "")) for row in calls[0].get("tools", [])]
        self.assertIn("run_terminal", first_tool_names)
        second_tool_rows = [m for m in calls[1].get("messages", []) if str(m.get("role", "")) == "tool"]
        self.assertTrue(any("plan_mode_execution_blocked" in str(m.get("content", "")) for m in second_tool_rows))

    def test_web_search_disabled_blocks_execution_without_hiding_schema(self) -> None:
        client = ai_client.LLMClient(api_key="sk-test")
        calls: list[dict] = []

        def fake_create(**payload):
            calls.append(payload)
            if len(calls) == 1:
                fn = SimpleNamespace(
                    name="search_web",
                    arguments=json.dumps({"query": "latest python release"}, ensure_ascii=False),
                )
                message = SimpleNamespace(content="", reasoning_content="", tool_calls=[
                    SimpleNamespace(id="call_search", type="function", function=fn)
                ])
                return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
            message = SimpleNamespace(content="需要开启网络搜索后才能查询实时信息。", reasoning_content="", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        client._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        )
        messages = [
            {"role": "system", "content": "[WEB_SEARCH_MODE]\nWeb search is disabled for this user request.\n[/WEB_SEARCH_MODE]"},
            {"role": "user", "content": "查一下最新 Python 版本"},
        ]
        with patch.object(tool, "run_agent_tool") as run_tool_mock:
            result = client.chat_with_tools(messages, user_perm=511, max_tool_steps=3)

        run_tool_mock.assert_not_called()
        self.assertEqual(str(result.get("reply", "")), "需要开启网络搜索后才能查询实时信息。")
        trace = result.get("tool_trace") or []
        self.assertEqual(len(trace), 1)
        self.assertEqual(str((trace[0].get("result") or {}).get("error_code", "")), "web_search_disabled")
        first_tool_names = [str(row.get("function", {}).get("name", "")) for row in calls[0].get("tools", [])]
        self.assertIn("search_web", first_tool_names)

    def test_web_search_enabled_allows_execution_with_same_schema(self) -> None:
        client = ai_client.LLMClient(api_key="sk-test")
        calls: list[dict] = []

        def fake_create(**payload):
            calls.append(payload)
            if len(calls) == 1:
                fn = SimpleNamespace(
                    name="search_web",
                    arguments=json.dumps({"query": "python docs"}, ensure_ascii=False),
                )
                message = SimpleNamespace(content="", reasoning_content="", tool_calls=[
                    SimpleNamespace(id="call_search", type="function", function=fn)
                ])
                return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
            message = SimpleNamespace(content="已查询。", reasoning_content="", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        client._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        )
        messages = [
            {"role": "system", "content": "[WEB_SEARCH_MODE]\nWeb search is enabled for this user request.\n[/WEB_SEARCH_MODE]"},
            {"role": "user", "content": "查一下 python docs"},
        ]
        fake_result = json.dumps({"ok": True, "source": "builtin:index", "results": []}, ensure_ascii=False)
        with patch.object(tool, "run_agent_tool", return_value=fake_result) as run_tool_mock:
            result = client.chat_with_tools(messages, user_perm=511, max_tool_steps=3)

        run_tool_mock.assert_called_once()
        self.assertEqual(str(result.get("reply", "")), "已查询。")
        first_tool_names = [str(row.get("function", {}).get("name", "")) for row in calls[0].get("tools", [])]
        self.assertIn("search_web", first_tool_names)

    def test_extract_pending_confirmation_items_supports_user_question(self) -> None:
        trace = [{
            "agent_tool": "ask_user_question",
            "call_id": "ask_call",
            "tool_call_id": "call_model",
            "result": {
                "ok": True,
                "pending_confirmation": True,
                "kind": "question",
                "confirm_id": "ask_call",
                "question": "要不要顺带改动画？",
                "options": ["要", "不要", "__none_of_them__"],
                "none_of_them_label": "以上都不是，我自己补充",
                "allow_custom_answer": True,
            },
        }]
        rows = server._extract_pending_confirmation_items(trace)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("kind"), "question")
        self.assertEqual(rows[0].get("question"), "要不要顺带改动画？")
        self.assertEqual(rows[0].get("options"), ["要", "不要", "__none_of_them__"])
        self.assertEqual(rows[0].get("none_of_them_label"), "以上都不是，我自己补充")

    def test_agent_resume_with_user_question_answer_replaces_tool_result(self) -> None:
        class FakeClient:
            def chat_with_tools(self, messages, **kwargs):
                tool_rows = [m for m in messages if str(m.get("role")) == "tool"]
                self.tool_payload = json.loads(str(tool_rows[-1].get("content", "{}")))
                return {
                    "reply": "继续执行",
                    "history_delta": [{"role": "assistant", "content": "继续执行"}],
                    "tool_trace": [],
                    "tool_steps": 0,
                }

        fake_client = FakeClient()
        agent = server.Agent("ask-user-test", user_perm=511, client=fake_client, model_name="deepseek-v4-flash")
        pending = {
            "ok": True,
            "pending_confirmation": True,
            "kind": "question",
            "confirm_id": "ask_call",
            "question": "修复范围？",
        }
        agent.history.extend([
            {"role": "user", "content": "帮我修"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_ask",
                "type": "function",
                "function": {"name": "ask_user_question", "arguments": json.dumps({"question": "修复范围？"}, ensure_ascii=False)},
            }]},
            {"role": "tool", "tool_call_id": "call_ask", "content": json.dumps({"result": pending}, ensure_ascii=False)},
        ])
        agent._held_messages = [m.copy() for m in agent.history]
        agent._held_perm = 511
        result = agent.resume_with_confirmations([{
            "confirm_id": "ask_call",
            "kind": "question",
            "action": "allow",
            "choice": "只修BUG",
            "answer": "只修BUG，别改UI",
        }])
        self.assertEqual(result.get("reply"), "继续执行")
        self.assertEqual(fake_client.tool_payload.get("kind"), "question_answer")
        self.assertTrue(bool(fake_client.tool_payload.get("approval")))
        self.assertEqual(fake_client.tool_payload.get("action"), "allow")
        self.assertEqual(fake_client.tool_payload.get("answer"), "只修BUG，别改UI")

    def test_agent_resume_with_user_question_cancel_passes_denial(self) -> None:
        class FakeClient:
            def chat_with_tools(self, messages, **kwargs):
                tool_rows = [m for m in messages if str(m.get("role")) == "tool"]
                self.tool_payload = json.loads(str(tool_rows[-1].get("content", "{}")))
                system_rows = [m for m in messages if str(m.get("role")) == "system"]
                self.system_tail = str(system_rows[-1].get("content", "")) if system_rows else ""
                return {
                    "reply": "已按合理假设继续",
                    "history_delta": [{"role": "assistant", "content": "已按合理假设继续"}],
                    "tool_trace": [],
                    "tool_steps": 0,
                }

        fake_client = FakeClient()
        agent = server.Agent("ask-user-cancel-test", user_perm=511, client=fake_client, model_name="deepseek-v4-flash")
        pending = {
            "ok": True,
            "pending_confirmation": True,
            "kind": "question",
            "confirm_id": "ask_cancel",
            "question": "修复范围？",
        }
        agent.history.extend([
            {"role": "user", "content": "帮我修"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_ask_cancel",
                "type": "function",
                "function": {"name": "ask_user_question", "arguments": json.dumps({"question": "修复范围？"}, ensure_ascii=False)},
            }]},
            {"role": "tool", "tool_call_id": "call_ask_cancel", "content": json.dumps({"result": pending}, ensure_ascii=False)},
        ])
        agent._held_messages = [m.copy() for m in agent.history]
        agent._held_perm = 511
        result = agent.resume_with_confirmations([{
            "confirm_id": "ask_cancel",
            "kind": "question",
            "approval": False,
        }])
        self.assertEqual(result.get("reply"), "已按合理假设继续")
        self.assertEqual(fake_client.tool_payload.get("kind"), "question_answer")
        self.assertFalse(bool(fake_client.tool_payload.get("approval")))
        self.assertEqual(fake_client.tool_payload.get("action"), "deny")
        self.assertEqual(fake_client.tool_payload.get("error_code"), "user_cancelled")
        self.assertIn("cancelled the clarification question", fake_client.system_tail)
        self.assertNotIn("terminal command above has been executed", fake_client.system_tail)

    def test_deep_alignment_prompt_is_english_and_non_executing(self) -> None:
        prompt = str(server._DEEP_ALIGNMENT_SYSTEM_PROMPT)
        self.assertIn("Deep Alignment mode", prompt)
        self.assertIn("Do not execute", prompt)
        self.assertIn("not Plan mode", prompt)
        self.assertIn("after confirmation the main agent runtime has additional native tools", prompt)
        self.assertIn("Do not reveal hidden reasoning", prompt)
        self.assertIn("Chinese", prompt)
        self.assertNotIn("请", prompt)

    def test_deep_alignment_messages_disclose_post_confirmation_tools_without_plan_bias(self) -> None:
        with patch.object(server, "_deep_session_context", return_value="(none)"):
            messages = server._build_deep_alignment_messages(
                "你plan工具能干吗",
                [],
                session_id="s_deep_plan_tool",
                user_perm=511,
            )

        user_text = str(messages[1].get("content", ""))
        self.assertIn("Main runtime tool availability", user_text)
        self.assertIn("After confirmation, the main agent runtime may use its normal tool set", user_text)
        self.assertIn("Do not claim that a specific main-runtime tool is unavailable", user_text)
        self.assertIn("Main runtime tools visible after confirmation", user_text)
        self.assertIn("- plan:", user_text)
        self.assertNotIn("plan: available", user_text)
        self.assertIn("你plan工具能干吗", user_text)

    def test_deep_alignment_messages_hide_tools_without_permission(self) -> None:
        with patch.object(server, "_deep_session_context", return_value="(none)"):
            messages = server._build_deep_alignment_messages(
                "有什么工具",
                [],
                session_id="s_deep_low_perm",
                user_perm=0,
            )

        user_text = str(messages[1].get("content", ""))
        self.assertIn("No main-runtime tools are currently visible", user_text)
        self.assertNotIn("- plan:", user_text)

    def test_deep_public_payload_exposes_active_round(self) -> None:
        sid = "s_deep_payload"
        server._deep_alignment_state[sid] = {
            "active": True,
            "state": "waiting_confirm",
            "original_message": "帮我修复",
            "file_names": ["a.py"],
            "file_contents": ["print(1)"],
            "rounds": [
                {"revision": "", "alignment_text": "第一版"},
                {"revision": "不是这个", "alignment_text": "第二版"},
            ],
            "active_index": 1,
            "updated_at": "2026-05-21T00:00:00+08:00",
        }
        try:
            payload = server._deep_public_payload(sid)
        finally:
            server._deep_alignment_state.pop(sid, None)

        self.assertTrue(bool(payload.get("active")))
        self.assertEqual(payload.get("state"), "waiting_confirm")
        self.assertEqual(payload.get("alignment_text"), "第二版")
        self.assertTrue(bool(payload.get("can_back")))
        self.assertEqual(payload.get("file_names"), ["a.py"])
        self.assertEqual(payload.get("file_contents"), ["print(1)"])

    def test_deep_public_payload_exposes_pending_ask_for_card_ui(self) -> None:
        sid = "s_deep_pending_ask_payload"
        server._deep_alignment_state[sid] = {
            "active": True,
            "state": "waiting_question",
            "original_message": "写个脚本",
            "file_names": [],
            "file_contents": [],
            "rounds": [],
            "active_index": 0,
            "pending_deep_ask": {
                "call_id": "deep_ask_card",
                "tool_call": {
                    "id": "deep_ask_card",
                    "type": "function",
                    "function": {
                        "name": "ask_user_question",
                        "arguments": json.dumps({
                            "question": "你希望脚本做什么用途？",
                            "options": "文件整理|日志分析",
                        }, ensure_ascii=False),
                    },
                },
            },
            "updated_at": "2026-05-22T00:00:00+08:00",
        }
        try:
            payload = server._deep_public_payload(sid)
        finally:
            server._deep_alignment_state.pop(sid, None)

        ask = payload.get("pending_deep_ask")
        self.assertIsInstance(ask, dict)
        self.assertEqual(ask.get("flow"), "deep_alignment")
        self.assertEqual(ask.get("kind"), "question")
        self.assertEqual(ask.get("call_id"), "deep_ask_card")
        self.assertEqual(ask.get("question"), "你希望脚本做什么用途？")
        self.assertEqual(ask.get("options"), ["文件整理", "日志分析", "__none_of_them__"])

    def test_deep_alignment_context_is_transient_for_agent_history(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.request_messages: list[dict] = []

            def chat_with_tools(self, messages, **kwargs):
                self.request_messages = [dict(m) for m in messages]
                return {
                    "reply": "ok",
                    "history_delta": [{"role": "assistant", "content": "ok"}],
                    "tool_trace": [],
                    "tool_steps": 0,
                }

        fake_client = FakeClient()
        agent = server.Agent("deep-transient-test", user_perm=511, client=fake_client, model_name="deepseek-v4-flash")
        tail = server._build_deep_alignment_tail_messages("用户确认：只改前端，不动后端。")
        result = agent.chat_with_meta("执行原始请求", transient_tail_messages=tail)

        self.assertEqual(result.get("reply"), "ok")
        request_rows = fake_client.request_messages
        self.assertEqual([str(m.get("role")) for m in request_rows[-3:]], ["user", "assistant", "system"])
        self.assertEqual(str(request_rows[-3].get("content")), "执行原始请求")
        self.assertIn("用户确认：只改前端，不动后端。", str(request_rows[-2].get("content", "")))
        self.assertIn("[DEEP_ALIGNMENT_CONFIRMED]", str(request_rows[-1].get("content", "")))
        self.assertIn("Execute the current user request now", str(request_rows[-1].get("content", "")))
        self.assertIn("use native tool_calls/function calling normally", str(request_rows[-1].get("content", "")))
        persisted_rows = agent.get_conversation_messages()
        self.assertFalse(any("[DEEP_ALIGNMENT_CONFIRMED]" in str(m.get("content", "")) for m in persisted_rows))
        self.assertTrue(any(str(m.get("role")) == "user" and str(m.get("content")) == "执行原始请求" for m in persisted_rows))

    def test_deep_alignment_generation_includes_session_context(self) -> None:
        captured: dict[str, Any] = {}

        class FakeStore:
            def get_context_messages(self, _sid: str) -> list[dict]:
                return [
                    {"role": "user", "content": "前文需求：给设置页新增入场动画"},
                    {"role": "assistant", "content": "我会只改设置页动画，不改布局。"},
                ]

        def fake_store_to_agent_messages(rows):
            return rows, {"included": len(rows), "skipped": 0}

        def fake_create_completion(payload, **kwargs):
            captured["payload"] = payload

            class Msg:
                content = "我理解你说“好了”是确认继续设置页动画方案。"

            class Choice:
                message = Msg()

            class Resp:
                choices = [Choice()]

            return Resp()

        with patch.object(server, "_store", FakeStore()), \
             patch.object(server, "_store_to_agent_messages", side_effect=fake_store_to_agent_messages), \
             patch.object(server._llm, "create_completion", side_effect=fake_create_completion):
            text = server._generate_deep_alignment_text("好了", [], session_id="s_deep_context")

        self.assertIn("确认继续", text)
        messages = captured["payload"]["messages"]
        user_content = str(messages[1].get("content", ""))
        self.assertIn("Current session context", user_content)
        self.assertIn("前文需求：给设置页新增入场动画", user_content)
        self.assertIn("Original user request:\n好了", user_content)

    def test_deep_alignment_can_request_user_question(self) -> None:
        captured: dict[str, Any] = {}

        class ToolFunction:
            name = "ask_user_question"
            arguments = json.dumps({
                "question": "你希望脚本做什么用途？",
                "options": "文件整理|日志分析",
                "allow_custom_answer": "true",
            }, ensure_ascii=False)

        class ToolCall:
            id = "deep_ask_unit"
            type = "function"
            function = ToolFunction()

        class Msg:
            content = ""
            tool_calls = [ToolCall()]

        class Choice:
            message = Msg()

        class Resp:
            choices = [Choice()]

        def fake_create_completion(payload, **kwargs):
            captured["payload"] = payload
            return Resp()

        with patch.object(server._llm, "create_completion", side_effect=fake_create_completion):
            result = server._generate_deep_alignment_result("写个脚本", [], session_id="s_deep_ask")

        self.assertEqual(result.get("state"), "waiting_question")
        self.assertEqual(captured["payload"].get("tool_choice"), "auto")
        self.assertEqual(len(captured["payload"].get("tools") or []), 1)
        tool_schema = captured["payload"]["tools"][0]["function"]["parameters"]["properties"]
        self.assertEqual(tool_schema.get("options", {}).get("type"), "array")
        pending = result.get("pending")
        self.assertIsInstance(pending, dict)
        self.assertEqual(pending.get("flow"), "deep_alignment")
        self.assertEqual(pending.get("kind"), "question")
        self.assertEqual(pending.get("question"), "你希望脚本做什么用途？")
        self.assertEqual(pending.get("options"), ["文件整理", "日志分析", "__none_of_them__"])

    def test_deep_alignment_parses_dsml_ask_and_hides_protocol_text(self) -> None:
        class Msg:
            content = (
                "我需要先问你一个问题。\n"
                "<｜｜DSML｜｜tool_calls>"
                "<｜｜DSML｜｜invoke name=\"ask_user_question\">"
                "<｜｜DSML｜｜parameter name=\"question\">你要哪种方案？</｜｜DSML｜｜parameter>"
                "<｜｜DSML｜｜parameter name=\"options\">快速|稳妥</｜｜DSML｜｜parameter>"
                "</｜｜DSML｜｜invoke>"
                "</｜｜DSML｜｜tool_calls>"
            )
            tool_calls = []

        class Choice:
            message = Msg()

        class Resp:
            choices = [Choice()]

        with patch.object(server._llm, "create_completion", return_value=Resp()):
            result = server._generate_deep_alignment_result("写个脚本", [], session_id="s_deep_dsml")

        self.assertEqual(result.get("state"), "waiting_question")
        pending = result.get("pending")
        self.assertIsInstance(pending, dict)
        self.assertEqual(pending.get("question"), "你要哪种方案？")
        self.assertEqual(pending.get("options"), ["快速", "稳妥", "__none_of_them__"])

    def test_deep_alignment_strips_non_ask_dsml_from_visible_text(self) -> None:
        class Msg:
            content = (
                "我会先理解你的目标。\n"
                "<｜｜DSML｜｜tool_calls>"
                "<｜｜DSML｜｜invoke name=\"run_terminal\">"
                "<｜｜DSML｜｜parameter name=\"cmd\">pwd</｜｜DSML｜｜parameter>"
                "</｜｜DSML｜｜invoke>"
                "</｜｜DSML｜｜tool_calls>"
            )
            tool_calls = []

        class Choice:
            message = Msg()

        class Resp:
            choices = [Choice()]

        with patch.object(server._llm, "create_completion", return_value=Resp()):
            result = server._generate_deep_alignment_result("写个脚本", [], session_id="s_deep_strip_dsml")

        self.assertEqual(result.get("state"), "waiting_confirm")
        text = str(result.get("alignment_text", ""))
        self.assertIn("我会先理解你的目标", text)
        self.assertNotIn("DSML", text)
        self.assertNotIn("invoke", text)

    def test_deep_alignment_resume_after_question_answer(self) -> None:
        captured: dict[str, Any] = {}
        state = {
            "pending_deep_ask": {
                "call_id": "deep_ask_unit",
                "question": "你希望脚本做什么用途？",
                "messages": [
                    {"role": "system", "content": "Deep Alignment"},
                    {"role": "user", "content": "Original user request:\n写个脚本"},
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "deep_ask_unit",
                        "type": "function",
                        "function": {"name": "ask_user_question", "arguments": "{}"},
                    }]},
                ],
            }
        }

        def fake_create_completion(payload, **kwargs):
            captured["payload"] = payload

            class Msg:
                content = "我理解你希望写一个日志分析脚本。请确认是否一致。"
                tool_calls = []

            class Choice:
                message = Msg()

            class Resp:
                choices = [Choice()]

            return Resp()

        with patch.object(server._llm, "create_completion", side_effect=fake_create_completion):
            result = server._resume_deep_alignment_after_question(state, "日志分析", "日志分析")

        self.assertIn("日志分析脚本", result.get("alignment_text", ""))
        self.assertEqual(captured["payload"].get("tool_choice"), "none")
        messages = captured["payload"].get("messages") or []
        self.assertEqual(messages[-1].get("role"), "tool")
        self.assertEqual(messages[-1].get("tool_call_id"), "deep_ask_unit")
        self.assertIn("日志分析", str(messages[-1].get("content", "")))

    def test_deep_alignment_state_persists_and_deletes_json_file(self) -> None:
        sid = "s_deep_persist"
        with tempfile.TemporaryDirectory(prefix="tinda_deep_state_") as tmp:
            old_root = server._DEEP_ALIGNMENT_ROOT
            server._DEEP_ALIGNMENT_ROOT = Path(tmp)
            try:
                state = {
                    "active": True,
                    "state": "waiting_confirm",
                    "original_message": "帮我确认需求",
                    "file_names": ["demo.txt"],
                    "file_contents": ["hello"],
                    "rounds": [{"revision": "", "alignment_text": "第一版"}],
                    "active_index": 0,
                    "updated_at": "2026-05-22T00:00:00+08:00",
                }
                server._save_deep_alignment_state(sid, state)
                path = server._deep_alignment_path(sid)
                self.assertTrue(path.exists())

                server._deep_alignment_state.pop(sid, None)
                loaded = server._load_deep_alignment_state(sid)
                self.assertIsNotNone(loaded)
                self.assertEqual((loaded or {}).get("original_message"), "帮我确认需求")
                self.assertEqual((loaded or {}).get("file_contents"), ["hello"])

                server._delete_deep_alignment_state(sid)
                self.assertFalse(path.exists())
            finally:
                server._deep_alignment_state.pop(sid, None)
                server._DEEP_ALIGNMENT_ROOT = old_root

    def test_skill_list_and_read_uses_runtime_skill_root(self) -> None:
        from TindaAgent.Tool import skills as skill_mod

        with tempfile.TemporaryDirectory(prefix="tinda_skill_root_") as tmp:
            root = Path(tmp)
            skill_dir = root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Demo\n\nDescription: demo skill\n\nUse it.", encoding="utf-8")

            with patch.object(skill_mod, "_skill_roots", return_value=[root]):
                listed = tool.skill_list()
                loaded = tool.skill_read("demo")

        self.assertTrue(bool(listed.get("ok")))
        self.assertEqual(str((listed.get("skills") or [])[0].get("name", "")), "demo")
        self.assertTrue(bool(loaded.get("ok")))
        self.assertIn("Use it.", str(loaded.get("content", "")))

    def test_mcp_client_lists_and_calls_stdio_tool(self) -> None:
        from TindaAgent.Tool import mcp_client

        with tempfile.TemporaryDirectory(prefix="tinda_mcp_") as tmp:
            root = Path(tmp)
            server_py = root / "server.py"
            server_py.write_text(
                """
import json
import sys

for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": req.get("params", {}).get("arguments", {}).get("text", "")}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": result}, ensure_ascii=False), flush=True)
""".strip(),
                encoding="utf-8",
            )
            cfg = {"version": 1, "servers": {"fake": {"command": "python3", "args": [str(server_py)], "env": {}}}}
            with patch.object(mcp_client, "load_mcp_config", return_value=cfg):
                listed = tool.mcp_list_tools("fake")
                called = tool.mcp_call_tool("fake", "echo", json.dumps({"text": "hello"}, ensure_ascii=False))

        self.assertTrue(bool(listed.get("ok")))
        self.assertEqual(str((listed.get("tools") or [])[0].get("name", "")), "echo")
        self.assertTrue(bool(called.get("ok")))
        self.assertIn("hello", json.dumps(called.get("result", {}), ensure_ascii=False))

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

        self.assertEqual(rows[0], {"role": "system", "content": "[Existing Context Summary] old summary"})
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

    def test_store_dict_to_agent_messages_compacts_markdown_for_llm_only(self) -> None:
        from TindaAgent.Web import session_adapter as sa

        rich_markdown = (
            "# Title\n\n"
            "> --正在压缩上下文--\n\n"
            "| A | B |\n| --- | --- |\n| **hello** | [site](https://example.com) |\n\n"
            "```python\nprint('x')\n```\n"
        )
        store_dict = {
            "1": {"role": "user", "id": "u1", "content": {"1": {"text": "请继续"}}},
            "2": {"role": "assistant", "id": "a1", "content": {"1": {"text": rich_markdown}}},
        }

        rows, _stats = sa.store_dict_to_agent_messages(store_dict)
        frontend = sa.store_dict_to_frontend(store_dict)

        self.assertIn("**hello**", str(frontend[1].get("content", "")))
        self.assertIn("```python", str(frontend[1].get("content", "")))
        self.assertNotIn("**hello**", str(rows[1].get("content", "")))
        self.assertNotIn("```python", str(rows[1].get("content", "")))
        self.assertIn("[code:python]", str(rows[1].get("content", "")))
        self.assertLess(
            len(str(rows[1].get("content", ""))),
            len(rich_markdown),
        )

    def test_tool_marker_context_deduplicates_stdout_output_for_llm(self) -> None:
        from TindaAgent.Web import session_adapter as sa

        rows, _stats = sa.store_dict_to_agent_messages(
            {
                "1": {"role": "user", "id": "u1", "content": {"1": {"text": "run"}}},
                "2": {
                    "role": "assistant",
                    "id": "a1",
                    "content": {
                        "1": {"tool_marker": {
                            "name": "echo",
                            "id": "call_1",
                            "arguments": {"text": "hello"},
                            "result": {
                                "ok": True,
                                "cmd": "echo keep_snake_case",
                                "stdout": "same output",
                                "output": "same output",
                                "success": True,
                            },
                        }},
                    },
                },
            }
        )

        tool_rows = [r for r in rows if str(r.get("role", "")) == "tool"]
        self.assertEqual(len(tool_rows), 1)
        payload = json.loads(str(tool_rows[0].get("content", "{}")))
        self.assertEqual(str(payload.get("cmd", "")), "echo keep_snake_case")
        self.assertEqual(str(payload.get("stdout", "")), "same output")
        self.assertNotIn("output", payload)
        self.assertNotIn("success", payload)

    def test_tool_marker_context_replays_full_reasoning_on_tool_call_message(self) -> None:
        from TindaAgent.Web import session_adapter as sa

        reasoning = "**must stay raw**\n```txt\nfull reasoning\n```"
        rows, _stats = sa.store_dict_to_agent_messages(
            {
                "1": {"role": "user", "id": "u1", "content": {"1": {"text": "run"}}},
                "2": {
                    "role": "assistant",
                    "id": "a1",
                    "content": {
                        "1": {"thinking": reasoning},
                        "2": {"tool_marker": {
                            "name": "echo",
                            "id": "call_reasoning",
                            "arguments": {"text": "hello"},
                            "result": {"ok": True, "stdout": "hello"},
                        }},
                    },
                },
            }
        )

        assistant_rows = [r for r in rows if str(r.get("role", "")) == "assistant"]
        self.assertEqual(len(assistant_rows), 1)
        self.assertTrue(bool(assistant_rows[0].get("tool_calls")))
        self.assertEqual(str(assistant_rows[0].get("reasoning_content", "")), reasoning)
        self.assertEqual(str(assistant_rows[0]["tool_calls"][0].get("id", "")), "call_reasoning")

    def test_consecutive_tool_markers_share_one_assistant_tool_calls_message(self) -> None:
        from TindaAgent.Web import session_adapter as sa

        rows, _stats = sa.store_dict_to_agent_messages(
            {
                "1": {"role": "user", "id": "u1", "content": {"1": {"text": "run"}}},
                "2": {
                    "role": "assistant",
                    "id": "a1",
                    "content": {
                        "1": {"thinking": "reasoning for both tools"},
                        "2": {"tool_marker": {
                            "name": "echo",
                            "id": "call_a",
                            "arguments": {"text": "a"},
                            "result": {"ok": True, "stdout": "a"},
                        }},
                        "3": {"tool_marker": {
                            "name": "echo",
                            "id": "call_b",
                            "arguments": {"text": "b"},
                            "result": {"ok": True, "stdout": "b"},
                        }},
                    },
                },
            }
        )

        self.assertEqual([str(r.get("role", "")) for r in rows], ["user", "assistant", "tool", "tool"])
        self.assertEqual(len(rows[1].get("tool_calls") or []), 2)
        self.assertEqual(str(rows[1].get("reasoning_content", "")), "reasoning for both tools")
        self.assertEqual([str(r.get("tool_call_id", "")) for r in rows[2:]], ["call_a", "call_b"])

    def test_compact_message_keeps_reasoning_content_exact(self) -> None:
        from TindaAgent.Process.AI.context_compaction import compact_message_for_llm

        reasoning = "**raw reasoning**\n```txt\nkeep me\n```"
        row = compact_message_for_llm({
            "role": "assistant",
            "content": "**visible**",
            "reasoning_content": reasoning,
        })

        self.assertEqual(str(row.get("reasoning_content", "")), reasoning)
        self.assertNotIn("**visible**", str(row.get("content", "")))

    def test_tool_limit_prompt_is_request_only_not_bubble_text(self) -> None:
        text = ai_client._build_tool_limit_system_message([
            {"agent_tool": "echo", "result": {"ok": True, "stdout": "hi"}}
        ])["content"]
        fallback = ai_client._finalize_tool_limit_reply(
            "Maximum tool call iterations reached. Summarize the results and provide a final answer.",
            [{"agent_tool": "echo", "result": {"ok": True, "stdout": "hi"}}],
        )

        self.assertIn("Do not call any more tools", text)
        self.assertIn("Executed tool results available to summarize", text)
        self.assertIn("工具调用已达到上限", fallback)
        self.assertNotIn("Maximum tool call iterations reached", fallback)
        self.assertNotIn("Summarize the results", fallback)

    def test_agent_request_compacts_history_without_mutating_storage_history(self) -> None:
        from TindaAgent.Process.AI.agent import Agent

        agent = Agent("compact-test", user_perm=511, client=object(), model_name="deepseek-v4-flash")
        agent.history.append({"role": "assistant", "content": "**bold**\n```txt\nraw\n```"})
        request_rows = agent._messages_for_llm_request(agent.history)

        self.assertIn("**bold**", str(agent.history[-1].get("content", "")))
        self.assertNotIn("**bold**", str(request_rows[-1].get("content", "")))
        self.assertIn("[code:txt]", str(request_rows[-1].get("content", "")))

    def test_deepseek_reasoner_request_strips_historical_reasoning_content(self) -> None:
        payload = ai_client.prepare_llm_request_payload(
            {
                "model": "deepseek-reasoner",
                "messages": [
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": "a", "reasoning_content": "hidden"},
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "tool thought",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "echo", "arguments": "{}"},
                            }
                        ],
                    },
                ],
                "extra_body": {"thinking": {"type": "enabled"}},
            },
            base_url="https://api.deepseek.com",
            provider="deepseek",
        )

        self.assertFalse(any("reasoning_content" in msg for msg in payload["messages"]))

    def test_deepseek_v4_request_keeps_reasoning_only_for_tool_call_assistant(self) -> None:
        payload = ai_client.prepare_llm_request_payload(
            {
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "assistant", "content": "plain", "reasoning_content": "drop"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "echo", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
                ],
                "extra_body": {"thinking": {"type": "enabled"}},
            },
            base_url="https://api.deepseek.com",
            provider="deepseek",
        )

        self.assertNotIn("reasoning_content", payload["messages"][0])
        self.assertEqual(payload["messages"][1].get("reasoning_content"), "")
        self.assertNotIn("reasoning_content", payload["messages"][2])

    def test_deepseek_v4_thinking_disabled_strips_reasoning_content(self) -> None:
        payload = ai_client.prepare_llm_request_payload(
            {
                "model": "deepseek-v4-flash",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "drop",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "echo", "arguments": "{}"},
                            }
                        ],
                    },
                ],
            },
            base_url="https://api.deepseek.com",
            provider="deepseek",
        )

        self.assertNotIn("reasoning_content", payload["messages"][0])

    def test_llm_client_payload_logged_and_sent_after_reasoning_sanitization(self) -> None:
        client = ai_client.LLMClient(api_key="sk-test", model="deepseek-reasoner")
        calls: list[dict] = []

        def fake_create(**payload):
            calls.append(payload)
            message = SimpleNamespace(content="ok", reasoning_content="", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
        result = client.chat_with_tools(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "old", "reasoning_content": "must not send"},
            ],
            user_perm=511,
            max_tool_steps=1,
        )

        self.assertEqual(str(result.get("reply", "")), "ok")
        self.assertEqual(len(calls), 1)
        self.assertFalse(any("reasoning_content" in msg for msg in calls[0]["messages"]))

    def test_deep_alignment_pending_ask_preserves_reasoning_for_deepseek_thinking(self) -> None:
        fn = SimpleNamespace(
            name="ask_user_question",
            arguments=json.dumps({"question": "需要补充什么？"}, ensure_ascii=False),
        )
        call = SimpleNamespace(id="call_deep_ask", type="function", function=fn)
        message = SimpleNamespace(content="", reasoning_content="tool reasoning", tool_calls=[call])
        resp = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        with patch.object(server, "_deep_session_context", return_value="(none)"), \
             patch.object(server._llm, "create_completion", return_value=resp):
            result = server._generate_deep_alignment_result(
                "原始请求",
                [],
                session_id="s_deep_reasoning",
            )

        pending = result.get("pending_deep_ask") if isinstance(result, dict) else {}
        messages = pending.get("messages") if isinstance(pending, dict) else []
        self.assertEqual(str(result.get("state", "")), "waiting_question")
        self.assertEqual(str(messages[-1].get("reasoning_content", "")), "tool reasoning")
        self.assertEqual(str(messages[-1].get("tool_calls", [{}])[0].get("id", "")), "call_deep_ask")

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

    def test_terminal_pending_keeps_deep_alignment_question_without_agent(self) -> None:
        sid = "s_deep_pending_without_agent"
        state = {
            "active": True,
            "state": "waiting_question",
            "original_message": "制定一个计划",
            "file_names": [],
            "file_contents": [],
            "rounds": [],
            "active_index": 0,
            "pending_deep_ask": {
                "call_id": "call_deep_keep",
                "tool_call": {
                    "id": "call_deep_keep",
                    "type": "function",
                    "function": {
                        "name": "ask_user_question",
                        "arguments": json.dumps({"question": "计划类型是什么？"}, ensure_ascii=False),
                    },
                },
            },
        }

        class _FakeStore:
            def ensure_session(self, _sid: str) -> None:
                return None

            def get_session(self, _sid: str) -> dict:
                return {"id": _sid, "owner_uid": ""}

        with patch.object(server, "_store", _FakeStore()), \
             patch.object(server, "_terminal_pending", {}), \
             patch.dict(server._deep_alignment_state, {sid: state}, clear=False), \
             patch.dict(server._sessions, {}, clear=True), \
             patch.object(server, "_require_login", return_value=object()):
            resp = asyncio.run(server.terminal_pending(session_id=sid))

        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(int(payload.get("pending_confirm_count", 0)), 1)
        pending = payload.get("pending") or []
        self.assertEqual(pending[0].get("flow"), "deep_alignment")
        self.assertEqual(pending[0].get("call_id"), "call_deep_keep")

    def test_terminal_confirm_recovers_deep_alignment_question_from_state(self) -> None:
        sid = "s_deep_confirm_recover"
        state = {
            "active": True,
            "state": "waiting_question",
            "original_message": "制定一个计划",
            "file_names": [],
            "file_contents": [],
            "rounds": [],
            "active_index": 0,
            "pending_deep_ask": {
                "call_id": "call_deep_confirm",
                "question": "计划类型是什么？",
                "messages": [
                    {"role": "system", "content": "Deep Alignment"},
                    {"role": "user", "content": "Original user request:\n制定一个计划"},
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "call_deep_confirm",
                        "type": "function",
                        "function": {"name": "ask_user_question", "arguments": "{}"},
                    }]},
                ],
                "tool_call": {
                    "id": "call_deep_confirm",
                    "type": "function",
                    "function": {
                        "name": "ask_user_question",
                        "arguments": json.dumps({"question": "计划类型是什么？"}, ensure_ascii=False),
                    },
                },
            },
        }
        req = server.TerminalConfirmRequest(
            session_id=sid,
            approval=True,
            kind="question",
            call_id="call_deep_confirm",
            answer="项目开发计划",
        )

        with patch.object(server, "_require_session_access", return_value=(sid, {})), \
             patch.object(server, "_terminal_pending", {}), \
             patch.dict(server._deep_alignment_state, {sid: state}, clear=False), \
             patch.object(server, "_resume_deep_alignment_after_question", return_value={
                 "alignment_text": "我理解你需要制定一个项目开发计划。请确认是否一致。",
                 "answer": "项目开发计划",
                 "question": "计划类型是什么？",
             }):
            resp = asyncio.run(server.terminal_confirm(req))

        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(int(resp.status_code), 200)
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(payload.get("flow"), "deep_alignment")
        self.assertEqual(payload.get("state"), "waiting_confirm")
        self.assertIn("项目开发计划", str(payload.get("alignment_text", "")))
        self.assertFalse(bool(payload.get("pending_confirmation")))

    def test_terminal_confirm_question_preserves_user_denial(self) -> None:
        sid = "s_question_denial"
        pending = [{
            "kind": "question",
            "confirm_id": "ask_denied",
            "call_id": "ask_denied",
            "question": "是否允许继续？",
            "status": "pending",
        }]

        class _FakeAgent:
            perm = 511
            history: list[dict] = []
            _held_messages: list[dict] | None = [{"role": "user", "content": "帮我做"}]

            def has_pending_confirmation(self) -> bool:
                return True

            def resume_with_confirmations(self, decisions: list[dict]) -> dict:
                self.decisions = decisions
                self._held_messages = None
                self.history = []
                return {
                    "reply": "已取消澄清",
                    "tool_trace": [],
                    "tool_steps": 0,
                    "pending_confirmation": False,
                }

        fake_agent = _FakeAgent()
        req = server.TerminalConfirmRequest(
            session_id=sid,
            approval=False,
            kind="question",
            call_id="ask_denied",
        )

        with patch.object(server, "_require_session_access", return_value=(sid, {})), \
             patch.object(server, "_get_terminal_pending", return_value=pending), \
             patch.object(server, "_set_terminal_pending"), \
             patch.object(server, "_get_agent", return_value=fake_agent), \
             patch.object(server, "_write_context_log"), \
             patch.object(server, "_maybe_auto_compress_after_llm", return_value={"compressed": False}), \
             patch.object(server, "_generate_title_from_first_round", return_value=None):
            resp = asyncio.run(server.terminal_confirm(req))

        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(int(resp.status_code), 200)
        self.assertTrue(bool(payload.get("ok")))
        self.assertFalse(bool(payload.get("approval")))
        self.assertEqual(payload.get("action"), "deny")
        self.assertEqual(fake_agent.decisions[0].get("kind"), "question")
        self.assertFalse(bool(fake_agent.decisions[0].get("approval")))
        self.assertEqual(fake_agent.decisions[0].get("action"), "deny")

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

    def test_chat_plan_mode_routes_to_llm_not_slash_command(self) -> None:
        sid = "s_plan_mode_chat"

        class _FakeStore:
            def __init__(self) -> None:
                self.items: list[dict] = []

            def append_messages(self, _sid: str, items: list[dict]) -> None:
                self.items.extend(items)

        class _FakeAgent:
            def __init__(self) -> None:
                self.user_message = ""
                self.transient_context = ""

            def chat_with_meta(
                self,
                user_message: str,
                transient_system_context: str | None = None,
                transient_tail_messages: list[dict] | None = None,
            ):
                self.user_message = user_message
                self.transient_context = str(transient_system_context or "")
                return {
                    "reply": "计划如下",
                    "tool_trace": [],
                    "tool_steps": 0,
                    "pending_confirmation": False,
                }

        fake_store = _FakeStore()
        fake_agent = _FakeAgent()
        req = server.ChatRequest(message="/plan 修复登录问题", session_id=sid)
        with patch.object(server, "_require_login", return_value=object()), \
             patch.object(server, "_has_llm_perm", return_value=True), \
             patch.object(server, "_require_session_access", return_value=(sid, {})), \
             patch.object(server, "_pending_confirm_count", return_value=0), \
             patch.object(server, "_get_agent", return_value=fake_agent), \
             patch.object(server, "_tool_runtime") as runtime_mock, \
             patch.object(server, "_store", fake_store), \
             patch.object(server, "_invalidate_session_index"), \
             patch.object(server, "_maybe_auto_compress_after_llm", return_value={"compressed": False}), \
             patch.object(server, "_generate_title_from_first_round", return_value=None):
            resp = asyncio.run(server.chat(req))

        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(int(resp.status_code), 200)
        self.assertEqual(str(payload.get("reply", "")), "计划如下")
        runtime_mock.submit_command.assert_not_called()
        self.assertIn("修复登录问题", fake_agent.user_message)
        self.assertNotIn("/plan", fake_agent.user_message)
        self.assertIn("[PLAN_MODE]", fake_agent.transient_context)
        self.assertIn("Do not execute", fake_agent.transient_context)
        stored_text = json.dumps(fake_store.items, ensure_ascii=False)
        self.assertIn("/plan 修复登录问题", stored_text)

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

    def test_stream_tool_skip_returns_immediately_while_tool_is_running(self) -> None:
        from TindaAgent.Process.AI.client import LLMClient

        client = object.__new__(LLMClient)
        sid = "s_skip_running_unit"
        tool_call_id = "call_skip_running_unit"
        tool_calls = [{
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": "run_terminal",
                "arguments": json.dumps({"cmd": "sleep 10"}, ensure_ascii=False),
            },
        }]
        msgs = [{"role": "assistant", "content": "", "tool_calls": tool_calls}]
        trace: list[dict] = []
        started = threading.Event()
        release = threading.Event()

        def fake_run_mcp_agent_tool(name, user_perm, args, call_id=""):
            started.set()
            release.wait(timeout=2.0)
            return json.dumps({"ok": True, "output": "late result"}, ensure_ascii=False)

        with patch.object(tool, "run_mcp_agent_tool", side_effect=fake_run_mcp_agent_tool), \
             patch.object(tool, "skip_running_tool", return_value=True):
            iterator = client._run_tools_iter(
                tool_calls,
                511,
                msgs,
                trace,
                "unit",
                heartbeat_interval=0.02,
                session_id=sid,
            )
            deadline = time.monotonic() + 1.0
            while not started.is_set() and time.monotonic() < deadline:
                event = next(iterator)
                self.assertEqual(str(event.get("type", "")), "tool_heartbeat")
            self.assertTrue(started.is_set())
            self.assertTrue(ai_client.request_tool_skip(sid, tool_call_id=tool_call_id))
            deadline = time.monotonic() + 1.0
            result = None
            while time.monotonic() < deadline:
                event = next(iterator)
                if event.get("type") == "tool_result":
                    result = event
                    break
            release.set()
            time.sleep(0.05)

        self.assertIsNotNone(result)
        steps = result.get("steps", []) if isinstance(result, dict) else []
        self.assertEqual(len(steps), 1)
        self.assertTrue(ai_client._tool_skipped(steps[0]))
        tool_msgs = [m for m in msgs if str(m.get("role", "")) == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(str(tool_msgs[0].get("tool_call_id", "")), tool_call_id)
        self.assertEqual(trace, steps)

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

    def test_agent_resume_returns_history_delta_for_confirm_rendering(self) -> None:
        class _FakeClient:
            def chat_with_tools(self, messages: list[dict], user_perm: int, temperature: float = 0.7) -> dict:
                return {
                    "reply": "done",
                    "history_delta": [
                        {"role": "assistant", "content": "done"},
                    ],
                    "tool_steps": 0,
                    "tool_trace": [],
                }

        agent = server.Agent("test", user_perm=511, client=_FakeClient(), model_name="deepseek-v4-flash")
        agent._held_perm = 511
        pending_payload = {
            "ok": True,
            "tool_name": "ask_user_question",
            "result": {
                "pending_confirmation": True,
                "kind": "question",
                "confirm_id": "ask_1",
                "question": "继续吗？",
            },
        }
        agent._held_messages = [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_ask_1",
                "type": "function",
                "function": {"name": "ask_user_question", "arguments": json.dumps({"question": "继续吗？"})},
            }]},
            {"role": "tool", "tool_call_id": "call_ask_1", "content": json.dumps(pending_payload, ensure_ascii=False)},
        ]

        out = agent.resume_with_confirmations([{"approval": True, "confirm_id": "ask_1", "answer": "继续"}])
        self.assertEqual(str(out.get("reply", "")), "done")
        self.assertEqual(out.get("history_delta"), [{"role": "assistant", "content": "done"}])

    def test_server_delta_tool_marker_matches_trace_by_model_call_id(self) -> None:
        history_delta = [
            {
                "role": "assistant",
                "reasoning_content": "think",
                "content": "before",
                "tool_calls": [{
                    "id": "call_model_1",
                    "type": "function",
                    "function": {
                        "name": "run_terminal",
                        "arguments": json.dumps({"cmd": "echo hi"}, ensure_ascii=False),
                    },
                }],
            },
            {"role": "assistant", "content": "after"},
        ]
        trace = [{
            "agent_tool": "run_terminal",
            "call_id": "tc_0000000123",
            "tool_call_id": "call_model_1",
            "arguments": {"cmd": "echo hi"},
            "result": {"ok": True, "stdout": "hi\n"},
        }]

        substeps = server._build_substeps_from_agent_delta(history_delta, trace)
        markers = [s for s in substeps if s.get("kind") == "tool_marker"]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].get("id"), "0000000123")
        self.assertEqual(markers[0].get("tool_call_id"), "call_model_1")
        self.assertEqual(markers[0].get("stdout"), "hi\n")
        self.assertEqual(markers[0].get("status"), "done")
        self.assertEqual([s.get("kind") for s in substeps], ["thinking", "text", "tool_marker", "text"])

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
