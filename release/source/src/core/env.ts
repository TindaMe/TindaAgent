import fs from "node:fs";
import path from "node:path";
import { config as dotenvConfig } from "dotenv";
import { projectRoot } from "./paths.js";

let loaded = false;

export interface LlmEnvConfig {
  provider: "deepseek" | "openai_compatible";
  apiKey: string;
  apiKeySource: "DEEPSEEK_API_KEY" | "OPENAI_API_KEY" | "missing";
  baseURL: string;
  baseURLSource: string;
  model: string;
}

export function loadRuntimeEnv(): void {
  if (loaded) return;
  const root = projectRoot();
  const files = [
    path.join(root, "TindaAgent", ".env"),
    path.join(root, ".env")
  ];
  for (const file of files) {
    if (fs.existsSync(file)) dotenvConfig({ path: file, quiet: true, override: true });
  }
  loaded = true;
}

export function maskSecret(value: string): string {
  const clean = String(value || "").trim();
  if (!clean) return "";
  if (clean.length <= 8) return "***";
  return `${clean.slice(0, 4)}***${clean.slice(-4)}`;
}

export function llmEnvConfig(): LlmEnvConfig {
  loadRuntimeEnv();
  const deepseekKey = String(process.env.DEEPSEEK_API_KEY || "").trim();
  const openaiKey = String(process.env.OPENAI_API_KEY || "").trim();
  if (deepseekKey) {
    return {
      provider: "deepseek",
      apiKey: deepseekKey,
      apiKeySource: "DEEPSEEK_API_KEY",
      baseURL: String(process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com").trim(),
      baseURLSource: process.env.DEEPSEEK_BASE_URL ? "DEEPSEEK_BASE_URL" : "default",
      model: String(process.env.DEEPSEEK_MODEL || "deepseek-v4-flash").trim()
    };
  }
  if (openaiKey) {
    return {
      provider: "openai_compatible",
      apiKey: openaiKey,
      apiKeySource: "OPENAI_API_KEY",
      baseURL: String(process.env.OPENAI_BASE_URL || process.env.DEEPSEEK_BASE_URL || "https://api.openai.com/v1").trim(),
      baseURLSource: process.env.OPENAI_BASE_URL ? "OPENAI_BASE_URL" : process.env.DEEPSEEK_BASE_URL ? "DEEPSEEK_BASE_URL" : "default",
      model: String(process.env.OPENAI_MODEL || process.env.DEEPSEEK_MODEL || "gpt-4o-mini").trim()
    };
  }
  return {
    provider: "deepseek",
    apiKey: "",
    apiKeySource: "missing",
    baseURL: String(process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com").trim(),
    baseURLSource: process.env.DEEPSEEK_BASE_URL ? "DEEPSEEK_BASE_URL" : "default",
    model: String(process.env.DEEPSEEK_MODEL || "deepseek-v4-flash").trim()
  };
}
