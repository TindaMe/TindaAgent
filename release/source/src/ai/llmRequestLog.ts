import { appDb } from "../core/sqlite.js";
import { nowIso } from "../core/json.js";

function ensureSchema(): void {
  appDb().exec(`
    CREATE TABLE IF NOT EXISTS llm_requests (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      model TEXT NOT NULL DEFAULT '',
      message_count INTEGER NOT NULL DEFAULT 0,
      tool_count INTEGER NOT NULL DEFAULT 0,
      content_chars INTEGER NOT NULL DEFAULT 0,
      payload_json TEXT NOT NULL DEFAULT '',
      summary_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_llm_requests_ts ON llm_requests(ts DESC);
  `);
}

function summarizePayload(payload: any): Record<string, any> {
  const messages = Array.isArray(payload?.messages) ? payload.messages : [];
  const tools = Array.isArray(payload?.tools) ? payload.tools : [];
  const contentChars = messages.reduce((sum: number, msg: any) => {
    const content = typeof msg?.content === "string" ? msg.content : JSON.stringify(msg?.content ?? "");
    return sum + content.length;
  }, 0);
  return {
    model: String(payload?.model || ""),
    message_count: messages.length,
    tool_count: tools.length,
    content_chars: contentChars,
    stream: Boolean(payload?.stream),
    temperature: payload?.temperature ?? null,
    saved_full_payload: process.env.TINDA_LLM_REQUEST_FULL === "1"
  };
}

export function logLlmRequest(payload: unknown): void {
  try {
    ensureSchema();
    const obj = payload && typeof payload === "object" ? (payload as any) : {};
    const summary = summarizePayload(obj);
    appDb()
      .prepare(
        `INSERT INTO llm_requests (ts, model, message_count, tool_count, content_chars, payload_json, summary_json)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
      .run(
        nowIso(),
        summary.model,
        summary.message_count,
        summary.tool_count,
        summary.content_chars,
        process.env.TINDA_LLM_REQUEST_FULL === "1" ? JSON.stringify(obj) : "",
        JSON.stringify(summary)
      );
    const keep = Math.max(20, Math.min(Number(process.env.TINDA_LLM_REQUEST_KEEP || 200), 5000));
    appDb().prepare("DELETE FROM llm_requests WHERE id NOT IN (SELECT id FROM llm_requests ORDER BY id DESC LIMIT ?)").run(keep);
  } catch {
    // request logging must never block LLM calls
  }
}

export function latestLlmRequestRecord() {
  try {
    ensureSchema();
    const row = appDb().prepare("SELECT * FROM llm_requests ORDER BY id DESC LIMIT 1").get() as any;
    if (!row) return { ok: true, latest: null, request: null, summary: {}, source: "sqlite" };
    const summary = JSON.parse(String(row.summary_json || "{}"));
    const payload = row.payload_json ? JSON.parse(String(row.payload_json)) : summary;
    return {
      ok: true,
      latest: { id: Number(row.id), ts: row.ts, summary, request: payload },
      request: payload,
      summary,
      source: "sqlite",
      full_payload_saved: Boolean(row.payload_json)
    };
  } catch (error: any) {
    return { ok: false, error: String(error?.message || error), latest: null, request: null, summary: {}, source: "sqlite" };
  }
}
