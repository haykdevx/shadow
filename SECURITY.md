# Security Policy

Shadow is a fork of Odysseus and is a self-hosted AI workspace with privileged local capabilities. Please do not run it as a public, unauthenticated service.

## Supported Versions

Security fixes are handled on the default branch until formal releases are cut.

## Deployment Guidance

- Keep `AUTH_ENABLED=true` for any network-accessible deployment.
- Keep `LOCALHOST_BYPASS=false` outside local development.
- Set `SECURE_COOKIES=true` when Shadow is served through HTTPS by a trusted reverse proxy or private access gateway.
- Use HTTPS when exposing the app beyond localhost.
- Put the authenticated Shadow web/API entrypoint behind a trusted reverse proxy or private access layer such as Cloudflare Access, Tailscale, or a VPN.
- Keep ChromaDB, SearXNG, ntfy, Ollama, vLLM, llama.cpp, databases, and raw model/provider APIs internal-only.
- Protect `.env`, `data/`, `logs/`, uploads, generated media, backups, auth/session files, database files, API keys, and model/provider tokens.
- Disable open signup unless you intentionally want new accounts.
- Keep demo/test users non-admin, and remove them entirely on serious deployments.
- Give admin accounts strong passwords and enable 2FA where possible.
- Leave high-risk agent tools restricted to admins: shell, Python, file read/write, email send/read, MCP, app API, task/skill/memory management, settings, tokens, and model serving.
- Rotate API keys, webhook secrets, and Shadow API tokens if they appear in logs, screenshots, demos, or shared chats.
- Treat shell, model-serving, MCP, email, calendar, and vault features as privileged admin functionality.
- Common internal-only ports are Shadow `7000`, SearXNG `8080`, ntfy `8091`, ChromaDB `8100`, Ollama `11434`, and local model/provider APIs such as `8000-8020`.

## Demo Mode

- `SHADOW_DEMO_MODE=true` simulates the home-PC bridge for public demos and reviews.
- Demo mode does not contact a real companion agent and does not execute real PC actions.
- Keep demo mode off for serious personal deployments unless you intentionally want simulated data.

## Shadow Home-PC Bridge

- Bind `scripts/shadow_home_agent.py` to the exact home-PC Tailscale address. Do not expose it publicly.
- Keep `SHADOW_HOME_AGENT_ALLOW_PUBLIC=false`; the VPS client rejects public bridge URLs by default.
- Use a random `SHADOW_HOME_AGENT_TOKEN` of at least 32 characters and store it only in mode-600 environment files.
- The companion exposes an allowlisted action API, not a general-purpose shell.
- Read-only actions execute directly. State-changing actions require an explicit short-lived approval.
- Set `SHADOW_PC_OWNER` to the intended Shadow username. Admin status alone does not grant linked-PC access; all other accounts default to deny and need explicit `view`, `control`, and/or `approve` grants.
- Pending actions, audit events, the AI `pc_control` tool, and Telegram identities are scoped to the linked Shadow account. Browser approval requires an interactive account with `approve`; internal tool and bearer API tokens cannot self-approve.
- Screenshots are captured into a temporary file, returned to the authenticated caller, and deleted immediately.

## Agent Sessions & Desktop Workspace (Computer Access)

- Computer access is opt-in per account: the `can_use_computer` privilege gates every workspace-agent write/dispatch route (admins always have it; new non-admin accounts default to off).
- Every workspace/session/policy route requires an interactive cookie session. Bearer API tokens and the internal agent identity get 403 — a model can never approve its own gated action, change a permission mode, or arm full access.
- Every tool call is declared (capability, device, workspace, mutating flag, risk, network flag, target path) and decided server-side by `src/workspace_policy.evaluate` (`ALLOW`/`REQUIRE_APPROVAL`/`DENY`). Models never decide their own permissions; the engine is pure and unit-tested.
- Containment is enforced twice: syntactic canonicalization server-side (null bytes, percent-encoding, `..`, UNC, Windows case folding) and authoritative `os.path.realpath` containment on the device against BOTH the workspace root and the device-local `SHADOW_ALLOWED_ROOTS`. Set `SHADOW_ALLOWED_ROOTS` narrowly on every device.
- Mutating agent actions additionally require the relay job to carry `confirmed=true`, which only an ALLOW decision or explicit approval sets.
- `full` access requires password reauthentication, is keyed to one owner+device, supports a time limit, shows a persistent indicator, writes append-only audit records (`data/workspace-agent/audit.log`), and still cannot leave the device's allowed roots. Cross-user/cross-device access is a structural DENY in every mode.
- Agent sessions checkpoint before their first mutation (per-file pre-images + git branch on clean trees), soft-delete to a workspace trash, and support per-file and full rollback. Session approvals are in-memory and drop on restart (fail closed); `once` approvals are single-use, and "always allow" rules are per-workspace, visible, and revocable in the UI. The full audit trail is viewable in Settings → System.

