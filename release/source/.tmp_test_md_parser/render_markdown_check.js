
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
