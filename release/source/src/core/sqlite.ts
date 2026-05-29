import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { sqliteDbFile } from "./paths.js";
import { nowIso } from "./json.js";

type DatabaseSyncType = any;
type DatabaseCtor = new (file: string) => DatabaseSyncType;

const nodeRequire = createRequire(import.meta.url);
let db: DatabaseSyncType | null = null;
let DatabaseSyncCtor: DatabaseCtor | null = null;

function loadDatabaseCtor(): DatabaseCtor {
  if (!DatabaseSyncCtor) {
    const originalEmitWarning = process.emitWarning;
    process.emitWarning = function patchedEmitWarning(warning: string | Error, ...args: any[]) {
      const type = String(args[0] || "");
      const text = String(warning instanceof Error ? warning.message : warning);
      if (type === "ExperimentalWarning" && text.includes("SQLite")) return;
      return originalEmitWarning.call(process, warning as any, ...args);
    } as typeof process.emitWarning;
    try {
      DatabaseSyncCtor = nodeRequire("node:sqlite").DatabaseSync as DatabaseCtor;
    } finally {
      process.emitWarning = originalEmitWarning;
    }
  }
  return DatabaseSyncCtor;
}

export function openSqliteDatabase(file: string): DatabaseSyncType {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const DatabaseSync = loadDatabaseCtor();
  const database = new DatabaseSync(file);
  database.exec(`
    PRAGMA journal_mode = WAL;
    PRAGMA synchronous = NORMAL;
    PRAGMA foreign_keys = ON;
    PRAGMA busy_timeout = 5000;
  `);
  return database;
}

export function appDb(): DatabaseSyncType {
  if (!db) {
    db = openSqliteDatabase(sqliteDbFile());
    db.exec(`
      CREATE TABLE IF NOT EXISTS kv_store (
        namespace TEXT NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (namespace, key)
      );
    `);
  }
  return db;
}

export function withTransaction<T>(fn: () => T): T {
  const database = appDb();
  database.exec("BEGIN IMMEDIATE");
  try {
    const result = fn();
    database.exec("COMMIT");
    return result;
  } catch (error) {
    try {
      database.exec("ROLLBACK");
    } catch {
      // ignore rollback failure
    }
    throw error;
  }
}

export function closeAppDb(): void {
  if (!db) return;
  db.close();
  db = null;
}

export function kvGet<T>(namespace: string, key: string, fallback: T): T {
  try {
    const row = appDb().prepare("SELECT value_json FROM kv_store WHERE namespace = ? AND key = ?").get(namespace, key) as any;
    if (!row?.value_json) return fallback;
    return JSON.parse(String(row.value_json)) as T;
  } catch {
    return fallback;
  }
}

export function kvSet(namespace: string, key: string, value: unknown): void {
  appDb()
    .prepare(
      `INSERT INTO kv_store (namespace, key, value_json, updated_at)
       VALUES (?, ?, ?, ?)
       ON CONFLICT(namespace, key) DO UPDATE SET
         value_json = excluded.value_json,
         updated_at = excluded.updated_at`
    )
    .run(namespace, key, JSON.stringify(value), nowIso());
}

export function kvDelete(namespace: string, key: string): void {
  appDb().prepare("DELETE FROM kv_store WHERE namespace = ? AND key = ?").run(namespace, key);
}
