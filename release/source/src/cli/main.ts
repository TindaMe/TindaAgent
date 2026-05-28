import readline from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { Agent, LlmClient } from "../ai/agent.js";
import { PUBLIC_EXECUTE, USER_ADMIN } from "../core/permissions.js";

async function main(): Promise<void> {
  const client = new LlmClient();
  const agent = new Agent("cli", USER_ADMIN | PUBLIC_EXECUTE, client, client.model);
  const rl = readline.createInterface({ input, output });
  console.log("TindaAgent CLI (TypeScript). Type /quit to exit.");
  while (true) {
    const line = await rl.question("> ");
    const text = line.trim();
    if (!text) continue;
    if (["/quit", "/exit", "exit", "quit"].includes(text)) break;
    if (text === "/version") {
      console.log(`model=${client.model}`);
      continue;
    }
    try {
      const result = await agent.chat(text);
      console.log(result.reply || "（无回复）");
      if (result.tool_steps) console.log(`[tools] ${result.tool_steps} step(s)`);
    } catch (error: any) {
      console.error(`[error] ${String(error?.message || error)}`);
    }
  }
  rl.close();
}

void main();
