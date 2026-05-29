import fs from "node:fs";
import { llmEnvConfig, loadRuntimeEnv, maskSecret } from "../core/env.js";
import { appVersion, ensureRuntimeDirs, logRoot, projectRoot, runtimeRoot, sessionsRoot, usersFile } from "../core/paths.js";
import { iterUsers } from "../core/users.js";

function ok(label: string, value: string): void {
  console.log(`[OK] ${label}: ${value}`);
}

function warn(label: string, value: string): void {
  console.log(`[WARN] ${label}: ${value}`);
}

loadRuntimeEnv();
ensureRuntimeDirs();
console.log("TindaAgent Doctor (TypeScript)");
ok("version", appVersion());
ok("node", process.version);
ok("project_root", projectRoot());
ok("runtime_root", runtimeRoot());
ok("sessions_root", sessionsRoot());
ok("log_root", logRoot());
ok("users_file", usersFile());
ok("users", String(iterUsers().length));
const llm = llmEnvConfig();
if (!llm.apiKey) {
  warn("llm_api_key", "DEEPSEEK_API_KEY/OPENAI_API_KEY not configured");
} else {
  ok("llm_api_key", `${llm.apiKeySource} ${maskSecret(llm.apiKey)}`);
  ok("llm_base_url", `${llm.baseURL} (${llm.baseURLSource})`);
  ok("llm_model", llm.model);
}
if (!fs.existsSync("dist/web/server.bundle.js")) {
  warn("build", "dist/web/server.bundle.js not found; run npm run build");
} else {
  ok("build", "dist/web/server.bundle.js");
}
