import { spawn } from "node:child_process";
import net from "node:net";

let port = 0;
let base = "";

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const selected = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolve(selected));
    });
  });
}

async function request(path: string, options: RequestInit = {}) {
  const res = await fetch(`${base}${path}`, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) }
  });
  const text = await res.text();
  let data: any = text;
  try {
    data = JSON.parse(text);
  } catch {
    // keep text
  }
  if (!res.ok) throw new Error(`${options.method || "GET"} ${path} -> ${res.status}: ${text}`);
  return data;
}

async function main() {
  port = await freePort();
  base = `http://127.0.0.1:${port}`;
  const proc = spawn(process.execPath, ["--no-warnings=ExperimentalWarning", "dist/web/server.bundle.js", "--host=127.0.0.1", `--port=${port}`, "--port-retries=0"], {
    cwd: process.cwd(),
    env: { ...process.env, PORT: String(port), HOST: "127.0.0.1", PORT_RETRIES: "0", TINDA_DEEP_ALIGNMENT_OFFLINE: "1" },
    stdio: ["ignore", "pipe", "pipe"]
  });
  let output = "";
  proc.stdout.on("data", (chunk) => {
    output += chunk.toString("utf8");
  });
  proc.stderr.on("data", (chunk) => {
    output += chunk.toString("utf8");
  });
  try {
    for (let i = 0; i < 240; i += 1) {
      if (proc.exitCode !== null) throw new Error(`server exited early with ${proc.exitCode}: ${output}`);
      if (output.includes(`127.0.0.1:${port}`)) break;
      await wait(100);
    }
    if (!output.includes(`127.0.0.1:${port}`)) throw new Error(`server did not start: ${output}`);
    const users = await request("/auth/local-users");
    const user = users.users?.[0];
    if (!user?.uid) throw new Error("no local user");
    const login = await request("/auth/local-login", { method: "POST", body: JSON.stringify({ uid: user.uid }) });
    const headers = { "X-User-Token": String(login.token || "") };
    const sid = `feature_${Date.now()}`;
    await request("/sessions", { method: "POST", headers, body: JSON.stringify({ session_id: sid, title: "feature" }) });

    const cfg = await request(`/sessions/${sid}/config`, { method: "PATCH", headers, body: JSON.stringify({ max_context_tokens: 32000 }) });
    if (cfg.config?.token_limit !== 32000) throw new Error("session config did not persist token limit");
    const cfg2 = await request(`/sessions/${sid}/config`, { headers });
    if (cfg2.config?.max_context_tokens !== 32000) throw new Error("session config did not reload");

    const deep = await request(`/sessions/${sid}/deep/align`, { method: "POST", headers, body: JSON.stringify({ session_id: sid, message: "迁移 Deep 对齐", file_names: ["a.txt"], file_contents: ["x"] }) });
    if (!deep.active || !deep.rounds?.length || !String(deep.alignment_text || "").includes("迁移 Deep 对齐")) throw new Error("deep align state invalid");
    const restored = await request(`/sessions/${sid}/deep`, { headers });
    if (!restored.active || !restored.rounds?.length) throw new Error("deep state did not restore");
    const confirmed = await request(`/sessions/${sid}/deep/confirm`, { method: "POST", headers });
    if (confirmed.active !== false || confirmed.state !== "confirmed") throw new Error("deep confirm invalid");

    await request("/session/events", {
      method: "POST",
      headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify({
        session_id: sid,
        entries: [
          { role: "user", content: "plan user" },
          { role: "assistant", content: "plan assistant" }
        ]
      })
    });
    const messages = await request(`/sessions/${sid}/messages`, { headers });
    if (!messages.plan || messages.plan.deleted !== false) throw new Error("plan envelope missing");
    await request(`/sessions/${sid}/plan`, { method: "DELETE", headers });
    const messagesAfterPlanDelete = await request(`/sessions/${sid}/messages`, { headers });
    if (!messagesAfterPlanDelete.plan?.deleted) throw new Error("plan delete did not persist");

    const pendingConflict = await fetch(`${base}/terminal/confirm`, {
      method: "POST",
      headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify({ session_id: sid, approval: true })
    });
    if (pendingConflict.status !== 409) throw new Error(`terminal confirm without pending expected 409, got ${pendingConflict.status}`);

    await request(`/sessions/${sid}`, { method: "DELETE", headers });
    console.log("http feature smoke passed");
  } finally {
    proc.kill("SIGTERM");
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
