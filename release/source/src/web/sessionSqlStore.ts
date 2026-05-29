import fs from "node:fs";
import { openSqliteDatabase } from "../core/sqlite.js";
import { nowIso, readJson, safeId } from "../core/json.js";
import type { SessionMeta } from "./sessionStore.js";
import type { StoreDict, StoreEntry } from "./sessionAdapter.js";

function stringify(value: unknown): string {
  return JSON.stringify(value ?? null);
}

function parseJson<T>(text: unknown, fallback: T): T {
  try {
    if (typeof text !== "string" || !text.trim()) return fallback;
    return JSON.parse(text) as T;
  } catch {
    return fallback;
  }
}

function emptyMeta(sessionId: string): SessionMeta {
  const now = nowIso();
  return {
    id: sessionId,
    title: "新对话",
    created_at: now,
    updated_at: now,
    owner_uid: "",
    message_count: 0,
    reset_anchor_msg_id: "",
    summary_anchor_msg_id: "",
    latest_summary_message_id: "",
    last_compress_anchor_msg_id: "",
    plan_deleted_at: ""
  };
}

export class SessionSqlStore {
  private initialized = false;

  constructor(private readonly dbFile: string) {
  }

  private ensureInitialized(): void {
    if (this.initialized) return;
    this.initialized = true;
    const db = openSqliteDatabase(this.dbFile);
    try {
      db.exec(`
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL DEFAULT '新对话',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          owner_uid TEXT NOT NULL DEFAULT '',
          message_count INTEGER NOT NULL DEFAULT 0,
          reset_anchor_msg_id TEXT NOT NULL DEFAULT '',
          summary_anchor_msg_id TEXT NOT NULL DEFAULT '',
          latest_summary_message_id TEXT NOT NULL DEFAULT '',
          last_compress_anchor_msg_id TEXT NOT NULL DEFAULT '',
          plan_deleted_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated ON sessions(owner_uid, updated_at DESC);

        CREATE TABLE IF NOT EXISTS session_messages (
          session_id TEXT NOT NULL,
          seq INTEGER NOT NULL,
          entry_json TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT '',
          message_id TEXT NOT NULL DEFAULT '',
          turn_id TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (session_id, seq),
          FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_session_messages_turn ON session_messages(session_id, turn_id, role);

        CREATE TABLE IF NOT EXISTS session_terminal (
          session_id TEXT NOT NULL,
          seq INTEGER NOT NULL,
          entry_json TEXT NOT NULL,
          ts TEXT NOT NULL,
          PRIMARY KEY (session_id, seq),
          FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS session_plans (
          session_id TEXT PRIMARY KEY,
          payload_json TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS session_configs (
          session_id TEXT PRIMARY KEY,
          config_json TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
      `);
    } finally {
      db.close();
    }
  }

  private db() {
    this.ensureInitialized();
    return openSqliteDatabase(this.dbFile);
  }

  private rowToMeta(row: any): SessionMeta {
    return {
      id: String(row.id || ""),
      title: String(row.title || "新对话"),
      created_at: String(row.created_at || nowIso()),
      updated_at: String(row.updated_at || nowIso()),
      owner_uid: String(row.owner_uid || ""),
      message_count: Number(row.message_count || 0),
      reset_anchor_msg_id: String(row.reset_anchor_msg_id || ""),
      summary_anchor_msg_id: String(row.summary_anchor_msg_id || ""),
      latest_summary_message_id: String(row.latest_summary_message_id || ""),
      last_compress_anchor_msg_id: String(row.last_compress_anchor_msg_id || ""),
      plan_deleted_at: String(row.plan_deleted_at || "")
    };
  }

  private touchMetaDb(db: any, sessionId: string, patch: Partial<SessionMeta> = {}): SessionMeta {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    const currentRow = db.prepare("SELECT * FROM sessions WHERE id = ?").get(sid) as any;
    const current = currentRow ? this.rowToMeta(currentRow) : emptyMeta(sid);
    const now = nowIso();
    const next: SessionMeta = {
      ...current,
      ...patch,
      id: sid,
      updated_at: now,
      title: patch.title !== undefined ? String(patch.title || "新对话").trim().slice(0, 15) || "新对话" : current.title
    };
    db.prepare(
      `INSERT INTO sessions (
        id, title, created_at, updated_at, owner_uid, message_count, reset_anchor_msg_id,
        summary_anchor_msg_id, latest_summary_message_id, last_compress_anchor_msg_id, plan_deleted_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(id) DO UPDATE SET
        title = excluded.title,
        updated_at = excluded.updated_at,
        owner_uid = excluded.owner_uid,
        message_count = excluded.message_count,
        reset_anchor_msg_id = excluded.reset_anchor_msg_id,
        summary_anchor_msg_id = excluded.summary_anchor_msg_id,
        latest_summary_message_id = excluded.latest_summary_message_id,
        last_compress_anchor_msg_id = excluded.last_compress_anchor_msg_id,
        plan_deleted_at = excluded.plan_deleted_at`
    ).run(
      next.id,
      next.title,
      next.created_at,
      next.updated_at,
      next.owner_uid || "",
      Number(next.message_count || 0),
      next.reset_anchor_msg_id || "",
      next.summary_anchor_msg_id || "",
      next.latest_summary_message_id || "",
      next.last_compress_anchor_msg_id || "",
      next.plan_deleted_at || ""
    );
    return { ...next };
  }

