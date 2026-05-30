import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { legacySessionsRoot, sessionsRoot, sqliteDbFile } from "../core/paths.js";
import { nowIso, readJson, safeId, writeJson } from "../core/json.js";
import {
  buildAssistantMessage,
  buildSystemMessage,
  buildUserMessage,
  effectiveStoreDict,
  filterRawChatEntries,
  isTransientAssistantDraft,
  normalizeChronologicalStoreDict,
  normalizeStoreDict,
  normalizeStoreEntry,
  storeDictToAgentMessages,
  storeDictToFrontend,
  stripTransientAssistantDrafts,
  terminalEntriesToFrontend,
  type StoreDict,
  type StoreEntry
} from "./sessionAdapter.js";
import { SessionSqlStore } from "./sessionSqlStore.js";

export interface SessionMeta {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  owner_uid: string;
  message_count: number;
  reset_anchor_msg_id: string;
  summary_anchor_msg_id: string;
  latest_summary_message_id: string;
  last_compress_anchor_msg_id: string;
  plan_deleted_at: string;
}

export class SessionStore {
  rootDir: string;
  legacyRootDir: string;
  sessionsFile: string;
  messagesDir: string;
  plansDir: string;
  exportsDir: string;
  private sql: SessionSqlStore;
  private legacyIndexImported = false;

  constructor(rootDir = sessionsRoot(), legacyRootDir = legacySessionsRoot()) {
    this.rootDir = path.resolve(rootDir);
    this.legacyRootDir = path.resolve(legacyRootDir);
    this.sessionsFile = path.join(this.rootDir, "sessions.json");
    this.messagesDir = path.join(this.rootDir, "messages");
    this.plansDir = path.join(this.rootDir, "plans");
    this.exportsDir = path.join(this.rootDir, "exports");
    [this.rootDir, this.messagesDir, this.plansDir, this.exportsDir].forEach((dir) => fs.mkdirSync(dir, { recursive: true }));
    const dbFile = path.resolve(this.rootDir) === path.resolve(sessionsRoot()) ? sqliteDbFile() : path.join(this.rootDir, "sessions.sqlite3");
    this.sql = new SessionSqlStore(dbFile);
  }

  private messagesPath(sessionId: string): string {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    return path.join(this.messagesDir, `${sid}.json`);
  }

  private legacyMessagesPath(sessionId: string): string {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    return path.join(this.legacyRootDir, "messages", `${sid}.jsonl`);
  }

  private terminalPath(sessionId: string): string {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    return path.join(this.messagesDir, `${sid}.terminal.json`);
  }

  private planPath(sessionId: string): string {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    return path.join(this.plansDir, `${sid}.json`);
  }

  private configPath(sessionId: string): string {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    return path.join(this.rootDir, "config", `${sid}.json`);
  }

  private readSessions(): { sessions: SessionMeta[] } {
    const primary = readJson<{ sessions?: SessionMeta[] }>(this.sessionsFile, { sessions: [] });
    if (Array.isArray(primary.sessions)) return { sessions: primary.sessions };
    const legacy = path.join(this.legacyRootDir, "sessions.json");
    return readJson<{ sessions: SessionMeta[] }>(legacy, { sessions: [] });
  }

  private writeSessions(payload: { sessions: SessionMeta[] }): void {
    writeJson(this.sessionsFile, { sessions: payload.sessions });
  }

  private syncMetaToJson(meta: SessionMeta): void {
    const payload = this.readSessions();
    const byId = new Map<string, SessionMeta>();
    for (const row of Array.isArray(payload.sessions) ? payload.sessions : []) {
      if (row?.id) byId.set(row.id, row);
    }
    for (const row of this.sql.listAllSessions(10000, 0).sessions) {
      if (row?.id) byId.set(row.id, { ...(byId.get(row.id) || {}), ...row });
    }
    byId.set(meta.id, { ...(byId.get(meta.id) || {}), ...meta });
    const rows = Array.from(byId.values());
    rows.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    this.writeSessions({ sessions: rows });
  }

  private importLegacySessionIndex(): void {
    if (this.legacyIndexImported) return;
    this.legacyIndexImported = true;
    this.sql.importJsonSessions(this.readSessions());
  }

  private touchMeta(sessionId: string, patch: Partial<SessionMeta> = {}): SessionMeta {
    const meta = this.sql.touchMeta(sessionId, patch);
    this.syncMetaToJson(meta);
    return meta;
  }

