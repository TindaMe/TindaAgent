# TindaAgent Architecture

TindaAgent is now a TypeScript-first application. The Python stack has been
removed from the repository.

## Runtime Layers

```mermaid
flowchart TB
    subgraph Entry[Entry]
        npm_start["npm start · dist/web/server.bundle.js"]
        start_sh["start.sh / start.bat"]
        cli["npm run tinda"]
        doctor["npm run doctor"]
    end

    subgraph Web[Web Server · src/web/server.ts]
        routes["Express routes"]
        auth["token auth middleware"]
        sse["/chat/stream SSE"]
        assets["HTML/JS assets from TindaAgent/Web"]
    end

    subgraph Core[Core · src/core]
        paths["paths.ts"]
        users["users.ts"]
        perms["permissions.ts"]
        audit["audit.ts"]
        json["json.ts"]
    end

    subgraph AI[AI · src/ai/agent.ts]
        agent["Agent"]
        llm["OpenAI-compatible client"]
        loop["tool-call loop"]
        log["LLM request logging"]
    end

    subgraph Tools[Tools · src/tools]
        registry["toolRegistry.ts"]
        runtime["toolRuntime.ts"]
        terminal["async terminal jobs"]
        files["read/search/edit files"]
        websearch["search_web"]
    end

    subgraph Sessions[Sessions · src/web]
        store["sessionStore.ts"]
        adapter["sessionAdapter.ts"]
        settings["settings.ts"]
    end

    subgraph Frontend[Frontend Assets]
        html["TindaAgent/Web/*.html"]
        runtime_js["TindaAgent/Web/chat_runtime/*.js"]
        renderers["chat_renderer.js / markdown_renderer.js / theme_toggle.js"]
    end

    npm_start --> Web
    start_sh --> Web
    cli --> AI
    doctor --> Core
    Web --> Core
    Web --> Sessions
    Web --> AI
    AI --> Tools
    Tools --> Core
    Sessions --> Core
    Web --> Frontend
```

## Data

- Runtime root: `TINDA_HOME` or `~/.tinda/agent`
- Users: `user/users.json`
- Sessions: `Data/Sessions`
- Logs: `log`
- Latest LLM request log: `log/llm_request.jsonl` unless `TINDA_LLM_REQUEST_LOG` is set

## Compatibility Surface

Existing frontend URLs are preserved:

- `/`
- `/app`
- `/chat`
- `/chat/stream`
- `/sessions`
- `/auth/*`
- `/terminal/*`
- `/logs`
- `/model-data`
- `/system/version`

The compatibility target is HTTP/API behavior and JSON storage shape, not Python
module imports.
