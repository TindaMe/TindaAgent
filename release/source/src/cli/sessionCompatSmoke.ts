import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { writeJson } from "../core/json.js";
import { buildAssistantMessage, buildSystemMessage, buildUserMessage } from "../web/sessionAdapter.js";
import { assistantSubstepsFromResult } from "../web/server.js";
import { SessionStore } from "../web/sessionStore.js";

function assert(condition: unknown, message: string): void {
  if (!condition) throw new Error(message);
}

function hasContent(rows: Array<{ content: string | null }>, needle: string): boolean {
  return rows.some((row) => String(row.content || "").includes(needle));
}

const root = fs.mkdtempSync(path.join(os.tmpdir(), "tinda-session-compat-"));
const runtime = path.join(root, "runtime", "Sessions");
const legacy = path.join(root, "legacy", "Sessions");
fs.mkdirSync(path.join(legacy, "messages"), { recursive: true });

const legacySid = "legacy_jsonl";
fs.writeFileSync(
  path.join(legacy, "messages", `${legacySid}.jsonl`),
  [
    JSON.stringify({ role: "user", entry_type: "chat", content: "旧 JSONL 用户消息" }),
    JSON.stringify({ role: "assistant", entry_type: "chat", reasoning_content: "旧 reasoning", content: "旧 JSONL 助手回复" }),
    JSON.stringify({ entry_type: "tool_marker", name: "echo", ok: true, stdin: "hello", stdout: "hello" })
  ].join("\n"),
  "utf8"
);

const store = new SessionStore(runtime, legacy);
store.ensureSession(legacySid, "u_test");
const legacyRows = store.getContextMessages(legacySid);
assert(hasContent(legacyRows, "旧 JSONL 用户消息"), "legacy JSONL user row not loaded");
assert(hasContent(legacyRows, "旧 JSONL 助手回复"), "legacy JSONL assistant row not loaded");
assert(legacyRows.some((row) => row.role === "tool"), "legacy tool marker was not replayed as tool row");

const sid = "compat";
store.createSession("兼容测试", sid, "u_test");
store.appendMessages(sid, [
  buildUserMessage("第一轮", { file_names: ["a.txt"], file_contents: ["文件内容"] }),
  buildAssistantMessage([{ kind: "thinking", content: "思考一" }, { kind: "text", content: "回复一" }]),
  buildUserMessage("第二轮"),
  buildAssistantMessage([
    { kind: "tool_marker", name: "echo", ok: true, stdin: "ping", stdout: "pong", id: "call_echo", arguments: { text: "ping" }, result: { ok: true, stdout: "pong" } },
    { kind: "text", content: "回复二" }
  ]),
  buildUserMessage("第三轮"),
  buildAssistantMessage([{ kind: "text", content: "回复三" }]),
  buildUserMessage("第四轮"),
  buildAssistantMessage([{ kind: "text", content: "回复四" }])
]);
store.appendTerminal(sid, [{ kind: "cmd", content: "npm test" }, { kind: "out", content: "passed" }]);

const beforeCompress = store.getContextMessages(sid);
assert(hasContent(beforeCompress, "[文件: a.txt]"), "user file block not included in LLM context");
assert(beforeCompress.some((row) => row.role === "assistant" && String(row.reasoning_content || "").includes("思考一")), "thinking not preserved");
assert(beforeCompress.some((row) => row.role === "tool" && row.tool_call_id === "call_echo"), "tool marker not converted to tool row");
assert(hasContent(beforeCompress, "[Terminal Context]"), "terminal context not included");
const renderedBeforeCompress = store.frontendMessages(sid).entries;
const thinkingEntry = renderedBeforeCompress.find((entry) => entry.role === "assistant" && Array.isArray(entry.content) && entry.content.some((step: any) => step.kind === "thinking"));
assert(thinkingEntry?.content?.some((step: any) => step.kind === "thinking" && String(step.data || "").includes("思考一")), "thinking substep not preserved for frontend reload");