  createSession(title = "新对话", sessionId = "", ownerUid = ""): SessionMeta {
    this.importLegacySessionIndex();
    let sid = safeId(sessionId || `s_${crypto.randomBytes(6).toString("hex")}`);
    const existing = new Set(this.sql.listSessions(10000, 0).sessions.map((s) => s.id));
    if (existing.has(sid)) sid = `${sid}_${crypto.randomBytes(3).toString("hex")}`;
    return this.touchMeta(sid, { title, owner_uid: ownerUid, message_count: 0 });
  }

  ensureSession(sessionId: string, ownerUid = ""): SessionMeta {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    const existing = this.getSession(sid);
    if (existing) {
      if (ownerUid && !String(existing.owner_uid || "").trim()) return this.touchMeta(sid, { owner_uid: ownerUid });
      return existing;
    }
    const count = Object.keys(this.loadMessagesRaw(sid)).filter((k) => /^\d+$/.test(k)).length;
    return this.touchMeta(sid, { owner_uid: ownerUid, message_count: count });
  }

  getSession(sessionId: string): SessionMeta | null {
    this.importLegacySessionIndex();
    const sid = safeId(sessionId);
    if (!sid) return null;
    return this.sql.getSession(sid);
  }

  listSessions(limit = 200, offset = 0, ownerUid = "") {
    this.importLegacySessionIndex();
    return this.sql.listSessions(limit, offset, ownerUid);
  }

  loadMessagesRaw(sessionId: string): StoreDict {
    const sid = safeId(sessionId);
    if (!sid) return {};
    const file = this.messagesPath(sid);
    if (fs.existsSync(file)) {
      const data = readJson<StoreDict>(file, {});
      return data && typeof data === "object" && !Array.isArray(data) ? data : {};
    }
    const legacy = this.legacyMessagesPath(sid);
    if (fs.existsSync(legacy)) return this.migrateJsonlToDict(legacy);
    return {};
  }

  loadMessages(sessionId: string): StoreDict {
    const sid = safeId(sessionId);
    if (!sid) return {};
    let raw = this.loadMessagesRaw(sid);
    if (Object.keys(raw).length) this.sql.importMessages(sid, raw);
    else {
      raw = this.sql.loadMessages(sid);
      if (Object.keys(raw).length) writeJson(this.messagesPath(sid), raw);
    }
    const [withoutDrafts, removedDrafts] = stripTransientAssistantDrafts(raw);
    if (removedDrafts) raw = withoutDrafts;
    const [normalized, changed] = normalizeChronologicalStoreDict(raw);
    if (changed || removedDrafts) this.writeMessages(sid, normalized);
    return normalized;
  }

  loadEffectiveMessages(sessionId: string): StoreDict {
    return effectiveStoreDict(this.loadMessages(sessionId), this.getSession(sessionId) || {});
  }

  writeMessages(sessionId: string, data: StoreDict): void {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    const [ordered] = normalizeChronologicalStoreDict(data);
    writeJson(this.messagesPath(sid), ordered);
    this.sql.writeMessages(sid, ordered);
    const meta = this.sql.getSession(sid);
    if (meta) this.syncMetaToJson(meta);
  }

  private migrateJsonlToDict(file: string): StoreDict {
    const out: StoreDict = {};
    let seq = 0;
    let lines: string[] = [];
    try {
      lines = fs.readFileSync(file, "utf8").split(/\r?\n/);
    } catch {
      return {};
    }
    for (const line of lines) {
      const raw = line.trim();
      if (!raw) continue;
      let row: Record<string, any>;
      try {
        row = JSON.parse(raw);
      } catch {
        continue;
      }
      const role = String(row.role || "").trim();
      const entryType = String(row.entry_type || "chat").trim();
      const content = String(row.content || "");
      if (role === "user") {
        seq += 1;
        out[String(seq)] = buildUserMessage(content);
      } else if (entryType === "notice") {
        seq += 1;
        out[String(seq)] = buildSystemMessage(content);
      } else if (role === "assistant" && entryType === "chat") {
        const substeps: StoreEntry[] = [];
        const reasoning = String(row.reasoning_content || "").trim();
        if (reasoning) substeps.push({ kind: "thinking", content: reasoning });
        substeps.push({ kind: "text", content });
        seq += 1;
        out[String(seq)] = buildAssistantMessage(substeps);
      } else if (entryType === "tool_marker" && seq > 0 && out[String(seq)]?.role === "assistant") {
        const previous = out[String(seq)];
        const previousContent = previous.content && typeof previous.content === "object" ? previous.content : {};
        const maxSub = Math.max(0, ...Object.keys(previousContent).filter((k) => /^\d+$/.test(k)).map(Number));
        previousContent[String(maxSub + 1)] = {
          tool_marker: {
            name: String(row.name || row.tool_name || "unknown"),
            ok: row.ok !== false,
            stdin: String(row.stdin || "").slice(0, 500),
            stdout: String(row.stdout || content || "").slice(0, 4000),
            id: String(row.id || row.call_id || ""),
            tool_call_id: String(row.tool_call_id || row.call_id || ""),
            arguments: row.arguments || row.args || {},
            result: row.result || {}
          }
        };
        previous.content = previousContent;
      }
    }
    return out;
  }

