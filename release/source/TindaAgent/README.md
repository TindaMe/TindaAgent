# TindaAgent

AI agent assistant with CLI and Web interfaces, built on DeepSeek/OpenAI-compatible models with tool-calling, session management, and long-term memory.

The active runtime is TypeScript + JavaScript + HTML + CSS. The Python stack has been removed from the repository.

## Quick Start

```bash
# Install Node dependencies
npm install
npm run build

# CLI
npm run tinda

# Web server
npm start
# → http://localhost:8000
```

`npm start` runs the already-built Web bundle for fast startup. Use `npm run start:build`
when you want to type-check, rebuild, and start in one command.

## Features

- **Dual interface** — CLI (`tinda`) with Node readline and Web UI (Express) with streaming SSE
- **Tool system** — Shell execution, memory, time, summarization, keyword extraction; registry-based registration with permission gating
- **Web search tool** — `search_web` uses Tavily when configured, then falls back to built-in DuckDuckGo search and a curated common-site index
- **Session management** — Per-session JSON storage, context compression via LLM summarization, Markdown/text export
- **Local user auth** — JSON-backed local accounts with token-based request isolation and permission bits
- **Context accounting** — Estimates content that is actually sent to the LLM request context
- **Model data panel** — Built-in `/model-data` page for DeepSeek balance, latest real SDK request body, messages, tools, thinking payload, and token-oriented summary fields
- **Web UX** — Pink themed Web UI with smooth entry/exit motion for home, chat, logs, user management, and session panels
- **Motion polish** — Layered glass-card animation system: HOME cards, changelog Markdown, runtime charts, chat header, input bar, overlays, terminal panel, admin/log/settings panels, and page exits use staggered direction-aware transitions
- **Version reporting** — Runtime version is sourced from `package.json` and surfaced through `/system/version`
- **Audit logging** — Structured event log (`total.jsonl`) with lookup by ID

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | TypeScript + JavaScript |
| Web framework | Express |
| LLM client | OpenAI JS SDK (DeepSeek-compatible) |
| CLI | Node readline |
| Frontend | Vanilla HTML/CSS/JS, pink theme |
| Data | JSON file storage |
| Validation | TypeScript runtime checks |

## Directory Structure

```
TindaAgent/
    Web/            Active HTML/JS frontend assets
    Permission/     Tool permission policy JSON
    Process/Versioning/  Release manifest schema
    docs/           CHANGELOG, architecture, policies
src/
    ai/             TypeScript Agent and OpenAI-compatible client
    cli/            TypeScript CLI and doctor entry points
    core/           Runtime paths, permissions, users, audit helpers
    tools/          Tool registry and async terminal jobs
    web/            Express server, settings, session store and adapters
dist/               Built TypeScript output
dist/web/server.bundle.js  Bundled Web server entry used by npm start/start scripts
```

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEEPSEEK_API_KEY` | (required) | API key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API endpoint |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | Default model |
| `TAVILY_API_KEY` | (optional) | Enables Tavily-backed `search_web` results |
| `TAVILY_BASE_URL` | `https://api.tavily.com` | Tavily-compatible base URL |
| `TAVILY_SEARCH_URL` | (optional) | Full Tavily-compatible search endpoint override |
| `TINDA_HOME` | `~/.tinda/agent` | Runtime data root |

Set these in `.env` at the project root.

## Runtime Data

- User accounts are stored in `~/.tinda/agent/user/users.json`.
- Sessions are stored under `~/.tinda/agent/Data/Sessions`.
- Logs are stored under `~/.tinda/agent/log`.
- Latest LLM request snapshots are logged to `~/.tinda/agent/log/llm_request.jsonl` by default, or `TINDA_LLM_REQUEST_LOG` if overridden.

## LLM Request Assembly

The runtime now assembles LLM requests in a cache-friendlier order:

- Stable English system policy stays at the very front of every request.
- Tool schemas are deterministic per permission set: tool names, parameter keys, and required lists are sorted and cached.
- Conversation history is replayed in chronological order.
- Terminal history is merged into the LLM context as `[Terminal Context]` blocks in time order.
- Dynamic memory context is injected near the end of the request, right before the latest user message, instead of mutating the leading system prompt.
- Display-rich content is compacted only for the LLM request: Markdown decorations, code fences, terminal output, and tool-result JSON duplicates are normalized before being sent to the model, while session files and frontend rendering keep the original rich content.