const resultSubsteps = assistantSubstepsFromResult({
  reply: "最终回复",
  reasoning_content: "最终思考",
  tool_trace: [],
  history_delta: [{ role: "assistant", reasoning_content: "真实思考", content: "真实回复" }]
});
assert(resultSubsteps[0]?.kind === "thinking" && resultSubsteps[1]?.kind === "text", "assistant result substeps should preserve thinking before text");
const resultToolSubsteps = assistantSubstepsFromResult({
  reply: "工具后回复",
  tool_trace: [{ agent_tool: "echo", call_id: "call_1", tool_call_id: "tool_1", arguments: { text: "ping" }, result: { ok: true, stdout: "pong" }, ok: true }],
  history_delta: [
    { role: "assistant", reasoning_content: "工具前思考", content: "", tool_calls: [{ id: "tool_1", function: { name: "echo", arguments: "{\"text\":\"ping\"}" } }] },
    { role: "tool", tool_call_id: "tool_1", content: "{\"ok\":true}" },
    { role: "assistant", content: "工具后回复" }
  ]
});
assert(resultToolSubsteps.map((step) => step.kind).join(",") === "thinking,tool_marker,text", "assistant result substeps should preserve tool order without duplicate markers");

const reversedSid = "reversed_reasoning";
writeJson(path.join(runtime, "messages", `${reversedSid}.json`), {
  "1": {
    role: "assistant",
    id: "reversed_assistant",
    type: "assistant_message",
    display_target: "chat",
    context_policy: "include",
    content: {
      "1": { tool_marker: { name: "echo", ok: true, stdout: "pong", id: "call_old", tool_call_id: "tool_old" } },
      "2": { text: "旧记录最终回复" },
      "3": { thinking: "旧记录思考" }
    },
    created_at: "2026-05-30T00:00:01.000Z"
  }
});
const reversedStore = new SessionStore(runtime, legacy);
const reversedLoaded = reversedStore.loadMessages(reversedSid);
assert("tool_marker" in (reversedLoaded["1"]?.content?.["1"] || {}), "legacy reversed assistant content should preserve leading tool marker");
assert("thinking" in (reversedLoaded["1"]?.content?.["2"] || {}), "legacy reversed assistant content was not repaired to thinking before text");
assert("text" in (reversedLoaded["1"]?.content?.["3"] || {}), "legacy reversed assistant content text was not preserved after thinking");
const reversedFrontend = reversedStore.frontendMessages(reversedSid).entries[0];
assert(reversedFrontend?.content?.[0]?.kind === "tool_marker" && reversedFrontend?.content?.[1]?.kind === "thinking" && reversedFrontend?.content?.[2]?.kind === "text", "frontend content order should render tool marker, thinking, then text after reload");

const chronologicalSid = "chronological_order";
writeJson(path.join(runtime, "messages", `${chronologicalSid}.json`), {
  "1": {
    role: "assistant",
    id: "late_assistant",
    type: "assistant_message",
    display_target: "chat",
    context_policy: "include",
    content: {
      "10": { text: "后写入但时间较晚" },
      "2": { thinking: "乱序思考模块" }
    },
    created_at: "2026-05-30T00:00:03.000Z"
  },
  "2": {
    role: "user",
    id: "early_user",
    type: "user_message",
    display_target: "chat",
    context_policy: "include",
    content: { "3": { user: "时间最早" } },
    created_at: "2026-05-30T00:00:01.000Z"
  },
  "3": {
    role: "assistant",
    id: "middle_assistant",
    type: "assistant_message",
    display_target: "chat",
    context_policy: "include",
    content: { "1": { text: "时间居中" } },
    created_at: "2026-05-30T00:00:02.000Z"
  }
});
const chronologicalStore = new SessionStore(runtime, legacy);
const chronologicalLoaded = chronologicalStore.loadMessages(chronologicalSid);
assert(chronologicalLoaded["1"]?.id === "early_user", "messages were not rekeyed by created_at ascending");
assert(chronologicalLoaded["2"]?.id === "middle_assistant", "middle timestamp message was not stored second");
assert(chronologicalLoaded["3"]?.id === "late_assistant", "latest timestamp message was not stored last");
assert(JSON.stringify(Object.keys(chronologicalLoaded["3"]?.content || {})) === JSON.stringify(["1", "2"]), "assistant content modules were not rekeyed sequentially");
const chronologicalFrontend = chronologicalStore.frontendMessages(chronologicalSid).entries;
assert(chronologicalFrontend.map((entry: any) => entry.id).join(",") === "early_user,middle_assistant,late_assistant", "frontend messages were not rendered in chronological order");
fs.rmSync(path.join(runtime, "messages", `${chronologicalSid}.json`), { force: true });
const chronologicalSqlStore = new SessionStore(runtime, legacy);
const chronologicalSqlLoaded = chronologicalSqlStore.loadMessages(chronologicalSid);
assert(chronologicalSqlLoaded["1"]?.id === "early_user", "sqlite mirror did not preserve chronological message order");
assert(JSON.stringify(Object.keys(chronologicalSqlLoaded["3"]?.content || {})) === JSON.stringify(["1", "2"]), "sqlite mirror did not preserve numeric content module keys");

