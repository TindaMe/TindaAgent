import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { dataRoot, logRoot, projectRoot, runtimeRoot, sqliteDbFile, systemRoot } from "../core/paths.js";
import { nowIso } from "../core/json.js";
import { appDb, kvGet, kvSet } from "../core/sqlite.js";

type Database = any;

type UsageRow = {
  ts?: string;
  method?: string;
  path?: string;
  status?: number;
};

type HomeUsageInput = {
  method: string;
  path: string;
  status: number;
  durationMs: number;
  uid?: string;
};

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const MAX_RUNTIME_USAGE_ROWS = 100000;
const LEGACY_USAGE_SOURCE = "legacy_home_usage";
const LEGACY_USAGE_MARKER = "legacy_home_usage_marker";
const WEB_USAGE_PRUNE_EVERY = 500;

let schemaReady = false;
let pruneTick = 0;
let legacySessionCache: { expires: number; times: number[] } | null = null;

function ensureHomeStatsSchema(): Database {
  const db = appDb();
  if (schemaReady) return db;
  db.exec(`
    CREATE TABLE IF NOT EXISTS web_usage (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      method TEXT NOT NULL DEFAULT '',
      path TEXT NOT NULL DEFAULT '',
      status INTEGER NOT NULL DEFAULT 0,
      duration_ms INTEGER NOT NULL DEFAULT 0,
      uid TEXT NOT NULL DEFAULT '',
      source TEXT NOT NULL DEFAULT 'runtime',
      source_key TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_web_usage_ts ON web_usage(ts);
    CREATE INDEX IF NOT EXISTS idx_web_usage_source ON web_usage(source, source_key);
    CREATE INDEX IF NOT EXISTS idx_web_usage_path ON web_usage(path);
  `);
  schemaReady = true;
  return db;
}

function cleanRequestPath(input: string): string {
  const raw = String(input || "").trim();
  if (!raw) return "";
  return raw.split("?")[0] || "/";
}

function shouldSkipUsagePath(pathOnly: string): boolean {
  if (!pathOnly) return true;
  if (pathOnly === "/favicon.ico" || pathOnly === "/home/stats") return true;
  if (pathOnly.startsWith("/assets/") || pathOnly.startsWith("/static/")) return true;
  if (pathOnly.startsWith("/chat-runtime/")) return true;
  return false;
}

function maybePruneRuntimeUsageRows(db: Database): void {
  pruneTick += 1;
  if (pruneTick % WEB_USAGE_PRUNE_EVERY !== 0) return;
  db.prepare(
    `DELETE FROM web_usage
     WHERE source = 'runtime'
       AND id NOT IN (
         SELECT id FROM web_usage WHERE source = 'runtime' ORDER BY id DESC LIMIT ?
       )`
  ).run(MAX_RUNTIME_USAGE_ROWS);
}

export function recordHomeUsage(input: HomeUsageInput): void {
  try {
    const pathOnly = cleanRequestPath(input.path);
    if (shouldSkipUsagePath(pathOnly)) return;
    const db = ensureHomeStatsSchema();
    db.prepare(
      `INSERT INTO web_usage (ts, method, path, status, duration_ms, uid, source, source_key, created_at)
       VALUES (?, ?, ?, ?, ?, ?, 'runtime', '', ?)`
    ).run(
      nowIso(),
      String(input.method || "").toUpperCase(),
      pathOnly,
      Math.max(0, Number(input.status) || 0),
      Math.max(0, Number(input.durationMs) || 0),
      String(input.uid || ""),
      nowIso()
    );
    maybePruneRuntimeUsageRows(db);
  } catch {
    // HOME telemetry must never affect request handling.
  }
}

function parseTimestamp(value: unknown): number | null {
  const text = String(value || "").trim();
  if (!text) return null;
  const ms = new Date(text).getTime();
  return Number.isFinite(ms) ? ms : null;
}

