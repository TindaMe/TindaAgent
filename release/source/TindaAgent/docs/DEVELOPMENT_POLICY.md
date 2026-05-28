# TindaAgent Development Policy

This repository is TypeScript + JavaScript + HTML + CSS only. Python source,
Python package metadata, Python tests, and Python launchers are not part of the
active stack.

## Required Checks

Run these before committing:

```bash
npm install
npm test
npm run doctor
```

For Web changes, also start the server and smoke test the main routes:

```bash
npm start
```

Required HTTP checks:

- `GET /`
- `GET /app`
- `GET /auth/local-users`
- `GET /system/version`
- authenticated `GET /sessions`

## Boundaries

- `src/web/server.ts` owns HTTP routes, auth middleware, SSE, and frontend asset serving.
- `src/web/sessionStore.ts` owns persisted session JSON compatibility.
- `src/web/sessionAdapter.ts` owns store-to-frontend and store-to-LLM conversion.
- `src/ai/agent.ts` owns LLM request assembly and tool-call loops.
- `src/tools/toolRegistry.ts` owns built-in tool schemas and execution.
- `src/tools/toolRuntime.ts` owns asynchronous terminal jobs and event polling.
- `src/core/*` owns paths, users, permissions, audit, and JSON helpers.
- `TindaAgent/Web/*` contains frontend assets served by the TypeScript Web server.

Keep new modules independently testable. Prefer adding focused TypeScript modules
over growing route handlers.

## Release Notes

Every user-visible behavior change must update `TindaAgent/docs/CHANGELOG.md`.
Architecture-impacting changes must update `TindaAgent/docs/architecture.md`.
User workflow changes must update `TindaAgent/README.md`.

## Prohibited

- Do not add Python source, Python package metadata, or Python runtime launchers.
- Do not commit secrets or plaintext API keys.
- Do not leave long-running local test servers running after verification.