This keeps the prefix more stable while preserving strict permission-based tool visibility.

## Agent Tools

TindaAgent exposes native tools through the same permission-aware registry used by Chat:

- `read_file` / `edit_file` provide Codex/Claude Code style exact text edits with optional `expected_sha256`, `dry_run`, and create support.
- `search_files` finds files by path/name substring and optional text content, returning bounded path, line, and snippet results for edit planning.
- `search_web` searches the network with `source=auto|tavily|builtin|index`: Tavily is used when `TAVILY_API_KEY` exists, built-in mode parses DuckDuckGo HTML without a paid API key, and index mode returns curated search links for common engines, docs, repositories, Q&A, package registries, AI docs, and research sites.
MCP and local skill bridge tools were part of the removed Python runtime and are not present in the TypeScript-only stack yet.

## Web Motion

The Web UI uses a layered motion system rather than single-step fades:

- HOME animates the changelog, hero card, and runtime status card as separate glass panels.
- Changelog Markdown fades in from top to bottom, while long code blocks and tables wrap instead of causing horizontal scrolling.
- Runtime status blocks, heatmaps, bar charts, donut charts, startup time, and system time appear in a top-down staggered sequence.
- Chat exit closes transient UI first, including terminal, model/time/session overlays, then plays the page exit transition.
- Settings, logs, model diagnostics, and user administration share the same theme bootstrap, dark glass palette, button alignment, and explicit transition rules.
- Motion respects reduced-motion preferences through CSS `prefers-reduced-motion` fallbacks.

## Web Terminal Performance

The Web terminal is optimized for long tool output and `search_web` traces:

- Chat page runtime JavaScript is split under `Web/chat_runtime/` and loaded as ordered assets instead of one large inline script.
- Terminal DOM writes are batched with `requestAnimationFrame` and `DocumentFragment`.
- Each logical terminal output shows at most 6 preview lines.
- Long output adds a `查看完整信息` action below the terminal bubble.
- Full output is loaded from an in-memory LRU cache only when requested; the active full-output cache keeps the latest 10 items.
- Terminal history loads a broader recent window, while long text stays out of the DOM until the user opens it.

## CLI Commands

```
/help         Show help
/sessions     List sessions
/session [id] Switch session
/new [title]  Create new session
/delete [id]  Delete session
/reset        Reset context
/last         Switch to last session
/model [name] Switch model
/version      Show version info
/quit         Exit
```

## Web Endpoints

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Home page |
| `/app` | GET | Chat interface |
| `/chat` | POST | Send message |
| `/chat/stream` | GET | Streaming chat (SSE) |
| `/sessions` | GET/POST | List / create sessions |
| `/sessions/{id}/context-usage` | GET | Context token usage for LLM-bound messages |
| `/sessions/{id}/compress` | POST | Manually compress session context |
| `/auth/status` | GET | Current authentication state |
| `/auth/local-users` | GET | List local JSON-backed accounts for local login |
| `/auth/local-login` | POST | Select a local account and return its token |
| `/settings` | GET | Settings page |
| `/logs` | GET | Log viewer |
| `/model-data` | GET | Model data panel with DeepSeek balance and latest LLM request payload |
| `/model-data/latest` | GET | Latest logged LLM request body + summary |
| `/model-data/balance` | GET | DeepSeek account balance via server-side API key |
| `/llm-request` | GET | Backward-compatible alias for the model data panel |
| `/model-diagnostics` | GET | Model diagnostics |
| `/user-admin` | GET | User administration |
| `/system/versions` | GET | Version management |

## Development

```bash
# Install dev dependencies
npm install

# Run doctor diagnostic
npm run doctor

# Run web server
npm start

# Build/type-check
npm test
```

See `docs/DEVELOPMENT_POLICY.md` for coding guidelines and `docs/WSL_WINDOWS_ACCESS.md` for WSL networking setup.
