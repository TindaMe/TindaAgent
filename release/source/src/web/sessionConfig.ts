import fs from "node:fs";
import { sessionsRoot, sqliteDbFile } from "../core/paths.js";
import { nowIso, readJson, safeId } from "../core/json.js";
import { normalizeContextTokenLimit } from "./settings.js";
import { SessionSqlStore } from "./sessionSqlStore.js";

function configPath(sessionId: string): string {
  const sid = safeId(sessionId);
  if (!sid) throw new Error("session_id invalid");
  return `${sessionsRoot()}/config/${sid}.json`;
}

function sqlStore(): SessionSqlStore {
  return new SessionSqlStore(sqliteDbFile());
}

export function loadSessionConfig(sessionId: string): Record<string, any> {
  const sid = safeId(sessionId);
  if (!sid) return {};
  let raw = sqlStore().loadConfig(sid);
  if (!Object.keys(raw).length) {
    raw = readJson<Record<string, any>>(configPath(sid), {});
    if (Object.keys(raw).length) sqlStore().saveConfig(sid, raw);
  }
  const out = raw && typeof raw === "object" && !Array.isArray(raw) ? { ...raw } : {};
  if (out.token_limit !== undefined || out.max_context_tokens !== undefined) {
    const value = normalizeContextTokenLimit(out.token_limit ?? out.max_context_tokens);
    out.token_limit = value;
    out.max_context_tokens = value;
  }
  return out;
}

export function saveSessionConfig(sessionId: string, patch: Record<string, any>): Record<string, any> {
  const sid = safeId(sessionId);
  if (!sid) throw new Error("session_id invalid");
  const current = loadSessionConfig(sid);
  const next: Record<string, any> = { ...current, ...(patch || {}), session_id: sid, updated_at: nowIso() };
  if (next.token_limit !== undefined || next.max_context_tokens !== undefined) {
    const value = normalizeContextTokenLimit(next.token_limit ?? next.max_context_tokens);
    next.token_limit = value;
    next.max_context_tokens = value;
  }
  sqlStore().saveConfig(sid, next);
  return loadSessionConfig(sid);
}

export function deleteSessionConfig(sessionId: string): void {
  sqlStore().deleteConfig(sessionId);
  try {
    fs.rmSync(configPath(sessionId), { force: true });
  } catch {
    // ignore
  }
}
