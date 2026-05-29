import fs from "node:fs";
import path from "node:path";
import { dataRoot } from "../core/paths.js";
import { nowIso, readJson, safeId, textOf, writeJson } from "../core/json.js";

export interface DeepRound {
  revision: string;
  alignment_text: string;
  ask_question?: string;
  ask_answer?: string;
  created_at: string;
  updated_at: string;
}

export interface DeepState {
  active: boolean;
  state: "idle" | "waiting_confirm" | "waiting_question" | "confirmed" | "cancelled";
  original_message: string;
  file_names: string[];
  file_contents: string[];
  rounds: DeepRound[];
  active_index: number;
  pending_deep_ask: Record<string, any> | null;
  updated_at: string;
}

function rootDir(): string {
  return path.join(dataRoot(), "DeepAlignment");
}

function statePath(sessionId: string): string {
  const sid = safeId(sessionId);
  if (!sid) throw new Error("session_id invalid");
  return path.join(rootDir(), `${sid}.json`);
}

function questionFromMessage(message: string): string {
  const lower = message.toLowerCase();
  if (/哪|哪个|选择|还是|是否|要不要|\bor\b|\bwhich\b|\bchoose\b/.test(lower)) {
    return "请确认你希望我按哪个方向继续？";
  }
  return "请补充一个关键约束或确认是否按当前理解继续。";
}

function buildAlignmentText(message: string, files: string[], revision = "", answer = ""): string {
  const chunks = [
    "我理解你的目标是：",
    message ? `- ${message}` : "- 处理你附带的输入内容。",
    "",
    "关键约束：",
    files.length ? `- 已包含附件：${files.filter(Boolean).join("、")}` : "- 未提供附件。",
    revision ? `- 修正要求：${revision}` : "- 按当前输入直接推进。",
    answer ? `- 你的补充回答：${answer}` : "",
    "",
    "确认后我会基于以上理解继续执行，不会在 Deep 对齐阶段直接改动任务结果。"
  ].filter(Boolean);
  return chunks.join("\n");
}

function normalizeState(sessionId: string, raw: any): DeepState | null {
  if (!raw || typeof raw !== "object") return null;
  const rounds = Array.isArray(raw.rounds)
    ? raw.rounds
        .filter((row: any) => row && typeof row === "object")
        .map((row: any) => ({
          revision: textOf(row.revision),
          alignment_text: textOf(row.alignment_text),
          ask_question: row.ask_question ? textOf(row.ask_question) : undefined,
          ask_answer: row.ask_answer ? textOf(row.ask_answer) : undefined,
          created_at: textOf(row.created_at || nowIso()),
          updated_at: textOf(row.updated_at || row.created_at || nowIso())
        }))
    : [];
  const latest = Math.max(0, rounds.length - 1);
  return {
    active: Boolean(raw.active),
    state: textOf(raw.state || "idle") as DeepState["state"],
    original_message: textOf(raw.original_message),
    file_names: Array.isArray(raw.file_names) ? raw.file_names.map(textOf) : [],
    file_contents: Array.isArray(raw.file_contents) ? raw.file_contents.map(textOf) : [],
    rounds,
    active_index: Math.max(0, Math.min(latest, Number(raw.active_index ?? latest) || 0)),
    pending_deep_ask: raw.pending_deep_ask && typeof raw.pending_deep_ask === "object" ? raw.pending_deep_ask : null,
    updated_at: textOf(raw.updated_at || nowIso())
  };
}

export function loadDeepState(sessionId: string): DeepState | null {
  return normalizeState(sessionId, readJson<Record<string, any>>(statePath(sessionId), {}));
}

export function saveDeepState(sessionId: string, state: DeepState): DeepState {
  fs.mkdirSync(rootDir(), { recursive: true });
  const next = { ...state, updated_at: nowIso() };
  writeJson(statePath(sessionId), next);
  return next;
}

export function deleteDeepState(sessionId: string): void {
  try {
    fs.rmSync(statePath(sessionId), { force: true });
  } catch {
    // ignore
  }
}

export function deepPublicPayload(sessionId: string): Record<string, any> {
  const sid = safeId(sessionId);
  const state = sid ? loadDeepState(sid) : null;
  if (!state) return { ok: true, session_id: sid, active: false, state: "idle", rounds: [], active_index: 0, pending_deep_ask: null };
  return {
    ok: true,
    session_id: sid,
    active: state.active,
    state: state.state,
    message: state.original_message,
    original_message: state.original_message,
    file_names: state.file_names,
    file_contents: state.file_contents,
    rounds: state.rounds,
    active_index: state.active_index,
    pending_deep_ask: state.pending_deep_ask,
    alignment_text: state.rounds[state.active_index]?.alignment_text || "",
    updated_at: state.updated_at
  };
}

export function startDeepState(sessionId: string, input: { message: string; file_names?: string[]; file_contents?: string[]; revision?: string; alignment_text?: string }): DeepState {
  const sid = safeId(sessionId);
  if (!sid) throw new Error("session_id invalid");
  const message = textOf(input.message).trim();
  const fileNames = Array.isArray(input.file_names) ? input.file_names.map(textOf) : [];
  const fileContents = Array.isArray(input.file_contents) ? input.file_contents.map(textOf) : [];
  const revision = textOf(input.revision).trim();
  const previous = loadDeepState(sid);
  const rounds = previous && previous.original_message === message ? [...previous.rounds] : [];
  const created = nowIso();
  rounds.push({
    revision,
    alignment_text: textOf(input.alignment_text).trim() || buildAlignmentText(message, fileNames, revision),
    created_at: created,
    updated_at: created
  });
  return saveDeepState(sid, {
    active: true,
    state: "waiting_confirm",
    original_message: message,
    file_names: fileNames,
    file_contents: fileContents,
    rounds,
    active_index: Math.max(0, rounds.length - 1),
    pending_deep_ask: null,
    updated_at: created
  });
}

export function createDeepQuestion(sessionId: string): Record<string, any> {
  const state = loadDeepState(sessionId);
  if (!state) throw new Error("no active deep alignment");
  const callId = `deep_${Date.now().toString(36)}`;
  const question = questionFromMessage(state.original_message);
  const pending = {
    flow: "deep_alignment",
    kind: "question",
    call_id: callId,
    confirm_id: callId,
    question,
    options: [
      { label: "按当前理解继续", value: "continue" },
      { label: "我补充说明", value: "custom" }
    ],
    allow_custom: true,
    placeholder: "补充你的答案或限制条件..."
  };
  state.active = true;
  state.state = "waiting_question";
  state.pending_deep_ask = pending;
  saveDeepState(sessionId, state);
  return pending;
}

export function answerDeepQuestion(sessionId: string, answer: string, choice = ""): DeepState {
  const state = loadDeepState(sessionId);
  if (!state) throw new Error("no active deep alignment");
  const answerText = textOf(answer || choice || "按当前理解继续").trim();
  const question = textOf(state.pending_deep_ask?.question || questionFromMessage(state.original_message));
  const now = nowIso();
  const rounds = [...state.rounds];
  rounds.push({
    revision: "",
    alignment_text: buildAlignmentText(state.original_message, state.file_names, "", answerText),
    ask_question: question,
    ask_answer: answerText,
    created_at: now,
    updated_at: now
  });
  return saveDeepState(sessionId, {
    ...state,
    active: true,
    state: "waiting_confirm",
    rounds,
    active_index: Math.max(0, rounds.length - 1),
    pending_deep_ask: null,
    updated_at: now
  });
}