  appendMessages(sessionId: string, messages: StoreEntry[]) {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    this.ensureSession(sid);
    const data = this.loadMessages(sid);
    let max = Math.max(0, ...Object.keys(data).filter((k) => /^\d+$/.test(k)).map(Number));
    let added = 0;
    for (const msg of messages) {
      const normalized = normalizeStoreEntry(msg);
      if (!normalized) continue;
      data[String(++max)] = normalized;
      added += 1;
    }
    this.writeMessages(sid, data);
    this.touchMeta(sid, { message_count: Object.keys(data).filter((k) => /^\d+$/.test(k)).length });
    return { session_id: sid, added, message_count: Object.keys(data).length };
  }

  replaceAssistantByTurn(sessionId: string, turnId: string, substeps: Array<Record<string, any>>): boolean {
    const sid = safeId(sessionId);
    const cleanTurn = String(turnId || "").trim();
    if (!sid || !cleanTurn) return false;
    const data = this.loadMessages(sid);
    const descKeys = Object.keys(data).filter((k) => /^\d+$/.test(k)).sort((a, b) => Number(b) - Number(a));
    const key = descKeys.find((k) => data[k]?.role === "assistant" && String(data[k]?.turn_id || "") === cleanTurn);
    if (key) {
      const replacement = buildAssistantMessage(substeps, { ...data[key], turn_id: cleanTurn, type: "assistant_message", context_policy: "include", display_target: "chat" });
      data[key] = { ...data[key], content: replacement.content, turn_id: cleanTurn, type: "assistant_message", context_policy: "include", display_target: "chat" };
    } else {
      const ascKeys = descKeys.slice().sort((a, b) => Number(a) - Number(b));
      const userKey = ascKeys.find((k) => data[k]?.role === "user" && String(data[k]?.turn_id || "") === cleanTurn);
      const insertAt = userKey ? Number(userKey) + 1 : Math.max(0, ...ascKeys.map(Number)) + 1;
      const nextData: StoreDict = {};
      const keys = ascKeys.map(Number);
      let inserted = false;
      for (const seq of keys) {
        if (!inserted && seq >= insertAt) {
          nextData[String(insertAt)] = buildAssistantMessage(substeps, { turn_id: cleanTurn });
          inserted = true;
        }
        const targetSeq = inserted && seq >= insertAt ? seq + 1 : seq;
        nextData[String(targetSeq)] = data[String(seq)];
      }
      if (!inserted) nextData[String(insertAt)] = buildAssistantMessage(substeps, { turn_id: cleanTurn });
      Object.keys(data).forEach((k) => delete data[k]);
      Object.assign(data, nextData);
    }
    this.writeMessages(sid, data);
    this.touchMeta(sid, { message_count: Object.keys(data).filter((k) => /^\d+$/.test(k)).length });
    return true;
  }

  ensureTurnUser(sessionId: string, userMessage: StoreEntry, turnId: string) {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    const cleanTurn = String(turnId || "").trim();
    const data = this.loadMessages(sid);
    const keys = Object.keys(data).filter((k) => /^\d+$/.test(k)).sort((a, b) => Number(a) - Number(b));
    let userKey = keys.find((k) => data[k]?.role === "user" && data[k]?.turn_id === cleanTurn);
    let max = Math.max(0, ...keys.map(Number));
    const userNorm = normalizeStoreEntry({ ...userMessage, turn_id: cleanTurn });
    if (userNorm && !userKey) {
      userKey = String(++max);
      data[userKey] = userNorm;
      this.writeMessages(sid, data);
      this.touchMeta(sid, { message_count: Object.keys(data).filter((k) => /^\d+$/.test(k)).length });
    }
    return { session_id: sid, user_key: userKey || "", message_count: Object.keys(data).length };
  }

