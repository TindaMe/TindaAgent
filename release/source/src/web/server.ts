import express, { type NextFunction, type Request, type Response } from "express";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import crypto from "node:crypto";
import { Agent, LlmClient, latestLlmRequest, type ChatMessage } from "../ai/agent.js";
import { auditEvent } from "../core/audit.js";
import { loadRuntimeEnv } from "../core/env.js";
import { appVersion, dataRoot, ensureRuntimeDirs, legacyLogRoot, logRoot, projectRoot, sqliteDbFile, webRoot } from "../core/paths.js";
import { nowIso, readJson, safeId, textOf, writeJson } from "../core/json.js";
import {
  PUBLIC_EXECUTE,
  PUBLIC_READ,
  USER_ADMIN,
  hasPerm,
  permissionItems,
  permLabel
} from "../core/permissions.js";
import {
  createUser,
  deleteUser,
  getUserFromToken,
  getUserFromUid,
  isSystemUser,
  iterUsers,
  maskToken,
  profilePayload,
  publicUser,
  resetUserToken,
  updateUser,
  type User
} from "../core/users.js";
import { buildAssistantMessage, buildSystemMessage, buildUserMessage } from "./sessionAdapter.js";
import { SessionStore } from "./sessionStore.js";
import {
  answerDeepQuestion,
  createDeepQuestion,
  deepPublicPayload,
  deleteDeepState,
  loadDeepState,
  saveDeepState,
  startDeepState
} from "./deepAlignment.js";
import { deleteSessionConfig, loadSessionConfig, saveSessionConfig } from "./sessionConfig.js";
import {
  loadTavilySettings,
  loadTerminalSettings,
  loadWebSettings,
  saveTavilySettings,
  saveTerminalSettings,
  saveWebSettings,
  validateContextTokenLimit
} from "./settings.js";
import { ToolRuntimeManager } from "../tools/toolRuntime.js";

loadRuntimeEnv();
ensureRuntimeDirs();

declare global {
  namespace Express {
    interface Request {
      user?: User | null;
    }
  }
}

const app = express();
const store = new SessionStore();
const llm = new LlmClient();
const toolRuntime = new ToolRuntimeManager();
const agents = new Map<string, Agent>();
const pendingBySession = new Map<string, any[]>();

app.use(express.json({ limit: "50mb" }));
app.use(express.urlencoded({ extended: true, limit: "50mb" }));
app.use((req, res, next) => {
  const origin = String(req.headers.origin || "");
  if (/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/i.test(origin)) {
    res.setHeader("Access-Control-Allow-Origin", origin);
  }
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-User-Token, Accept");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS");
  if (req.method === "OPTIONS") return res.sendStatus(204);
  next();
});

const AUTH_OPEN_PATHS = new Set([
  "/",
  "/home",
  "/home/changelog",
  "/home/stats",
  "/chat",
  "/app",
  "/settings",
  "/logs",
  "/model-diagnostics",
  "/model-data",
  "/llm-request",
  "/favicon.ico",
  "/system/version",
  "/user-admin",
  "/auth/status",
  "/auth/select-user",
  "/auth/local-users",
  "/auth/local-login",
  "/chat_renderer.js",
  "/markdown_renderer.js",
  "/theme_toggle.js"
]);

function resolveUser(req: Request): User | null {
  const token = String(req.header("X-User-Token") || "").trim();
  return getUserFromToken(token);
}

app.use((req, res, next) => {
  const pathOnly = req.path;
  req.user = resolveUser(req);
  const open =
    AUTH_OPEN_PATHS.has(pathOnly) ||
    pathOnly.startsWith("/chat-runtime/") ||
    pathOnly.startsWith("/assets/") ||
    pathOnly.startsWith("/static/");
  if (!open && !req.user) return res.status(401).json({ detail: "not logged in" });
  const started = Date.now();
  res.on("finish", () => {
    auditEvent({
      op_type: req.method === "GET" ? "PUBLIC_READ" : "PUBLIC_WRITE",
      subsystem: "web-ts",
      func: `${req.method} ${req.path}`,
      content: `${req.method} ${req.path} -> ${res.statusCode}`,
      extra: { duration_ms: Date.now() - started, uid: req.user?.uid || "" }
    });
  });
  next();
});

function requireLogin(req: Request): User {
  if (!req.user) {
    const err: any = new Error("not logged in");
    err.status = 401;
    throw err;
  }
  return req.user;
}

function requireAdmin(req: Request): User {
  const user = requireLogin(req);
  if (!hasPerm(user.perm, USER_ADMIN)) {
    const err: any = new Error("permission denied");
    err.status = 403;
    throw err;
  }
  return user;
}

function requirePublicRead(req: Request): User {
  const user = requireLogin(req);
  if (!hasPerm(user.perm, PUBLIC_READ)) {
    const err: any = new Error("permission denied");
    err.status = 403;
    throw err;
  }
  return user;
}

function jsonError(res: Response, status: number, error: string) {
  return res.status(status).json({ ok: false, error });
}

function sendHtml(res: Response, fileName: string) {
  const file = path.join(webRoot(), fileName);
  return res.type("html").send(fs.readFileSync(file, "utf8"));
}

function sendJs(res: Response, fileName: string) {
  const file = path.join(webRoot(), fileName);
  return res.type("application/javascript").send(fs.readFileSync(file, "utf8"));
}

function sse(name: string, data: any): string {
  return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
}

function sendSseError(res: Response, error: unknown): void {
  const message = String((error as any)?.message || error || "internal error");
  if (res.writableEnded) return;
  res.write(sse("error", { message }));
  res.write(sse("done", { reply: "", tool_trace: [], tool_steps: 0, error: message }));
  res.end();
}

function normalizeTurnId(value: unknown): string {
  return String(value || "").trim().replace(/[^A-Za-z0-9_.:-]/g, "_").slice(0, 80);
}

function getAgent(sessionId: string, user: User): Agent {
  const sid = safeId(sessionId);
  let agent = agents.get(sid);
  if (!agent) {
    agent = new Agent(`web-bot-${sid}`, user.perm, llm, llm.model);
    agent.sessionId = sid;
    agents.set(sid, agent);
  }
  agent.userPerm = user.perm;
  agent.sessionId = sid;
  const rows = store.getContextMessages(sid) as ChatMessage[];
  agent.replaceConversation(rows);
  return agent;
}