const compressed = store.compressContext(sid, "摘要：第一轮到第二轮。");
assert(compressed.compressed === true, "first compression did not run");
const effective = store.frontendMessages(sid).entries;
assert(effective.length === 5, `compressed effective visible count expected 5, got ${effective.length}`);
assert(effective[0]?.type === "summary", "summary is not the first effective frontend entry");
const afterCompress = store.getContextMessages(sid);
assert(hasContent(afterCompress, "[Context Summary] 摘要：第一轮到第二轮。"), "summary not included in LLM context");
assert(!hasContent(afterCompress, "文件内容"), "older file block was not hidden after compression");
assert(!hasContent(afterCompress, "回复一"), "older assistant row was not hidden after compression");
assert(hasContent(afterCompress, "第三轮"), "tail rows missing after compression");
const secondCompress = store.compressContext(sid, "摘要不应重复。");
assert(secondCompress.compressed === false && secondCompress.reason === "already_compressed", "duplicate compression guard failed");

store.markResetAnchor(sid);
store.appendMessages(sid, [buildUserMessage("reset 后消息"), buildSystemMessage("内部通知")]);
const afterReset = store.frontendMessages(sid).entries;
assert(afterReset.length === 2, `reset effective count expected 2, got ${afterReset.length}`);
assert(hasContent(store.getContextMessages(sid), "reset 后消息"), "post-reset row missing from LLM context");
assert(!hasContent(store.getContextMessages(sid), "第三轮"), "pre-reset row leaked into LLM context");

const draftSid = "draft_placeholder";
writeJson(path.join(runtime, "sessions.json"), { sessions: [] });
writeJson(path.join(runtime, "messages", `${draftSid}.json`), {
  "1": {
    role: "user",
    id: "m_user",
    type: "user_message",
    display_target: "chat",
    context_policy: "include",
    content: { "1": { user: "历史消息" } },
    created_at: "2026-05-30T00:00:00.000Z",
    turn_id: "turn_draft"
  },
  "2": {
    role: "assistant",
    id: "m_draft",
    type: "assistant_message",
    display_target: "chat",
    context_policy: "include",
    content: { "1": { text: "（正在生成，若页面刷新可稍后继续查看）" } },
    created_at: "2026-05-30T00:00:01.000Z",
    turn_id: "turn_draft"
  }
});
const draftStore = new SessionStore(runtime, legacy);
const loadedDraft = draftStore.loadMessages(draftSid);
assert(Object.keys(loadedDraft).length === 1, "draft placeholder was not stripped from loaded messages");
assert(!hasContent(draftStore.frontendMessages(draftSid).entries, "正在生成，若页面刷新可稍后继续查看"), "draft placeholder leaked into frontend render");
draftStore.appendMessages(draftSid, [buildUserMessage("新的消息")]);
assert(fs.existsSync(path.join(runtime, "messages", `${draftSid}.json`)), "session messages json partition was not written");

fs.rmSync(root, { recursive: true, force: true });
console.log("session compatibility smoke passed");
