from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class ChatHeaderV178Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.chat_html = cls.repo_root / "TindaAgent" / "Web" / "chat.html"
        cls.chat_renderer = cls.repo_root / "TindaAgent" / "Web" / "chat_renderer.js"
        cls.md_renderer = cls.repo_root / "TindaAgent" / "Web" / "markdown_renderer.js"
        if not cls.chat_html.exists():
            raise unittest.SkipTest("chat.html not found")
        if not cls.chat_renderer.exists():
            raise unittest.SkipTest("chat_renderer.js not found")
        if not cls.md_renderer.exists():
            raise unittest.SkipTest("markdown_renderer.js not found")
        cls.content = cls.chat_html.read_text(encoding="utf-8")
        cls.renderer_content = cls.chat_renderer.read_text(encoding="utf-8")
        cls.md_content = cls.md_renderer.read_text(encoding="utf-8")

    def test_header_uses_account_popup_not_select_switch(self) -> None:
        self.assertIn('id="accountBtn"', self.content)
        self.assertIn('id="accountPopup"', self.content)
        self.assertIn('id="accountList"', self.content)
        self.assertNotIn('id="headerUserSwitchSelect"', self.content)
        self.assertNotIn('id="headerUserSwitchBtn"', self.content)

    def test_header_uses_dynamic_quick_buttons_container(self) -> None:
        self.assertIn('id="quickBtns"', self.content)
        self.assertIn('id="quickSep"', self.content)
        self.assertIn('const QUICK_BUTTON_DEFS = {', self.content)
        self.assertIn("function renderQuickButtons()", self.content)
        self.assertIn('href="/settings"', self.content)
        self.assertIn("deleteAllSessionsFromPanel()", self.content)

    def test_status_pill_format_kept_online_session_context(self) -> None:
        self.assertIn('在线 <span class="status-sep">·</span> 新会话 <span class="status-sep">·</span> 0', self.content)
        self.assertIn("function renderHeaderStatus()", self.content)
        self.assertIn("context-usage", self.content)

    def test_markdown_table_parser_breaks_table_on_blank_line(self) -> None:
        # markdown_renderer.js 的表格解析在遇到空行时终止当前表格
        # （v1.8.2 重构后语义：空行就是表格边界，不容忍中间空行）
        self.assertIn("isTableSeparatorLine(nextTrim)", self.md_content)
        self.assertIn("if (!rowTrim) {", self.md_content)
        self.assertIn("break;", self.md_content)

    def test_pending_confirm_modal_is_present_and_terminal_confirm_widget_removed(self) -> None:
        self.assertIn('id="pendingConfirmOverlay"', self.content)
        self.assertIn("submitPendingConfirmation", self.content)
        self.assertNotIn("renderTermConfirmInTerminal(", self.content)
        self.assertNotIn(".term-confirm {", self.content)

    def test_markdown_table_parser_accepts_colon_dash_colon_separator(self) -> None:
        # 表格分隔行正则:接受 :--: / --: / :-- 形式;v1.8.2 后要求 ≥2 个横杠
        self.assertIn(r"/^:?-{2,}:?$/.test(cell)", self.md_content)

    def test_render_markdown_accepts_loose_llm_pipe_tables(self) -> None:
        sample_text = """三重搜索源：

源 | 条件 | 方式

tavily | 设了 TAVILY_API_KEY | 官方 API，支持 answer + raw_content
builtin (DuckDuckGo) | 无 API Key | HTML 解析 DuckDuckGo 结果页
index | 离线模式 | 内置索引（30+ 站点直达链接）

内置索引覆盖的站点（按分类）：

搜索引擎 | 开发者文档 | 社区 | AI 文档 | 其他

google, bing, brave, baidu | MDN, Python Docs, React, Next.js | Reddit, HN, Stack Overflow | OpenAI, DeepSeek, Tavily | GitHub, PyPI, npm, arXiv
"""
        node_script = r"""
const fs = require("fs");
global.window = {};
const source = fs.readFileSync(process.argv[2], "utf8");
eval(source);
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = window.renderMarkdown(String(payload.content || ""));
const tableCount = (out.match(/<table>/g) || []).length;
process.stdout.write(JSON.stringify({ tableCount, out }, null, 2));
"""
        with tempfile.TemporaryDirectory(prefix="tinda_md_loose_table_") as tmp:
            tmp_dir = Path(tmp)
            script_path = tmp_dir / "render_loose_table_check.js"
            payload_path = tmp_dir / "loose_table_sample.json"
            script_path.write_text(node_script, encoding="utf-8")
            payload_path.write_text(json.dumps({"content": sample_text}, ensure_ascii=False), encoding="utf-8")

            out = subprocess.run(
                ["node", str(script_path), str(self.md_renderer), str(payload_path)],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        data = json.loads(out.stdout)
        rendered = str(data.get("out", ""))
        self.assertEqual(int(data.get("tableCount", 0)), 2)
        self.assertIn("<th", rendered)
        self.assertIn("TAVILY_API_KEY", rendered)
        self.assertNotIn("源 | 条件 | 方式", rendered)

    def test_render_markdown_accepts_bbcode_code_blocks(self) -> None:
        sample_text = """[code]
TindaAgent 搜索请求

↓

DuckDuckGo → ❌ Network is unreachable

↓

自动降级到 builtin:index

↓

返回 5 个相关搜索引擎直达链接 ✅
[/code]"""
        node_script = r"""
const fs = require("fs");
global.window = {};
const source = fs.readFileSync(process.argv[2], "utf8");
eval(source);
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = window.renderMarkdown(String(payload.content || ""));
const codeCount = (out.match(/<pre><code/g) || []).length;
process.stdout.write(JSON.stringify({ codeCount, out }, null, 2));
"""
        with tempfile.TemporaryDirectory(prefix="tinda_md_bbcode_code_") as tmp:
            tmp_dir = Path(tmp)
            script_path = tmp_dir / "render_bbcode_code_check.js"
            payload_path = tmp_dir / "bbcode_code_sample.json"
            script_path.write_text(node_script, encoding="utf-8")
            payload_path.write_text(json.dumps({"content": sample_text}, ensure_ascii=False), encoding="utf-8")

            out = subprocess.run(
                ["node", str(script_path), str(self.md_renderer), str(payload_path)],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        data = json.loads(out.stdout)
        rendered = str(data.get("out", ""))
        self.assertEqual(int(data.get("codeCount", 0)), 1)
        self.assertIn("<pre><code>", rendered)
        self.assertIn("Network is unreachable", rendered)
        self.assertNotIn("[code]", rendered)
        self.assertNotIn("[/code]", rendered)

    def test_chat_has_deep_alignment_controls(self) -> None:
        self.assertIn('id="deepBtn"', self.content)
        self.assertIn("DEEP_ENABLED_KEY", self.content)
        self.assertIn("function toggleDeepMode()", self.content)
        self.assertIn("function startDeepAlignment(payload)", self.content)
        self.assertIn("function renderDeepAlignmentCard(payload)", self.content)
        self.assertIn("function confirmDeepAlignment(card)", self.content)
        self.assertIn("function submitDeepRevision(card)", self.content)
        self.assertIn("function restoreDeepAlignmentCard(sid)", self.content)
        self.assertIn("function renderDeepAskPanel(wrapper, panel, ask)", self.content)
        self.assertIn("function submitDeepAskAnswer(wrapper)", self.content)
        self.assertIn("class=\"deep-ask-panel\"", self.content)
        self.assertIn("pending_deep_ask", self.content)
        self.assertIn("force_latest", self.content)
        self.assertIn("deepAlignmentContext", self.content)
        self.assertNotIn("function buildDeepExecuteMessage(payload)", self.content)
        self.assertIn("Deep 理解确认", self.content)

    def test_chat_has_ask_user_question_pending_ui(self) -> None:
        self.assertIn('id="pendingQuestionOptions"', self.content)
        self.assertIn('id="pendingQuestionAnswer"', self.content)
        self.assertIn("question-mode", self.content)
        self.assertIn("selected_choice", self.content)
        self.assertIn("__none_of_them__", self.content)
        self.assertIn("以上都不是，我自己补充", self.content)
        self.assertIn("提交回答", self.content)
        self.assertIn("INPUT_PLACEHOLDER_QUESTION_LOCK", self.content)
        self.assertIn("存在待回答问题，请先在弹窗中提交回答或取消", self.content)
        self.assertIn("pendingConfirmLockMessage", self.content)

    def test_chat_renderer_has_plan_tool_marker_view(self) -> None:
        self.assertIn("function renderPlanMarkerMarkdown", self.renderer_content)
        self.assertIn("--计划已记录--", self.renderer_content)
        self.assertIn("等待用户确认完成", self.renderer_content)
        self.assertIn("完成说明", self.renderer_content)
        self.assertIn("innerResult = result.result", self.renderer_content)
        self.assertIn('name === "plan" && innerResult.kind === "plan"', self.renderer_content)
        self.assertIn("renderPlanMarkerMarkdown: renderPlanMarkerMarkdown", self.renderer_content)

    def test_chat_has_movable_plan_panel_ui(self) -> None:
        self.assertIn('id="planFloat"', self.content)
        self.assertIn('id="planFloatHead"', self.content)
        self.assertIn("function renderPlanFloat(plan)", self.content)
        self.assertIn("function startPlanFloatDrag(e)", self.content)
        self.assertIn("function showPlanPanelFromToolResult(step)", self.content)
        self.assertIn("showPlanPanelsFromTrace(trace)", self.content)
        self.assertIn("function showLatestPlanFromSessionEntries(entries)", self.content)
        self.assertIn("showLatestPlanFromSessionEntries(entries)", self.content)
        self.assertIn("showPlanPanelsFromTrace(toolTrace);", self.content)
        self.assertIn("planFloatHeadEl?.addEventListener(\"mousedown\", startPlanFloatDrag)", self.content)
        self.assertIn("terminalTrace = trace.filter((step) => !isPlanToolTraceStep(step))", self.content)
        self.assertIn("plan-state", self.content)
        self.assertIn("requires_completion_confirmation", self.content)
        self.assertIn("completion_note", self.content)

    def test_chat_composer_has_plus_menu_and_selected_tool_row(self) -> None:
        self.assertIn('id="inputBox"', self.content)
        self.assertIn('id="composerSelectedRow"', self.content)
        self.assertIn('id="composerPlusBtn"', self.content)
        self.assertIn('id="composerMenu"', self.content)
        self.assertIn('id="webSearchBtn"', self.content)
        self.assertIn("function renderComposerSelections()", self.content)
        self.assertIn("web_search_enabled", self.content)
        self.assertIn("var(--composer-bottom-space", self.content)
        self.assertIn("function updateComposerBottomSpace()", self.content)
        self.assertIn("const maxInputHeight = 168", self.content)
        self.assertIn("window.addEventListener(\"resize\", () =>", self.content)

    def test_render_markdown_handles_two_tables_from_real_session_sample(self) -> None:
        sample_path = Path("/mnt/e/.tinda/agent/Data/Sessions/messages/s_c61b6eaa61d6.jsonl")
        if not sample_path.exists():
            raise unittest.SkipTest("session sample not found")

        sample_text = ""
        with sample_path.open("r", encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if row.get("id") == "m_cd952d53612b4da5":
                    sample_text = str(row.get("content", ""))
                    break
        if not sample_text:
            raise unittest.SkipTest("target sample message not found")

        node_script = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[2], "utf8");
const inputPath = process.argv[3];
function pickFunction(name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`missing function: ${name}`);
  let i = source.indexOf("{", start);
  let depth = 0;
  for (let j = i; j < source.length; j++) {
    const ch = source[j];
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return source.slice(start, j + 1);
    }
  }
  throw new Error(`unterminated function: ${name}`);
}
const fnNames = [
  "escapeHtml",
  "safeHref",
  "renderInlineMarkdown",
  "parseTableCells",
  "isTableSeparatorLine",
  "parseTableAlign",
  "findNextNonEmptyLine",
  "renderMarkdown"
];
let runtime = "\"use strict\";\n";
for (const fnName of fnNames) runtime += pickFunction(fnName) + "\n";
runtime += `
const payload = JSON.parse(fs.readFileSync(inputPath, "utf8"));
const out = renderMarkdown(String(payload.content || ""));
const tableCount = (out.match(/<table>/g) || []).length;
process.stdout.write(JSON.stringify({ tableCount, out }, null, 2));
`;
eval(runtime);
"""
        tmp_dir = self.repo_root / ".tmp_test_md_parser"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        script_path = tmp_dir / "render_markdown_check.js"
        payload_path = tmp_dir / "sample.json"
        payload_path.write_text(json.dumps({"content": sample_text}, ensure_ascii=False), encoding="utf-8")
        script_path.write_text(node_script, encoding="utf-8")

        out = subprocess.run(
            ["node", str(script_path), str(self.chat_html), str(payload_path)],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(int(data.get("tableCount", 0)), 2)


if __name__ == "__main__":
    unittest.main()