function sessionAccess(req: Request, sessionId: string, create = true): string {
  const user = requireLogin(req);
  const sid = safeId(sessionId);
  if (!sid) throw Object.assign(new Error("session_id invalid"), { status: 400 });
  let meta = store.getSession(sid);
  if (!meta && !create) throw Object.assign(new Error("session not found"), { status: 404 });
  meta = store.ensureSession(sid, user.uid);
  if (meta.owner_uid && meta.owner_uid !== user.uid && !hasPerm(user.perm, USER_ADMIN)) {
    throw Object.assign(new Error("permission denied"), { status: 403 });
  }
  return sid;
}

function buildRuntimeVersionState() {
  const version = appVersion();
  return {
    version,
    display: `v${version}`,
    running_version: version,
    running_display: `v${version}`,
    effective_version: version,
    effective_display: `v${version}`,
    app_version: version,
    selected_version: version,
    selected_display: `v${version}`,
    selected_version_raw: version,
    version_consistent: true,
    signature_id: "",
    verified: false,
    verify_label: "TypeScript 本地开发版（未签名）",
    source: "local",
    source_label: "本地源码",
    current_path: projectRoot(),
    switched_at: "",
    switch_enabled: true
  };
}

function modelProvidersPayload() {
  const payload = llm.modelPayload();
  const label = llm.provider === "deepseek" ? "DeepSeek" : "OpenAI-compatible";
  return {
    ok: true,
    current_provider: llm.provider,
    providers: [
      {
        key: llm.provider,
        label,
        name: label,
        adapter: "openai_compatible",
        base_url: llm.baseURL,
        current_model: llm.model,
        models: payload.models,
        enabled: true
      }
    ]
  };
}

function readLogTail(file: string, maxLines: number): string[] {
  const text = fs.readFileSync(file, "utf8");
  return text.split(/\r?\n/).slice(-maxLines);
}

function safeLogName(raw: string): string {
  const name = path.basename(String(raw || "").trim());
  if (!/^[A-Za-z0-9._-]+$/.test(name)) return "";
  return name;
}

function logFileCandidates(name: string): string[] {
  return [path.join(logRoot(), name), path.join(legacyLogRoot(), name)];
}

function estimateUsage(rows: Array<{ content?: string | null }>): number {
  return rows.reduce((sum, row) => sum + textOf(row.content).length, 0);
}

function chatTransientContext(planMode: boolean, webSearchEnabled: boolean): string {
  const chunks: string[] = [];
  if (planMode) {
    chunks.push("[PLAN_MODE]\nCreate a concise execution plan first. Do not execute task tools until the user confirms.\n[/PLAN_MODE]");
  }
  chunks.push(
    webSearchEnabled
      ? "[WEB_SEARCH_MODE]\nWeb search is enabled for this request.\n[/WEB_SEARCH_MODE]"
      : "[WEB_SEARCH_MODE]\nWeb search is disabled for this request. Do not call search_web.\n[/WEB_SEARCH_MODE]"
  );
  return chunks.join("\n\n");
}

function stripPlanPrefix(text: string): string {
  return /^\/plan(?:\s|$)/i.test(text) ? text.replace(/^\/plan(?:\s+|$)/i, "").trim() || "Create a plan for the current request." : text;
}

function toolTraceToSubsteps(trace: any[]) {
  return (trace || []).map((step) => ({
    kind: "tool_marker",
    name: String(step.agent_tool || step.name || "unknown"),
    ok: step.result?.ok !== false,
    stdin: textOf(step.arguments?.cmd || step.arguments?.text || step.arguments?.path || "").slice(0, 500),
    stdout: textOf(step.result?.stdout || step.result?.result || step.raw_result || "").slice(0, 500),
    id: String(step.call_id || ""),
    tool_call_id: String(step.tool_call_id || ""),
    status: "done",
    arguments: step.arguments || {},
    result: step.result || {}
  }));
}

function planStatePath(sessionId: string): string {
  const sid = safeId(sessionId);
  if (!sid) throw new Error("session_id invalid");
  return path.join(dataRoot(), "Plan", `${sid}.json`);
}

function normalizePlanPayload(raw: any): Record<string, any> | null {
  const source = raw?.result && typeof raw.result === "object" ? raw.result : raw;
  if (!source || typeof source !== "object") return null;
  if (String(source.kind || "") !== "plan" && String(source.name || source.agent_tool || "") !== "plan") return null;
  const action = String(source.action || "update").trim() || "update";
  if (action === "clear") return { kind: "plan", action: "clear", deleted: true, updated_at: nowIso() };
  const steps = Array.isArray(source.steps)
    ? source.steps
        .map((step: any, idx: number) =>
          typeof step === "string"
            ? { index: idx + 1, text: step, status: "pending" }
            : {
                index: Number(step?.index || idx + 1),
                text: textOf(step?.text || step?.title || step?.content),
                status: textOf(step?.status || "pending") || "pending"
              }
        )
        .filter((step: any) => String(step.text || "").trim())
    : [];
  return {
    kind: "plan",
    action,
    goal: textOf(source.goal),
    steps,
    status: textOf(source.status || (source.completed ? "complete" : "planned")),
    completed: Boolean(source.completed) || textOf(source.status) === "complete",
    notes: textOf(source.notes),
    completion_note: textOf(source.completion_note),
    step_index: Number(source.step_index || 0),
    step_status: textOf(source.step_status),
    updated_at: textOf(source.updated_at || nowIso())
  };
}

function mergePlanPayload(current: Record<string, any> | null, update: Record<string, any>): Record<string, any> | null {
  if (!update) return current;
  if (update.action === "clear") return null;
  const base: Record<string, any> | null = current && typeof current === "object" ? { ...current, steps: Array.isArray(current.steps) ? [...current.steps] : [] } : null;
  if (update.action === "set_step_status" && base) {
    const idx = Number(update.step_index || 0) - 1;
    if (idx >= 0 && idx < base.steps.length) base.steps[idx] = { ...base.steps[idx], status: update.step_status || update.status || "done" };
    base.updated_at = update.updated_at || nowIso();
    return base;
  }
  return { ...(base || {}), ...update, steps: update.steps?.length ? update.steps : base?.steps || [] };
}

function loadPlanPayload(sessionId: string): { current: Record<string, any> | null; deleted: boolean; deleted_at?: string } {
  const sid = safeId(sessionId);
  if (!sid) return { current: null, deleted: false };
  const saved = store.loadPlan(sid);
  if (saved && typeof saved === "object" && ("current" in saved || "deleted" in saved)) {
    return { current: saved.deleted ? null : saved.current || null, deleted: Boolean(saved.deleted), deleted_at: saved.deleted_at || "" };
  }
  const legacy = readJson<Record<string, any>>(planStatePath(sid), {});
  return { current: legacy.current || null, deleted: Boolean(legacy.deleted), deleted_at: legacy.deleted_at || "" };
}

