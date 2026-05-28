import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import fs from "node:fs";

const TINDA_HOME_ENV = "TINDA_HOME";

export function projectRoot(): string {
  const here = path.dirname(fileURLToPath(import.meta.url));
  return path.resolve(here, "../..");
}

function envHomeDir(): string {
  const rawHome = String(process.env.HOME || "").trim();
  const rawUser = String(process.env.USER || process.env.USERNAME || "").trim();
  if (rawHome) {
    const resolved = path.resolve(rawHome);
    if (rawUser && path.basename(resolved).toLowerCase() !== rawUser.toLowerCase()) {
      return path.join(resolved, rawUser);
    }
    return resolved;
  }
  return os.homedir();
}

export function runtimeRoot(): string {
  const raw = String(process.env[TINDA_HOME_ENV] || "").trim();
  return raw ? path.resolve(raw.replace(/^~(?=$|\/|\\)/, os.homedir())) : path.join(envHomeDir(), ".tinda", "agent");
}

export const dataRoot = () => path.join(runtimeRoot(), "Data");
export const logRoot = () => path.join(runtimeRoot(), "log");
export const sessionsRoot = () => path.join(dataRoot(), "Sessions");
export const systemRoot = () => path.join(dataRoot(), "System");
export const userRoot = () => path.join(runtimeRoot(), "user");
export const usersFile = () => path.join(userRoot(), "users.json");
export const memoryFile = () => path.join(systemRoot(), "memory.json");
export const versionsRoot = () => path.join(runtimeRoot(), "versions");
export const currentVersionFile = () => path.join(runtimeRoot(), "current.json");
export const legacyDataRoot = () => path.join(projectRoot(), "Data");
export const legacyLogRoot = () => path.join(projectRoot(), "log");
export const legacySessionsRoot = () => path.join(legacyDataRoot(), "Sessions");
export const legacyUsersFile = () => path.join(legacyDataRoot(), "User", "users.json");
export const webRoot = () => path.join(projectRoot(), "TindaAgent", "Web");

export function ensureRuntimeDirs(): void {
  [
    sessionsRoot(),
    path.join(sessionsRoot(), "messages"),
    path.join(sessionsRoot(), "plans"),
    path.join(sessionsRoot(), "exports"),
    systemRoot(),
    userRoot(),
    logRoot(),
    versionsRoot(),
    path.join(runtimeRoot(), "shared"),
    path.join(runtimeRoot(), "trust"),
    path.join(runtimeRoot(), "migrations")
  ].forEach((dir) => fs.mkdirSync(dir, { recursive: true }));
}

export function appVersion(): string {
  const pkgPath = path.join(projectRoot(), "package.json");
  try {
    const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
    return String(pkg.version || "0.0.0");
  } catch {
    return "0.0.0";
  }
}
