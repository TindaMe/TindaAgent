export const PUBLIC_READ = 1 << 0;
export const PUBLIC_WRITE = 1 << 1;
export const PUBLIC_EXECUTE = 1 << 2;
export const TOOL_READ = 1 << 3;
export const TOOL_WRITE = 1 << 4;
export const TOOL_EXECUTE = 1 << 5;
export const SYSTEM_READ = 1 << 6;
export const SYSTEM_WRITE = 1 << 7;
export const SYSTEM_EXECUTE = 1 << 8;

export const PUBLIC_ALL = PUBLIC_READ | PUBLIC_WRITE | PUBLIC_EXECUTE;
export const TOOL_ALL = TOOL_READ | TOOL_WRITE | TOOL_EXECUTE;
export const SYSTEM_ALL = SYSTEM_READ | SYSTEM_WRITE | SYSTEM_EXECUTE;
export const USER_VISITOR = PUBLIC_ALL;
export const USER_BASE = USER_VISITOR | TOOL_ALL;
export const USER_ADMIN = USER_BASE | SYSTEM_ALL;
export const LLM_BASE = PUBLIC_ALL;

export const PERM_ITEMS = [
  ["PUBLIC_READ", PUBLIC_READ, "公共读取"],
  ["PUBLIC_WRITE", PUBLIC_WRITE, "公共写入"],
  ["PUBLIC_EXECUTE", PUBLIC_EXECUTE, "公共执行"],
  ["TOOL_READ", TOOL_READ, "工具读取"],
  ["TOOL_WRITE", TOOL_WRITE, "工具写入"],
  ["TOOL_EXECUTE", TOOL_EXECUTE, "工具执行"],
  ["SYSTEM_READ", SYSTEM_READ, "系统读取"],
  ["SYSTEM_WRITE", SYSTEM_WRITE, "系统写入"],
  ["SYSTEM_EXECUTE", SYSTEM_EXECUTE, "系统执行"]
] as const;

export function hasPerm(userPerm: number, requiredPerm: number): boolean {
  return (Number(userPerm) & Number(requiredPerm)) === Number(requiredPerm);
}

export function permLabels(value: number): string[] {
  const p = Number(value) || 0;
  return PERM_ITEMS.filter(([, bit]) => (p & bit) === bit).map(([key]) => key);
}

export function permLabel(value: number): string {
  const labels = permLabels(value);
  return labels.length ? labels.join(" | ") : "NONE";
}

export function permissionItems(): Array<{ bit: number; key: string; label: string }> {
  return PERM_ITEMS.map(([key, bit, label]) => ({ bit, key, label }));
}