  getSession(sessionId: string): SessionMeta | null {
    const sid = safeId(sessionId);
    if (!sid) return null;
    const db = this.db();
    try {
      const row = db.prepare("SELECT * FROM sessions WHERE id = ?").get(sid) as any;
      return row ? this.rowToMeta(row) : null;
    } finally {
      db.close();
    }
  }

  listSessions(limit = 200, offset = 0, ownerUid = "") {
    const safeLimit = Math.max(1, Math.min(Number(limit) || 200, 500));
    const safeOffset = Math.max(0, Number(offset) || 0);
    const owner = String(ownerUid || "");
    const db = this.db();
    try {
      const where = owner ? "WHERE message_count > 0 AND (owner_uid = '' OR owner_uid = ?)" : "WHERE message_count > 0";
      const totalRow = db.prepare(`SELECT COUNT(*) AS count FROM sessions ${where}`).get(...(owner ? [owner] : [])) as any;
      const rows = db
        .prepare(`SELECT * FROM sessions ${where} ORDER BY updated_at DESC LIMIT ? OFFSET ?`)
        .all(...(owner ? [owner, safeLimit, safeOffset] : [safeLimit, safeOffset])) as any[];
      return { sessions: rows.map((row) => this.rowToMeta(row)), total: Number(totalRow?.count || 0), limit: safeLimit, offset: safeOffset };
    } finally {
      db.close();
    }
  }

  listAllSessions(limit = 200, offset = 0, ownerUid = "") {
    const safeLimit = Math.max(1, Math.min(Number(limit) || 200, 20000));
    const safeOffset = Math.max(0, Number(offset) || 0);
    const owner = String(ownerUid || "");
    const db = this.db();
    try {
      const where = owner ? "WHERE owner_uid = '' OR owner_uid = ?" : "";
      const totalRow = db.prepare(`SELECT COUNT(*) AS count FROM sessions ${where}`).get(...(owner ? [owner] : [])) as any;
      const rows = db
        .prepare(`SELECT * FROM sessions ${where} ORDER BY updated_at DESC LIMIT ? OFFSET ?`)
        .all(...(owner ? [owner, safeLimit, safeOffset] : [safeLimit, safeOffset])) as any[];
      return { sessions: rows.map((row) => this.rowToMeta(row)), total: Number(totalRow?.count || 0), limit: safeLimit, offset: safeOffset };
    } finally {
      db.close();
    }
  }

  touchMeta(sessionId: string, patch: Partial<SessionMeta> = {}): SessionMeta {
    const db = this.db();
    try {
      return this.touchMetaDb(db, sessionId, patch);
    } finally {
      db.close();
    }
  }

  createSession(title: string, sessionId: string, ownerUid = ""): SessionMeta {
    return this.touchMeta(sessionId, { title, owner_uid: ownerUid, message_count: 0 });
  }

  loadMessages(sessionId: string): StoreDict {
    const sid = safeId(sessionId);
    if (!sid) return {};
    const db = this.db();
    try {
      const rows = db.prepare("SELECT seq, entry_json FROM session_messages WHERE session_id = ? ORDER BY seq ASC").all(sid) as any[];
      const out: StoreDict = {};
      for (const row of rows) out[String(Number(row.seq || 0))] = parseJson<StoreEntry>(row.entry_json, {});
      return out;
    } finally {
      db.close();
    }
  }

