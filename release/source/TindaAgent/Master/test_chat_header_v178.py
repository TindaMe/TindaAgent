from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


class ChatHeaderV178Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.chat_html = cls.repo_root / "TindaAgent" / "Web" / "chat.html"
        cls.md_renderer = cls.repo_root / "TindaAgent" / "Web" / "markdown_renderer.js"
        if not cls.chat_html.exists():
            raise unittest.SkipTest("chat.html not found")
        if not cls.md_renderer.exists():
            raise unittest.SkipTest("markdown_renderer.js not found")
        cls.content = cls.chat_html.read_text(encoding="utf-8")
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
