import path from "node:path";
import { runtimeRoot } from "../core/paths.js";
import { readJson, writeJson } from "../core/json.js";

export const CONTEXT_TOKEN_LIMIT_MIN = 16000;
export const CONTEXT_TOKEN_LIMIT_MAX = 200000;
export const CONTEXT_TOKEN_LIMIT_DEFAULT = 16000;

export const DEFAULT_WEB_SETTINGS = {
  stream_enabled: true,
  terminal_open: false,
  token_limit: CONTEXT_TOKEN_LIMIT_DEFAULT,
  quick_buttons: ["model", "stream", "terminal", "compress"],
  restore_last_session: false,
  last_session_id: "",
  title_model: "deepseek-v4-flash",
  compress_model: "deepseek-v4-flash",
  tavily_api_key: "",
  tavily_base_url: ""
};

function settingsPath(): string {
  return path.join(runtimeRoot(), "web-settings.json");
}

function terminalSettingsPath(): string {
  return path.join(runtimeRoot(), "terminal-settings.json");
}

export function normalizeContextTokenLimit(value: unknown): number {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  if (Number.isFinite(parsed) && parsed >= CONTEXT_TOKEN_LIMIT_MIN && parsed <= CONTEXT_TOKEN_LIMIT_MAX) return parsed;
  return CONTEXT_TOKEN_LIMIT_DEFAULT;
}

export function validateContextTokenLimit(value: unknown): [boolean, number, string] {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  if (!Number.isFinite(parsed)) {
    return [false, CONTEXT_TOKEN_LIMIT_DEFAULT, `上下文阈值必须是数字，范围为 ${CONTEXT_TOKEN_LIMIT_MIN} ~ ${CONTEXT_TOKEN_LIMIT_MAX}`];
  }
  if (parsed < CONTEXT_TOKEN_LIMIT_MIN || parsed > CONTEXT_TOKEN_LIMIT_MAX) {
    return [false, CONTEXT_TOKEN_LIMIT_DEFAULT, `上下文阈值范围为 ${CONTEXT_TOKEN_LIMIT_MIN} ~ ${CONTEXT_TOKEN_LIMIT_MAX}`];
  }
  return [true, parsed, ""];
}

export function loadWebSettings(): typeof DEFAULT_WEB_SETTINGS {
  const raw = readJson<Record<string, unknown>>(settingsPath(), {});
  const merged = { ...DEFAULT_WEB_SETTINGS, ...raw } as typeof DEFAULT_WEB_SETTINGS;
  merged.token_limit = normalizeContextTokenLimit(merged.token_limit);
  if (!Array.isArray(merged.quick_buttons)) merged.quick_buttons = [...DEFAULT_WEB_SETTINGS.quick_buttons];
  return merged;
}

export function saveWebSettings(data: Record<string, unknown>): typeof DEFAULT_WEB_SETTINGS {
  const merged = { ...loadWebSettings(), ...data };
  merged.token_limit = normalizeContextTokenLimit(merged.token_limit);
  const clean = Object.fromEntries(Object.keys(DEFAULT_WEB_SETTINGS).map((key) => [key, (merged as Record<string, unknown>)[key]]));
  writeJson(settingsPath(), clean);
  return loadWebSettings();
}

export function loadTerminalSettings() {
  const raw = readJson<Record<string, unknown>>(terminalSettingsPath(), {});
  return {
    ok: true,
    whitelist: Array.isArray(raw.whitelist) ? raw.whitelist.map(String) : [],
    blacklist: Array.isArray(raw.blacklist) ? raw.blacklist.map(String) : [],
    bypass_terminal_confirm: Boolean(raw.bypass_terminal_confirm)
  };
}

export function saveTerminalSettings(data: Record<string, unknown>) {
  const current = loadTerminalSettings();
  const next = {
    whitelist: Array.isArray(data.whitelist) ? data.whitelist.map(String).filter(Boolean) : current.whitelist,
    blacklist: Array.isArray(data.blacklist) ? data.blacklist.map(String).filter(Boolean) : current.blacklist,
    bypass_terminal_confirm:
      typeof data.bypass_terminal_confirm === "boolean" ? data.bypass_terminal_confirm : current.bypass_terminal_confirm
  };
  writeJson(terminalSettingsPath(), next);
  return loadTerminalSettings();
}

export function loadTavilySettings() {
  const settings = loadWebSettings();
  const key = String(process.env.TAVILY_API_KEY || settings.tavily_api_key || "").trim();
  const baseUrl = String(process.env.TAVILY_BASE_URL || settings.tavily_base_url || "https://api.tavily.com").trim();
  return {
    has_key: !!key,
    masked_key: key ? (key.length > 8 ? `${key.slice(0, 4)}***${key.slice(-4)}` : "***") : "",
    base_url: baseUrl
  };
}

export function saveTavilySettings(apiKey = "", baseUrl = "") {
  saveWebSettings({ tavily_api_key: String(apiKey || "").trim(), tavily_base_url: String(baseUrl || "").trim() });
  return loadTavilySettings();
}