  ensureTurnDraft(sessionId: string, userMessage: StoreEntry, assistantMessage: StoreEntry, turnId: string) {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    const data = this.loadMessages(sid);
    const keys = Object.keys(data).filter((k) => /^\d+$/.test(k)).sort((a, b) => Number(a) - Number(b));
    let userKey = keys.find((k) => data[k]?.role === "user" && data[k]?.turn_id === turnId);
    let assistantKey = keys.find((k) => data[k]?.role === "assistant" && data[k]?.turn_id === turnId);
    let max = Math.max(0, ...keys.map(Number));
    const userNorm = normalizeStoreEntry({ ...userMessage, turn_id: turnId });
    const assistantNorm = normalizeStoreEntry({ ...assistantMessage, turn_id: turnId });
    if (userNorm && !userKey) {
      userKey = String(++max);
      data[userKey] = userNorm;
    }
    if (assistantNorm && !assistantKey && !isTransientAssistantDraft(assistantNorm)) {
      assistantKey = String(++max);
      data[assistantKey] = assistantNorm;
    }
    this.writeMessages(sid, data);
    this.touchMeta(sid, { message_count: Object.keys(data).filter((k) => /^\d+$/.test(k)).length });
    return { session_id: sid, user_key: userKey || "", assistant_key: assistantKey || "", message_count: Object.keys(data).length };
  }

  getContextMessages(sessionId: string) {
    const sid = safeId(sessionId);
    if (!sid) return [];
    return storeDictToAgentMessages(this.loadMessages(sid), this.getSession(sid) || {}, this.loadTerminal(sid));
  }

  frontendMessages(sessionId: string, limit = 0, beforeSeq = 0) {
    const sid = safeId(sessionId);
    const data = this.loadEffectiveMessages(sid);
    let keys = Object.keys(data).filter((k) => /^\d+$/.test(k)).map(Number).sort((a, b) => a - b);
    const total = keys.length;
    if (beforeSeq > 0) keys = keys.filter((k) => k < beforeSeq);
    const requested = Math.max(0, Math.min(Number(limit) || 0, 500));
    if (requested > 0) keys = keys.slice(-requested);
    const subset: StoreDict = {};
    keys.forEach((k) => {
      subset[String(k)] = data[String(k)];
    });
    const entries = storeDictToFrontend(subset);
    entries.forEach((entry, idx) => {
      entry.seq = keys[idx] || entry.seq || idx + 1;
    });
    return {
      ok: true,
      session_id: sid,
      entries,
      total,
      oldest_seq: keys[0] || 0,
      newest_seq: keys[keys.length - 1] || 0,
      has_more: Boolean(keys[0] && keys[0] > 1),
      limit: requested,
      source: "json_partition_store"
    };
  }

  setSessionTitle(sessionId: string, title: string): SessionMeta {
    return this.touchMeta(sessionId, { title });
  }

  deleteSession(sessionId: string): boolean {
    const sid = safeId(sessionId);
    if (!sid) return false;
    const deleted = this.sql.deleteSession(sid);
    [this.messagesPath(sid), this.terminalPath(sid), this.planPath(sid), this.configPath(sid)].forEach((file) => {
      try {
        fs.rmSync(file, { force: true });
      } catch {
        // ignore legacy cleanup failure
      }
    });
    const payload = this.readSessions();
    const byId = new Map<string, SessionMeta>();
    for (const row of Array.isArray(payload.sessions) ? payload.sessions : []) {
      if (row?.id) byId.set(row.id, row);
    }
    for (const row of this.sql.listAllSessions(10000, 0).sessions) {
      if (row?.id) byId.set(row.id, { ...(byId.get(row.id) || {}), ...row });
    }
    byId.delete(sid);
    const rows = Array.from(byId.values());
    rows.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    this.writeSessions({ sessions: rows });
    return deleted;
  }

  clearAll(ownerUid = ""): number {
    const rows = this.listSessions(10000, 0, ownerUid).sessions;
    let deleted = 0;
    for (const row of rows) {
      if (this.deleteSession(row.id)) deleted += 1;
    }
    return deleted;
  }

  markResetAnchor(sessionId: string) {
    const sid = safeId(sessionId);
    const data = this.loadMessages(sid);
    const keys = Object.keys(data).filter((k) => /^\d+$/.test(k)).map(Number).sort((a, b) => a - b);
    const anchor = keys.length ? String(data[String(keys[keys.length - 1])]?.id || "") : "";
    this.touchMeta(sid, {
      reset_anchor_msg_id: anchor,
      summary_anchor_msg_id: "",
      latest_summary_message_id: "",
      last_compress_anchor_msg_id: "",
      message_count: keys.length
    });
    return { session_id: sid, reset_anchor_msg_id: anchor, message_count: keys.length };
  }

  loadPlan(sessionId: string): Record<string, any> {
    const sid = safeId(sessionId);
    if (!sid) return {};
    const legacy = readJson<Record<string, any>>(this.planPath(sid), {});
    if (Object.keys(legacy).length) {
      this.sql.savePlan(sid, legacy);
      return legacy;
    }
    const current = this.sql.loadPlan(sid);
    if (Object.keys(current).length) writeJson(this.planPath(sid), current);
    return current;
  }

