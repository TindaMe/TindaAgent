import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import crypto from "node:crypto";
import {
  PUBLIC_EXECUTE,
  PUBLIC_READ,
  PUBLIC_WRITE,
  SYSTEM_EXECUTE,
  TOOL_EXECUTE,
  TOOL_READ,
  TOOL_WRITE,
  hasPerm
} from "../core/permissions.js";
import { memoryFile, projectRoot } from "../core/paths.js";
import { nowIso, readJson, safeId, textOf, writeJson } from "../core/json.js";

const execFileAsync = promisify(execFile);

export interface ToolDef {
  name: string;
  description: string;
  perm: number;
  parameters: Record<string, any>;
  handler: (args: Record<string, any>, context: { userPerm: number; callId?: string; sessionId?: string }) => Promise<any> | any;
}

const tools = new Map<string, ToolDef>();

function register(def: ToolDef): void {
  tools.set(def.name, def);
}

function schema(properties: Record<string, any>, required: string[] = []) {
  return { type: "object", properties, required, additionalProperties: false };
}

function parseBool(value: unknown): boolean {
  const text = String(value ?? "").trim().toLowerCase();
  return ["1", "true", "yes", "y", "on"].includes(text);
}

function parseIntBounded(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, parsed));
}

function memoryPayload() {
  return readJson<{ version: number; items: Array<{ time: string; data: string }> }>(memoryFile(), { version: 1, items: [] });
}

function saveMemoryPayload(payload: unknown) {
  writeJson(memoryFile(), payload);
}

function sha256(text: string): string {
  return crypto.createHash("sha256").update(text, "utf8").digest("hex");
}

function safeRoot(root: string): string {
  const raw = String(root || ".").trim() || ".";
  return path.resolve(raw.replace(/^~(?=$|\/|\\)/, os.homedir()));
}

function walkFiles(root: string, maxDepth: number, out: string[]): void {
  const baseDepth = root.split(path.sep).length;
  const stack = [root];
  while (stack.length && out.length < 5000) {
    const current = stack.pop()!;
    let stat: fs.Stats;
    try {
      stat = fs.statSync(current);
    } catch {
      continue;
    }
    if (stat.isFile()) {
      out.push(current);
      continue;
    }
    if (!stat.isDirectory()) continue;
    const depth = current.split(path.sep).length - baseDepth;
    if (depth >= maxDepth) continue;
    let names: string[];
    try {
      names = fs.readdirSync(current);
    } catch {
      continue;
    }
    for (const name of names.reverse()) {
      if (["node_modules", ".git", "__pycache__", "dist"].includes(name)) continue;
      stack.push(path.join(current, name));
    }
  }
}

function dangerousCommand(cmd: string): boolean {
  return /\b(rm\s+-rf|mkfs|dd\s+if=|shutdown|reboot|poweroff|reg\s+delete|del\s+\/[fsq]|format\s+[a-z]:|chmod\s+-R\s+777)\b/i.test(cmd);
}

register({
  name: "echo",
  description: "Print text to tool stdout.",
  perm: PUBLIC_EXECUTE,
  parameters: schema({ text: { type: "string" } }),
  handler: ({ text }) => String(text || "")
});

register({
  name: "get_current_time",
  description: "Get current time. Parameter tz is an IANA timezone such as Asia/Shanghai.",
  perm: PUBLIC_READ,
  parameters: schema({ tz: { type: "string" } }),
  handler: ({ tz }) => {
    const zone = String(tz || "Asia/Shanghai");
    const date = new Date();
    return {
      ok: true,
      timezone: zone,
      iso: date.toISOString(),
      text: date.toLocaleString("zh-CN", { timeZone: zone, hour12: false })
    };
  }
});

register({
  name: "summarize_text",
  description: "Summarize long text without calling an external model.",
  perm: PUBLIC_READ,
  parameters: schema({ text: { type: "string" }, max_sentences: { type: "string" } }, ["text"]),
  handler: ({ text, max_sentences }) => {
    const max = parseIntBounded(max_sentences, 3, 1, 8);
    const parts = String(text || "").split(/[。！？!?]\s*|\n+/).map((x) => x.trim()).filter(Boolean);
    return parts.slice(0, max).join("。") + (parts.length > max ? "。" : "");
  }
});

