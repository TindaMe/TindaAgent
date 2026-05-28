import crypto from "node:crypto";
import { nowIso, textOf } from "../core/json.js";

export type StoreEntry = Record<string, any>;
export type StoreDict = Record<string, StoreEntry>;

export function makeMessageId(): string {
  return `m_${crypto.randomBytes(8).toString("hex")}`;
}

function sortedNumericKeys(obj: Record<string, unknown>): string[] {
  return Object.keys(obj)
    .filter((k) => /^\d+$/.test(k))
    .sort((a, b) => Number(a) - Number(b));
}

function normalizeContentSteps(content: unknown): Record<string, any> {
  if (content && typeof content === "object" && !Array.isArray(content)) {
    const input = content as Record<string, any>;
    if (sortedNumericKeys(input).length > 0) return input;
    if ("text" in input || "user" in input) return { "1": { text: textOf(input.text ?? input.user) } };
  }
  return { "1": { text: textOf(content) } };
}

export function buildUserMessage(text: string, extra: Record<string, unknown> = {}): StoreEntry {
  return {
    role: "user",
    id: makeMessageId(),
    type: "user_message",
    display_target: "chat",
    context_policy: "include",
    content: { "1": { user: textOf(text) } },
    created_at: nowIso(),
    ...extra
  };
}

export function buildAssistantMessage(substeps: Array<Record<string, any>>, extra: Record<string, unknown> = {}): StoreEntry {
  const content: Record<string, any> = {};
  let idx = 1;
  for (const step of substeps || []) {
    const kind = String(step?.kind || "text");
    if (kind === "thinking") content[String(idx++)] = { thinking: textOf(step.content ?? step.data) };
    else if (kind === "tool_marker") {
      const marker = { ...step };
      delete marker.kind;
      content[String(idx++)] = { tool_marker: marker };
    } else if (kind === "system") content[String(idx++)] = { system: step.content ?? step.data ?? {} };
    else content[String(idx++)] = { text: textOf(step?.content ?? step?.data ?? step) };
  }
  if (idx === 1) content["1"] = { text: "" };
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
  return {
    role: "system",
    id: makeMessageId(),
    type: "system_notice",
    display_target: "chat",
    context_policy: "exclude",
    content: { text: textOf(text) },
    created_at: nowIso(),
    ...extra
  };
}

export function normalizeStoreEntry(raw: StoreEntry): StoreEntry | null {
  if (!raw || typeof raw !== "object") return null;
  const role = String(raw.role || "").trim();
  if (!["user", "assistant", "system"].includes(role)) return null;
  const id = String(raw.id || makeMessageId());
  const createdAt = String(raw.created_at || raw.ts || nowIso());
  if (role === "system") {
    const text = typeof raw.content === "object" && raw.content ? textOf((raw.content as any).text) : textOf(raw.content);
    return buildSystemMessage(text, {
      ...raw,
      id,
      created_at: createdAt,
      type: raw.type || "system_notice",
      context_policy: raw.context_policy || "exclude"
    });
  }
  if (role === "user") {
    const text =
      typeof raw.content === "object" && raw.content
        ? textOf((raw.content as any).user ?? (raw.content as any).text ?? firstText(raw.content))
        : textOf(raw.content);
    return {
      ...buildUserMessage(text, raw),
      id,
      created_at: createdAt,
      content: normalizeContentSteps(raw.content ?? text)
    };
  }
  return {
    ...buildAssistantMessage([{ kind: "text", content: firstText(raw.content) || textOf(raw.content) }], raw),
    id,
    created_at: createdAt,
    content: normalizeContentSteps(raw.content)
  };
}

function firstText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!content || typeof content !== "object") return "";
  const obj = content as Record<string, any>;
  for (const key of sortedNumericKeys(obj)) {
    const step = obj[key];
    if (!step || typeof step !== "object") continue;
    if ("user" in step) return textOf(step.user);
    if ("text" in step) return textOf(step.text);
    if ("thinking" in step) return textOf(step.thinking);
    if ("system" in step) return textOf(step.system);
  }
  return textOf(obj.user ?? obj.text ?? "");
}

