import fs from "node:fs";
import path from "node:path";
import { logRoot } from "./paths.js";
import { nowIso } from "./json.js";

let nextId = 1;
let initialized = false;

function initCounter(): void {
  if (initialized) return;
  initialized = true;
  const file = path.join(logRoot(), "total.jsonl");
  try {
    if (!fs.existsSync(file)) return;
    const lines = fs.readFileSync(file, "utf8").trim().split(/\r?\n/).slice(-200);
    for (const line of lines) {
      try {
        const row = JSON.parse(line);
        const id = Number(row?.id || row?.event?.id || 0);
        if (Number.isFinite(id)) nextId = Math.max(nextId, id + 1);
      } catch {
        // ignore malformed historical log rows
      }
    }
  } catch {
    // logging should never block request handling
  }
}

export function auditEvent(args: {
  op_type?: string;
  subsystem?: string;
  func?: string;
  file_path?: string;
  content?: string;
  extra?: Record<string, unknown>;
}): number {
  initCounter();
  const id = nextId++;
  const row = {
    id,
    ts: nowIso(),
    event: {
      id,
      op_type: args.op_type || "SYSTEM_READ",
      subsystem: args.subsystem || "typescript",
      func: args.func || "",
      file_path: args.file_path || "",
      content: args.content || "",
      extra: args.extra || {}
    }
  };
  try {
    const file = path.join(logRoot(), "total.jsonl");
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.appendFileSync(file, `${JSON.stringify(row)}\n`, "utf8");
  } catch {
    // best effort
  }
  return id;
}
