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
- `routes/api_token_routes.py` mints scoped `shd_` API tokens. The `shd_`
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
- Image generation, memory, RAG, and email remain MCP-backed. The agent
  browser is native and in-process (`browser` tool → `src/browser_manager.py`)
  so it receives `owner`; the optional `@playwright/mcp` stdio server still
  exists but is secondary and shares the same UI toggle.

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

- Branding is `Shadow` throughout. Internal identifiers
  such as `SHADOW_*` env variables, `shd_` tokens, `shadow_session`,
  `X-Shadow-*` headers, and local-storage keys remain stable unless a
  migration is added.
- Home-PC control is a separate allowlisted companion service intended for a
  private Tailscale link. The VPS must never expose a general-purpose home
  shell to the public internet.
- Read-only home-PC actions can execute immediately. State-changing actions
  create short-lived pending approvals and require an explicit confirmation
  before the companion service receives `confirmed=true`.
- Browser approval endpoints require a real admin cookie. An internal agent
  token or bearer integration token cannot self-approve an action.
- The agent browser is a native in-process tool (`src/browser_manager.py`,
  `routes/browser_routes.py`, `do_browser`), not an MCP server, because
  native tools receive `owner` and per-account profile isolation depends on
  it. New gated capabilities should follow its pattern: pure `classify_risk`
  → in-memory pending approval → interactive-cookie-only confirm, with the
  approval voided when the page context drifts.
- Telegram reuses `POST /api/v1/chat` with an owner-scoped chat token rather
  than creating another LLM loop.

## Autonomous Missions, Desktop Workspace, And Permission Policy

Verified integration points this subsystem builds on (do not duplicate them):

- **Device relay**: `src/shadow_devices.py` (`dispatch_action`,
  `poll_job`/`complete_job`, file-backed job queue, SHA-256 device tokens,
  owner-scoped lookups) and `routes/shadow_routes.py`
  (`/api/shadow/device/poll|result|enroll`). Workspace file/git/command
  actions are new agent actions dispatched through this exact relay; nothing
  on the device is exposed to the internet.
- **Device agent**: `companion/home_agent.py` executes allowlisted actions
  with `SHADOW_ALLOWED_ROOTS` containment; `companion/workspace_agent.py`
  adds the workspace/git/patch/checkpoint actions and re-enforces root
  containment with `os.path.realpath` (symlink-escape proof) — defense in
  depth even against a compromised server. Installers fetch agent files from
  the allowlist in `routes/shadow_routes.py::shadow_device_source`.
- **Model providers**: `ModelEndpoint` DB rows; per-user visibility via the
  pattern in `routes/magi_routes.py::_visible_endpoints`; calls go through
  `src/llm_core.llm_call_async(url, model, messages, headers=...)`. Mission
  roles map to `{endpoint_id, model}` pairs — no provider is hardcoded.
- **Approvals**: missions use durable JSON approvals (mission state must
  survive restarts), but keep the established contract from
  `src/shadow_pc.py`/`src/browser_manager.py`: approval endpoints require an
  interactive cookie (`_real_user`); agent identities and API tokens can
  never self-approve.
- **Policy engine**: `src/mission_policy.py` is the single decision point
  (`ALLOW` / `REQUIRE_APPROVAL` / `DENY`) for every workspace tool call, on
  top of declared `ActionRequest` capabilities. Models never decide their
  own permissions; the engine is pure and unit-tested. The device agent
  independently re-checks roots.
- **Durable state**: missions, workspaces, policy rules, and the audit log
  live under `data/missions/` using the `core/atomic_io` JSON pattern (same
  family as `data/shadow-devices.json`); no SQL schema changes.
- **Agent sessions**: `src/agent_sessions.py` is the direct (no-DAG)
  conversational tool loop for simple tasks; it reuses the exact
  `mission_workspaces.dispatch` → policy → relay path, the mission
  checkpoint contract, and the `endpoint_resolver`/`llm_core` model path.
  The `unattended` workspace mode lives in `mission_policy.evaluate`
  (ALLOW/DENY only — REQUIRE_APPROVAL is unreachable in that mode).
- **Frontend**: `static/js/missionsPage.js` follows the `commandPage.js`
  full-page module pattern (`openPage`/`closePage`, route in `app.py` +
  `static/app.js` `_routeOpen`, sidebar button in `static/index.html`).
- Chat, MAGI, Command, Telegram, devices, and the agent browser are not
  modified by this subsystem beyond the wiring listed above.

