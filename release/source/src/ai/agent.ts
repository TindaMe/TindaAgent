import fs from "node:fs";
import path from "node:path";
import OpenAI from "openai";
import { config as dotenvConfig } from "dotenv";
import { logRoot, projectRoot } from "../core/paths.js";
import { nowIso, writeJson } from "../core/json.js";
import { listToolSchemas, runAgentTool, toolTraceStep } from "../tools/toolRegistry.js";

dotenvConfig({ path: path.join(projectRoot(), ".env") });

export interface ChatMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_call_id?: string;
  tool_calls?: any[];
  reasoning_content?: string;
}

export interface ChatResult {
  reply: string;
  reasoning_content?: string;
  tool_trace: any[];
  tool_steps: number;
  history_delta: ChatMessage[];
}

function systemPrompt(modelName = ""): string {
  void modelName;
  return [
    "You are TindaAgent, developed by Tinda.",
    "This stable policy prompt must remain at the start of every LLM request.",
    "",
    "Strict rules:",
    "1. When introducing yourself, only say: \"I am TindaAgent, developed by Tinda.\"",
    "2. If asked about the underlying model, always reply: \"Underlying technical details are confidential.\"",
    "3. Be concise and accurate. Always respond in the same language as the user.",
    "4. Use available native tool_calls when they are useful. Never fabricate tool results.",
    "5. If the task needs external current information and web search is enabled, call search_web.",
    "6. For file edits, read the file first, then use exact replacement edits."
  ].join("\n");
}

export class LlmClient {
  client: OpenAI;
  model: string;
  baseURL: string;

  constructor() {
    this.baseURL = process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com";
    this.model = process.env.DEEPSEEK_MODEL || "deepseek-v4-flash";
    this.client = new OpenAI({
      apiKey: process.env.DEEPSEEK_API_KEY || process.env.OPENAI_API_KEY || "missing",
      baseURL: this.baseURL
    });
  }

  private logRequest(payload: unknown): void {
    try {
      const file = process.env.TINDA_LLM_REQUEST_LOG || path.join(logRoot(), "llm_request.jsonl");
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.appendFileSync(file, `${JSON.stringify({ ts: nowIso(), request: payload })}\n`, "utf8");
    } catch {
      // best effort
    }
  }

  async chatWithTools(messages: ChatMessage[], userPerm: number, sessionId = "", temperature?: number | null): Promise<ChatResult> {
    const history = messages.map((m) => ({ ...m }));
    const trace: any[] = [];
    const delta: ChatMessage[] = [];
    const tools = listToolSchemas(userPerm);
    const maxSteps = 20;
    for (let step = 0; step < maxSteps; step += 1) {
      const payload: any = {
        model: this.model,
        messages: history,
        temperature: temperature ?? undefined,
        tools: tools.length ? tools : undefined
      };
      this.logRequest(payload);
      const resp: any = await this.client.chat.completions.create(payload);
      const msg = resp.choices?.[0]?.message || {};
      const assistant: ChatMessage = {
        role: "assistant",
        content: msg.content ?? "",
        reasoning_content: msg.reasoning_content || msg.reasoning || ""
      };
      if (Array.isArray(msg.tool_calls) && msg.tool_calls.length) assistant.tool_calls = msg.tool_calls;
      history.push(assistant);
      delta.push(assistant);
      if (!assistant.tool_calls?.length) {
        return {
          reply: String(assistant.content || ""),
          reasoning_content: assistant.reasoning_content || "",
          tool_trace: trace,
          tool_steps: trace.length,
          history_delta: delta
        };
      }
      for (const call of assistant.tool_calls) {
        const fn = call?.function || {};
        const name = String(fn.name || "");
        let args: Record<string, any> = {};
        try {
          args = JSON.parse(String(fn.arguments || "{}"));
        } catch {
          args = {};
        }
        const callId = `tc_${Math.random().toString(16).slice(2, 10)}`;
        const result = await runAgentTool(name, args, userPerm, sessionId, callId);
        const raw = JSON.stringify(result);
        const toolMessage: ChatMessage = { role: "tool", tool_call_id: String(call.id || callId), content: raw };
        history.push(toolMessage);
        delta.push(toolMessage);
        trace.push(toolTraceStep(name, callId, args, result, String(call.id || "")));
      }
    }
    return {
      reply: "工具调用轮次过多，已停止继续执行。",
      tool_trace: trace,
      tool_steps: trace.length,
      history_delta: delta
    };
  }

