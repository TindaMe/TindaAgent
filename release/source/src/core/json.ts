import fs from "node:fs";
import path from "node:path";

export function readJson<T>(filePath: string, fallback: T): T {
  try {
    if (!fs.existsSync(filePath)) return fallback;
    const text = fs.readFileSync(filePath, "utf8");
    if (!text.trim()) return fallback;
    return JSON.parse(text) as T;
  } catch {
    return fallback;
  }
}

export function writeJson(filePath: string, payload: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(payload, null, 2), "utf8");
  fs.renameSync(tmp, filePath);
}

export function nowIso(): string {
  return new Date().toISOString();
}

export function safeId(raw: string, max = 80): string {
  return String(raw || "").trim().replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, max);
}

export function textOf(value: unknown): string {
  return value == null ? "" : String(value);
}
