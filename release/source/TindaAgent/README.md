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
- **Web UX** — Pink themed Web UI with smooth entry/exit motion for home, chat, logs, user management, and session panels
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