function savePlanPayload(sessionId: string, current: Record<string, any> | null, deleted = false) {
  const payload = store.savePlan(sessionId, current, deleted);
  writeJson(planStatePath(sessionId), { ...payload, deleted_at: deleted ? nowIso() : "" });
  return payload;
}

function updatePlanFromTrace(sessionId: string, trace: any[]): Record<string, any> | null {
  let current = loadPlanPayload(sessionId).current;
  let changed = false;
  for (const step of trace || []) {
    const name = String(step?.agent_tool || step?.name || step?.tool_name || "").trim();
    if (name !== "plan") continue;
    const payload = normalizePlanPayload(step);
    if (!payload) continue;
    current = mergePlanPayload(current, payload);
    changed = true;
  }
  if (changed) savePlanPayload(sessionId, current, current === null);
  return current;
}

function pendingItemsFromTrace(trace: any[], turnId: string): any[] {
  const out: any[] = [];
  for (const step of trace || []) {
    const result = step?.result && typeof step.result === "object" ? step.result : {};
    if (!result.pending_confirmation) continue;
    const kind = String(result.kind || "").trim() || "terminal";
    if (kind === "question") {
      out.push({
        flow: result.flow || "agent",
        kind: "question",
        call_id: String(result.call_id || step.call_id || ""),
        confirm_id: String(result.confirm_id || result.call_id || step.call_id || ""),
        question: String(result.question || "需要你补充一个条件。"),
        options: Array.isArray(result.options) ? result.options : [],
        allow_custom: result.allow_custom !== false,
        placeholder: String(result.placeholder || "补充你的答案或限制条件..."),
        turn_id: turnId
      });
    } else {
      out.push({
        kind: kind || "terminal",
        call_id: String(result.call_id || step.call_id || ""),
        confirm_id: String(result.confirm_id || result.call_id || step.call_id || ""),
        cmd: String(result.cmd || result.command || step.arguments?.cmd || ""),
        turn_id: turnId
      });
    }
  }
  return out;
}

function setPending(sessionId: string, items: any[]): void {
  const clean = (items || []).filter((item) => item && typeof item === "object");
  if (clean.length) pendingBySession.set(sessionId, clean);
  else pendingBySession.delete(sessionId);
}

async function generateDeepAlignmentText(message: string, fileNames: string[], revision = ""): Promise<string> {
  if (String(process.env.TINDA_DEEP_ALIGNMENT_OFFLINE || "").trim() === "1") return "";
  const prompt = [
    "你是 TindaAgent 的 Deep 对齐模块。请用中文写一段简洁的用户确认摘要。",
    "只做意图和约束对齐，不要执行任务，不要制定冗长计划。",
    "",
    `用户请求：${message || "(无文本，仅附件)"}`,
    fileNames.length ? `附件：${fileNames.filter(Boolean).join("、")}` : "附件：无",
    revision ? `用户修正：${revision}` : "",
    "",
    "输出包括：我理解的目标、关键约束、确认后我会做什么。"
  ].filter(Boolean).join("\n");
  try {
    const probe = await llm.probe(llm.model, [{ role: "user", content: prompt }], 45000);
    return probe.content.trim();
  } catch {
    return "";
  }
}

function buildUserText(body: any): string {
  let raw = textOf(body.message).trim();
  const names = Array.isArray(body.file_names) ? body.file_names : [];
  const contents = Array.isArray(body.file_contents) ? body.file_contents : [];
  names.forEach((name: string, idx: number) => {
    raw = `[文件: ${name}]\n\`\`\`\n${contents[idx] || ""}\n\`\`\`\n${raw}`;
  });
  return raw;
}

function commandFromToolJobBody(body: any): string {
  const args = body && typeof body.args === "object" && !Array.isArray(body.args) ? body.args : {};
  const command = body?.command ?? body?.cmd ?? args.command ?? args.cmd ?? body?.message ?? body?.text;
  return String(command || "").trim();
}

app.get("/", (_req, res) => sendHtml(res, "home.html"));
app.get("/home", (_req, res) => sendHtml(res, "home.html"));
app.get("/chat", (_req, res) => sendHtml(res, "chat.html"));
app.get("/app", (_req, res) => sendHtml(res, "chat.html"));
app.get("/settings", (_req, res) => sendHtml(res, "settings.html"));
app.get("/logs", (_req, res) => sendHtml(res, "logs.html"));
app.get("/model-diagnostics", (_req, res) => sendHtml(res, "model_diagnostics.html"));
app.get("/llm-request", (_req, res) => sendHtml(res, "llm_request.html"));
app.get("/model-data", (_req, res) => sendHtml(res, "llm_request.html"));
app.get("/user-admin", (_req, res) => sendHtml(res, "user_admin.html"));
app.get("/favicon.ico", (_req, res) => res.status(204).send(""));
app.get("/chat_renderer.js", (_req, res) => sendJs(res, "chat_renderer.js"));
app.get("/markdown_renderer.js", (_req, res) => sendJs(res, "markdown_renderer.js"));
app.get("/theme_toggle.js", (_req, res) => sendJs(res, "theme_toggle.js"));
app.get("/chat-runtime/:file", (req, res) => {
  const file = path.basename(req.params.file || "");
  if (!/^[A-Za-z0-9_.-]+\.js$/.test(file)) return res.sendStatus(404);
  const target = path.join(webRoot(), "chat_runtime", file);
  if (!fs.existsSync(target)) return res.sendStatus(404);
  return res.type("application/javascript").send(fs.readFileSync(target, "utf8"));
});

app.get("/home/changelog", (_req, res) => {
  const file = path.join(projectRoot(), "TindaAgent", "docs", "CHANGELOG.md");
  res.type("text/markdown").send(fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "# TindaAgent\n\n暂无更新日志。");
});

app.get("/home/stats", (req, res) => {
  const now = new Date();
  const month = String(req.query.month || `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`);
  const [year, mon] = month.split("-").map(Number);
  const daysInMonth = new Date(year || now.getFullYear(), mon || now.getMonth() + 1, 0).getDate();
  const days = Array.from({ length: daysInMonth }, (_, i) => {
    const day = `${year}-${String(mon).padStart(2, "0")}-${String(i + 1).padStart(2, "0")}`;
    return { date: day, count: 0, level: 0 };
  });
  const usage_24h = Array.from({ length: 8 }, (_, i) => ({ label: `${String(i * 3).padStart(2, "0")}`, count: 0, percent: 0 }));
  const memory = process.memoryUsage();
  res.json({
    ok: true,
    month,
    current_month: month,
    months: [month],
    days,
    usage_24h,
    runtime: {
      app_version: appVersion(),
      pid: process.pid,
      node: process.version,
      uptime_sec: Math.round(process.uptime()),
      system_time: nowIso()
    },
    memory: {
      rss_bytes: memory.rss,
      heap_used_bytes: memory.heapUsed,
      heap_total_bytes: memory.heapTotal
    },
    storage: []
  });
});

