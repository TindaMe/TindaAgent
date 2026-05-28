import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { legacyUsersFile, usersFile } from "./paths.js";
import { readJson, writeJson } from "./json.js";
import { USER_ADMIN, USER_VISITOR, permLabel } from "./permissions.js";
import { auditEvent } from "./audit.js";

export interface User {
  uid: string;
  name: string;
  perm: number;
  token: string;
}

interface UsersPayload {
  next_uid?: number;
  users?: User[];
}

let registry: User[] = [];
let loaded = false;

function normalizeUid(uid: string | number | undefined): string {
  const text = String(uid || "").trim();
  if (!text) return "";
  const n = Number.parseInt(text, 10);
  return Number.isFinite(n) ? String(Math.max(1, n)).padStart(10, "0") : text;
}

function token(): string {
  return crypto.randomBytes(32).toString("hex");
}

function migrateUsersFileIfNeeded(): void {
  const target = usersFile();
  if (fs.existsSync(target)) return;
  const legacy = legacyUsersFile();
  if (!fs.existsSync(legacy)) return;
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.copyFileSync(legacy, target);
}

function persist(): void {
  const maxUid = registry.reduce((max, u) => Math.max(max, Number.parseInt(u.uid, 10) || 0), 0);
  writeJson(usersFile(), { next_uid: maxUid + 1, users: registry });
}

export function loadUsers(): User[] {
  if (loaded) return registry;
  loaded = true;
  migrateUsersFileIfNeeded();
  const payload = readJson<UsersPayload>(usersFile(), { users: [] });
  registry = Array.isArray(payload.users)
    ? payload.users
        .filter((u) => u && String(u.name || "").trim())
        .map((u) => ({
          uid: normalizeUid(u.uid),
          name: String(u.name || "").trim(),
          perm: Number(u.perm ?? USER_VISITOR) || 0,
          token: String(u.token || "").trim() || token()
        }))
    : [];
  ensureSeedUsers();
  return registry;
}

function ensureSeedUsers(): void {
  let changed = false;
  if (!registry.some((u) => u.name === "Tinda")) {
    registry.push({ uid: nextUid(), name: "Tinda", perm: USER_ADMIN, token: token() });
    changed = true;
  }
  if (!registry.some((u) => Number(u.perm) === 0)) {
    registry.push({ uid: nextUid(), name: "guest0", perm: 0, token: token() });
    changed = true;
  }
  if (changed) persist();
}

function nextUid(): string {
  const maxUid = registry.reduce((max, u) => Math.max(max, Number.parseInt(u.uid, 10) || 0), 0);
  return String(maxUid + 1).padStart(10, "0");
}

export function iterUsers(): User[] {
  return [...loadUsers()];
}

export function isSystemUser(user: User | null | undefined): boolean {
  return !!user && String(user.name || "").startsWith("web-bot-");
}

export function getUserFromToken(rawToken: string | undefined): User | null {
  const needle = String(rawToken || "").trim();
  if (!needle) return null;
  return loadUsers().find((u) => u.token === needle && !isSystemUser(u)) || null;
}

export function getUserFromUid(uid: string): User | null {
  const key = normalizeUid(uid);
  return loadUsers().find((u) => u.uid === key && !isSystemUser(u)) || null;
}

export function publicUser(user: User | null | undefined, currentUid = "") {
  if (!user) return null;
  return {
    uid: user.uid,
    name: user.name,
    perm: Number(user.perm) || 0,
    perm_label: permLabel(Number(user.perm) || 0),
    is_current: !!currentUid && user.uid === currentUid
  };
}

export function profilePayload(user: User | null | undefined) {
  if (!user) return { name: "", uid: "", perm: 0, perm_label: "NONE" };
  return {
    name: user.name,
    uid: user.uid,
    perm: Number(user.perm) || 0,
    perm_label: permLabel(Number(user.perm) || 0)
  };
}

export function maskToken(value: string): string {
  const text = String(value || "");
  if (!text) return "";
  if (text.length <= 12) return "*".repeat(text.length);
  return `${text.slice(0, 6)}...${text.slice(-4)}`;
}

export function createUser(name: string, perm: number, userToken = ""): User {
  const cleanName = String(name || "").trim();
  if (!cleanName) throw new Error("用户名不能为空");
  if (loadUsers().some((u) => u.name === cleanName)) throw new Error("用户名已存在");
  const user = { uid: nextUid(), name: cleanName, perm: Number(perm) || 0, token: String(userToken || "").trim() || token() };
  registry.push(user);
  persist();
  auditEvent({ op_type: "SYSTEM_WRITE", subsystem: "user", func: "createUser", content: `create_user_done uid=${user.uid}` });
  return user;
}

export function updateUser(uid: string, patch: { name?: string; perm?: number; token?: string }): User | null {
  const user = getUserFromUid(uid);
  if (!user) return null;
  if (patch.name !== undefined) {
    const cleanName = String(patch.name || "").trim();
    if (!cleanName) throw new Error("用户名不能为空");
    if (loadUsers().some((u) => u.uid !== user.uid && u.name === cleanName)) throw new Error("用户名已存在");
    user.name = cleanName;
  }
  if (patch.perm !== undefined) user.perm = Number(patch.perm) || 0;
  if (patch.token !== undefined) {
    const cleanToken = String(patch.token || "").trim();
    if (!cleanToken) throw new Error("token 不能为空");
    user.token = cleanToken;
  }
  persist();
  return user;
}

export function resetUserToken(uid: string): User | null {
  const user = getUserFromUid(uid);
  if (!user) return null;
  user.token = token();
  persist();
  return user;
}

export function deleteUser(uid: string): boolean {
  const key = normalizeUid(uid);
  const before = registry.length;
  registry = loadUsers().filter((u) => u.uid !== key);
  if (registry.length === before) return false;
  persist();
  return true;
}