export function normalizeStoreDict(raw: StoreDict): [StoreDict, boolean] {
  const out: StoreDict = {};
  let changed = false;
  let next = 1;
  for (const key of sortedNumericKeys(raw || {})) {
    const norm = normalizeStoreEntry(raw[key]);
    if (!norm) {
      changed = true;
      continue;
    }
    out[String(next)] = norm;
    if (String(next) !== key || JSON.stringify(norm) !== JSON.stringify(raw[key])) changed = true;
    next += 1;
  }
  return [out, changed];
}

export function storeDictToFrontend(storeDict: StoreDict): any[] {
  const [normalized] = normalizeStoreDict(storeDict || {});
  return sortedNumericKeys(normalized).map((key) => entryToFrontend(normalized[key], Number(key)));
}

function entryBase(entry: StoreEntry, seq: number) {
  const role = String(entry.role || "");
  const type =
    String(entry.type || "") ||
    (role === "user" ? "user_message" : role === "assistant" ? "assistant_message" : "system_notice");
  return {
    role,
    id: String(entry.id || ""),
    type,
    display_target: String(entry.display_target || "chat"),
    context_policy: String(entry.context_policy || (role === "system" ? "exclude" : "include")),
    seq,
    source_seq: Number(entry.source_seq || seq),
    ...(entry.turn_id ? { turn_id: String(entry.turn_id) } : {})
  };
}

export function entryToFrontend(entry: StoreEntry, seq: number): any {
  const base = entryBase(entry, seq);
  const content = entry.content;
  if (entry.role === "user") {
    if (content && typeof content === "object") {
      const steps = sortedNumericKeys(content).flatMap((key) => {
        const step = content[key];
        if (!step || typeof step !== "object") return [];
        return Object.entries(step).map(([kind, data]) => ({ kind, data }));
      });
      return { ...base, content: steps.length ? steps : firstText(content) };
    }
    return { ...base, content: textOf(content) };
  }
  if (entry.role === "assistant") {
    if (content && typeof content === "object") {
      const steps = sortedNumericKeys(content).flatMap((key) => {
        const step = content[key];
        if (!step || typeof step !== "object") return [];
        return Object.entries(step).map(([kind, data]) => ({ kind, data }));
      });
      return { ...base, content: steps };
    }
    return { ...base, content: textOf(content) };
  }
  return { ...base, content: firstText(content) || textOf(content) };
}

export function storeDictToAgentMessages(storeDict: StoreDict): Array<{ role: string; content: string }> {
  const [normalized] = normalizeStoreDict(storeDict || {});
  const out: Array<{ role: string; content: string }> = [];
  for (const key of sortedNumericKeys(normalized)) {
    const entry = normalized[key];
    if (String(entry.context_policy || "") === "exclude") continue;
    const role = String(entry.role || "");
    if (!["user", "assistant", "system"].includes(role)) continue;
    const content = firstText(entry.content);
    if (content.trim()) out.push({ role, content });
  }
  return out;
}

export function effectiveStoreDict(storeDict: StoreDict, meta: Record<string, any> = {}): StoreDict {
  const [normalized] = normalizeStoreDict(storeDict || {});
  const resetAnchor = String(meta.reset_anchor_msg_id || "").trim();
  if (!resetAnchor) return normalized;
  const entries = sortedNumericKeys(normalized).map((key) => [Number(key), normalized[key]] as const);
  const anchor = entries.find(([_, entry]) => String(entry.id || "") === resetAnchor)?.[0] || 0;
  if (!anchor) return normalized;
  const out: StoreDict = {};
  let next = 1;
  for (const [seq, entry] of entries) {
    if (seq > anchor) out[String(next++)] = entry;
  }
  return out;
}

export function terminalEntriesToFrontend(entries: any[]): any[] {
  return (entries || []).filter(Boolean).map((raw, idx) => ({
    role: "assistant",
    id: String(raw.id || `terminal-${idx + 1}`),
    type: "terminal",
    display_target: "terminal",
    context_policy: "include",
    seq: Number(raw.seq || idx + 1),
    source_seq: Number(raw.source_seq || 0),
    source: String(raw.source || ""),
    job_id: String(raw.job_id || ""),
    kind: String(raw.kind || raw.terminal_kind || "out"),
    class: String(raw.class || raw.terminal_class || "").toLowerCase(),
    content: textOf(raw.content ?? raw.text),
    ts: String(raw.ts || raw.created_at || "")
  }));
}