register({
  name: "extract_keywords",
  description: "Extract simple keywords from text.",
  perm: PUBLIC_READ,
  parameters: schema({ text: { type: "string" }, top_k: { type: "string" } }, ["text"]),
  handler: ({ text, top_k }) => {
    const top = parseIntBounded(top_k, 8, 1, 20);
    const stop = new Set(["the", "and", "for", "this", "that", "with", "一个", "这个", "我们", "可以", "需要"]);
    const words = String(text || "").toLowerCase().match(/[\p{L}\p{N}_-]{2,}/gu) || [];
    const count = new Map<string, number>();
    for (const word of words) {
      if (stop.has(word)) continue;
      count.set(word, (count.get(word) || 0) + 1);
    }
    return [...count.entries()].sort((a, b) => b[1] - a[1]).slice(0, top).map(([word]) => word);
  }
});

register({
  name: "read_profile_snippet",
  description: "Read Tinda profile snippet by key: full/bio/project/contact/slogan.",
  perm: PUBLIC_READ,
  parameters: schema({ key: { type: "string" } }),
  handler: ({ key }) => {
    const snippets: Record<string, string> = {
      bio: "我是Tinda，来自中国的一名开发者。自2025.8.23学习计算机相关知识。",
      project: "当前项目：TindaAgent",
      contact: "联系方式：3431955251@qq.com（或搜索qq号，备注来意）",
      slogan: "Tinda · Touch into new dimensions anytime"
    };
    const clean = String(key || "full");
    if (clean === "full") return Object.values(snippets).join("\n");
    return snippets[clean] || "";
  }
});

register({
  name: "read_memories",
  description: "Read global memory as JSON.",
  perm: PUBLIC_READ,
  parameters: schema({}),
  handler: () => memoryPayload()
});

register({
  name: "save_memory",
  description: "Write a global memory entry.",
  perm: PUBLIC_WRITE,
  parameters: schema({ data: { type: "string" }, time: { type: "string" } }, ["data"]),
  handler: ({ data, time }) => {
    const payload = memoryPayload();
    payload.items = Array.isArray(payload.items) ? payload.items : [];
    payload.items.push({ time: String(time || nowIso()), data: String(data || "").slice(0, 2000) });
    payload.items = payload.items.slice(-500);
    saveMemoryPayload(payload);
    return { ok: true, count: payload.items.length };
  }
});

register({
  name: "delete_memory",
  description: "Delete memory entries by text match.",
  perm: PUBLIC_WRITE,
  parameters: schema({ contains: { type: "string" } }, ["contains"]),
  handler: ({ contains }) => {
    const needle = String(contains || "").trim();
    const payload = memoryPayload();
    const before = payload.items.length;
    payload.items = payload.items.filter((item) => !String(item.data || "").includes(needle));
    saveMemoryPayload(payload);
    return { ok: true, deleted: before - payload.items.length, count: payload.items.length };
  }
});

register({
  name: "read_file",
  description: "Read a UTF-8 text file before editing. Parameters: path.",
  perm: TOOL_READ | PUBLIC_READ,
  parameters: schema({ path: { type: "string" } }, ["path"]),
  handler: ({ path: filePath }) => {
    const target = path.resolve(String(filePath || ""));
    const content = fs.readFileSync(target, "utf8");
    return { ok: true, path: target, content, sha256: sha256(content), size: Buffer.byteLength(content, "utf8") };
  }
});

