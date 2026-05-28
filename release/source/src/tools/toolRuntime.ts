import crypto from "node:crypto";
import { nowIso, safeId } from "../core/json.js";
import { submitShellCommand } from "./toolRegistry.js";

interface Job {
  job_id: string;
  session_id: string;
  command: string;
  status: "running" | "done";
  created_at: string;
  updated_at: string;
  result?: any;
}

interface EventRow {
  seq: number;
  job_id: string;
  session_id: string;
  kind: string;
  class?: string;
  content: string;
  ts: string;
}

export class ToolRuntimeManager {
  private jobs = new Map<string, Job>();
  private events = new Map<string, EventRow[]>();
  private seq = new Map<string, number>();

  submitCommand(sessionId: string, command: string, userPerm: number): Job {
    const sid = safeId(sessionId);
    const cmd = String(command || "").trim();
    if (!sid) throw new Error("session_id required");
    if (!cmd) throw new Error("command required");
    const shellCommand = cmd.startsWith("/") ? cmd.replace(/^\/(?:tool|terminal|run)?\s*/i, "").trim() || "help" : cmd;
    const job: Job = {
      job_id: `job_${crypto.randomBytes(8).toString("hex")}`,
      session_id: sid,
      command: shellCommand,
      status: "running",
      created_at: nowIso(),
      updated_at: nowIso()
    };
    this.jobs.set(`${sid}:${job.job_id}`, job);
    this.push(sid, job.job_id, "status", `执行命令: ${shellCommand}`, "dim");
    void submitShellCommand(shellCommand, undefined, (event) => {
      this.push(sid, job.job_id, String(event.kind || "out"), String(event.content || ""), String(event.class || ""));
    }).then((result) => {
      job.status = "done";
      job.updated_at = nowIso();
      job.result = result;
    });
    return job;
  }

  private push(sessionId: string, jobId: string, kind: string, content: string, cls = ""): void {
    const sid = safeId(sessionId);
    const next = (this.seq.get(sid) || 0) + 1;
    this.seq.set(sid, next);
    const row: EventRow = { seq: next, job_id: jobId, session_id: sid, kind, class: cls, content, ts: nowIso() };
    const bucket = this.events.get(sid) || [];
    bucket.push(row);
    this.events.set(sid, bucket.slice(-2000));
  }

  getEvents(sessionId: string, afterSeq = 0, limit = 200) {
    const sid = safeId(sessionId);
    const rows = (this.events.get(sid) || []).filter((e) => e.seq > Number(afterSeq || 0));
    const safeLimit = Math.max(1, Math.min(Number(limit) || 200, 500));
    const events = rows.slice(0, safeLimit);
    const nextSeq = events.length ? events[events.length - 1].seq : Number(afterSeq || 0);
    return { ok: true, session_id: sid, events, next_seq: nextSeq, total: this.events.get(sid)?.length || 0 };
  }

  getJob(sessionId: string, jobId: string): Job | null {
    return this.jobs.get(`${safeId(sessionId)}:${String(jobId || "").trim()}`) || null;
  }
}
