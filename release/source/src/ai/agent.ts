import { projectRoot } from "../core/paths.js";
import { writeJson } from "../core/json.js";
import { llmEnvConfig, loadRuntimeEnv, maskSecret, type LlmEnvConfig } from "../core/env.js";
import { listToolSchemas, runAgentTool, toolTraceStep } from "../tools/toolRegistry.js";

loadRuntimeEnv();

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
  pending_confirmation?: boolean;
  pending?: any[];
}

interface StreamToolCall {
  id: string;
  type: string;
  function: {
    name: string;
    arguments: string;
  };
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
  private clientPromise: Promise<any> | null = null;
  model: string;
  baseURL: string;
  provider: LlmEnvConfig["provider"];
  apiKeySource: LlmEnvConfig["apiKeySource"];
  baseURLSource: string;
  apiKeyConfigured: boolean;

  constructor() {
    const cfg = llmEnvConfig();
    this.provider = cfg.provider;
    this.baseURL = cfg.baseURL;
    this.baseURLSource = cfg.baseURLSource;
    this.model = cfg.model;
    this.apiKeySource = cfg.apiKeySource;
    this.apiKeyConfigured = !!cfg.apiKey;
  }

  private client(): Promise<any> {
    if (!this.clientPromise) {
      this.clientPromise = import("openai").then(({ default: OpenAI }) =>
        new OpenAI({
          apiKey: llmEnvConfig().apiKey || "missing",
          baseURL: this.baseURL
        })
      );
    }
    return this.clientPromise;
  }

  private llmError(error: any): Error {
    if (!this.apiKeyConfigured) {
      return new Error("LLM API key is not configured. Set DEEPSEEK_API_KEY or OPENAI_API_KEY in TindaAgent/.env, .env, or the process environment.");
    }
    const raw = String(error?.message || error || "unknown LLM error");
    const rejected = Number(error?.status || error?.code) === 401 || /authentication|api key|unauthorized|invalid/i.test(raw);
    if (rejected) {
      return new Error(
        `LLM API key from ${this.apiKeySource} was rejected by ${this.baseURL}. Check that ${this.apiKeySource} matches ${this.baseURLSource} (${this.baseURL}). Provider said: ${raw}`
      );
    }
    return error instanceof Error ? error : new Error(raw);
  }

  private logRequest(payload: unknown): void {
    void import("./llmRequestLog.js").then(({ logLlmRequest }) => logLlmRequest(payload)).catch(() => {});
  }

  private parseToolArguments(raw: string): Record<string, any> {
    try {
      return JSON.parse(String(raw || "{}"));
    } catch {
      return {};
    }
  }

  private mergeToolCallDelta(calls: StreamToolCall[], incoming: any): void {
    const index = Number.isFinite(Number(incoming?.index)) ? Number(incoming.index) : calls.length;
    const existing =
      calls[index] ||
      {
        id: "",
        type: "function",
        function: { name: "", arguments: "" }
      };
    const fn = incoming?.function || {};
    if (incoming?.id) existing.id = String(incoming.id);
    if (incoming?.type) existing.type = String(incoming.type);
    if (fn.name) existing.function.name += String(fn.name);
    if (fn.arguments) existing.function.arguments += String(fn.arguments);
    calls[index] = existing;
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
      let resp: any;
      try {
        resp = await (await this.client()).chat.completions.create(payload);
      } catch (error: any) {
        throw this.llmError(error);
      }
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
        const args = this.parseToolArguments(String(fn.arguments || "{}"));
        const callId = `tc_${Math.random().toString(16).slice(2, 10)}`;
        const result = await runAgentTool(name, args, userPerm, sessionId, callId);
        trace.push(toolTraceStep(name, callId, args, result, String(call.id || "")));
        if (result?.pending_confirmation) {
          return {
            reply: "",
            reasoning_content: assistant.reasoning_content || "",
            tool_trace: trace,
            tool_steps: trace.length,
            history_delta: delta,
            pending_confirmation: true,
            pending: [result]
          };
        }
        const raw = JSON.stringify(result);
        const toolMessage: ChatMessage = { role: "tool", tool_call_id: String(call.id || callId), content: raw };
        history.push(toolMessage);
        delta.push(toolMessage);
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
        tools: tools.length ? tools : undefined,
        stream: true
      };
      this.logRequest(payload);
      let stream: AsyncIterable<any>;
      try {
        stream = (await (await this.client()).chat.completions.create(payload)) as unknown as AsyncIterable<any>;
      } catch (error: any) {
        throw this.llmError(error);
      }

      let content = "";
      let reasoning = "";
      const toolCalls: StreamToolCall[] = [];
      for await (const chunk of stream) {
        const choice = chunk?.choices?.[0] || {};
        const item = choice.delta || {};
        const reasoningDelta = String(item.reasoning_content || item.reasoning || "");
        const contentDelta = String(item.content || "");
        if (reasoningDelta) {
          reasoning += reasoningDelta;
          yield { type: "reasoning_delta", content: reasoningDelta };
        }
        if (contentDelta) {
          content += contentDelta;
          yield { type: "delta", content: contentDelta };
        }
        if (Array.isArray(item.tool_calls)) {
          for (const call of item.tool_calls) this.mergeToolCallDelta(toolCalls, call);
        }
      }