app.get("/auth/status", (req, res) => {
  const user = req.user || resolveUser(req);
  res.json(user ? { logged_in: true, user: profilePayload(user) } : { logged_in: false, user: null });
});

app.get("/auth/local-users", (_req, res) => {
  res.json({ users: iterUsers().filter((u) => !isSystemUser(u)).map((u) => publicUser(u)).filter(Boolean) });
});

app.post("/auth/local-login", (req, res) => {
  const uid = String(req.body?.uid || "").trim();
  const user = getUserFromUid(uid);
  if (!user) return jsonError(res, 404, "user not found");
  res.json({ ok: true, logged_in: true, user: publicUser(user), token: user.token });
});

app.post("/auth/select-user", (req, res) => {
  const user = requireLogin(req);
  res.json({ ok: true, logged_in: true, user: profilePayload(user) });
});

app.get("/auth/users", (req, res) => {
  const user = requireLogin(req);
  res.json({
    users: iterUsers().filter((u) => !isSystemUser(u)).map((u) => publicUser(u, user.uid)).filter(Boolean),
    current_uid: user.uid
  });
});

app.get("/user/profile", (req, res) => res.json(profilePayload(requireLogin(req))));
app.get("/users", (req, res) => {
  const user = requireLogin(req);
  res.json({
    users: iterUsers().filter((u) => !isSystemUser(u)).map((u) => publicUser(u, user.uid)).filter(Boolean),
    current_uid: user.uid
  });
});

app.post("/user/switch", (req, res) => res.json({ ok: true, ...profilePayload(requireLogin(req)) }));

app.get("/admin/users", (req, res) => {
  const current = requireAdmin(req);
  res.json({
    ok: true,
    users: iterUsers()
      .filter((u) => !isSystemUser(u))
      .map((u) => ({ ...publicUser(u, current.uid), token_masked: maskToken(u.token) })),
    current_uid: current.uid
  });
});

app.get("/admin/permissions", (req, res) => {
  requireAdmin(req);
  res.json({ ok: true, items: permissionItems() });
});

app.post("/admin/users", (req, res) => {
  requireAdmin(req);
  try {
    const user = createUser(req.body?.name, Number(req.body?.perm || 0), req.body?.token);
    res.json({ ok: true, user: { ...publicUser(user), token_masked: maskToken(user.token) }, token: user.token });
  } catch (error: any) {
    jsonError(res, 400, String(error?.message || error));
  }
});

app.patch("/admin/users/:uid", (req, res) => {
  const current = requireAdmin(req);
  if (safeId(req.params.uid) === current.uid) return jsonError(res, 400, "cannot modify current user");
  try {
    const user = updateUser(req.params.uid, { name: req.body?.name, perm: req.body?.perm, token: req.body?.token });
    if (!user) return jsonError(res, 404, "user not found");
    res.json({ ok: true, user: { ...publicUser(user, current.uid), token_masked: maskToken(user.token) } });
  } catch (error: any) {
    jsonError(res, 400, String(error?.message || error));
  }
});

app.patch("/admin/users/:uid/permissions", (req, res) => {
  const current = requireAdmin(req);
  if (safeId(req.params.uid) === current.uid) return jsonError(res, 400, "cannot modify current user");
  const user = updateUser(req.params.uid, { perm: Number(req.body?.perm || 0) });
  if (!user) return jsonError(res, 404, "user not found");
  res.json({ ok: true, user: { ...publicUser(user, current.uid), token_masked: maskToken(user.token) } });
});

app.post("/admin/users/:uid/token/reset", (req, res) => {
  const current = requireAdmin(req);
  if (safeId(req.params.uid) === current.uid) return jsonError(res, 400, "cannot modify current user");
  const user = resetUserToken(req.params.uid);
  if (!user) return jsonError(res, 404, "user not found");
  res.json({ ok: true, user: { ...publicUser(user, current.uid), token_masked: maskToken(user.token) }, token: user.token });
});

app.delete("/admin/users/:uid", (req, res) => {
  const current = requireAdmin(req);
  if (safeId(req.params.uid) === current.uid) return jsonError(res, 400, "cannot delete current user");
  const ok = deleteUser(req.params.uid);
  if (!ok) return jsonError(res, 404, "user not found");
  res.json({ ok: true, uid: req.params.uid });
});

app.get("/web-settings", (_req, res) => res.json(loadWebSettings()));
app.put("/web-settings", (req, res) => {
  const body = req.body || {};
  if (body.token_limit !== undefined) {
    const [ok, value, error] = validateContextTokenLimit(body.token_limit);
    if (!ok) return jsonError(res, 400, error);
    body.token_limit = value;
  }
  res.json({ ok: true, ...saveWebSettings(body) });
});
app.get("/terminal/settings", (_req, res) => res.json(loadTerminalSettings()));
app.put("/terminal/settings", (req, res) => res.json(saveTerminalSettings(req.body || {})));
app.get("/tavily/settings", (_req, res) => res.json(loadTavilySettings()));
app.put("/tavily/settings", (req, res) => res.json(saveTavilySettings(req.body?.api_key || "", req.body?.base_url || "")));

app.get("/system/version", (_req, res) => res.json({ ok: true, ...buildRuntimeVersionState() }));
app.get("/system/versions", (req, res) => {
  requirePublicRead(req);
  res.json({ ok: true, source: "local", repo: "TindaMe/TindaAgent", current: buildRuntimeVersionState(), local_versions: [], remote_versions: [], remote_ok: true });
});
app.post("/system/version/install", (req, res) => {
  requireAdmin(req);
  res.status(400).json({ ok: false, error: "TypeScript 版本暂不支持在线安装历史版本" });
});
app.post("/system/version/switch", (req, res) => {
  requireAdmin(req);
  res.status(400).json({ ok: false, error: "TypeScript 版本暂不支持在线切换历史版本" });
});
app.post("/system/version/snapshot", (req, res) => {
  requireAdmin(req);
  res.json({ ok: true, version: req.body?.version || appVersion(), source: "local_snapshot" });
});
app.post("/system/version/snapshot/current", (req, res) => {
  requireAdmin(req);
  res.json({ ok: true, version: appVersion(), source: "local_snapshot" });
});
app.get("/system/version/compat", (req, res) => {
  requirePublicRead(req);
  res.json({ ok: true, compatible: true, target: String(req.query.target || "") });
});

