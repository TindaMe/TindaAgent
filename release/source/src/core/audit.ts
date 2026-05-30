import fs from "node:fs";
import path from "node:path";
import { appDb } from "./sqlite.js";
import { legacyLogRoot, logRoot, sqliteDbFile } from "./paths.js";
import { nowIso, textOf } from "./json.js";

export interface AuditEventInput {
  op_type?: string;
  subsystem?: string;
  func?: string;
  dir?: string;
  file?: string;
  file_path?: string;
  content?: string;
  extra?: Record<string, unknown>;
}

export interface AuditEventRow {
  id: number;
  ts: string;
  op_type: string;
  subsystem: string;
  func: string;
  dir: string;
  file: string;
  file_path: string;
  content: string;
  extra: Record<string, unknown>;
  source: string;
  source_file: string;
  legacy_format: string;
  created_at: string;
}

type ParsedAuditEvent = Omit<AuditEventRow, "created_at"> & { raw_json?: string };

const SCHEMA_SQL = `
  CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    op_type TEXT NOT NULL DEFAULT '',
    subsystem TEXT NOT NULL DEFAULT '',
    func TEXT NOT NULL DEFAULT '',
    dir TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'runtime',
    source_file TEXT NOT NULL DEFAULT '',
    legacy_format TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_audit_events_ts ON audit_events(ts DESC);
  CREATE INDEX IF NOT EXISTS idx_audit_events_source ON audit_events(source, source_file);
  CREATE INDEX IF NOT EXISTS idx_audit_events_op ON audit_events(op_type, subsystem);

  CREATE TABLE IF NOT EXISTS audit_log_sources (
    source_file TEXT PRIMARY KEY,
    source_path TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mtime_ms INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0
  );
`;

const DEFAULT_LEGACY_LOG_NAMES = ["total.jsonl"];
const FULL_LEGACY_LOG_NAMES = [
  ...DEFAULT_LEGACY_LOG_NAMES,
  "total.log",
  "web.log",
  "permission.log",
  "tool.log",
  "session.log",
  "ai.log",
  "user.log",
  "versioning.log",
  "tool_runtime.log",
  "storage_migration.log",
  "error.log",
  "audit_error.log",
  "audit_test.log",
  "context_injection.log"
];