  savePlan(sessionId: string, current: Record<string, any> | null, deleted = false) {
    const payload = { version: 1, session_id: safeId(sessionId), updated_at: nowIso(), deleted, current: deleted ? null : current };
    writeJson(this.planPath(sessionId), payload);
    const saved = this.sql.savePlan(sessionId, payload);
    const meta = this.sql.getSession(sessionId);
    if (meta) this.syncMetaToJson(meta);
    return saved;
  }

  markPlanDeleted(sessionId: string) {
    const deletedAt = nowIso();
    this.savePlan(sessionId, null, true);
    return this.touchMeta(sessionId, { plan_deleted_at: deletedAt });
  }

  appendTerminal(sessionId: string, entries: any[]) {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    const existing = this.loadTerminal(sid);
    const rows = [...existing, ...(entries || [])];
    writeJson(this.terminalPath(sid), rows);
    const result = this.sql.appendTerminal(sid, entries || []);
    const meta = this.sql.getSession(sid);
    if (meta) this.syncMetaToJson(meta);
    return result;
  }

  loadTerminal(sessionId: string): any[] {
    const sid = safeId(sessionId);
    if (!sid) return [];
    const fileRows = readJson<any[]>(this.terminalPath(sid), []);
    if (fileRows.length) {
      this.sql.importTerminal(sid, this.terminalPath(sid));
      return fileRows;
    }
    const rows = this.sql.loadTerminal(sid);
    if (rows.length) writeJson(this.terminalPath(sid), rows);
    return rows;
  }

  frontendTerminal(sessionId: string, limit = 300) {
    const all = this.loadTerminal(sessionId);
    const safeLimit = Math.max(1, Math.min(Number(limit) || 300, 2000));
    const entries = all.length > safeLimit ? all.slice(-safeLimit) : all;
    return {
      ok: true,
      session_id: safeId(sessionId),
      entries: terminalEntriesToFrontend(entries),
      total: all.length,
      limit: safeLimit,
      truncated: all.length > safeLimit,
      omitted: Math.max(0, all.length - safeLimit)
    };
  }

  compressContext(sessionId: string, summaryText: string) {
    const sid = safeId(sessionId);
    const data = this.loadMessages(sid);
    const meta: Partial<SessionMeta> = this.getSession(sid) || {};
    const fullRows = filterRawChatEntries(data);
    if (fullRows.length >= 4) {
      const lastAnchorId = String(fullRows[fullRows.length - 4]?.id || "");
      if (lastAnchorId && lastAnchorId === String(meta.last_compress_anchor_msg_id || "")) {
        return {
          session_id: sid,
          compressed: false,
          reason: "already_compressed",
          anchor_message_id: lastAnchorId,
          visible_count: Object.keys(this.loadEffectiveMessages(sid)).filter((k) => /^\d+$/.test(k)).length
        };
      }
    }
    const rawRows = filterRawChatEntries(effectiveStoreDict(data, meta));
    if (rawRows.length < 6) throw new Error("消息数量不足，至少需要 6 条消息才能压缩");
    const keepTail = rawRows.slice(-4);
    const older = rawRows.slice(0, -4);
    const anchorId = String(keepTail[0]?.id || "");
    const summary = buildSystemMessage(summaryText, { is_summary: true, type: "summary", display_target: "chat", context_policy: "summary" });
    const keys = Object.keys(data).filter((k) => /^\d+$/.test(k)).map(Number).sort((a, b) => a - b);
    const max = Math.max(0, ...keys);
    const tailStart = Number(keepTail[0]?.seq || keepTail[0]?.source_seq || 0);
    let inserted = false;
    if (tailStart > 0) {
      for (let key = tailStart; key <= max; key += 1) {
        if (!data[String(key)]) {
          data[String(key)] = summary;
          inserted = true;
          break;
        }
      }
    }
    if (!inserted) data[String(max + 1)] = summary;
    this.writeMessages(sid, data);
    this.touchMeta(sid, {
      latest_summary_message_id: String(summary.id || ""),
      summary_anchor_msg_id: anchorId,
      last_compress_anchor_msg_id: anchorId,
      message_count: Object.keys(data).filter((k) => /^\d+$/.test(k)).length
    });
    return {
      session_id: sid,
      compressed: true,
      compressed_count: older.length,
      summary_message_id: summary.id,
      anchor_message_id: anchorId,
      visible_count: 1 + keepTail.length
    };
  }
}