register({
  name: "search_files",
  description: "Search files by path/name substring and optional text content.",
  perm: TOOL_READ | PUBLIC_READ,
  parameters: schema({
    root: { type: "string" },
    query: { type: "string" },
    content: { type: "string" },
    max_results: { type: "string" },
    max_depth: { type: "string" }
  }),
  handler: ({ root, query, content, max_results, max_depth }) => {
    const dir = safeRoot(String(root || "."));
    const max = parseIntBounded(max_results, 50, 1, 200);
    const depth = parseIntBounded(max_depth, 8, 0, 32);
    const nameNeedle = String(query || "").toLowerCase();
    const contentNeedle = String(content || "");
    const files: string[] = [];
    walkFiles(dir, depth, files);
    const results: any[] = [];
    for (const file of files) {
      if (nameNeedle && !file.toLowerCase().includes(nameNeedle)) continue;
      let line = 0;
      let snippet = "";
      if (contentNeedle) {
        let text = "";
        try {
          text = fs.readFileSync(file, "utf8");
        } catch {
          continue;
        }
        const idx = text.indexOf(contentNeedle);
        if (idx < 0) continue;
        line = text.slice(0, idx).split(/\r?\n/).length;
        snippet = text.slice(Math.max(0, idx - 120), idx + contentNeedle.length + 120);
      }
      results.push({ path: file, line, snippet });
      if (results.length >= max) break;
    }
    return { ok: true, root: dir, results, count: results.length };
  }
});

register({
  name: "edit_file",
  description: "Edit a UTF-8 text file by exact replacement.",
  perm: TOOL_WRITE | PUBLIC_WRITE,
  parameters: schema({
    path: { type: "string" },
    old_text: { type: "string" },
    new_text: { type: "string" },
    expected_sha256: { type: "string" },
    create: { type: "string" },
    dry_run: { type: "string" }
  }, ["path", "old_text", "new_text"]),
  handler: ({ path: filePath, old_text, new_text, expected_sha256, create, dry_run }) => {
    const target = path.resolve(String(filePath || ""));
    const canCreate = parseBool(create);
    const isDry = parseBool(dry_run);
    let current = "";
    if (fs.existsSync(target)) current = fs.readFileSync(target, "utf8");
    else if (!canCreate) return { ok: false, error: "file not found" };
    if (expected_sha256 && current && sha256(current) !== String(expected_sha256)) return { ok: false, error: "sha256 mismatch" };
    const oldText = String(old_text ?? "");
    const newText = String(new_text ?? "");
    const next = current ? current.replace(oldText, newText) : newText;
    if (current && next === current) return { ok: false, error: "old_text not found" };
    if (!isDry) {
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, next, "utf8");
    }
    return { ok: true, path: target, dry_run: isDry, sha256: sha256(next), bytes: Buffer.byteLength(next, "utf8") };
  }
});

register({
  name: "search_web",
  description: "Search the web using DuckDuckGo HTML fallback and common site links.",
  perm: TOOL_READ | PUBLIC_READ,
  parameters: schema({ query: { type: "string" }, max_results: { type: "string" }, source: { type: "string" } }, ["query"]),
  handler: async ({ query, max_results }) => {
    const q = String(query || "").trim();
    const max = parseIntBounded(max_results, 5, 1, 20);
    if (!q) return { ok: false, error: "query required", results: [] };
    const results: any[] = [];
    try {
      const url = `https://duckduckgo.com/html/?q=${encodeURIComponent(q)}`;
      const res = await fetch(url, { headers: { "user-agent": "Mozilla/5.0 TindaAgent" } });
      const html = await res.text();
      const re = /<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)<\/a>/gims;
      let match: RegExpExecArray | null;
      while ((match = re.exec(html)) && results.length < max) {
        const title = match[2].replace(/<[^>]+>/g, "").replace(/&amp;/g, "&").trim();
        let href = match[1].replace(/&amp;/g, "&");
        try {
          const parsed = new URL(href);
          const uddg = parsed.searchParams.get("uddg");
          if (uddg) href = uddg;
        } catch {
          // keep raw href
        }
        results.push({ title, url: href, snippet: "" });
      }
    } catch {
      // fall back below
    }
    if (!results.length) {
      results.push(
        { title: `Google: ${q}`, url: `https://www.google.com/search?q=${encodeURIComponent(q)}`, snippet: "Search link fallback" },
        { title: `Bing: ${q}`, url: `https://www.bing.com/search?q=${encodeURIComponent(q)}`, snippet: "Search link fallback" },
        { title: `GitHub: ${q}`, url: `https://github.com/search?q=${encodeURIComponent(q)}`, snippet: "Search link fallback" }
      );
    }
    return { ok: true, query: q, results: results.slice(0, max) };
  }
});

