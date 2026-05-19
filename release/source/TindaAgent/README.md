# TindaAgent

AI agent assistant with CLI and Web interfaces, built on DeepSeek models with tool-calling, session management, and long-term memory.

## Quick Start

```bash
# CLI
pip install -e .
tinda

# Web server
python run_web.py
# → http://localhost:8000
```

## Features

- **Dual interface** — CLI (`tinda`) with prompt_toolkit and Web UI (FastAPI) with streaming SSE
- **Tool system** — Shell execution, memory, time, summarization, keyword extraction; decorator-based registration with permission gating
- **Session management** — Per-session JSON storage, context compression via LLM summarization, Markdown/text export
- **Local user auth** — JSON-backed local accounts with token-based request isolation and permission bits
- **Context token accounting** — Counts only content that is actually sent to the LLM request context, with DeepSeek tokenizer support
- **Model data panel** — Built-in `/model-data` page for DeepSeek balance, latest real SDK request body, messages, tools, thinking payload, and token-oriented summary fields
- **Web UX** — Pink themed Web UI with smooth entry/exit motion for home, chat, logs, user management, and session panels
- **Motion polish** — Layered glass-card animation system: HOME cards, changelog Markdown, runtime charts, chat header, input bar, overlays, terminal panel, admin/log/settings panels, and page exits use staggered direction-aware transitions
- **Version management** — GitHub Releases integration, Ed25519 signature verification, multi-version install and switch
- **Audit logging** — Structured event log (`total.jsonl`) with lookup by ID

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.9+ |
| Web framework | FastAPI + Uvicorn |
| LLM client | OpenAI SDK (DeepSeek-compatible) |
| CLI | prompt_toolkit |
| Frontend | Vanilla HTML/CSS/JS, pink theme |
| Data | JSON file storage |
| Validation | Pydantic |

## Directory Structure

```
TindaAgent/
    CLI/            CLI interface (prompt_toolkit)
    Web/            FastAPI server, session store, adapter, HTML pages
    Process/AI/     Agent, LLM client, tokenizer
    Process/Architecture/  Paths, permissions, versioning
    Process/Observability/  Audit logging
    Process/Security/       Terminal policy
    Process/Versioning/     Version management
    Tool/           Tool registry and implementations
    User/           User data and session management
    Permission/     Bitmask permission engine
    docs/           CHANGELOG, architecture, policies
```

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEEPSEEK_API_KEY` | (required) | API key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API endpoint |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | Default model |
| `TINDA_TITLE_MODEL` | `deepseek-v4-flash` | Title generation model |
| `TINDA_COMPRESS_MODEL` | `deepseek-v4-flash` | Context compression model |
| `TINDA_HOME` | `~/.tinda/agent` | Runtime data root |

Set these in `.env` at the project root.

## Runtime Data

- User accounts are stored in `~/.tinda/agent/user/users.json`.
- Legacy user data at `~/.tinda/agent/Data/User/users.json` is treated as a migration/compatibility source.
- Sessions are stored under `~/.tinda/agent/Data/Sessions`.
- Logs are stored under `~/.tinda/agent/log`.
- DeepSeek tokenizer files are loaded from `~/.tinda/agent/tokenizer/` when available; otherwise token counting falls back to a heuristic estimator.
- Latest LLM request snapshots are logged to `~/.tinda/agent/log/llm_request.jsonl` by default, or `TINDA_LLM_REQUEST_LOG` if overridden.

## LLM Request Assembly

The runtime now assembles LLM requests in a cache-friendlier order:

- Stable English system policy stays at the very front of every request.
- Tool schemas are deterministic per permission set: tool names, parameter keys, and required lists are sorted and cached.
- Conversation history is replayed in chronological order.
- Terminal history is merged into the LLM context as `[Terminal Context]` blocks in time order.
- Dynamic memory context is injected near the end of the request, right before the latest user message, instead of mutating the leading system prompt.

This keeps the prefix more stable while preserving strict permission-based tool visibility.

## Web Motion

The Web UI uses a layered motion system rather than single-step fades:

- HOME animates the changelog, hero card, and runtime status card as separate glass panels.
- Changelog Markdown fades in from top to bottom, while long code blocks and tables wrap instead of causing horizontal scrolling.
- Runtime status blocks, heatmaps, bar charts, donut charts, startup time, and system time appear in a top-down staggered sequence.
- Chat exit closes transient UI first, including terminal, model/time/session overlays, then plays the page exit transition.
- Settings, logs, model diagnostics, and user administration share the same theme bootstrap, dark glass palette, button alignment, and explicit transition rules.
- Motion respects reduced-motion preferences through CSS `prefers-reduced-motion` fallbacks.

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
pip install -e ".[dev]"

# Run doctor diagnostic
python doctor.py

# Run web server with hot reload
python run_web.py --reload
```

See `docs/DEVELOPMENT_POLICY.md` for coding guidelines and `docs/WSL_WINDOWS_ACCESS.md` for WSL networking setup.