  async *streamChat(
    messages: ChatMessage[],
    userPerm: number,
    sessionId = "",
    temperature?: number | null
  ): AsyncGenerator<Record<string, any>, ChatResult, unknown> {
    const result = await this.chatWithTools(messages, userPerm, sessionId, temperature);
    const text = result.reply || "";
    if (result.reasoning_content) yield { type: "reasoning_delta", content: result.reasoning_content };
    if (result.tool_trace.length) yield { type: "tool_step", trace: result.tool_trace };
    const chunkSize = 24;
    for (let i = 0; i < text.length; i += chunkSize) {
      yield { type: "delta", content: text.slice(i, i + chunkSize) };
    }
    yield { type: "done", ...result };
    return result;
  }

  modelPayload() {
    const models = [
      { id: this.model, label: this.model },
      { id: "deepseek-v4-flash", label: "deepseek-v4-flash" },
      { id: "deepseek-chat", label: "deepseek-chat" },
      { id: "deepseek-reasoner", label: "deepseek-reasoner" }
    ];
    return {
      ok: true,
      provider: "deepseek",
      current_model: this.model,
      model: this.model,
      models,
      providers: {
        deepseek: {
          key: "deepseek",
          label: "DeepSeek",
          current_model: this.model,
          models
        }
      }
    };
  }

  switchModel(model: string): void {
    const clean = String(model || "").trim();
    if (!clean) throw new Error("model required");
    this.model = clean;
  }

  async fetchBalance() {
    const key = process.env.DEEPSEEK_API_KEY || "";
    if (!key) return { ok: false, error: "DEEPSEEK_API_KEY not configured" };
    try {
      const root = (process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com").replace(/\/+$/, "");
      const res = await fetch(`${root}/user/balance`, { headers: { Authorization: `Bearer ${key}` } });
      const data = await res.json().catch(() => ({}));
      return { ok: res.ok, status: res.status, ...data };
    } catch (error: any) {
      return { ok: false, error: String(error?.message || error) };
    }
  }
}

export class Agent {
  history: ChatMessage[];
  maxContextTokens = 16000;
  sessionId = "";

  constructor(public userName: string, public userPerm: number, public client: LlmClient, modelName = "") {
    this.history = [{ role: "system", content: systemPrompt(modelName) }];
  }

  replaceConversation(messages: ChatMessage[]): void {
    const base = this.history[0];
    const clean = (messages || []).filter((m) => ["user", "assistant", "tool", "system"].includes(m.role)).map((m) => ({ ...m }));
    this.history = [base, ...clean.filter((m) => m.role !== "system")];
  }

  async chat(message: string, transientSystemContext = "", tailMessages: ChatMessage[] = []): Promise<ChatResult> {
    const requestHistory = [...this.history];
    if (transientSystemContext.trim()) requestHistory.splice(Math.max(1, requestHistory.length - 1), 0, { role: "system", content: transientSystemContext });
    requestHistory.push(...tailMessages);
    requestHistory.push({ role: "user", content: message });
    const result = await this.client.chatWithTools(requestHistory, this.userPerm, this.sessionId);
    this.history.push({ role: "user", content: message }, ...result.history_delta);
    return result;
  }

  async *stream(message: string, transientSystemContext = "", tailMessages: ChatMessage[] = []) {
    const requestHistory = [...this.history];
    if (transientSystemContext.trim()) requestHistory.push({ role: "system", content: transientSystemContext });
    requestHistory.push(...tailMessages);
    requestHistory.push({ role: "user", content: message });
    let final: ChatResult | null = null;
    for await (const event of this.client.streamChat(requestHistory, this.userPerm, this.sessionId)) {
      if (event.type === "done") {
        final = event as ChatResult;
      }
      yield event;
    }
    if (final) this.history.push({ role: "user", content: message }, ...final.history_delta);
  }
}

export function latestLlmRequest() {
  const file = process.env.TINDA_LLM_REQUEST_LOG || path.join(logRoot(), "llm_request.jsonl");
  try {
    if (!fs.existsSync(file)) return { ok: true, latest: null, summary: {} };
    const lines = fs.readFileSync(file, "utf8").trim().split(/\r?\n/);
    const latest = JSON.parse(lines[lines.length - 1] || "{}");
    const request = latest.request || {};
    return {
      ok: true,
      latest,
      request,
      summary: {
        model: request.model || "",
        message_count: Array.isArray(request.messages) ? request.messages.length : 0,
        tool_count: Array.isArray(request.tools) ? request.tools.length : 0
      }
    };
  } catch (error: any) {
    return { ok: false, error: String(error?.message || error) };
  }
}