  writeMessages(sessionId: string, data: StoreDict): void {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    const db = this.db();
    try {
      db.exec("BEGIN IMMEDIATE");
      this.touchMetaDb(db, sid);
      db.prepare("DELETE FROM session_messages WHERE session_id = ?").run(sid);
      const stmt = db.prepare(
        `INSERT INTO session_messages (session_id, seq, entry_json, role, message_id, turn_id, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
      );
      const now = nowIso();
      const keys = Object.keys(data).filter((k) => /^\d+$/.test(k)).sort((a, b) => Number(a) - Number(b));
      for (const key of keys) {
        const entry = data[key] || {};
        stmt.run(sid, Number(key), stringify(entry), String(entry.role || ""), String(entry.id || ""), String(entry.turn_id || ""), String(entry.created_at || now), now);
      }
      this.touchMetaDb(db, sid, { message_count: keys.length });
      db.exec("COMMIT");
    } catch (error) {
      try {
        db.exec("ROLLBACK");
      } catch {
        // ignore rollback failure
      }
      throw error;
    } finally {
      db.close();
    }
  }

  deleteSession(sessionId: string): boolean {
    const sid = safeId(sessionId);
    if (!sid) return false;
    const db = this.db();
    try {
      const result = db.prepare("DELETE FROM sessions WHERE id = ?").run(sid) as any;
      return Number(result?.changes || 0) > 0;
    } finally {
      db.close();
    }
  }

  loadPlan(sessionId: string): Record<string, any> {
    const sid = safeId(sessionId);
    if (!sid) return {};
    const db = this.db();
    try {
      const row = db.prepare("SELECT payload_json FROM session_plans WHERE session_id = ?").get(sid) as any;
      return parseJson<Record<string, any>>(row?.payload_json, {});
    } finally {
      db.close();
    }
  }

  savePlan(sessionId: string, payload: Record<string, any>): Record<string, any> {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    const db = this.db();
    try {
      this.touchMetaDb(db, sid);
      db.prepare(
        `INSERT INTO session_plans (session_id, payload_json, updated_at)
         VALUES (?, ?, ?)
         ON CONFLICT(session_id) DO UPDATE SET payload_json = excluded.payload_json, updated_at = excluded.updated_at`
      ).run(sid, stringify(payload), nowIso());
      return payload;
    } finally {
      db.close();
    }
  }

  appendTerminal(sessionId: string, entries: any[]): { session_id: string; added: number; total: number } {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    const rows = entries || [];
    const db = this.db();
    try {
      this.touchMetaDb(db, sid);
      const maxRow = db.prepare("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM session_terminal WHERE session_id = ?").get(sid) as any;
      let seq = Number(maxRow?.max_seq || 0);
      const stmt = db.prepare("INSERT INTO session_terminal (session_id, seq, entry_json, ts) VALUES (?, ?, ?, ?)");
      for (const entry of rows) {
        seq += 1;
        stmt.run(sid, seq, stringify(entry), String(entry?.ts || nowIso()));
      }
      const totalRow = db.prepare("SELECT COUNT(*) AS count FROM session_terminal WHERE session_id = ?").get(sid) as any;
      return { session_id: sid, added: rows.length, total: Number(totalRow?.count || 0) };
    } finally {
      db.close();
    }
  }

  loadTerminal(sessionId: string): any[] {
    const sid = safeId(sessionId);
    if (!sid) return [];
    const db = this.db();
    try {
      const rows = db.prepare("SELECT entry_json FROM session_terminal WHERE session_id = ? ORDER BY seq ASC").all(sid) as any[];
      return rows.map((row) => parseJson<Record<string, any>>(row.entry_json, {}));
    } finally {
      db.close();
    }
  }

  loadConfig(sessionId: string): Record<string, any> {
    const sid = safeId(sessionId);
    if (!sid) return {};
    const db = this.db();
    try {
      const row = db.prepare("SELECT config_json FROM session_configs WHERE session_id = ?").get(sid) as any;
      return parseJson<Record<string, any>>(row?.config_json, {});
    } finally {
      db.close();
    }
  }

  saveConfig(sessionId: string, config: Record<string, any>): Record<string, any> {
    const sid = safeId(sessionId);
    if (!sid) throw new Error("session_id invalid");
    const db = this.db();
    try {
      this.touchMetaDb(db, sid);
      db.prepare(
        `INSERT INTO session_configs (session_id, config_json, updated_at)
         VALUES (?, ?, ?)
         ON CONFLICT(session_id) DO UPDATE SET config_json = excluded.config_json, updated_at = excluded.updated_at`
      ).run(sid, stringify(config), nowIso());
      return config;
    } finally {
      db.close();
    }
  }

  deleteConfig(sessionId: string): void {
    const sid = safeId(sessionId);
    if (!sid) return;
    const db = this.db();
    try {
      db.prepare("DELETE FROM session_configs WHERE session_id = ?").run(sid);
    } finally {
      db.close();
    }
  }

  importJsonSessions(payload: { sessions?: SessionMeta[] }): void {
    const rows = Array.isArray(payload.sessions) ? payload.sessions : [];
    for (const row of rows) {
      if (!safeId(row.id)) continue;
      if (!this.getSession(row.id)) this.touchMeta(row.id, row);
    }
  }

  importMessages(sessionId: string, data: StoreDict): void {
    if (Object.keys(data || {}).length) this.writeMessages(sessionId, data);
  }

  importTerminal(sessionId: string, file: string): void {
    if (!fs.existsSync(file) || this.loadTerminal(sessionId).length) return;
    const rows = readJson<any[]>(file, []);
    if (rows.length) this.appendTerminal(sessionId, rows);
  }
}