app.get("/model", (req, res) => {
  requireLogin(req);
  res.json({ ...llm.modelPayload(), current_provider: llm.provider, providers: modelProvidersPayload().providers });
});
app.post("/model", (req, res) => {
  requireAdmin(req);
  try {
    llm.switchModel(req.body?.model);
    res.json({ ...llm.modelPayload(), ok: true, current_provider: llm.provider, providers: modelProvidersPayload().providers });
  } catch (error: any) {
    jsonError(res, 400, String(error?.message || error));
  }
});
app.get("/model-data/providers", (_req, res) => res.json(modelProvidersPayload()));
app.post("/model-data/providers", (_req, res) => res.json(modelProvidersPayload()));
app.post("/model-data/models", (req, res) => res.json({ ok: true, provider: req.body?.provider || "deepseek", model_id: req.body?.model_id || "" }));
app.delete("/model-data/models", (req, res) => res.json({ ok: true, provider: req.query.provider || "deepseek", model_id: req.query.model_id || "" }));
app.get("/model-data/balance", async (req, res) => res.json(await llm.fetchBalance()));
app.get("/llm-request/latest", async (_req, res) => {
  const data = await latestLlmRequest();
  res.json({ ...data, exists: !!data.request, payload: data.request, log_file: sqliteDbFile() });
});
app.get("/model-data/latest", async (_req, res) => {
  const data = await latestLlmRequest();
  res.json({ ...data, exists: !!data.request, payload: data.request, log_file: sqliteDbFile() });
});
app.post("/model-diagnostics/run", async (req, res) => {
  try {
    requireLogin(req);
    const allowed = new Set(["connectivity", "reasoning", "image", "video"]);
    const tests = (Array.isArray(req.body?.tests) ? req.body.tests.map((x: any) => String(x).trim().toLowerCase()).filter(Boolean) : ["connectivity"]).filter((test: string, idx: number, arr: string[]) => arr.indexOf(test) === idx);
    if (!tests.length) return jsonError(res, 400, "tests 不能为空");
    const invalid = tests.find((test: string) => !allowed.has(test));
    if (invalid) return jsonError(res, 400, `不支持的 tests 项: ${invalid}`);
    const model = String(req.body?.model || llm.model).trim();
    if (!model) return jsonError(res, 400, "model 无效");
    const startedAt = nowIso();
    const results = [];
    for (const test of tests) {
      if (test === "connectivity") {
        try {
          const probe = await llm.probe(model, [{ role: "user", content: "请仅回复：PONG" }]);
          const ok = /pong/i.test(probe.content);
          results.push({ test, status: ok ? "pass" : "warn", latency_ms: probe.latency_ms, summary: ok ? "连接正常" : "模型有响应但未按预期回复 PONG", detail: probe.content });
        } catch (error: any) {
          results.push({ test, status: "fail", latency_ms: 0, summary: "连接失败", detail: String(error?.message || error) });
        }
      } else if (test === "reasoning") {
        try {
          const probe = await llm.probe(model, [{ role: "user", content: "小李比小王大2岁，小王比小张大3岁。请问小李比小张大几岁？只回答数字和单位。" }]);
          const ok = probe.content.includes("5");
          results.push({ test, status: ok ? "pass" : "warn", latency_ms: probe.latency_ms, summary: ok ? "基础推理正常" : "模型有响应但答案需人工确认", detail: probe.content, reasoning_content: probe.reasoning_content });
        } catch (error: any) {
          results.push({ test, status: "fail", latency_ms: 0, summary: "推理测试失败", detail: String(error?.message || error) });
        }
      } else {
        results.push({ test, status: "unsupported", latency_ms: 0, summary: "当前 TypeScript OpenAI-compatible 文本通道暂未启用该多模态诊断", detail: "" });
      }
    }
    res.json({ ok: true, provider: req.body?.provider || llm.provider, model, started_at: startedAt, finished_at: nowIso(), results });
  } catch (error) {
    jsonError(res, 500, String((error as any)?.message || error));
  }
});

app.post("/sessions", (req, res) => {
  const user = requireLogin(req);
  const row = store.createSession(req.body?.title || "新对话", req.body?.session_id || "", user.uid);
  res.json({ ok: true, session: row });
});
app.get("/sessions", (req, res) => {
  const user = requireLogin(req);
  res.json(store.listSessions(Number(req.query.limit || 100), Number(req.query.offset || 0), user.uid));
});
app.patch("/sessions/:session_id/config", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id);
  const cfg = saveSessionConfig(sid, req.body || {});
  res.json({ ok: true, session_id: sid, config: cfg });
});
app.get("/sessions/:session_id/config", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id);
  res.json({ ok: true, session_id: sid, config: loadSessionConfig(sid) });
});
app.get("/sessions/:session_id/messages", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  res.json({ ...store.frontendMessages(sid, Number(req.query.limit || 0), Number(req.query.before_seq || 0)), plan: loadPlanPayload(sid) });
});
app.post("/sessions/:session_id/title", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id);
  res.json({ ok: true, session: store.setSessionTitle(sid, req.body?.title || "新对话") });
});
app.delete("/sessions/:session_id", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  agents.delete(sid);
  pendingBySession.delete(sid);
  deleteDeepState(sid);
  deleteSessionConfig(sid);
  const ok = store.deleteSession(sid);
  res.status(ok ? 200 : 404).json(ok ? { ok: true, session_id: sid } : { ok: false, error: "session not found" });
});
app.delete("/sessions", (req, res) => {
  const user = requireLogin(req);
  const deleted = store.clearAll(user.uid);
  agents.clear();
  res.json({ ok: true, deleted });
});
app.delete("/sessions/:session_id/plan", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  const row = store.markPlanDeleted(sid);
  savePlanPayload(sid, null, true);
  res.json({ ok: true, session_id: sid, plan: { deleted: true, deleted_at: row.plan_deleted_at } });
});
app.get("/sessions/:session_id/context-usage", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  const rows = store.getContextMessages(sid);
  const meta = store.getSession(sid);
  const cfg = loadSessionConfig(sid);
  const tokenLimit = Number(cfg.token_limit || cfg.max_context_tokens || loadWebSettings().token_limit || 16000);
  res.json({ ok: true, session_id: sid, title: meta?.title || "新对话", usage_length: estimateUsage(rows), max_context_tokens: tokenLimit });
});
app.post("/sessions/:session_id/compress", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  try {
    const result = store.compressContext(sid, "之前对话已压缩为摘要。");
    res.json({ ok: true, ...result, usage_before: 0, usage_after: 0, max_context_tokens: loadWebSettings().token_limit });
  } catch (error: any) {
    jsonError(res, 400, String(error?.message || error));
  }
});

