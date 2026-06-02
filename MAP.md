# Shadow Architecture Map

This map records the verified upstream architecture before the Shadow product
extensions were added. It is intentionally concrete: future changes should use
these extension points instead of duplicating working subsystems.

## Runtime Composition

- `app.py` is the FastAPI composition root. It initializes auth middleware,
  managers, STT/TTS, the task scheduler, MCP, routers, SPA routes, and startup
  tasks.
- `src/app_initializer.py` builds the shared manager graph used by routes.
- `docker-compose.yml` runs the application with bundled ChromaDB, SearXNG,
  and ntfy services. The default host bindings are loopback-only.
- `core/database.py` owns persisted relational state. Application data lives
  under `data/`, which is intentionally ignored by Git.

## Authentication And Secrets

- `core/auth.py` owns password hashing, session auth, admin roles, and TOTP 2FA.
- `app.py` installs `AuthMiddleware`, validates cookie sessions and hashed
  bearer API tokens, and keeps proxy-forwarded traffic out of localhost bypass.
- `core/middleware.py` owns security headers and the loopback-only internal-tool
  token used by the in-process agent bridge.
- `src/secret_storage.py` encrypts configured secrets at rest with Fernet.
- `routes/api_token_routes.py` mints scoped `ody_` API tokens. The `ody_`
  prefix remains as a compatibility identifier even though the visible product
  is Shadow.

## Chat, Agent Loop, And Tools

- `routes/chat_routes.py` serves the streaming browser chat workflow.
- `routes/webhook_routes.py` serves `POST /api/v1/chat`, the owner-scoped,
  chat-token-authenticated sync API used by external clients such as the
  Shadow Telegram bridge.
- `src/agent_loop.py` coordinates iterative tool use.
- `src/agent_tools.py` is the tool facade and canonical `TOOL_TAGS` registry.
- `src/tool_schemas.py` exposes OpenAI-compatible native tool schemas.
- `src/tool_execution.py` dispatches native and MCP tool calls and applies
  admin policy before execution.
- `src/tool_implementations.py` holds the application-specific native tool
  implementations.
- `src/tool_security.py` defines tools unavailable to non-admin users.
- `src/tool_index.py` indexes searchable tool descriptions for agent-mode
  retrieval.

## MCP

- `src/mcp_manager.py` manages MCP lifecycle.
- `src/builtin_mcp.py` registers built-in stdio MCP servers at startup.
- Bash, Python, filesystem reads/writes, and web search have direct in-process
  execution paths in `src/tool_execution.py`.
- Image generation, memory, RAG, email, and optional Playwright browser tools
  remain MCP-backed.

## Existing Jarvis-Class Features

- Voice already exists: `services/stt/`, `services/tts/`,
  `routes/stt_routes.py`, `routes/tts_routes.py`,
  `static/js/voiceRecorder.js`, and `static/js/tts-ai.js`.
- The proactive assistant already exists:
  `routes/assistant_routes.py`, `src/task_scheduler.py`,
  `src/assistant_log.py`, and `static/js/assistant.js`.
- Notes, scheduled tasks, calendar, email, persistent memory, document RAG,
  deep research, model compare, Cookbook, image generation, and browser tools
  are existing subsystems and must be extended rather than rebuilt.
- `companion/` exposes owner-scoped pairing and read-only mobile bridge routes.

## Frontend

- `static/index.html` is the SPA shell and welcome surface.
- `static/app.js` is the browser orchestration entry.
- `static/style.css` contains the tokenized theme layer and component styles.
- `static/js/` contains modular feature implementations.
- `static/login.html` and `static/manifest.json` contain visible product
  branding.

## Shadow Extension Boundaries

- Visible branding changes to `Shadow`. Internal compatibility identifiers
  such as `ODYSSEUS_*` env variables, `ody_` tokens, `odysseus_session`,
  `X-Odysseus-*` headers, and local-storage keys remain stable unless a
  migration is added.
- Home-PC control is a separate allowlisted companion service intended for a
  private Tailscale link. The VPS must never expose a general-purpose home
  shell to the public internet.
- Read-only home-PC actions can execute immediately. State-changing actions
  create short-lived pending approvals and require an explicit confirmation
  before the companion service receives `confirmed=true`.
- Browser approval endpoints require a real admin cookie. An internal agent
  token or bearer integration token cannot self-approve an action.
- Telegram reuses `POST /api/v1/chat` with an owner-scoped chat token rather
  than creating another LLM loop.