register({
  name: "run_terminal",
  description: "Execute a shell command in terminal. Parameters: cmd, cwd optional.",
  perm: TOOL_EXECUTE | PUBLIC_EXECUTE,
  parameters: schema({ cmd: { type: "string" }, cwd: { type: "string" }, note: { type: "string" } }, ["cmd"]),
  handler: async ({ cmd, cwd }, context) => {
    const command = String(cmd || "").trim();
    if (!command) return { ok: false, error: "cmd required" };
    if (dangerousCommand(command) && !hasPerm(context.userPerm, SYSTEM_EXECUTE)) {
      const callId = context.callId || `term_${crypto.randomBytes(5).toString("hex")}`;
      return {
        ok: false,
        pending_confirmation: true,
        kind: "terminal",
        call_id: callId,
        confirm_id: callId,
        cmd: command,
        message: "command requires user confirmation"
      };
    }
    const shell = process.platform === "win32" ? "cmd.exe" : "bash";
    const args = process.platform === "win32" ? ["/d", "/s", "/c", command] : ["-lc", command];
    try {
      const { stdout, stderr } = await execFileAsync(shell, args, {
        cwd: cwd ? path.resolve(String(cwd)) : projectRoot(),
        timeout: 120000,
        maxBuffer: 1024 * 1024 * 4,
        env: { ...process.env }
      });
      return { ok: true, command, stdout: String(stdout || ""), stderr: String(stderr || ""), returncode: 0 };
    } catch (error: any) {
      return {
        ok: false,
        command,
        stdout: String(error?.stdout || ""),
        stderr: String(error?.stderr || error?.message || ""),
        returncode: Number(error?.code || 1)
      };
    }
  }
});

register({
  name: "ask_user_question",
  description: "Ask the user one blocking clarification question and wait for their answer before continuing.",
  perm: PUBLIC_EXECUTE,
  parameters: schema({
    question: { type: "string" },
    options: {
      type: "array",
      items: {
        type: "object",
        properties: {
          label: { type: "string" },
          value: { type: "string" },
          description: { type: "string" }
        },
        additionalProperties: true
      }
    },
    allow_custom: { type: "boolean" },
    placeholder: { type: "string" }
  }, ["question"]),
  handler: ({ question, options, allow_custom, placeholder }, context) => {
    const callId = context.callId || `ask_${crypto.randomBytes(5).toString("hex")}`;
    return {
      ok: false,
      pending_confirmation: true,
      kind: "question",
      flow: "agent",
      call_id: callId,
      confirm_id: callId,
      question: String(question || "需要你补充一个条件。"),
      options: Array.isArray(options) ? options : [],
      allow_custom: allow_custom !== false,
      placeholder: String(placeholder || "补充你的答案或限制条件..."),
      message: "waiting for user clarification"
    };
  }
});

function normalizePlanSteps(raw: unknown): Array<{ index: number; text: string; status: string }> {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item, idx) => {
      if (typeof item === "string") return { index: idx + 1, text: item.trim(), status: "pending" };
      if (item && typeof item === "object") {
        const row = item as Record<string, any>;
        return {
          index: Number(row.index || idx + 1),
          text: String(row.text || row.title || row.content || "").trim(),
          status: String(row.status || "pending").trim() || "pending"
        };
      }
      return { index: idx + 1, text: "", status: "pending" };
    })
    .filter((step) => step.text);
}