app.get("/sessions/:session_id/deep", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  res.json(deepPublicPayload(sid));
});
app.post("/sessions/:session_id/deep/align", async (req, res) => {
  const sid = sessionAccess(req, req.params.session_id);
  const reqSid = safeId(req.body?.session_id || sid);
  if (reqSid && reqSid !== sid) return jsonError(res, 400, "session_id mismatch");
  const message = textOf(req.body?.message).trim();
  const fileNames = Array.isArray(req.body?.file_names) ? req.body.file_names.map(textOf) : [];
  if (!message && !fileNames.length) return jsonError(res, 400, "message required");
  const revision = textOf(req.body?.revision);
  const alignmentText = await generateDeepAlignmentText(message, fileNames, revision);
  startDeepState(sid, {
    message,
    file_names: fileNames,
    file_contents: Array.isArray(req.body?.file_contents) ? req.body.file_contents.map(textOf) : [],
    revision,
    alignment_text: alignmentText
  });
  if (String(req.body?.require_question || "").trim() === "1") {
    const pending = createDeepQuestion(sid);
    setPending(sid, [pending]);
    return res.json({ ...deepPublicPayload(sid), pending_confirmation: true, pending_confirm_count: 1, pending: [pending] });
  }
  setPending(sid, []);
  res.json(deepPublicPayload(sid));
});
app.post("/sessions/:session_id/deep/revise", async (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  const state = loadDeepState(sid);
  if (!state) return jsonError(res, 404, "no active deep alignment");
  const revision = textOf(req.body?.revision);
  const alignmentText = await generateDeepAlignmentText(state.original_message, state.file_names, revision);
  startDeepState(sid, {
    message: state.original_message,
    file_names: state.file_names,
    file_contents: state.file_contents,
    revision,
    alignment_text: alignmentText
  });
  res.json(deepPublicPayload(sid));
});
app.post("/sessions/:session_id/deep/back", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  const state = loadDeepState(sid);
  if (!state) return jsonError(res, 404, "no active deep alignment");
  state.active_index = Math.max(0, state.active_index - 1);
  state.state = "waiting_confirm";
  state.pending_deep_ask = null;
  saveDeepState(sid, state);
  res.json(deepPublicPayload(sid));
});
app.post("/sessions/:session_id/deep/confirm", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  if (!loadDeepState(sid)) return jsonError(res, 404, "no active deep alignment");
  const payload = deepPublicPayload(sid);
  deleteDeepState(sid);
  setPending(sid, []);
  res.json({ ...payload, state: "confirmed", active: false });
});
app.post("/sessions/:session_id/deep/cancel", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  deleteDeepState(sid);
  setPending(sid, []);
  res.json({ ok: true, session_id: sid, active: false, state: "idle" });
});

app.post("/chat", async (req, res, next) => {
  try {
    const user = requireLogin(req);
    if (!hasPerm(user.perm, PUBLIC_EXECUTE)) return jsonError(res, 403, "权限不足：当前账户不可调用 LLM 对话");
    const sid = sessionAccess(req, req.body?.session_id);
    const turnId = normalizeTurnId(req.body?.client_turn_id) || `turn_${crypto.randomBytes(6).toString("hex")}`;
    const message = buildUserText(req.body);
    if (!message.trim()) return res.json({ reply: "", tool_trace: [], tool_steps: 0, turn_id: turnId });
    if (message.startsWith("/") && !/^\/plan(?:\s|$)/i.test(message)) {
      const job = toolRuntime.submitCommand(sid, message, user.perm);
      store.appendMessages(sid, [
        buildUserMessage(message, { turn_id: turnId }),
        buildAssistantMessage([{ kind: "text", content: "> >_<\n> --调用工具中--" }], { turn_id: turnId, type: "tool_marker", context_policy: "exclude" })
      ]);
      return res.json({ reply: "> --调用工具中--", tool_trace: [], tool_steps: 0, tool_job: job, tool_async: true, turn_id: turnId });
    }
    const agent = getAgent(sid, user);
    const planMode = /^\/plan(?:\s|$)/i.test(message);
    const result = await agent.chat(stripPlanPrefix(message), chatTransientContext(planMode, Boolean(req.body?.web_search_enabled)));
    const pending = pendingItemsFromTrace(result.tool_trace, turnId);
    setPending(sid, pending);
    updatePlanFromTrace(sid, result.tool_trace);
    const substeps = [...toolTraceToSubsteps(result.tool_trace), { kind: "text", content: result.reply || "（无回复）" }];
    store.appendMessages(sid, [buildUserMessage(textOf(req.body?.message), { turn_id: turnId }), buildAssistantMessage(substeps, { turn_id: turnId })]);
    res.json({ ...result, turn_id: turnId, pending_confirmation: pending.length > 0, pending_confirm_count: pending.length, pending, plan: loadPlanPayload(sid) });
  } catch (error) {
    next(error);
  }
});

app.get("/chat/stream", async (req, res, next) => {
  let streamStarted = false;
  try {
    const user = requireLogin(req);
    if (!hasPerm(user.perm, PUBLIC_EXECUTE)) {
      res.type("text/event-stream").send(sse("error", { message: "权限不足：当前账户不可调用 LLM 对话" }) + sse("done", { reply: "", tool_trace: [], tool_steps: 0 }));
      return;
    }
    const sid = sessionAccess(req, String(req.query.session_id || ""));
    const turnId = normalizeTurnId(req.query.client_turn_id) || `turn_${crypto.randomBytes(6).toString("hex")}`;
    const message = textOf(req.query.message).trim();
    res.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive"
    });
    streamStarted = true;
    if (message.startsWith("/") && !/^\/plan(?:\s|$)/i.test(message)) {
      const job = toolRuntime.submitCommand(sid, message, user.perm);
      store.appendMessages(sid, [
        buildUserMessage(message, { turn_id: turnId }),
        buildAssistantMessage([{ kind: "text", content: "> >_<\n> --调用工具中--" }], { turn_id: turnId, type: "tool_marker", context_policy: "exclude" })
      ]);
      res.write(sse("replace_segment", { content: "" }));
      res.write(sse("delta", { content: "> >_<\n> --调用工具中--" }));
      res.write(sse("done", { reply: "> --调用工具中--", tool_trace: [], tool_steps: 0, tool_job: job, tool_async: true, turn_id: turnId }));
      res.end();
      return;
    }
    store.ensureTurnDraft(
      sid,
      buildUserMessage(message, { turn_id: turnId }),
      buildAssistantMessage([{ kind: "text", content: "（正在生成，若页面刷新可稍后继续查看）" }], { turn_id: turnId }),
      turnId
    );
    const agent = getAgent(sid, user);
    const planMode = /^\/plan(?:\s|$)/i.test(message);
    let final: any = null;
    for await (const event of agent.stream(stripPlanPrefix(message), chatTransientContext(planMode, String(req.query.web_search_enabled || "") === "1"))) {
      if (event.type === "done") final = event;
      res.write(sse(event.type, event));
    }
    const reply = textOf(final?.reply || "");
    const pending = pendingItemsFromTrace(final?.tool_trace || [], turnId);
    setPending(sid, pending);
    updatePlanFromTrace(sid, final?.tool_trace || []);
    const substeps = [...toolTraceToSubsteps(final?.tool_trace || []), { kind: "text", content: reply || "（无回复）" }];
    store.replaceAssistantByTurn(sid, turnId, substeps);
    res.end();
  } catch (error) {
    if (streamStarted || res.headersSent) {
      sendSseError(res, error);
      return;
    }
    next(error);
  }
});