function monthKey(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function dayKey(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function normalizeMonth(input: unknown): string {
  const now = new Date();
  const fallback = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  const raw = String(input || fallback).trim();
  const m = /^(\d{4})-(\d{2})$/.exec(raw);
  if (!m) return fallback;
  const year = Number(m[1]);
  const month = Number(m[2]);
  if (!Number.isFinite(year) || year < 1970 || month < 1 || month > 12) return fallback;
  return raw;
}

function safeRows(db: Database, sql: string): any[] {
  try {
    return db.prepare(sql).all() as any[];
  } catch {
    return [];
  }
}

function readLegacyUsageRows(file: string): UsageRow[] {
  try {
    if (!fs.existsSync(file)) return [];
    const text = fs.readFileSync(file, "utf8");
    const rows: UsageRow[] = [];
    let lineNo = 0;
    for (const line of text.split(/\r?\n/)) {
      lineNo += 1;
      const clean = line.trim();
      if (!clean) continue;
      try {
        const row = JSON.parse(clean) as UsageRow;
        if (!parseTimestamp(row.ts)) continue;
        rows.push({
          ts: String(row.ts || ""),
          method: String(row.method || ""),
          path: cleanRequestPath(String(row.path || "")) || "/",
          status: Number(row.status || 0),
          source_key: String(lineNo)
        } as UsageRow & { source_key: string });
      } catch {
        // ignore malformed historical rows
      }
    }
    return rows;
  } catch {
    return [];
  }
}

function importLegacyHomeUsage(db: Database): void {
  const file = path.join(systemRoot(), "home_usage.jsonl");
  try {
    if (!fs.existsSync(file)) return;
    const stat = fs.statSync(file);
    const marker = `${stat.size}:${Math.round(stat.mtimeMs)}`;
    if (kvGet("home_stats", LEGACY_USAGE_MARKER, "") === marker) return;

    const rows = readLegacyUsageRows(file);
    const insert = db.prepare(
      `INSERT INTO web_usage (ts, method, path, status, duration_ms, uid, source, source_key, created_at)
       VALUES (?, ?, ?, ?, 0, '', ?, ?, ?)`
    );
    db.exec("BEGIN IMMEDIATE");
    try {
      db.prepare("DELETE FROM web_usage WHERE source = ?").run(LEGACY_USAGE_SOURCE);
      rows.forEach((row: UsageRow & { source_key?: string }) => {
        insert.run(
          String(row.ts || ""),
          String(row.method || "").toUpperCase(),
          cleanRequestPath(String(row.path || "")) || "/",
          Math.max(0, Number(row.status) || 0),
          LEGACY_USAGE_SOURCE,
          String(row.source_key || ""),
          nowIso()
        );
      });
      db.exec("COMMIT");
    } catch (error) {
      try {
        db.exec("ROLLBACK");
      } catch {
        // ignore rollback failure
      }
      throw error;
    }
    kvSet("home_stats", LEGACY_USAGE_MARKER, marker);
  } catch {
    // legacy import is best effort; live stats still work without it.
  }
}

function appendTableTimes(db: Database, target: number[], table: string, columns: string[]): void {
  for (const column of columns) {
    const rows = safeRows(db, `SELECT ${column} AS ts FROM ${table} WHERE ${column} IS NOT NULL AND ${column} != '' LIMIT 200000`);
    rows.forEach((row) => {
      const ms = parseTimestamp(row?.ts);
      if (ms !== null) target.push(ms);
    });
  }
}

function legacySessionMessageTimes(): number[] {
  const now = Date.now();
  if (legacySessionCache && legacySessionCache.expires > now) return legacySessionCache.times;
  const times: number[] = [];
  const dir = path.join(dataRoot(), "Sessions", "messages");
  try {
    const files = fs.existsSync(dir)
      ? fs.readdirSync(dir).filter((file) => file.endsWith(".json")).slice(0, 250)
      : [];
    for (const file of files) {
      try {
        const raw = fs.readFileSync(path.join(dir, file), "utf8");
        const parsed = JSON.parse(raw);
        const rows = Array.isArray(parsed) ? parsed : Object.values(parsed || {});
        for (const row of rows.slice(0, 5000) as any[]) {
          const ms = parseTimestamp(row?.created_at || row?.updated_at || row?.ts || row?.time);
          if (ms !== null) times.push(ms);
        }
      } catch {
        // ignore old or malformed session files
      }
    }
  } catch {
    // ignore
  }
  legacySessionCache = { expires: now + 60000, times };
  return times;
}

function collectUsageTimes(db: Database): number[] {
  const times: number[] = [];
  const webRows = safeRows(db, "SELECT ts FROM web_usage WHERE ts IS NOT NULL AND ts != '' ORDER BY ts DESC LIMIT 200000");
  webRows.forEach((row) => {
    const ms = parseTimestamp(row?.ts);
    if (ms !== null) times.push(ms);
  });
  appendTableTimes(db, times, "sessions", ["created_at", "updated_at"]);
  appendTableTimes(db, times, "session_messages", ["created_at", "updated_at"]);
  appendTableTimes(db, times, "session_terminal", ["ts"]);
  appendTableTimes(db, times, "llm_requests", ["ts"]);
  times.push(...legacySessionMessageTimes());
  return times;
}

function buildUsage(month: string, times: number[]) {
  const [year, mon] = month.split("-").map(Number);
  const daysInMonth = new Date(year, mon, 0).getDate();
  const months = new Set<string>([month]);
  const dayCounts = new Map<string, number>();
  let total = 0;

  times.forEach((ms) => {
    const mk = monthKey(ms);
    months.add(mk);
    if (mk !== month) return;
    const dk = dayKey(ms);
    dayCounts.set(dk, (dayCounts.get(dk) || 0) + 1);
    total += 1;
  });

  const maxDay = Math.max(0, ...Array.from(dayCounts.values()));
  const days = Array.from({ length: daysInMonth }, (_, i) => {
    const date = `${year}-${String(mon).padStart(2, "0")}-${String(i + 1).padStart(2, "0")}`;
    const count = dayCounts.get(date) || 0;
    const level = count <= 0 || maxDay <= 0 ? 0 : Math.max(1, Math.min(4, Math.ceil((count / maxDay) * 4)));
    return { date, count, level };
  });

  const now = Date.now();
  const start = now - DAY_MS;
  const bucketMs = 3 * HOUR_MS;
  const bucketCounts = Array.from({ length: 8 }, () => 0);
  times.forEach((ms) => {
    if (ms < start || ms > now) return;
    const idx = Math.max(0, Math.min(7, Math.floor((ms - start) / bucketMs)));
    bucketCounts[idx] += 1;
  });
  const maxBucket = Math.max(1, ...bucketCounts);
  const last24h = bucketCounts.map((count, i) => {
    const d = new Date(start + i * bucketMs);
    return {
      label: `${String(d.getHours()).padStart(2, "0")}:00`,
      count,
      percent: count <= 0 ? 0 : (count / maxBucket) * 100
    };
  });

  const sortedMonths = Array.from(months).sort();
  return {
    month,
    months: sortedMonths,
    days,
    last24h,
    total_count: total,
    total_events: times.length
  };
}

function percent(used: number, total: number): number {
  return total > 0 ? Math.max(0, Math.min(100, (used / total) * 100)) : 0;
}

function memoryStats() {
  const total = os.totalmem();
  const free = os.freemem();
  const used = Math.max(0, total - free);
  const proc = process.memoryUsage();
  return {
    memory: {
      total,
      free,
      used,
      percent: percent(used, total),
      source: `${os.platform()} ${os.release()}`
    },
    process_memory: {
      rss: proc.rss,
      heap_used: proc.heapUsed,
      heap_total: proc.heapTotal,
      external: proc.external,
      array_buffers: proc.arrayBuffers,
      percent_of_system: percent(proc.rss, total)
    }
  };
}

function firstExistingPath(target: string): string {
  let current = path.resolve(target);
  while (!fs.existsSync(current)) {
    const parent = path.dirname(current);
    if (parent === current) return current;
    current = parent;
  }
  return current;
}

function mountRootFor(target: string): string {
  let current = firstExistingPath(target);
  try {
    let currentDev = fs.statSync(current).dev;
    while (true) {
      const parent = path.dirname(current);
      if (parent === current) return current;
      const parentStat = fs.statSync(parent);
      if (parentStat.dev !== currentDev) return current;
      current = parent;
      currentDev = parentStat.dev;
    }
  } catch {
    return path.parse(current).root || current;
  }
}

function volumeStats(target: string, label: string, isRuntime = false) {
  try {
    const existing = firstExistingPath(target);
    const root = mountRootFor(existing);
    const stat = fs.statfsSync(existing);
    const total = Number(stat.blocks) * Number(stat.bsize);
    const free = Number(stat.bavail) * Number(stat.bsize);
    const used = Math.max(0, total - free);
    return {
      root,
      label,
      path: target,
      total,
      free,
      used,
      percent: percent(used, total),
      is_runtime: isRuntime
    };
  } catch {
    return null;
  }
}

function fileSize(file: string): number {
  try {
    return fs.statSync(file).size;
  } catch {
    return 0;
  }
}

function buildStorages() {
  const candidates = [
    volumeStats(runtimeRoot(), "运行数据", true),
    volumeStats(projectRoot(), "项目源码", false),
    volumeStats(os.homedir(), "HOME", false)
  ].filter(Boolean) as NonNullable<ReturnType<typeof volumeStats>>[];

  const byRoot = new Map<string, NonNullable<ReturnType<typeof volumeStats>>>();
  candidates.forEach((disk) => {
    const existing = byRoot.get(disk.root);
    if (!existing || disk.is_runtime) {
      byRoot.set(disk.root, disk);
    }
  });
  const storages = Array.from(byRoot.values());
  const runtimeDisk = storages.find((disk) => disk.is_runtime) || storages[0] || null;
  if (runtimeDisk) {
    (runtimeDisk as any).database_bytes = fileSize(sqliteDbFile());
    (runtimeDisk as any).home_usage_bytes = fileSize(path.join(systemRoot(), "home_usage.jsonl"));
    (runtimeDisk as any).log_total_jsonl_bytes = fileSize(path.join(logRoot(), "total.jsonl"));
  }
  return { storage: runtimeDisk, storages };
}

export function buildHomeStats(monthInput?: unknown) {
  const db = ensureHomeStatsSchema();
  importLegacyHomeUsage(db);
  const month = normalizeMonth(monthInput);
  const times = collectUsageTimes(db);
  const usage = buildUsage(month, times);
  const mem = memoryStats();
  const storage = buildStorages();
  const load = os.loadavg();
  const uptimeSeconds = Math.round(process.uptime());
  const startedAt = new Date(Date.now() - process.uptime() * 1000).toISOString();
  const systemTime = nowIso();

  return {
    ok: true,
    system_time: systemTime,
    started_at: startedAt,
    uptime_seconds: uptimeSeconds,
    load_average: {
      one: load[0] || 0,
      five: load[1] || 0,
      fifteen: load[2] || 0
    },
    usage,
    month: usage.month,
    current_month: usage.month,
    months: usage.months,
    days: usage.days,
    usage_24h: usage.last24h,
    runtime: {
      app_started_at: startedAt,
      started_at: startedAt,
      pid: process.pid,
      node: process.version,
      uptime_sec: uptimeSeconds,
      uptime_seconds: uptimeSeconds,
      system_time: systemTime,
      platform: process.platform,
      arch: process.arch
    },
    ...mem,
    ...storage
  };
}