      const assistant: ChatMessage = {
        role: "assistant",
        content,
        reasoning_content: reasoning
      };
      const completeToolCalls = toolCalls.filter((call) => call.function.name || call.id);
      if (completeToolCalls.length) assistant.tool_calls = completeToolCalls;
      history.push(assistant);
      delta.push(assistant);

      if (!completeToolCalls.length) {
        const result: ChatResult = {
          reply: content,
          reasoning_content: reasoning,
          tool_trace: trace,
          tool_steps: trace.length,
          history_delta: delta
        };
        yield { type: "done", ...result };
        return result;
      }

      yield {
        type: "tool_call_start",
        calls: completeToolCalls.map((call) => ({
          tool_call_id: call.id,
          name: call.function.name,
          arguments: this.parseToolArguments(call.function.arguments)
        }))
      };

      for (const call of completeToolCalls) {
        const fn = call.function || { name: "", arguments: "{}" };
        const args = this.parseToolArguments(fn.arguments);
        const callId = `tc_${Math.random().toString(16).slice(2, 10)}`;
        const result = await runAgentTool(String(fn.name || ""), args, userPerm, sessionId, callId);
        const stepTrace = toolTraceStep(String(fn.name || ""), callId, args, result, String(call.id || ""));
        trace.push(stepTrace);
        yield { type: "tool_step", trace: [stepTrace] };
        if (result?.pending_confirmation) {
          const pendingResult: ChatResult = {
            reply: "",
            reasoning_content: reasoning,
            tool_trace: trace,
            tool_steps: trace.length,
            history_delta: delta,
            pending_confirmation: true,
            pending: [result]
          };
          yield { type: "done", ...pendingResult };
          return pendingResult;
        }
        const raw = JSON.stringify(result);
        const toolMessage: ChatMessage = { role: "tool", tool_call_id: String(call.id || callId), content: raw };
        history.push(toolMessage);
        delta.push(toolMessage);
      }
    }
    const result: ChatResult = {
      reply: "工具调用轮次过多，已停止继续执行。",
      tool_trace: trace,
      tool_steps: trace.length,
      history_delta: delta
    };
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
      provider: this.provider,
      current_model: this.model,
      model: this.model,
      base_url: this.baseURL,
      api_key_source: this.apiKeySource,
      api_key_configured: this.apiKeyConfigured,
      provider_kind: this.provider,
      models,
      providers: {
        [this.provider]: {
          key: this.provider,
          label: this.provider === "deepseek" ? "DeepSeek" : "OpenAI-compatible",
          current_model: this.model,
          base_url: this.baseURL,
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

  async probe(model: string, messages: ChatMessage[], timeoutMs = 30000): Promise<{ content: string; reasoning_content: string; latency_ms: number }> {
    const started = Date.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), Math.max(1000, timeoutMs));
    try {
      const payload: any = {
        model: String(model || this.model),
        messages,
        temperature: 0
      };
      this.logRequest(payload);
      const resp: any = await (await this.client()).chat.completions.create(payload, { signal: controller.signal });
      const msg = resp.choices?.[0]?.message || {};
      return {
        content: String(msg.content || ""),
        reasoning_content: String(msg.reasoning_content || msg.reasoning || ""),
        latency_ms: Date.now() - started
      };
    } catch (error: any) {
      throw this.llmError(error);
    } finally {
      clearTimeout(timeout);
    }
  }

  async fetchBalance() {
    const cfg = llmEnvConfig();
    const key = cfg.apiKeySource === "DEEPSEEK_API_KEY" ? cfg.apiKey : "";
    if (!key) return { ok: false, error: "DEEPSEEK_API_KEY not configured" };
    try {
      const root = cfg.baseURL.replace(/\/+$/, "");
      const res = await fetch(`${root}/user/balance`, { headers: { Authorization: `Bearer ${key}` } });
      const data = await res.json().catch(() => ({}));
      return { ok: res.ok, status: res.status, api_key: maskSecret(key), ...data };
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

  async resumeWithToolResult(toolCallId: string, resultPayload: Record<string, any>): Promise<ChatResult> {
    const callId = String(toolCallId || "").trim();
    if (!callId) throw new Error("tool_call_id required");
    this.history.push({ role: "tool", tool_call_id: callId, content: JSON.stringify(resultPayload) });
    const result = await this.client.chatWithTools(this.history, this.userPerm, this.sessionId);
    this.history.push(...result.history_delta);
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
  return import("./llmRequestLog.js")
    .then(({ latestLlmRequestRecord }) => latestLlmRequestRecord())
    .catch((error: any) => ({ ok: false, error: String(error?.message || error), latest: null, request: null, summary: {}, source: "sqlite" }));
}