register({
  name: "plan",
  description: "Create or update the visible task plan. Actions: create, update, set_step_status, block, complete, clear.",
  perm: PUBLIC_EXECUTE,
  parameters: schema({
    action: { type: "string" },
    goal: { type: "string" },
    steps: {
      type: "array",
      items: {
        type: "object",
        properties: {
          index: { type: "number" },
          text: { type: "string" },
          status: { type: "string" }
        },
        additionalProperties: true
      }
    },
    status: { type: "string" },
    step_index: { type: "number" },
    step_status: { type: "string" },
    notes: { type: "string" },
    completion_note: { type: "string" }
  }),
  handler: (args) => {
    const action = String(args.action || "update").trim() || "update";
    const status = String(args.status || (action === "complete" ? "complete" : action === "block" ? "blocked" : "planned")).trim();
    return {
      ok: true,
      kind: "plan",
      action,
      goal: String(args.goal || "").trim(),
      steps: normalizePlanSteps(args.steps),
      status,
      completed: status === "complete" || action === "complete",
      step_index: Number(args.step_index || 0),
      step_status: String(args.step_status || "").trim(),
      notes: String(args.notes || "").trim(),
      completion_note: String(args.completion_note || "").trim(),
      updated_at: nowIso()
    };
  }
});

export function listToolSchemas(userPerm: number): any[] {
  return [...tools.values()]
    .filter((tool) => hasPerm(userPerm, tool.perm))
    .map((tool) => ({
      type: "function",
      function: {
        name: tool.name,
        description: tool.description,
        parameters: tool.parameters
      }
    }));
}

export function listTools(userPerm = 0): Record<string, string> {
  const out: Record<string, string> = {};
  for (const tool of tools.values()) {
    if (!userPerm || hasPerm(userPerm, tool.perm)) out[tool.name] = tool.description;
  }
  return out;
}

export async function runAgentTool(name: string, args: Record<string, any>, userPerm: number, sessionId = "", callId = "") {
  const tool = tools.get(name);
  if (!tool) return { ok: false, error: `Tool not registered: ${name}`, tool_name: name };
  if (!hasPerm(userPerm, tool.perm)) {
    return { ok: false, error_code: "permission_denied", error: "permission denied", tool_name: name, required_perm_bits: tool.perm, user_perm: userPerm };
  }
  try {
    const result = await tool.handler(args || {}, { userPerm, sessionId, callId });
    return result && typeof result === "object" ? result : { ok: true, result };
  } catch (error: any) {
    return { ok: false, error: String(error?.message || error), tool_name: name };
  }
}

export function submitShellCommand(command: string, cwd: string | undefined, onEvent: (event: Record<string, any>) => void): Promise<Record<string, any>> {
  return new Promise((resolve) => {
    const shell = process.platform === "win32" ? "cmd.exe" : "bash";
    const args = process.platform === "win32" ? ["/d", "/s", "/c", command] : ["-lc", command];
    const proc = spawn(shell, args, { cwd: cwd || projectRoot(), env: { ...process.env } });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (chunk) => {
      const text = chunk.toString("utf8");
      stdout += text;
      onEvent({ kind: "out", content: text });
    });
    proc.stderr.on("data", (chunk) => {
      const text = chunk.toString("utf8");
      stderr += text;
      onEvent({ kind: "err", class: "err", content: text });
    });
    proc.on("close", (code) => {
      const result = { ok: code === 0, command, stdout, stderr, returncode: code ?? 0 };
      onEvent({ kind: "status", class: code === 0 ? "ok" : "err", content: `process exited with code ${code ?? 0}` });
      resolve(result);
    });
    proc.on("error", (error) => {
      const result = { ok: false, command, stdout, stderr: String(error.message), returncode: 1 };
      onEvent({ kind: "err", class: "err", content: String(error.message) });
      resolve(result);
    });
  });
}

export function toolTraceStep(name: string, callId: string, args: Record<string, any>, result: any, toolCallId = "") {
  const raw = JSON.stringify(result);
  return {
    agent_tool: name,
    call_id: safeId(callId || `call_${crypto.randomBytes(6).toString("hex")}`),
    tool_call_id: toolCallId || callId,
    arguments: args || {},
    result,
    raw_result: raw,
    ok: result?.ok !== false
  };
}