app.get("/terminal/pending", (req, res) => {
  const sid = sessionAccess(req, String(req.query.session_id || ""), false);
  const pending = pendingBySession.get(sid) || [];
  res.json({ ok: true, session_id: sid, pending, pending_confirm_count: pending.length });
});
app.post("/terminal/confirm", async (req, res) => {
  const sid = sessionAccess(req, req.body?.session_id, false);
  const pending = pendingBySession.get(sid) || [];
  if (!pending.length) return res.status(409).json({ ok: false, error: "no pending confirmation for this session", error_code: "no_pending_confirmation", pending_confirm_count: 0, pending: [] });
  const callId = textOf(req.body?.call_id || req.body?.confirm_id);
  const index = callId ? pending.findIndex((item) => [item.call_id, item.confirm_id].map(textOf).includes(callId)) : 0;
  if (index < 0) return jsonError(res, 400, `call_id not pending: ${callId}`);
  const target = pending[index];
  const remaining = pending.filter((_, idx) => idx !== index);
  if (String(target.flow || "") === "deep_alignment" && String(target.kind || "") === "question") {
    if (!req.body?.approval) {
      deleteDeepState(sid);
      setPending(sid, remaining);
      return res.json({ ok: true, flow: "deep_alignment", state: "cancelled", active: false, session_id: sid, reply: "", tool_trace: [], tool_steps: 0, pending_confirmation: remaining.length > 0, pending_confirm_count: remaining.length, pending: remaining });
    }
    answerDeepQuestion(sid, textOf(req.body?.answer), textOf(req.body?.choice));
    setPending(sid, remaining);
    return res.json({ ...deepPublicPayload(sid), flow: "deep_alignment", reply: "", tool_trace: [], tool_steps: 0, pending_confirmation: remaining.length > 0, pending_confirm_count: remaining.length, pending: remaining });
  }
  setPending(sid, remaining);
  const agent = agents.get(sid);
  if (agent && String(target.call_id || target.confirm_id || "").trim()) {
    const approval = Boolean(req.body?.approval);
    const kind = String(target.kind || "").trim();
    const resultPayload =
      kind === "question"
        ? {
            ok: approval,
            kind: "question_answer",
            approval,
            question: String(target.question || ""),
            answer: textOf(req.body?.answer || req.body?.choice || (approval ? "按当前理解继续" : "用户取消")),
            message: approval ? "The user answered the clarification question. Continue using this answer." : "The user cancelled the clarification question."
          }
        : {
            ok: approval,
            kind: "terminal_confirmation",
            approval,
            cmd: String(target.cmd || ""),
            message: approval ? "The user approved the command. Continue with this confirmation." : "The user denied the command. Do not execute it."
          };
    try {
      const result = await agent.resumeWithToolResult(String(target.call_id || target.confirm_id), resultPayload);
      const nextPending = pendingItemsFromTrace(result.tool_trace || [], String(target.turn_id || ""));
      const combinedPending = [...remaining, ...nextPending];
      setPending(sid, combinedPending);
      updatePlanFromTrace(sid, result.tool_trace || []);
      const substeps = [...toolTraceToSubsteps(result.tool_trace || []), { kind: "text", content: result.reply || "（无回复）" }];
      if (substeps.length) store.appendMessages(sid, [buildAssistantMessage(substeps, { turn_id: String(target.turn_id || "") || undefined })]);
      return res.json({ ok: true, session_id: sid, reply: result.reply, tool_trace: result.tool_trace, tool_steps: result.tool_steps, pending_confirmation: combinedPending.length > 0, pending_confirm_count: combinedPending.length, pending: combinedPending });
    } catch (error: any) {
      return jsonError(res, 500, String(error?.message || error));
    }
  }
  res.json({ ok: true, session_id: sid, reply: "", tool_trace: [], tool_steps: 0, pending_confirmation: remaining.length > 0, pending_confirm_count: remaining.length, pending: remaining });
});
app.post("/reset", (req, res) => {
  const sid = sessionAccess(req, req.body?.session_id, false);
  agents.delete(sid);
  pendingBySession.delete(sid);
  res.json({ ok: true, ...store.markResetAnchor(sid) });
});
app.post("/session/events", (req, res) => {
  const sid = sessionAccess(req, req.body?.session_id);
  const chatRows: any[] = [];
  const terminalRows: any[] = [];
  for (const item of Array.isArray(req.body?.entries) ? req.body.entries : []) {
    if (item?.entry_type === "terminal") terminalRows.push({ kind: item.terminal_kind || "out", class: item.terminal_class || "", content: textOf(item.content), ts: item.ts || nowIso() });
    else if (item?.role === "user") chatRows.push(buildUserMessage(textOf(item.content)));
    else if (item?.role === "system") chatRows.push(buildSystemMessage(textOf(item.content)));
    else chatRows.push(buildAssistantMessage([{ kind: "text", content: textOf(item.content) }]));
  }
  const result: any = { ok: true, session_id: sid };
  if (chatRows.length) result.chat_saved = store.appendMessages(sid, chatRows);
  if (terminalRows.length) result.terminal_saved = store.appendTerminal(sid, terminalRows);
  res.json(result);
});
app.get("/sessions/:session_id/terminal", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  res.json(store.frontendTerminal(sid, Number(req.query.limit || 300)));
});
app.post("/sessions/:session_id/tool-jobs", (req, res) => {
  const user = requireLogin(req);
  const sid = sessionAccess(req, req.params.session_id);
  try {
    const job = toolRuntime.submitCommand(sid, commandFromToolJobBody(req.body), user.perm);
    res.json({ ok: true, job });
  } catch (error: any) {
    jsonError(res, 400, String(error?.message || error));
  }
});
app.get("/sessions/:session_id/tool-events", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  const payload = toolRuntime.getEvents(sid, Number(req.query.after_seq || 0), Number(req.query.limit || 200));
  if (payload.events.length) {
    store.appendTerminal(sid, payload.events.map((e) => ({ ...e, id: `terminal_${e.seq}`, source_seq: e.seq, source: "tool_runtime" })));
  }
  res.json(payload);
});
app.get("/sessions/:session_id/tool-jobs/:job_id", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  const job = toolRuntime.getJob(sid, req.params.job_id);
  if (!job) return jsonError(res, 404, "job not found");
  res.json({ ok: true, job });
});
app.post("/sessions/:session_id/tool-calls/:tool_call_id/skip", (req, res) => {
  const sid = sessionAccess(req, req.params.session_id, false);
  res.json({ ok: true, session_id: sid, tool_call_id: req.params.tool_call_id, call_id: req.body?.call_id || "" });
});