const IMPORT_LINE_LIMIT = 50000;
const IMPORT_TAIL_CHUNK_BYTES = 64 * 1024;
const LEGACY_TEXT_RE = /^\[(\d+)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s*(.*)$/;

let schemaReady = false;
let legacyImported = false;
let sequenceSeeded = false;

function db() {
  const database = appDb();
  ensureSchema(database);
  return database;
}

function ensureSchema(database: any): void {
  if (schemaReady) return;
  database.exec(SCHEMA_SQL);
  schemaReady = true;
}

function stringifyExtra(extra: unknown): string {
  try {
    if (!extra || typeof extra !== "object") return "{}";
    return JSON.stringify(extra);
  } catch {
    return "{}";
  }
}

function parseExtra(text: unknown): Record<string, unknown> {
  try {
    if (typeof text !== "string" || !text.trim()) return {};
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function normalizeTs(value: unknown): string {
  const text = textOf(value).trim();
  return text || nowIso();
}

function fileSize(file: string): number {
  try {
    return fs.statSync(file).size;
  } catch {
    return 0;
  }
}

function rowFromDb(row: any): AuditEventRow {
  return {
    id: Number(row.id || 0),
    ts: String(row.ts || ""),
    op_type: String(row.op_type || ""),
    subsystem: String(row.subsystem || ""),
    func: String(row.func || ""),
    dir: String(row.dir || ""),
    file: String(row.file || ""),
    file_path: String(row.file_path || ""),
    content: String(row.content || ""),
    extra: parseExtra(row.extra_json),
    source: String(row.source || ""),
    source_file: String(row.source_file || ""),
    legacy_format: String(row.legacy_format || ""),
    created_at: String(row.created_at || "")
  };
}

function insertAuditEvent(database: any, event: Partial<ParsedAuditEvent>, preserveId: boolean): number {
  const extraJson = stringifyExtra(event.extra);
  const id = Number(event.id || 0);
  if (preserveId && Number.isFinite(id) && id > 0) {
    const result = database
      .prepare(
        `INSERT OR IGNORE INTO audit_events (
          id, ts, op_type, subsystem, func, dir, file, file_path, content, extra_json,
          source, source_file, legacy_format, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
      )
      .run(
        Math.floor(id),
        normalizeTs(event.ts),
        textOf(event.op_type),
        textOf(event.subsystem),
        textOf(event.func),
        textOf(event.dir),
        textOf(event.file),
        textOf(event.file_path),
        textOf(event.content),
        extraJson,
        textOf(event.source || "legacy"),
        textOf(event.source_file),
        textOf(event.legacy_format),
        nowIso()
      );
    if (Number(result?.changes || 0) > 0) return Math.floor(id);
    const existing = database.prepare("SELECT source, source_file FROM audit_events WHERE id = ?").get(Math.floor(id)) as any;
    if (String(existing?.source || "") === textOf(event.source || "legacy") && String(existing?.source_file || "") === textOf(event.source_file)) {
      return Math.floor(id);
    }
    return insertAuditEvent(
      database,
      {
        ...event,
        id: 0,
        extra: { ...(event.extra || {}), legacy_id: Math.floor(id) }
      },
      false
    );
  }
  const result = database
    .prepare(
      `INSERT INTO audit_events (
        ts, op_type, subsystem, func, dir, file, file_path, content, extra_json,
        source, source_file, legacy_format, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .run(
      normalizeTs(event.ts),
      textOf(event.op_type),
      textOf(event.subsystem),
      textOf(event.func),
      textOf(event.dir),
      textOf(event.file),
      textOf(event.file_path),
      textOf(event.content),
      extraJson,
      textOf(event.source || "runtime"),
      textOf(event.source_file),
      textOf(event.legacy_format),
      nowIso()
    ) as any;
  return Number(result?.lastInsertRowid || 0);
}

function parseJsonAuditLine(line: string, sourceFile: string): ParsedAuditEvent | null {
  try {
    const raw = JSON.parse(line);
    const event = raw?.event && typeof raw.event === "object" ? raw.event : raw;
    const id = Number(raw?.id || event?.id || 0);
    if (!Number.isFinite(id) || id <= 0) return null;
    return {
      id,
      ts: normalizeTs(raw?.ts || raw?.time || event?.ts || event?.time),
      op_type: textOf(event?.op_type),
      subsystem: textOf(event?.subsystem),
      func: textOf(event?.func),
      dir: textOf(event?.dir),
      file: textOf(event?.file),
      file_path: textOf(event?.file_path || event?.path || raw?.path),
      content: textOf(event?.content),
      extra: event?.extra && typeof event.extra === "object" ? event.extra : {},
      source: "legacy",
      source_file: sourceFile,
      legacy_format: "jsonl",
      raw_json: line
    };
  } catch {
    return null;
  }
}

function parseTextAuditLine(line: string, sourceFile: string): ParsedAuditEvent | null {
  const match = LEGACY_TEXT_RE.exec(line);
  if (!match) return null;
  const id = Number(match[1] || 0);
  if (!Number.isFinite(id) || id <= 0) return null;
  return {
    id,
    ts: normalizeTs(match[2]),
    op_type: textOf(match[3]),
    func: textOf(match[4]),
    dir: textOf(match[5]),
    file: textOf(match[6]),
    file_path: textOf(match[7]),
    subsystem: textOf(match[5]).toLowerCase(),
    content: textOf(match[8]),
    extra: {},
    source: "legacy",
    source_file: sourceFile,
    legacy_format: "text"
  };
}

function parseLooseJsonLine(line: string, sourceFile: string): ParsedAuditEvent | null {
  try {
    const raw = JSON.parse(line);
    return {
      id: 0,
      ts: normalizeTs(raw?.ts || raw?.time),
      op_type: textOf(raw?.op_type || "SYSTEM_READ"),
      subsystem: textOf(raw?.subsystem || path.basename(sourceFile).replace(/\..+$/, "")),
      func: textOf(raw?.func || raw?.reason || ""),
      dir: textOf(raw?.dir),
      file: textOf(raw?.file),
      file_path: textOf(raw?.file_path || raw?.path),
      content: textOf(raw?.content || raw?.reason || line),
      extra: raw && typeof raw === "object" ? raw : {},
      source: "legacy",
      source_file: sourceFile,
      legacy_format: "json"
    };
  } catch {
    return null;
  }
}

function parseAuditLine(line: string, sourceFile: string): ParsedAuditEvent | null {
  const text = line.trim();
  if (!text) return null;
  if (text.startsWith("{")) return parseJsonAuditLine(text, sourceFile) || parseLooseJsonLine(text, sourceFile);
  return parseTextAuditLine(text, sourceFile);
}

function readLogLines(file: string, maxLines = IMPORT_LINE_LIMIT): string[] {
  let fd: number | null = null;
  try {
    if (file.endsWith(".gz")) return [];
    const stat = fs.statSync(file);
    fd = fs.openSync(file, "r");
    let position = stat.size;
    let newlineCount = 0;
    const chunks: string[] = [];
    while (position > 0 && newlineCount <= maxLines) {
      const readSize = Math.min(IMPORT_TAIL_CHUNK_BYTES, position);
      position -= readSize;
      const buffer = Buffer.allocUnsafe(readSize);
      const bytesRead = fs.readSync(fd, buffer, 0, readSize, position);
      const chunk = buffer.toString("utf8", 0, bytesRead);
      chunks.push(chunk);
      newlineCount += (chunk.match(/\n/g) || []).length;
    }
    const lines = chunks.reverse().join("").split(/\r?\n/).filter((line) => line.trim());
    return lines.length > maxLines ? lines.slice(-maxLines) : lines;
  } catch {
    return [];
  } finally {
    if (fd !== null) {
      try {
        fs.closeSync(fd);
      } catch {
        // ignore close failure during best-effort legacy import
      }
    }
  }
}

function maxLegacyAuditId(maxLines = 1000): number {
  let maxId = 0;
  for (const file of legacyLogFiles(false)) {
    const sourceFile = path.basename(file);
    for (const line of readLogLines(file, maxLines)) {
      const event = parseAuditLine(line, sourceFile);
      const id = Number(event?.id || 0);
      if (Number.isFinite(id) && id > maxId) maxId = id;
    }
  }
  return maxId;
}

function seedAuditSequence(database: any): void {
  if (sequenceSeeded) return;
  sequenceSeeded = true;
  try {
    const row = database.prepare("SELECT MAX(id) AS max_id FROM audit_events").get() as any;
    const minSeq = Math.max(Number(row?.max_id || 0), maxLegacyAuditId());
    if (!Number.isFinite(minSeq) || minSeq <= 0) return;
    const seqRow = database.prepare("SELECT seq FROM sqlite_sequence WHERE name = ?").get("audit_events") as any;
    const currentSeq = Number(seqRow?.seq || 0);
    if (currentSeq >= minSeq) return;
    if (seqRow) database.prepare("UPDATE sqlite_sequence SET seq = ? WHERE name = ?").run(Math.floor(minSeq), "audit_events");
    else database.prepare("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)").run("audit_events", Math.floor(minSeq));
  } catch {
    // Audit IDs remain best-effort if legacy probing fails.
  }
}

function sourceAlreadyImported(database: any, file: string): boolean {
  try {
    const stat = fs.statSync(file);
    const sourceFile = path.basename(file);
    const row = database.prepare("SELECT size_bytes, mtime_ms FROM audit_log_sources WHERE source_file = ?").get(sourceFile) as any;
    return !!row && Number(row.size_bytes || 0) === stat.size && Number(row.mtime_ms || 0) === Math.floor(stat.mtimeMs);
  } catch {
    return true;
  }
}

function markSourceImported(database: any, file: string, rowCount: number): void {
  try {
    const stat = fs.statSync(file);
    const sourceFile = path.basename(file);
    database
      .prepare(
        `INSERT INTO audit_log_sources (source_file, source_path, size_bytes, mtime_ms, imported_at, row_count)
         VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT(source_file) DO UPDATE SET
           source_path = excluded.source_path,
           size_bytes = excluded.size_bytes,
           mtime_ms = excluded.mtime_ms,
           imported_at = excluded.imported_at,
           row_count = excluded.row_count`
      )
      .run(sourceFile, file, stat.size, Math.floor(stat.mtimeMs), nowIso(), rowCount);
  } catch {
    // ignore source bookkeeping failures
  }
}

function legacyLogFiles(full = false): string[] {
  const roots = [logRoot(), legacyLogRoot()];
  const files: string[] = [];
  const seen = new Set<string>();
  const names = full ? FULL_LEGACY_LOG_NAMES : DEFAULT_LEGACY_LOG_NAMES;
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    for (const name of names) {
      const file = path.join(root, name);
      if (!fs.existsSync(file) || !fs.statSync(file).isFile()) continue;
      const key = path.basename(file);
      if (seen.has(key)) continue;
      seen.add(key);
      files.push(file);
    }
  }
  return files;
}

export function importLegacyAuditLogs(force = false, full = false): void {
  const database = db();
  for (const file of legacyLogFiles(full)) {
    if (!force && sourceAlreadyImported(database, file)) continue;
    const sourceFile = path.basename(file);
    const lines = readLogLines(file);
    let count = 0;
    database.exec("BEGIN IMMEDIATE");
    try {
      for (const line of lines) {
        const event = parseAuditLine(line, sourceFile);
        if (!event) continue;
        insertAuditEvent(database, event, Number(event.id || 0) > 0);
        count += 1;
      }
      markSourceImported(database, file, count);
      database.exec("COMMIT");
    } catch {
      try {
        database.exec("ROLLBACK");
      } catch {
        // ignore rollback failure
      }
    }
  }
}

export function ensureAuditReady(): void {
  db();
  if (!legacyImported) {
    importLegacyAuditLogs(false);
    legacyImported = true;
  }
}

export function auditEvent(args: AuditEventInput): number {
  const database = db();
  try {
    seedAuditSequence(database);
    const id = insertAuditEvent(
      database,
      {
        ts: nowIso(),
        op_type: args.op_type || "SYSTEM_READ",
        subsystem: args.subsystem || "typescript",
        func: args.func || "",
        dir: args.dir || "",
        file: args.file || "",
        file_path: args.file_path || "",
        content: args.content || "",
        extra: args.extra || {},
        source: "runtime",
        source_file: "sqlite",
        legacy_format: "sqlite"
      },
      false
    );
    return id;
  } catch {
    return 0;
  }
}

export function listAuditSources(): Array<{ name: string; size_bytes: number; updated_at: string; row_count: number; source: string }> {
  ensureAuditReady();
  const database = db();
  const total = database.prepare("SELECT COUNT(*) AS count, MAX(created_at) AS updated_at FROM audit_events").get() as any;
  const sources = database
    .prepare("SELECT source_file, size_bytes, imported_at, row_count FROM audit_log_sources ORDER BY imported_at DESC")
    .all() as any[];
  return [
    {
      name: "audit_events",
      size_bytes: fileSize(sqliteDbFile()),
      updated_at: String(total?.updated_at || nowIso()),
      row_count: Number(total?.count || 0),
      source: "sqlite"
    },
    ...sources.map((row) => ({
      name: String(row.source_file || ""),
      size_bytes: Number(row.size_bytes || 0),
      updated_at: String(row.imported_at || ""),
      row_count: Number(row.row_count || 0),
      source: "legacy_import"
    }))
  ];
}

export function readAuditEvents(limit = 300, sourceFile = "audit_events"): AuditEventRow[] {
  ensureAuditReady();
  const safeLimit = Math.max(20, Math.min(Number(limit) || 300, 2000));
  const safeSource = textOf(sourceFile || "audit_events");
  const database = db();
  const rows =
    safeSource && safeSource !== "audit_events"
      ? (database.prepare("SELECT * FROM audit_events WHERE source_file = ? ORDER BY id DESC LIMIT ?").all(safeSource, safeLimit) as any[])
      : (database.prepare("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?").all(safeLimit) as any[]);
  return rows.map(rowFromDb);
}

export function getAuditEventById(id: number): AuditEventRow | null {
  ensureAuditReady();
  const safeId = Math.floor(Number(id) || 0);
  if (safeId <= 0) return null;
  const database = db();
  const row = database.prepare("SELECT * FROM audit_events WHERE id = ?").get(safeId) as any;
  return row ? rowFromDb(row) : null;
}

export function auditEventToLegacyLine(row: AuditEventRow): string {
  const subsystem = row.dir || row.subsystem || "";
  const file = row.file || "";
  const filePath = row.file_path || "";
  return `[${row.id}] [${row.ts}] [${row.op_type}] [${row.func}] [${subsystem}] [${file}] [${filePath}] ${row.content}`;
}