## Agent Browser

- Every `/api/browser/*` endpoint requires an interactive cookie session. Internal agent identities and bearer API tokens are rejected outright, so an agent can never approve its own gated action. On multi-user installs the `browser` tool is additionally admin-only by default.
- Irreversible or sensitive actions — purchases, sends, posts, deletes, password/OTP/card field fills, raw `eval` JS — never execute directly. They become server-side pending approvals (short TTL, in-memory, dropped on restart) that run only after explicit approval from the Browser panel. An approval is voided if the page navigated since it was requested.
- Each account gets an isolated persistent Chromium profile under `data/browser/profiles/<account>` (mode 0700). Profiles, cookies, and logged-in sessions are never shared across accounts; `POST /api/browser/wipe` destroys the profile.
- URL policy: http/https only; localhost, RFC1918, link-local, and cloud-metadata addresses are blocked by hostname and again at DNS resolution; main-frame navigations are re-checked after redirects; subresource requests to private space are aborted. `SHADOW_BROWSER_ALLOW_DOMAINS` / `SHADOW_BROWSER_DENY_DOMAINS` narrow it further. Known residual: hostname-level subresource checks do not defeat DNS rebinding — use a filtering egress proxy if that is in your threat model.
- Actions are rate-limited per account (`SHADOW_BROWSER_ACTIONS_PER_MINUTE`); approval listings and history redact fill values and URL query strings; screenshots are stored per-account and pruned. Stored secrets are never auto-entered without a per-action approval.
- The Telegram `/browse` path uses a file queue on the shared `data/` volume (0700, files 0600) rather than a network endpoint; the bot container's pairing is the authorization boundary.
- Never bind the app publicly without the TLS reverse proxy in DEPLOY.md; the browser endpoints inherit whatever exposure the app has.

## Interactive Remote Desktop

- MeshCentral is optional and must bind to loopback (`SHADOW_MESH_BIND=127.0.0.1`). Expose it only through the authenticated Shadow HTTPS origin at `/remote/`.
- The internal gateway has no published port and accepts only a random `SHADOW_MESH_GATEWAY_KEY` of at least 32 characters.
- Every Shadow account maps to a distinct MeshCentral user and device group. Remote credentials are encrypted with `data/.app_key`; preserve that key in backups.
- Browser access uses short-lived login tokens. MeshCentral users receive desktop-control rights only; terminal, files, server tools, group creation, and settings changes remain disabled.
- Keep `meshcentral-data`, `meshcentral-files`, and `meshcentral-backups` private. Never expose port `8443` directly to the internet.

## Publishing A Fork

Before pushing a public fork, run:

```bash
git status --short
git check-ignore -v .env data/auth.json data/app.db logs/compound.log shadow.db
python scripts/public_readiness_check.py --strict
git grep -n -I -E "(sk-[A-Za-z0-9_-]{20,}|xox[baprs]-|AIza[0-9A-Za-z_-]{20,}|Bearer [A-Za-z0-9._~+/-]{20,})" -- . ':!static/lib/**' ':!package-lock.json'
```

Only `.env.example`, docs, source, tests, and static assets should be committed. Never commit live `.env` values, `data/` contents, local databases, uploaded files, generated media, logs, backups, auth/session files, API keys, model/provider tokens, password hashes, or personal documents.

## Reporting

Please report vulnerabilities privately via GitHub security advisories if available, or by opening a minimal issue that does not disclose exploit details.
