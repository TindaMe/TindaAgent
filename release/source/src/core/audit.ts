import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
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
  source_path?: string;
  source_line?: number;
  raw_line?: string;
}

type ParsedAuditEvent = Omit<AuditEventRow, "created_at"> & { raw_json?: string };
type AuditSourceSummary = { name: string; size_bytes: number; updated_at: string; row_count: number; source: string };
type LegacyLogFileEntry = AuditSourceSummary & { source_path: string; mtime_ms: number };
type LogTail = { lines: string[]; truncated: boolean };

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

  CREATE TABLE IF NOT EXISTS audit_legacy_file_index (
    source_file TEXT PRIMARY KEY,
    source_path TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mtime_ms INTEGER NOT NULL DEFAULT 0,
    min_id INTEGER NOT NULL DEFAULT 0,
    max_id INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS audit_legacy_event_index (
    event_id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mtime_ms INTEGER NOT NULL DEFAULT 0,
    source_line INTEGER NOT NULL DEFAULT 0,
    raw_line TEXT NOT NULL DEFAULT '',
    indexed_at TEXT NOT NULL
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
const LOG_TAIL_MAX_BYTES = 2 * 1024 * 1024;
const LOG_HEAD_BYTES = 256 * 1024;
const LOG_RANGE_TAIL_LINES = 80;
const LEGACY_SOURCE_CACHE_MS = 5000;
const LEGACY_TEXT_RE = /^\[(\d+)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s*(.*)$/;
const SKIP_LEGACY_LOG_NAMES = new Set(["__init__.py", "id_counter.txt", "total.idx"]);
const HEAVY_NON_AUDIT_LOG_NAMES = new Set(["llm_request.jsonl", "llm_context.jsonl"]);

let schemaReady = false;
let legacyImported = false;
let sequenceSeeded = false;
let legacySourceCache: { expiresAt: number; files: LegacyLogFileEntry[] } | null = null;

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
    created_at: String(row.created_at || ""),
    source_path: sqliteDbFile(),
    source_line: Number(row.id || 0)
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

function splitLines(text: string): string[] {
  return text.split(/\r?\n/).filter((line) => line.trim());
}

function readPlainLogTail(file: string, maxLines: number): LogTail {
  let fd: number | null = null;
  try {
    const stat = fs.statSync(file);
    fd = fs.openSync(file, "r");
    let position = stat.size;
    let newlineCount = 0;
    let bytesReadTotal = 0;
    const chunks: Buffer[] = [];
    const maxBytes = Math.max(IMPORT_TAIL_CHUNK_BYTES, Math.min(LOG_TAIL_MAX_BYTES, maxLines * 32 * 1024));
    while (position > 0 && newlineCount <= maxLines && bytesReadTotal < maxBytes) {
      const readSize = Math.min(IMPORT_TAIL_CHUNK_BYTES, position, maxBytes - bytesReadTotal);
      position -= readSize;
      const buffer = Buffer.allocUnsafe(readSize);
      const bytesRead = fs.readSync(fd, buffer, 0, readSize, position);
      const chunk = Buffer.from(buffer.subarray(0, bytesRead));
      chunks.push(chunk);
      bytesReadTotal += bytesRead;
      for (const byte of chunk) {
        if (byte === 10) newlineCount += 1;
      }
    }
    const lines = splitLines(Buffer.concat(chunks.reverse()).toString("utf8"));
    return { lines: lines.length > maxLines ? lines.slice(-maxLines) : lines, truncated: position > 0 || lines.length > maxLines };
  } catch {
    return { lines: [], truncated: false };
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

function readGzipLogTail(file: string, maxLines: number): LogTail {
  try {
    const lines = splitLines(zlib.gunzipSync(fs.readFileSync(file)).toString("utf8"));
    return { lines: lines.length > maxLines ? lines.slice(-maxLines) : lines, truncated: lines.length > maxLines };
  } catch {
    return { lines: [], truncated: false };
  }
}

function readLogTail(file: string, maxLines = IMPORT_LINE_LIMIT): LogTail {
  return file.endsWith(".gz") ? readGzipLogTail(file, maxLines) : readPlainLogTail(file, maxLines);
}

function readLogLines(file: string, maxLines = IMPORT_LINE_LIMIT): string[] {
  return readLogTail(file, maxLines).lines;
}

function readPlainLogHeadLines(file: string, maxBytes = LOG_HEAD_BYTES): string[] {
  let fd: number | null = null;
  try {
    const stat = fs.statSync(file);
    if (stat.size <= 0) return [];
    const readSize = Math.min(maxBytes, stat.size);
    const buffer = Buffer.allocUnsafe(readSize);
    fd = fs.openSync(file, "r");
    const bytesRead = fs.readSync(fd, buffer, 0, readSize, 0);
    return splitLines(buffer.toString("utf8", 0, bytesRead));
  } catch {
    return [];
  } finally {
    if (fd !== null) {
      try {
        fs.closeSync(fd);
      } catch {
        // ignore close failure during best-effort legacy probing
      }
    }
  }
}

function readAllLogLines(file: string): string[] {
  try {
    const text = file.endsWith(".gz") ? zlib.gunzipSync(fs.readFileSync(file)).toString("utf8") : fs.readFileSync(file, "utf8");
    return splitLines(text);
  } catch {
    return [];
  }
}

function findLineById(file: string, sourceFile: string, id: number): { line: string; lineNumber: number } | null {
  if (file.endsWith(".gz")) {
    const lines = readAllLogLines(file);
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      if (!lineMayContainId(lines[index], id)) continue;
      const parsed = parseAuditLine(lines[index], sourceFile);
      if (Number(parsed?.id || 0) === id) return { line: lines[index], lineNumber: index + 1 };
    }
    return null;
  }
  let fd: number | null = null;
  try {
    const stat = fs.statSync(file);
    fd = fs.openSync(file, "r");
    let position = stat.size;
    let carry = "";
    while (position > 0) {
      const readSize = Math.min(IMPORT_TAIL_CHUNK_BYTES, position);
      position -= readSize;
      const buffer = Buffer.allocUnsafe(readSize);
      const bytesRead = fs.readSync(fd, buffer, 0, readSize, position);
      const text = buffer.toString("utf8", 0, bytesRead) + carry;
      const parts = text.split(/\r?\n/);
      carry = parts.shift() || "";
      for (let index = parts.length - 1; index >= 0; index -= 1) {
        const line = parts[index];
        if (!line.trim()) continue;
        if (!lineMayContainId(line, id)) continue;
        const parsed = parseAuditLine(line, sourceFile);
        if (Number(parsed?.id || 0) !== id) continue;
        return { line, lineNumber: 0 };
      }
    }
    if (carry.trim() && lineMayContainId(carry, id)) {
      const parsed = parseAuditLine(carry, sourceFile);
      if (Number(parsed?.id || 0) === id) return { line: carry, lineNumber: 0 };
    }
    return null;
  } catch {
    return null;
  } finally {
    if (fd !== null) {
      try {
        fs.closeSync(fd);
      } catch {
        // ignore close failure during best-effort legacy search
      }
    }
  }
}

function lineMayContainId(line: string, id: number): boolean {
  const text = line.trimStart();
  return text.startsWith(`[${id}]`) || text.includes(`"id": ${id}`) || text.includes(`"id":${id}`);
}

function parsedLineToRow(parsed: ParsedAuditEvent, sourceFile: string, sourcePath: string, sourceLine: number, rawLine: string): AuditEventRow {
  return {
    id: Number(parsed.id || 0),
    ts: parsed.ts || "",
    op_type: parsed.op_type || "",
    subsystem: parsed.subsystem || "",
    func: parsed.func || "",
    dir: parsed.dir || "",
    file: parsed.file || "",
    file_path: parsed.file_path || "",
    content: parsed.content || "",
    extra: parsed.extra || {},
    source: "legacy_file",
    source_file: sourceFile,
    legacy_format: parsed.legacy_format || "",
    created_at: "",
    source_path: sourcePath,
    source_line: sourceLine,
    raw_line: rawLine
  };
}

function auditIdsInLines(lines: string[], sourceFile: string): number[] {
  const ids: number[] = [];
  for (const line of lines) {
    const event = parseAuditLine(line, sourceFile);
    const id = Number(event?.id || 0);
    if (Number.isFinite(id) && id > 0) ids.push(id);
  }
  return ids;
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

function legacyLogRoots(): string[] {
  const roots: string[] = [];
  const seen = new Set<string>();
  for (const root of [logRoot(), legacyLogRoot()]) {
    const resolved = path.resolve(root);
    if (seen.has(resolved)) continue;
    seen.add(resolved);
    roots.push(root);
  }
  return roots;
}

function legacySourceName(file: string, root: string): string {
  const rel = path.relative(root, file).replace(/\\/g, "/");
  return rel && !rel.startsWith("..") ? rel : path.basename(file);
}

function findLegacyLogFile(name: string): string {
  if (!name || name === "audit_events") return "";
  const safeName = path.basename(name);
  if (safeName !== name) return "";
  for (const root of legacyLogRoots()) {
    const file = path.join(root, safeName);
    try {
      if (fs.statSync(file).isFile()) return file;
    } catch {
      // try next root
    }
  }
  return "";
}

function legacyLogFiles(full = false): string[] {
  const files: string[] = [];
  const seen = new Set<string>();
  const names = full ? FULL_LEGACY_LOG_NAMES : DEFAULT_LEGACY_LOG_NAMES;
  for (const root of legacyLogRoots()) {
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

function scanLegacyLogFiles(): LegacyLogFileEntry[] {
  const files: LegacyLogFileEntry[] = [];
  const seen = new Set<string>();
  for (const root of legacyLogRoots()) {
    if (!fs.existsSync(root)) continue;
    for (const name of fs.readdirSync(root)) {
      if (!name || name.startsWith(".") || SKIP_LEGACY_LOG_NAMES.has(name)) continue;
      const file = path.join(root, name);
      let stat: fs.Stats;
      try {
        stat = fs.statSync(file);
      } catch {
        continue;
      }
      if (!stat.isFile()) continue;
      const sourceName = legacySourceName(file, root);
      if (seen.has(sourceName)) continue;
      seen.add(sourceName);
      files.push({
        name: sourceName,
        source_path: file,
        size_bytes: stat.size,
        mtime_ms: Math.floor(stat.mtimeMs),
        updated_at: stat.mtime.toISOString(),
        row_count: 0,
        source: "legacy_file"
      });
    }
  }
  files.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  return files;
}

function listLegacyLogFiles(): LegacyLogFileEntry[] {
  const now = Date.now();
  if (legacySourceCache && legacySourceCache.expiresAt > now) return legacySourceCache.files;
  const files = scanLegacyLogFiles();
  legacySourceCache = { expiresAt: now + LEGACY_SOURCE_CACHE_MS, files };
  return files;
}

function legacyFilesChanged(database: any): boolean {
  try {
    for (const file of listLegacyLogFiles()) {
      if (file.source !== "legacy_file") continue;
      const row = database
        .prepare("SELECT size_bytes, mtime_ms FROM audit_legacy_file_index WHERE source_file = ?")
        .get(file.name) as any;
      if (!row || Number(row.size_bytes || 0) !== file.size_bytes || Number(row.mtime_ms || 0) !== file.mtime_ms) return true;
    }
    return false;
  } catch {
    return true;
  }
}

function refreshLegacyFileIndex(database: any, force = false): void {
  if (!force && !legacyFilesChanged(database)) return;
  const indexedAt = nowIso();
  database.exec("BEGIN IMMEDIATE");
  try {
    const seen = new Set<string>();
    for (const file of listLegacyLogFiles()) {
      seen.add(file.name);
      const existing = database
        .prepare("SELECT size_bytes, mtime_ms FROM audit_legacy_file_index WHERE source_file = ?")
        .get(file.name) as any;
      if (!force && existing && Number(existing.size_bytes || 0) === file.size_bytes && Number(existing.mtime_ms || 0) === file.mtime_ms) continue;
      database.prepare("DELETE FROM audit_legacy_event_index WHERE source_file = ?").run(file.name);
      let minId = 0;
      let maxId = 0;
      if (!HEAVY_NON_AUDIT_LOG_NAMES.has(file.name)) {
        const probeLines = file.name.endsWith(".gz")
          ? readAllLogLines(file.source_path)
          : [...readPlainLogHeadLines(file.source_path), ...readLogLines(file.source_path, LOG_RANGE_TAIL_LINES)];
        for (const id of auditIdsInLines(probeLines, file.name)) {
          minId = minId ? Math.min(minId, id) : id;
          maxId = Math.max(maxId, id);
        }
      }
      database
        .prepare(
          `INSERT INTO audit_legacy_file_index (source_file, source_path, size_bytes, mtime_ms, min_id, max_id, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_file) DO UPDATE SET
             source_path = excluded.source_path,
             size_bytes = excluded.size_bytes,
             mtime_ms = excluded.mtime_ms,
             min_id = excluded.min_id,
             max_id = excluded.max_id,
             indexed_at = excluded.indexed_at`
        )
        .run(file.name, file.source_path, file.size_bytes, file.mtime_ms, minId, maxId, indexedAt);
    }
    const rows = database.prepare("SELECT source_file FROM audit_legacy_file_index").all() as any[];
    for (const row of rows) {
      const name = String(row.source_file || "");
      if (!seen.has(name)) {
        database.prepare("DELETE FROM audit_legacy_file_index WHERE source_file = ?").run(name);
        database.prepare("DELETE FROM audit_legacy_event_index WHERE source_file = ?").run(name);
      }
    }
    database.exec("COMMIT");
  } catch {
    try {
      database.exec("ROLLBACK");
    } catch {
      // ignore rollback failure
    }
  }
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

export function listAuditSources(): AuditSourceSummary[] {
  ensureAuditReady();
  const database = db();
  const latest = database.prepare("SELECT id, created_at FROM audit_events ORDER BY id DESC LIMIT 1").get() as any;
  const sources = database
    .prepare("SELECT source_file, size_bytes, imported_at, row_count FROM audit_log_sources ORDER BY imported_at DESC")
    .all() as any[];
  const merged: AuditSourceSummary[] = [
    {
      name: "audit_events",
      size_bytes: fileSize(sqliteDbFile()),
      updated_at: String(latest?.created_at || nowIso()),
      row_count: Number(latest?.id || 0),
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
  const seen = new Set(merged.map((row) => row.name));
  for (const file of listLegacyLogFiles()) {
    if (seen.has(file.name)) continue;
    merged.push(file);
    seen.add(file.name);
  }
  return merged;
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

export function readAuditLogLines(sourceFile = "audit_events", limit = 300): { lines: string[]; truncated: boolean; source: string } {
  const safeSource = textOf(sourceFile || "audit_events");
  const safeLimit = Math.max(20, Math.min(Number(limit) || 300, 2000));
  if (!safeSource || safeSource === "audit_events") {
    const rows = readAuditEvents(safeLimit, "audit_events");
    return { lines: rows.reverse().map(auditEventToLegacyLine), truncated: false, source: "sqlite" };
  }
  const file = findLegacyLogFile(safeSource);
  if (file) {
    const tail = readLogTail(file, safeLimit);
    return { lines: tail.lines, truncated: tail.truncated, source: "legacy_file" };
  }
  const rows = readAuditEvents(safeLimit, safeSource);
  return { lines: rows.reverse().map(auditEventToLegacyLine), truncated: false, source: "sqlite" };
}

export function getAuditEventById(id: number): AuditEventRow | null {
  ensureAuditReady();
  const safeId = Math.floor(Number(id) || 0);
  if (safeId <= 0) return null;
  const database = db();
  const row = database.prepare("SELECT * FROM audit_events WHERE id = ?").get(safeId) as any;
  return row ? rowFromDb(row) : null;
}

export function getAuditLogEventById(id: number): AuditEventRow | null {
  const row = getAuditEventById(id);
  if (row) return row;
  const safeId = Math.floor(Number(id) || 0);
  if (safeId <= 0) return null;
  const database = db();
  refreshLegacyFileIndex(database);
  const cached = database.prepare("SELECT * FROM audit_legacy_event_index WHERE event_id = ?").get(safeId) as any;
  if (cached?.raw_line) {
    const parsed = parseAuditLine(String(cached.raw_line), String(cached.source_file || ""));
    if (parsed) return parsedLineToRow(parsed, String(cached.source_file || ""), String(cached.source_path || ""), Number(cached.source_line || 0), String(cached.raw_line || ""));
  }
  const candidates = database
    .prepare(
      `SELECT source_file, source_path, size_bytes, mtime_ms, min_id, max_id
       FROM audit_legacy_file_index
       WHERE min_id > 0 AND max_id >= ? AND min_id <= ?
       ORDER BY max_id DESC`
    )
    .all(safeId, safeId) as any[];
  for (const file of candidates) {
    const sourceFile = String(file.source_file || "");
    const sourcePath = String(file.source_path || findLegacyLogFile(sourceFile));
    if (!sourcePath || HEAVY_NON_AUDIT_LOG_NAMES.has(sourceFile)) continue;
    const found = findLineById(sourcePath, sourceFile, safeId);
    if (!found) continue;
    const parsed = parseAuditLine(found.line, sourceFile);
    if (!parsed || Number(parsed.id || 0) !== safeId) continue;
    database
      .prepare(
        `INSERT INTO audit_legacy_event_index (event_id, source_file, source_path, size_bytes, mtime_ms, source_line, raw_line, indexed_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(event_id) DO UPDATE SET
           source_file = excluded.source_file,
           source_path = excluded.source_path,
           size_bytes = excluded.size_bytes,
           mtime_ms = excluded.mtime_ms,
           source_line = excluded.source_line,
           raw_line = excluded.raw_line,
           indexed_at = excluded.indexed_at`
      )
      .run(safeId, sourceFile, sourcePath, Number(file.size_bytes || 0), Number(file.mtime_ms || 0), found.lineNumber, found.line, nowIso());
    return parsedLineToRow(parsed, sourceFile, sourcePath, found.lineNumber, found.line);
  }
  return null;
}

export function auditEventToLegacyLine(row: AuditEventRow): string {
  const subsystem = row.dir || row.subsystem || "";
  const file = row.file || "";
  const filePath = row.file_path || "";
  return `[${row.id}] [${row.ts}] [${row.op_type}] [${row.func}] [${subsystem}] [${file}] [${filePath}] ${row.content}`;
}
