import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { writeJson } from "../core/json.js";
import { buildAssistantMessage, buildSystemMessage, buildUserMessage } from "../web/sessionAdapter.js";
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

fs.rmSync(root, { recursive: true, force: true });
console.log("session compatibility smoke passed");
