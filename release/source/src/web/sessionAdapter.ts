import crypto from "node:crypto";
import { nowIso, textOf } from "../core/json.js";

export type StoreEntry = Record<string, any>;
export type StoreDict = Record<string, StoreEntry>;

export interface AgentMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_call_id?: string;
  tool_calls?: any[];
  reasoning_content?: string;
}

export const STREAM_DRAFT_PLACEHOLDER = "（正在生成，若页面刷新可稍后继续查看）";

const TOOL_CALLS_BLOCK_RE = /\s*<[^>]*(?:tool[_\-\u2581]?calls|toolcalls)[^>]*>.*?<\/[^>]*(?:tool[_\-\u2581]?calls|toolcalls)[^>]*>\s*/gis;
const INVOKE_BLOCK_RE = /\s*<[^>]*invoke[^>]*name\s*=\s*(['"])(.*?)\1[^>]*>.*?<\/[^>]*invoke[^>]*>\s*/gis;
const INVOKE_RE = /<[^>]*invoke[^>]*name\s*=\s*(['"])(.*?)\1[^>]*>(.*?)<\/[^>]*invoke[^>]*>/gis;
const PARAMETER_RE = /<[^>]*parameter[^>]*name\s*=\s*(['"])(.*?)\1[^>]*>(.*?)<\/[^>]*parameter[^>]*>/gis;
const TOOL_PROTOCOL_START_RE = /<[^>\n]{0,240}(?:dsml|tool[_\-\u2581]?calls|toolcalls|invoke\b)[^>]*>/i;

export function makeMessageId(): string {
  return `m_${crypto.randomBytes(8).toString("hex")}`;
}

function isRecord(value: unknown): value is Record<string, any> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function sortedNumericKeys(obj: Record<string, unknown> | null | undefined): string[] {
  if (!obj || typeof obj !== "object") return [];
  return Object.keys(obj)
    .filter((k) => /^\d+$/.test(k))
    .sort((a, b) => Number(a) - Number(b));
}

function stableHash(payload: unknown): string {
  return crypto.createHash("sha1").update(JSON.stringify(payload), "utf8").digest("hex").slice(0, 12);
}

function compactText(value: unknown, max = 24000): string {
  const text = textOf(value).replace(/[ \t]+\n/g, "\n").replace(/\n{4,}/g, "\n\n\n").trim();
  return text.length > max ? `${text.slice(0, max)}\n...[truncated]` : text;
}

function hasToolProtocolArtifacts(content: string): boolean {
  const lower = content.toLowerCase();
  if (!/(dsml|tool[_\-\u2581]?calls|toolcalls|invoke)/i.test(lower)) return false;
  TOOL_CALLS_BLOCK_RE.lastIndex = 0;
  INVOKE_BLOCK_RE.lastIndex = 0;
  return TOOL_CALLS_BLOCK_RE.test(content) || INVOKE_BLOCK_RE.test(content) || TOOL_PROTOCOL_START_RE.test(content);
}

function stripToolProtocolArtifacts(content: string): string {
  if (!hasToolProtocolArtifacts(content)) return content;
  TOOL_CALLS_BLOCK_RE.lastIndex = 0;
  INVOKE_BLOCK_RE.lastIndex = 0;
  let cleaned = content.replace(TOOL_CALLS_BLOCK_RE, "\n").replace(INVOKE_BLOCK_RE, "\n");
  const match = TOOL_PROTOCOL_START_RE.exec(cleaned);
  if (match && match.index >= 0) cleaned = cleaned.slice(0, match.index);
  return compactText(cleaned);
}

function extractToolMarkers(content: string, idPrefix: string): StoreEntry[] {
  const markers: StoreEntry[] = [];
  INVOKE_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  let idx = 0;
  while ((match = INVOKE_RE.exec(content))) {
    const name = textOf(match[2]).trim();
    if (!name) continue;
    const body = textOf(match[3]);
    const argumentsPayload: Record<string, string> = {};
    PARAMETER_RE.lastIndex = 0;
    let param: RegExpExecArray | null;
    while ((param = PARAMETER_RE.exec(body))) {
      const key = textOf(param[2]).trim();
      if (key) argumentsPayload[key] = textOf(param[3]).trim();
    }
    const callId = `hist_${stableHash({ name, argumentsPayload, idPrefix, idx })}`;
    markers.push({
      name,
      ok: false,
      stdin: textOf(argumentsPayload.cmd ?? argumentsPayload.text ?? argumentsPayload.key ?? "").slice(0, 500),
      stdout: "历史工具调用协议文本未执行",
      id: callId,
      tool_call_id: callId,
      arguments: argumentsPayload,
      result: {
        ok: false,
        tool_name: name,
        call_id: callId,
        error: "historical tool-call protocol text was persisted and was not executed",
        source: "tool_protocol_fallback"
      }
    });
    idx += 1;
  }
  return markers;
}

function storageStepsFromText(raw: unknown, idPrefix: string): StoreEntry[] {
  const text = textOf(raw);
  if (!text) return [];
  if (!hasToolProtocolArtifacts(text)) return [{ text }];

  const steps: StoreEntry[] = [];
  const matches = [...text.matchAll(TOOL_CALLS_BLOCK_RE)];
  const blocks = matches.length ? matches : [...text.matchAll(INVOKE_BLOCK_RE)];
  let pos = 0;
  blocks.forEach((match, idx) => {
    const before = stripToolProtocolArtifacts(text.slice(pos, match.index ?? 0));
    if (before) steps.push({ text: before });
    for (const marker of extractToolMarkers(match[0], `${idPrefix}:${idx}`)) steps.push({ tool_marker: normalizeToolMarker(marker) });
    pos = (match.index ?? 0) + match[0].length;
  });
  const after = stripToolProtocolArtifacts(text.slice(pos));
  if (after) steps.push({ text: after });
  return steps.length ? steps : [{ text: stripToolProtocolArtifacts(text) }];
}

function normalizeSystemPayload(raw: unknown): StoreEntry {
  const payload: StoreEntry = isRecord(raw) ? { ...raw } : { text: textOf(raw) };
  payload.kind = textOf(payload.kind || "system");
  payload.display = textOf(payload.display || "inline");
  payload.context_policy = textOf(payload.context_policy || "exclude");
  if (!("text" in payload)) payload.text = textOf(payload.summary ?? payload.content ?? "");
  return payload;
}

function normalizeToolMarker(raw: StoreEntry): StoreEntry {
  const marker = isRecord(raw.tool_marker) ? raw.tool_marker : raw;
  const out: StoreEntry = {
    name: textOf(marker.name ?? marker.tool_name ?? "unknown") || "unknown",
    ok: marker.ok === undefined ? false : Boolean(marker.ok),
    stdin: textOf(marker.stdin).slice(0, 500),
    stdout: textOf(marker.stdout).slice(0, 4000),
    id: textOf(marker.id ?? marker.call_id ?? marker.tool_call_id)
  };
  const toolCallId = textOf(marker.tool_call_id).trim();
  if (toolCallId) out.tool_call_id = toolCallId;
  const status = textOf(marker.status).trim();
  if (status) out.status = status;
  const alias = textOf(marker.mcp_alias ?? marker.alias).trim();
  if (alias) out.mcp_alias = alias;
  if (marker.arguments !== undefined && marker.arguments !== null && marker.arguments !== "") out.arguments = marker.arguments;
  if (marker.result !== undefined && marker.result !== null && marker.result !== "") out.result = marker.result;
  return out;
}

function storageStepsFromSubstep(raw: unknown, idPrefix: string): StoreEntry[] {
  if (!isRecord(raw)) return raw == null ? [] : [{ text: textOf(raw) }];
  const kind = textOf(raw.kind).trim();
  if (kind === "thinking") return [{ thinking: textOf(raw.content ?? raw.data ?? raw.thinking) }];
  if (kind === "text") return storageStepsFromText(raw.content ?? raw.data ?? raw.text, idPrefix);
  if (kind === "tool_marker") return [{ tool_marker: normalizeToolMarker(raw) }];
  if (kind === "system") return [{ system: normalizeSystemPayload(raw.content ?? raw.data ?? raw.system ?? raw.text) }];
  if ("thinking" in raw) return [{ thinking: textOf(raw.thinking) }];
  if ("text" in raw) return storageStepsFromText(raw.text, idPrefix);
  if ("user" in raw) return [{ user: textOf(raw.user) }];
  if ("file" in raw) return [{ file: isRecord(raw.file) ? { ...raw.file } : raw.file }];
  if ("tool_marker" in raw) return [{ tool_marker: normalizeToolMarker(raw) }];
  if ("system" in raw) return [{ system: normalizeSystemPayload(raw.system) }];
  return [{ ...raw }];
}

function normalizeContentSteps(content: unknown, idPrefix: string, fallbackKind: "user" | "text" = "text"): Record<string, any> {
  const out: Record<string, any> = {};
  let idx = 0;
  if (isRecord(content)) {
    const numeric = sortedNumericKeys(content);
    if (numeric.length) {
      for (const key of numeric) {
        for (const step of storageStepsFromSubstep(content[key], `${idPrefix}:${key}`)) out[String(++idx)] = step;
      }
      return idx ? out : { "1": { [fallbackKind]: "" } };
    }
    for (const step of storageStepsFromSubstep(content, `${idPrefix}:content`)) out[String(++idx)] = step;
    return idx ? out : { "1": { [fallbackKind]: "" } };
  }
  return { "1": { [fallbackKind]: textOf(content) } };
}

function fileContentSteps(extra: Record<string, unknown>): Record<string, any> {
  const content: Record<string, any> = {};
  const names = Array.isArray(extra.file_names) ? extra.file_names : [];
  const bodies = Array.isArray(extra.file_contents) ? extra.file_contents : [];
  let idx = 0;
  names.forEach((name, i) => {
    const fileName = textOf(name).trim();
    if (!fileName) return;
    content[String(++idx)] = { file: { file_name: fileName, file_content: textOf(bodies[i]) } };
  });
  return content;
}

export function buildUserMessage(text: string, extra: Record<string, unknown> = {}): StoreEntry {
  const files = fileContentSteps(extra);
  const content: Record<string, any> = { ...files };
  const next = Object.keys(content).length + 1;
  if (textOf(text).trim() || !next) content[String(next)] = { user: textOf(text) };
  return {
    role: "user",
    id: makeMessageId(),
    type: "user_message",
    display_target: "chat",
    context_policy: "include",
    content,
    created_at: nowIso(),
    ...extra
  };
}

export function buildAssistantMessage(substeps: Array<Record<string, any>>, extra: Record<string, unknown> = {}): StoreEntry {
  const content: Record<string, any> = {};
  let idx = 0;
  for (const step of substeps || []) {
    for (const storageStep of storageStepsFromSubstep(step, `build:${idx + 1}`)) content[String(++idx)] = storageStep;
  }
  if (idx === 0) content["1"] = { text: "" };
  return {
    role: "assistant",
    id: makeMessageId(),
    type: "assistant_message",
    display_target: "chat",
    context_policy: "include",
    content,
    created_at: nowIso(),
    ...extra
  };
}

export function buildSystemMessage(text: string, extra: Record<string, unknown> = {}): StoreEntry {
  const isSummary = Boolean(extra.is_summary) || textOf(extra.type) === "summary";
  return {
    role: "system",
    id: makeMessageId(),
    type: isSummary ? "summary" : "system_notice",
    display_target: "chat",
    context_policy: isSummary ? "summary" : "exclude",
    content: { text: textOf(text) },
    created_at: nowIso(),
    ...extra
  };
}

export function normalizeStoreEntry(raw: StoreEntry): StoreEntry | null {
  if (!isRecord(raw)) return null;
  const role = textOf(raw.role).trim();
  if (!["user", "assistant", "system"].includes(role)) return null;
  const id = textOf(raw.id || makeMessageId());
  const createdAt = textOf(raw.created_at || raw.ts || nowIso());
  const base: StoreEntry = {
    ...raw,
    role,
    id,
    created_at: createdAt,
    type:
      raw.type ||
      (raw.is_summary ? "summary" : role === "user" ? "user_message" : role === "assistant" ? "assistant_message" : "system_notice"),
    display_target: raw.display_target || "chat",
    context_policy: raw.context_policy || (raw.is_summary || raw.type === "summary" ? "summary" : role === "system" ? "exclude" : "include")
  };
  if (role === "system") {
    const content = isRecord(raw.content) ? { ...raw.content, text: textOf(raw.content.text ?? raw.content.summary ?? "") } : { text: textOf(raw.content) };
    return { ...base, content };
  }
  if (role === "user") {
    return { ...base, content: normalizeContentSteps(raw.content ?? "", id, "user") };
  }
  return { ...base, content: normalizeContentSteps(raw.content ?? "", id, "text") };
}

function firstText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!isRecord(content)) return "";
  for (const key of sortedNumericKeys(content)) {
    const step = content[key];
    if (!isRecord(step)) continue;
    if ("user" in step) return textOf(step.user);
    if ("text" in step) return textOf(step.text);
    if ("thinking" in step) return textOf(step.thinking);
    if ("system" in step) return isRecord(step.system) ? textOf(step.system.text) : textOf(step.system);
  }
  return textOf(content.user ?? content.text ?? "");
}

export function isTransientAssistantDraft(entry: unknown): boolean {
  if (!isRecord(entry)) return false;
  if (textOf(entry.role).trim() !== "assistant") return false;
  const type = textOf(entry.type).trim();
  if (type === "assistant_draft") return true;
  const content = entry.content;
  if (!isRecord(content)) return textOf(content).trim() === STREAM_DRAFT_PLACEHOLDER;
  const keys = sortedNumericKeys(content);
  if (keys.length !== 1) return false;
  const only = content[keys[0]];
  if (!isRecord(only)) return false;
  return "text" in only && textOf(only.text).trim() === STREAM_DRAFT_PLACEHOLDER;
}

export function stripTransientAssistantDrafts(raw: StoreDict): [StoreDict, boolean] {
  if (!isRecord(raw)) return [{}, true];
  const out: StoreDict = {};
  let changed = false;
  let next = 0;
  for (const key of sortedNumericKeys(raw)) {
    const entry = raw[key];
    if (isTransientAssistantDraft(entry)) {
      changed = true;
      continue;
    }
    next += 1;
    out[String(next)] = entry;
    if (String(next) !== key) changed = true;
  }
  return [out, changed];
}

export function normalizeStoreDict(raw: StoreDict): [StoreDict, boolean] {
  const out: StoreDict = {};
  let changed = false;
  if (!isRecord(raw)) return [{}, true];
  for (const key of sortedNumericKeys(raw)) {
    const norm = normalizeStoreEntry(raw[key]);
    if (!norm) {
      changed = true;
      continue;
    }
    out[key] = norm;
    if (JSON.stringify(norm) !== JSON.stringify(raw[key])) changed = true;
  }
  if (Object.keys(out).length !== sortedNumericKeys(raw).length) changed = true;
  return [out, changed];
}

function entryType(entry: StoreEntry): string {
  const role = textOf(entry.role);
  return textOf(entry.type || (entry.is_summary ? "summary" : role === "user" ? "user_message" : role === "assistant" ? "assistant_message" : "system_notice"));
}

function entryBase(entry: StoreEntry, seq: number) {
  const role = textOf(entry.role);
  const type = entryType(entry);
  return {
    role,
    id: textOf(entry.id),
    type,
    display_target: textOf(entry.display_target || "chat"),
    context_policy: textOf(entry.context_policy || (type === "summary" ? "summary" : role === "system" ? "exclude" : "include")),
    seq,
    source_seq: Number(entry.source_seq || seq),
    ...(entry.turn_id ? { turn_id: textOf(entry.turn_id) } : {})
  };
}

export function entryToFrontend(entry: StoreEntry, seq: number): any {
  const base = entryBase(entry, seq);
  const content = entry.content;
  if (entry.role === "user" || entry.role === "assistant") {
    if (isRecord(content)) {
      const steps = sortedNumericKeys(content).flatMap((key) => {
        const step = content[key];
        if (!isRecord(step)) return [];
        return Object.entries(step).map(([kind, data]) => ({ kind, data }));
      });
      return { ...base, content: steps.length ? steps : firstText(content) };
    }
    return { ...base, content: textOf(content) };
  }
  return { ...base, content: firstText(content) || (isRecord(content) ? textOf(content.text) : textOf(content)) };
}

export function storeDictToFrontend(storeDict: StoreDict): any[] {
  const [normalized] = normalizeStoreDict(storeDict || {});
  return sortedNumericKeys(normalized).map((key) => entryToFrontend(normalized[key], Number(key)));
}

function toolResultContent(marker: StoreEntry): string {
  if (marker.result !== undefined && marker.result !== null && marker.result !== "") {
    try {
      return JSON.stringify(marker.result);
    } catch {
      return textOf(marker.result);
    }
  }
  const payload: StoreEntry = {
    ok: Boolean(marker.ok),
    tool_name: textOf(marker.name ?? marker.tool_name ?? "unknown")
  };
  if (textOf(marker.stdin).trim()) payload.stdin = textOf(marker.stdin).trim();
  if (textOf(marker.stdout).trim()) payload.stdout = textOf(marker.stdout).trim();
  return JSON.stringify(payload);
}

function toolArgumentsText(marker: StoreEntry): string {
  const args = marker.arguments;
  if (typeof args === "string") return args.trim() ? args : "{}";
  if (args !== undefined && args !== null && args !== "" && !(Array.isArray(args) && !args.length)) {
    try {
      return JSON.stringify(args);
    } catch {
      return JSON.stringify({ value: textOf(args) });
    }
  }
  const stdin = textOf(marker.stdin).trim();
  return stdin ? JSON.stringify({ cmd: stdin }) : "{}";
}

function entryToLlmRows(entry: StoreEntry): AgentMessage[] {
  const normalized = normalizeStoreEntry(entry);
  if (!normalized) return [];
  const role = textOf(normalized.role).trim();
  const content = normalized.content;
  const eventType = entryType(normalized).toLowerCase();
  const displayTarget = textOf(normalized.display_target).trim().toLowerCase();
  const contextPolicy = textOf(normalized.context_policy).trim().toLowerCase();

  if (contextPolicy === "exclude") return [];
  if (displayTarget && !["chat", "context"].includes(displayTarget) && contextPolicy !== "include") return [];

  if (role === "user") {
    const fileBlocks: string[] = [];
    const textParts: string[] = [];
    if (isRecord(content)) {
      for (const key of sortedNumericKeys(content)) {
        const step = content[key];
        if (!isRecord(step)) continue;
        if (isRecord(step.file)) {
          const name = textOf(step.file.file_name).trim();
          if (name) fileBlocks.push(`[文件: ${name}]\n\`\`\`\n${textOf(step.file.file_content)}\n\`\`\``);
        } else if ("user" in step || "text" in step) {
          textParts.push(textOf(step.user ?? step.text));
        }
      }
      if (!fileBlocks.length && !textParts.length) textParts.push(textOf(content.user ?? content.text));
    } else if (textOf(content).trim()) {
      textParts.push(textOf(content));
    }
    const text = [...fileBlocks, ...textParts].join("\n").trim();
    return text ? [{ role: "user", content: text }] : [];
  }

  if (role === "assistant") {
    if (!isRecord(content)) {
      const text = compactText(content);
      return text ? [{ role: "assistant", content: text }] : [];
    }
    const rows: AgentMessage[] = [];
    const pendingReasoning: string[] = [];
    const pendingText: string[] = [];
    const pendingCalls: any[] = [];
    const pendingToolRows: AgentMessage[] = [];

    const flush = () => {
      if (!pendingText.length && !pendingCalls.length && !pendingReasoning.length) return;
      const assistant: AgentMessage = {
        role: "assistant",
        content: pendingText.filter((x) => x.trim()).join("\n\n"),
        reasoning_content: pendingReasoning.filter((x) => x.trim()).join("\n\n")
      };
      if (pendingCalls.length) assistant.tool_calls = pendingCalls.map((x) => ({ ...x }));
      rows.push(assistant);
      if (pendingCalls.length) rows.push(...pendingToolRows);
      pendingReasoning.length = 0;
      pendingText.length = 0;
      pendingCalls.length = 0;
      pendingToolRows.length = 0;
    };

    for (const key of sortedNumericKeys(content)) {
      const step = content[key];
      if (!isRecord(step)) continue;
      if (isRecord(step.tool_marker)) {
        const marker = normalizeToolMarker(step.tool_marker);
        const name = textOf(marker.mcp_alias ?? marker.alias ?? marker.name ?? "unknown") || "unknown";
        const callId = textOf(marker.tool_call_id ?? marker.id ?? marker.call_id) || `call_${key}`;
        pendingCalls.push({
          id: callId,
          type: "function",
          function: { name, arguments: toolArgumentsText(marker) }
        });
        pendingToolRows.push({ role: "tool", tool_call_id: callId, content: toolResultContent(marker) });
      } else if ("thinking" in step) {
        if (pendingCalls.length) flush();
        pendingReasoning.push(textOf(step.thinking));
      } else if ("text" in step) {
        if (pendingCalls.length) flush();
        const text = compactText(step.text);
        if (text) pendingText.push(text);
      } else if ("system" in step) {
        if (pendingCalls.length) flush();
        const payload = isRecord(step.system) ? step.system : { text: textOf(step.system) };
        const policy = textOf(payload.context_policy || "exclude").toLowerCase();
        if (["include", "summary"].includes(policy)) {
          const text = compactText(payload.text);
          if (text) pendingText.push(`${policy === "summary" ? "[Context Summary]" : "[System Event]"} ${text}`);
        }
      }
    }
    flush();
    return rows;
  }

  if (role === "system") {
    const text = isRecord(content) ? textOf(content.text) : textOf(content);
    const include =
      ["include", "summary"].includes(contextPolicy) ||
      ["summary", "terminal_context"].includes(eventType) ||
      Boolean(normalized.is_summary);
    if (!include || !text.trim()) return [];
    const prefix = eventType === "summary" || contextPolicy === "summary" || normalized.is_summary ? "[Context Summary]" : "[System Context]";
    return [{ role: "assistant", content: `${prefix} ${compactText(text)}` }];
  }

  return [];
}

function withEventMeta(
  entry: StoreEntry,
  sourceSeq: number,
  eventType?: string,
  displayTarget = "chat",
  contextPolicy?: string
): StoreEntry {
  const item: StoreEntry = { ...entry, source_seq: sourceSeq };
  if (eventType) item.type = eventType;
  if (displayTarget) item.display_target = displayTarget;
  if (contextPolicy) item.context_policy = contextPolicy;
  return item;
}

function terminalEntriesToContextEntries(entries: any[], startSeq: number): Array<[number, StoreEntry]> {
  const out: Array<[number, StoreEntry]> = [];
  let batch: string[] = [];
  let batchTs = "";
  let batchIdx = 0;
  const flush = () => {
    const text = batch.map((x) => x.trimEnd()).filter(Boolean).join("\n").trim();
    if (!text) {
      batch = [];
      return;
    }
    batchIdx += 1;
    out.push([
      startSeq + batchIdx,
      {
        role: "system",
        id: `terminal-${batchIdx}`,
        created_at: batchTs,
        type: "terminal_context",
        display_target: "context",
        context_policy: "include",
        content: { text: `[Terminal Context]\n${compactText(text, 12000)}` }
      }
    ]);
    batch = [];
    batchTs = "";
  };

  for (const row of entries || []) {
    if (!isRecord(row)) continue;
    const kind = textOf(row.kind ?? row.terminal_kind ?? "out").trim();
    const text = textOf(row.content ?? row.text).trim();
    if (!text) continue;
    if (kind === "sep") {
      flush();
      continue;
    }
    if (!batchTs) batchTs = textOf(row.ts ?? row.created_at);
    batch.push(`${kind === "cmd" ? "$ " : ""}${text}`);
  }
  flush();
  return out;
}

export function storeDictToAgentMessages(storeDict: StoreDict, meta: Record<string, any> = {}, terminalEntries: any[] = []): AgentMessage[] {
  const [normalized] = normalizeStoreDict(storeDict || {});
  let entries: Array<[number, StoreEntry]> = sortedNumericKeys(normalized).map((key) => [Number(key), normalized[key]]);
  const resetAnchor = textOf(meta.reset_anchor_msg_id).trim();
  const latestSummaryId = textOf(meta.latest_summary_message_id).trim();
  const summaryAnchorId = textOf(meta.summary_anchor_msg_id).trim();

  if (resetAnchor) {
    const resetAfter = entries.find(([_, entry]) => textOf(entry.id) === resetAnchor)?.[0] ?? -1;
    if (resetAfter >= 0) entries = entries.filter(([seq]) => seq > resetAfter);
  }

  const contextEntries = [...entries, ...terminalEntriesToContextEntries(terminalEntries, entries.length + 1)].sort((a, b) => a[0] - b[0]);
  if (latestSummaryId && summaryAnchorId) {
    let summaryRows: AgentMessage[] = [];
    let anchorIdx = -1;
    contextEntries.forEach(([seq, entry], idx) => {
      if (textOf(entry.id) === latestSummaryId) {
        summaryRows = entryToLlmRows(withEventMeta(entry, seq, "summary", "context", "summary"));
      }
      if (textOf(entry.id) === summaryAnchorId) anchorIdx = idx;
    });
    if (summaryRows.length && anchorIdx >= 0) {
      const out = [...summaryRows];
      for (const [_, entry] of contextEntries.slice(anchorIdx)) {
        if (textOf(entry.id) === latestSummaryId) continue;
        out.push(...entryToLlmRows(entry));
      }
      return out;
    }
  }

  return contextEntries.flatMap(([_, entry]) => entryToLlmRows(entry));
}

export function effectiveStoreDict(storeDict: StoreDict, meta: Record<string, any> = {}): StoreDict {
  const [normalized] = normalizeStoreDict(storeDict || {});
  let entries: Array<[number, StoreEntry]> = sortedNumericKeys(normalized).map((key) => [Number(key), normalized[key]]);
  const resetAnchor = textOf(meta.reset_anchor_msg_id).trim();
  const latestSummaryId = textOf(meta.latest_summary_message_id).trim();
  const summaryAnchorId = textOf(meta.summary_anchor_msg_id).trim();

  if (resetAnchor) {
    const resetAfter = entries.find(([_, entry]) => textOf(entry.id) === resetAnchor)?.[0] ?? -1;
    if (resetAfter >= 0) entries = entries.filter(([seq]) => seq > resetAfter);
  }

  if (latestSummaryId && summaryAnchorId) {
    let summaryEntry: StoreEntry | null = null;
    let anchorSeq = -1;
    for (const [seq, entry] of entries) {
      const msgId = textOf(entry.id);
      if (msgId === latestSummaryId) summaryEntry = entry;
      if (msgId === summaryAnchorId) anchorSeq = seq;
    }
    if (summaryEntry && anchorSeq >= 0) {
      const visible: Array<[number, StoreEntry]> = [];
      if (textOf(summaryEntry.display_target || "chat") === "chat") {
        visible.push([anchorSeq, withEventMeta(summaryEntry, anchorSeq, "summary", "chat", "summary")]);
      }
      const seen = new Set<string>();
      for (const [seq, entry] of entries) {
        const msgId = textOf(entry.id);
        if (msgId === latestSummaryId || seq < anchorSeq) continue;
        if (msgId && seen.has(msgId)) continue;
        if (msgId) seen.add(msgId);
        visible.push([seq, withEventMeta(entry, seq, entryType(entry))]);
      }
      return Object.fromEntries(visible.map(([_, entry], idx) => [String(idx + 1), entry]));
    }
  }

  return Object.fromEntries(entries.map(([seq, entry], idx) => [String(idx + 1), withEventMeta(entry, seq, entryType(entry))]));
}

export function filterRawChatEntries(storeDict: StoreDict): StoreEntry[] {
  const [normalized] = normalizeStoreDict(storeDict || {});
  const raw: StoreEntry[] = [];
  for (const key of sortedNumericKeys(normalized)) {
    const entry = normalized[key];
    if (!isRecord(entry)) continue;
    const role = textOf(entry.role).trim();
    if (!["user", "assistant"].includes(role)) continue;
    if (textOf(entry.context_policy).trim() === "exclude" || textOf(entry.type).trim() === "tool_marker") continue;
    const content = entry.content;
    if (role === "user") {
      const text = firstText(content);
      if (text.trim()) raw.push({ role, content: text, id: textOf(entry.id), seq: Number(key) });
    } else {
      const parts: string[] = [];
      if (isRecord(content)) {
        for (const subKey of sortedNumericKeys(content)) {
          const step = content[subKey];
          if (isRecord(step) && "text" in step) parts.push(textOf(step.text));
        }
      } else {
        parts.push(textOf(content));
      }
      const text = parts.filter((x) => x.trim()).join("\n\n");
      if (text.trim()) raw.push({ role, content: text, id: textOf(entry.id), seq: Number(key), source_seq: entry.source_seq });
    }
  }
  return raw;
}

export function terminalEntriesToFrontend(entries: any[]): any[] {
  return (entries || []).filter(Boolean).map((raw, idx) => ({
    role: "assistant",
    id: textOf(raw.id || `terminal-${idx + 1}`),
    type: "terminal",
    display_target: "terminal",
    context_policy: "include",
    seq: Number(raw.seq || idx + 1),
    source_seq: Number(raw.source_seq || 0),
    source: textOf(raw.source),
    job_id: textOf(raw.job_id),
    kind: textOf(raw.kind || raw.terminal_kind || "out"),
    class: textOf(raw.class || raw.terminal_class).toLowerCase(),
    content: textOf(raw.content ?? raw.text),
    ts: textOf(raw.ts || raw.created_at)
  }));
}