app.get("/logs/files", (req, res) => {
  requirePublicRead(req);
  const roots = [logRoot(), legacyLogRoot()];
  const seen = new Set<string>();
  const files: any[] = [];
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    for (const name of fs.readdirSync(root)) {
      if (seen.has(name) || name.startsWith(".")) continue;
      const file = path.join(root, name);
      if (!fs.statSync(file).isFile()) continue;
      const st = fs.statSync(file);
      files.push({ name, size_bytes: st.size, updated_at: st.mtime.toISOString() });
      seen.add(name);
    }
  }
  files.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  res.json({ ok: true, files });
});
app.get("/logs/read", (req, res) => {
  requirePublicRead(req);
  const name = safeLogName(String(req.query.file || ""));
  if (!name) return jsonError(res, 400, "invalid file name");
  const file = logFileCandidates(name).find((p) => fs.existsSync(p));
  if (!file) return jsonError(res, 404, "file not found");
  const lines = readLogTail(file, Math.max(20, Math.min(Number(req.query.lines || 300), 2000)));
  res.json({ ok: true, file: name, line_count: lines.length, truncated: false, lines });
});
app.get("/logs/by-id", (req, res) => {
  requirePublicRead(req);
  const id = Number.parseInt(String(req.query.id || "").replace(/^tc_/, ""), 10);
  if (!Number.isFinite(id)) return jsonError(res, 400, "invalid id");
  for (const file of logFileCandidates("total.jsonl")) {
    if (!fs.existsSync(file)) continue;
    const lines = fs.readFileSync(file, "utf8").split(/\r?\n/).reverse();
    for (const line of lines) {
      try {
        const row = JSON.parse(line);
        if (Number(row.id || row.event?.id) === id) return res.json({ ok: true, id, event: row.event || row, source_file: path.basename(file), source_path: file, source_line: 0 });
      } catch {
        // ignore
      }
    }
  }
  res.status(404).json({ ok: false, error: "id not found", id });
});

app.use((error: any, _req: Request, res: Response, _next: NextFunction) => {
  if (res.headersSent) {
    console.error("[server] request failed after response started:", error);
    if (!res.writableEnded) res.end();
    return;
  }
  const status = Number(error?.status || 500);
  res.status(status).json({ ok: false, detail: String(error?.message || error), error: String(error?.message || error) });
});

function isPortAvailable(host: string, port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const tester = net.createServer();
    tester.once("error", () => resolve(false));
    tester.once("listening", () => {
      tester.close(() => resolve(true));
    });
    tester.listen(port, host);
  });
}

async function pickPort(host: string, startPort: number, retries: number): Promise<{ port: number; offset: number }> {
  const base = Math.max(1, Math.min(65535, Number(startPort) || 8000));
  const maxRetries = Math.max(0, Number(retries) || 0);
  for (let offset = 0; offset <= maxRetries; offset += 1) {
    const candidate = base + offset;
    if (candidate > 65535) break;
    if (await isPortAvailable(host, candidate)) return { port: candidate, offset };
  }
  throw new Error(`未找到可用端口：start=${base}, retries=${maxRetries}`);
}

function currentEnvTag(): string {
  if (process.platform === "win32") return "windows";
  if (process.env.WSL_DISTRO_NAME) return "wsl";
  try {
    if (fs.readFileSync("/proc/version", "utf8").toLowerCase().includes("microsoft")) return "wsl";
  } catch {
    // ignore
  }
  return "linux";
}

function trackPort(port: number): void {
  try {
    fs.writeFileSync(path.join(projectRoot(), ".tinda_ports.list"), `${currentEnvTag()}:${port}\n`, "utf8");
  } catch {
    // best effort
  }
}

function untrackPort(port: number): void {
  try {
    const file = path.join(projectRoot(), ".tinda_ports.list");
    if (!fs.existsSync(file)) return;
    const lines = fs
      .readFileSync(file, "utf8")
      .split(/\r?\n/)
      .filter((line) => line.trim() && !line.trim().endsWith(`:${port}`) && line.trim() !== String(port));
    fs.writeFileSync(file, lines.length ? `${lines.join("\n")}\n` : "", "utf8");
  } catch {
    // best effort
  }
}

export async function startServer(port = Number(process.env.PORT || 8000), host = process.env.HOST || "0.0.0.0", portRetries = Number(process.env.PORT_RETRIES || 20)) {
  const selected = await pickPort(host, port, portRetries);
  if (selected.offset > 0) console.log(`[start] 端口 ${port} 已占用，自动切换到 ${selected.port}（+${selected.offset}）`);
  trackPort(selected.port);
  const server = app.listen(selected.port, host, () => {
    const visitHost = host === "0.0.0.0" || host === "::" ? "127.0.0.1" : host;
    console.log(`TindaAgent TypeScript server listening on http://${visitHost}:${selected.port}`);
  });
  const cleanup = () => untrackPort(selected.port);
  server.on("close", cleanup);
  process.once("SIGINT", () => {
    cleanup();
    process.exit(0);
  });
  process.once("SIGTERM", () => {
    cleanup();
    process.exit(0);
  });
  return server;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const portArg = process.argv.find((arg) => arg.startsWith("--port="));
  const hostArg = process.argv.find((arg) => arg.startsWith("--host="));
  const retriesArg = process.argv.find((arg) => arg.startsWith("--port-retries="));
  const port = portArg ? Number(portArg.split("=", 2)[1]) : Number(process.env.PORT || 8000);
  const host = hostArg ? hostArg.split("=", 2)[1] : process.env.HOST || "0.0.0.0";
  const retries = retriesArg ? Number(retriesArg.split("=", 2)[1]) : Number(process.env.PORT_RETRIES || 20);
  void startServer(port, host, retries).catch((error) => {
    console.error(`[start] ${String(error?.message || error)}`);
    process.exit(1);
  });
}

export default app;
