# Shadow

**Shadow is a private, self-hosted personal AI workspace for a single operator** — chat, autonomous agents, voice, long-term memory, deep research, a full productivity suite (mail, calendar, notes, tasks, documents), a free music player, and explicitly-approved control of a linked Linux PC. It runs on a VPS and reaches your home machine only over a private Tailscale link.

Shadow is a fork of [Odysseus](https://github.com/pewdiepie-archdaemon/odysseus). The upstream MIT license and acknowledgments are preserved.

---

## Why Shadow

Most self-hosted AI projects stop at a chat box. Shadow is built as a private **command center**: the assistant can reason, remember, research, deliberate across multiple models (MAGI), run real tools, and — only through explicit approvals — control a linked Linux PC. It is designed for one serious operator first, then packaged so contributors can safely run it in demo mode.

---

## Features

**AI core**
- **Chat** — any local model or API; adding them is trivial.<br><sub>vLLM · llama.cpp · Ollama · OpenRouter · OpenAI</sub>
- **Agent** — hand it tools and let it run the whole task itself.<br><sub>built on [opencode](https://github.com/anomalyco/opencode) · MCP · web · files · shell · skills · memory</sub>
- **MAGI** — tri-model deliberation with **vote**, **debate**, and **judge** resolution modes.
- **Deep Research** — multi-step runs that gather, read, and synthesize sources into a visual report.<br><sub>adapted from [Tongyi DeepResearch](https://github.com/Alibaba-NLP/DeepResearch)</sub>
- **Compare** — blind side-by-side model comparison, then synthesis.
- **Memory / Skills** — persistent, evolving memory and skills.<br><sub>ChromaDB · fastembed (ONNX) · vector + keyword retrieval · import/export</sub>
- **Cookbook** — scans your hardware, recommends models, click to download & serve.<br><sub>built on [llmfit](https://github.com/AlexsJones/llmfit) · VRAM-aware · GGUF / FP8 / AWQ · vLLM / llama.cpp</sub>

**Productivity**
- **Email** — IMAP/SMTP inbox with AI triage: urgency reminders, auto-tag, summary, reply drafts, spam.
- **Calendar** — local-first with CalDAV sync (Radicale / Nextcloud / Apple / Fastmail) and `.ics` import/export.
- **Notes & Tasks** — quick notes with reminders, checklists, and cron-style tasks the agent can act on (ntfy / browser / email).
- **Documents & Library** — multi-tab editor (markdown / HTML / CSV) with AI edits, plus a document library with vision + PDF ingest.
- **Contacts**, **Gallery / image editor**, **Presets**, **Sessions**.

**Media**
- **Music** — a free, full-catalog player (search, queue, playlists, liked songs, now-playing bar, OS media-key support) streaming through a resilient YouTube pipeline. See [Music pipeline](#music-pipeline).

**System control**
- **Command** — every Shadow account can enroll its own Linux, macOS, or Windows PC with a one-line installer, then access vitals, screenshots, processes, files, windows, runbooks, and approvals.
- The account-owned device agent connects outbound over HTTPS; it opens no home-router port. Destructive actions remain server- and device-gated behind explicit confirmation.
- **Telegram inbox + remote** — pair one Telegram identity to one Shadow account. Bot chats appear inside Shadow and commands can control only that account's enrolled PCs.

**Platform** — responsive, installable **PWA**, 2FA (TOTP), scoped API tokens, and a public-safe **demo mode** (`SHADOW_DEMO_MODE=true`).

---

## Architecture

Shadow is a **FastAPI backend** serving a **vanilla-JS single-page frontend**, with a handful of bundled infrastructure containers. There is no frontend build step — the browser loads ES modules directly.

```
                         ┌──────────────────────────────────────────────┐
   Browser (SPA)         │                  Shadow app                   │
  ┌───────────────┐      │  FastAPI (uvicorn)                            │
  │ static/*.js    │ HTTPS│  ├─ auth middleware (cookie session + 2FA)   │
  │ ES modules     │◀────▶│  ├─ routes/*.py  setup_*_routes()  /api/*    │
  │ modalManager   │      │  ├─ src/  agent loop · LLM core · RAG · tasks │
  │ + windowDrag   │      │  └─ services/  memory, caches                 │
  └───────────────┘      └───────┬───────────┬───────────┬──────────────┘
        │ media keys              │           │           │
        │ (Media Session)   SQLite/SQLAlchemy ChromaDB   SearXNG · ntfy
                                  │        (vectors)    (search · push)
                          ┌───────┴────────┐
                          │ bgutil-pot      │  (PO-token sidecar for Music)
                          └─────────────────┘
                                  ⋮ Tailscale (private)
                          ┌─────────────────┐
                          │ Home-PC companion│  allowlisted pc_control
                          └─────────────────┘
```

### Backend
- **FastAPI + uvicorn**. `app.py` wires everything together and mounts feature routers.
- **Route modules** (`routes/*.py`) each expose `setup_<feature>_routes() -> APIRouter` under `/api/<feature>` and are registered in `app.py`. Auth is resolved per-request via `src/auth_helpers.get_current_user`.
- **Core logic** (`src/`): the streaming agent loop, LLM endpoint resolution, RAG/vector + memory layers, the task scheduler, built-in agent actions, CalDAV sync, and the home-PC control layer.
- **Auth**: cookie sessions (`shadow_session`) with bcrypt password hashing and optional TOTP 2FA; scoped bearer **API tokens** (`shd_…`) for paired clients; a sandboxed demo mode.

### Frontend
- **Vanilla ES-module SPA** (`static/`). `static/app.js` is the orchestrator: each feature module exports `init(API_BASE)` and is initialized on boot.
- **Floating-window system** — `static/js/modalManager.js` (register / minimize-to-rail) + `static/js/windowDrag.js` (drag, snap-to-edge, dock) give every tool (Command, MAGI, Music, Calendar, …) the same draggable, minimizable window chrome and open animation.
- **Theming** via CSS custom properties (`--bg`, `--panel`, `--brand-color`, `--border`, …) so features inherit the active theme automatically.

### Data & retrieval
- **SQLite via SQLAlchemy** by default (override with `DATABASE_URL`) for relational data (notes, tasks, sessions, contacts, …).
- **ChromaDB** (bundled container) with **fastembed** (ONNX) embeddings for memory/skills/RAG; hybrid vector + keyword retrieval.
- **Per-user JSON stores** under `data/` for lightweight feature state (e.g. music library/playlists at `data/music/<user>.json`). The whole `data/` directory is the persistence volume and is git-ignored.

### Bundled services (Docker Compose)
| Service | Role |
|---|---|
| `shadow` | the FastAPI app (this repo) |
| `chromadb` | vector store for memory / RAG |
| `searxng` | private metasearch backend for web tools / research |
| `ntfy` | push notifications for reminders & tasks |
| `bgutil-pot` | PO-token sidecar for the Music pipeline |

All bundled ports bind to `127.0.0.1` by default — reachable from the host, not your LAN/public, unless you opt in.

### Music pipeline
The player is **YouTube-backed** and engineered to survive YouTube's anti-bot measures on a server:
- **Search & resolve** via `yt-dlp`; resolved audio URLs are IP-locked and short-lived, so the backend **proxies the audio with HTTP Range support** so the `<audio>` element can seek.
- **`bgutil` PO-token provider** (sidecar container) mints the proof-of-origin tokens YouTube now requires.
- **Deno + `yt-dlp-ejs`** solve YouTube's n-signature JavaScript challenge (baked into the image).
- **Cookies** (`data/music/cookies.txt`, git-ignored) authenticate requests so they pass even from a datacenter IP.
- The frontend uses one persistent `<audio>` element (playback survives minimizing the window) and the **Media Session API** so laptop media keys & lock-screen controls work.

### Account-owned device companion
Open **Command**, create a single-use setup code, and run the generated command on Linux, macOS, or Windows. Linux/macOS install `companion/relay_agent.py`; Windows installs a native PowerShell/.NET relay with no Python, pip, Node, admin rights, or third-party dependencies. The agent starts at login and long-polls Shadow over HTTPS, so the PC needs no inbound port. Device credentials are stored hashed on the server; the Windows copy is additionally protected locally with DPAPI. Device lists, jobs, pending approvals, audit events, and Telegram commands are owner-scoped. The older Tailscale home-agent URL remains only as a migration adapter for `SHADOW_PC_OWNER`.

### Interactive Remote Desktop
Enable the optional `remote` Docker profile to add browser-based screen control powered by MeshCentral. Shadow provisions a separate restricted MeshCentral identity and device group per account, encrypts the per-user credential at rest, and launches the embedded console with a three-minute login token. MeshCentral terminal and file access are disabled; those capabilities stay behind Shadow's existing approval gates. The service binds to loopback and is published only as same-origin `/remote/` through the TLS reverse proxy.

### MAGI deliberation engine
**MAGI** runs one query through three independent units — **MELCHIOR・01** (Scientist), **BALTHASAR・02** (Guardian), **CASPER・03** (Skeptic) — each backed by its own endpoint/model, then resolves their verdicts into a single answer. It is text-only and entirely additive: when MAGI is disabled the normal chat path is untouched.

**Strict structured contract.** Provider output is untrusted until it parses into the verdict schema; arbitrary prose is never accepted as a successful vote.
```json
{ "stance": "APPROVE|REJECT|CONDITIONAL", "answer": "...", "confidence": 0.0,
  "reason": "...", "risks": [], "dissent": "", "next_step": "" }
```
`confidence` is normalized to `0..1` (percentages are coerced); a malformed response triggers **exactly one** repair re-ask with the exact schema, and if it still fails the unit is marked **MALFUNCTION** and excluded from voting. Parsing/validation lives in `src/magi_deliberation.py` (pure, fully unit-tested).

**Concurrent, resilient orchestration** (`src/magi_orchestrator.py`): units run in parallel with a per-unit timeout; one provider failing or timing out never aborts the others (the result is reported `degraded`). Resolution modes:
- **VOTE** — `unanimous` / `majority` (>½ of valid units) / `deadlock` / `insufficient` (1 valid) / `malfunction` (0 valid); the majority answer is selected and dissent is listed.
- **DEBATE** — one real peer-review round where each unit re-evaluates against its peers, then a re-vote on the revised verdicts. A failed debate round cleanly retains the unit's round-one verdict.
- **JUDGE** — a synthesis pass that fuses the valid verdicts into one decisive answer while explicitly reporting agreement and dissent. A deadlock auto-escalates to the judge only when ≥2 valid units exist; judge failure falls back to the vote without destroying unit results.

**Streaming.** `POST /api/magi/deliberate/stream` (SSE over a streaming `fetch`) emits independent `unit` → `peer_review`/`judge` `system` → `resolved` events, so each node updates the instant it answers instead of waiting for the slowest model. A client disconnect cancels any unfinished work. Exactly one user message and one final assistant message are persisted per deliberation; provider keys/headers are never exposed. The legacy non-streaming `POST /api/magi/deliberate` is retained. The backend enforces per-user endpoint/model visibility and rejects explicitly selected hidden/nonexistent models, while **auto** mode maximizes distinct endpoint/model assignments across the three units (`routes/magi_routes.py`).

**NERV interface** (`static/js/magi.js` + the MAGI theme in `static/style.css`): a near-black amber-terminal sub-theme with a triangular three-node layout (stacked on mobile) around a central bilingual verdict readout — `DELIBERATING / 審議中`, `APPROVED / 可決`, `REJECTED / 否決`, `MALFUNCTION / 停止` — color-coded green/red/amber by stance, with `UNANIMOUS · MAJORITY · DEADLOCK · DEGRADED` status, CSS-only scanlines and corner ticks, expandable per-unit answers, and `prefers-reduced-motion` support. Confidence is rendered as a percentage while the backend stays on the `0..1` scale.

### Deployment
- **Docker Compose**, single command. The image (`python:3.12-slim`) bundles Node, Deno, `tmux`, `gosu`, and OpenSSH; the entrypoint drops to `PUID/PGID` (default `1000:1000`) and repairs bind-mount ownership.
- For internet exposure: an **nginx** reverse proxy terminating TLS via **Let's Encrypt**, proxying to the loopback-bound app. See [DEPLOY.md](DEPLOY.md) for the hardened runbook and [MAP.md](MAP.md) for verified extension boundaries.

---

## Tech Stack

| Layer | Technologies |
|---|---|
| **Backend** | Python 3.12 · FastAPI · uvicorn · Pydantic · SQLAlchemy · httpx |
| **Auth / security** | bcrypt · PyOTP (TOTP 2FA) · cryptography · scoped API tokens |
| **AI / agents** | opencode-style agent loop · MCP · vLLM / llama.cpp / Ollama / OpenRouter / OpenAI |
| **Retrieval** | ChromaDB · fastembed (ONNX) · SearXNG |
| **Productivity** | icalendar · caldav · croniter · markdown · ntfy |
| **Music** | yt-dlp · Deno + yt-dlp-ejs · bgutil PO-token provider |
| **Frontend** | Vanilla JS (ES modules) · CSS custom properties · PWA · Media Session API |
| **Infra** | Docker Compose · nginx · Let's Encrypt · Tailscale (companion) |

---

## Repository Layout

```
app.py              FastAPI app: builds managers, mounts every router
setup.py            first-boot admin provisioning
routes/             per-feature API modules (setup_*_routes → /api/*)
src/                agent loop, LLM core, RAG/memory, scheduler, pc control, helpers
services/           memory store + caches
static/             SPA — app.js orchestrator, js/<feature>.js modules, style.css
companion/          home-PC agent (Tailscale-only, allowlisted)
scripts/            `shadow` CLI dispatcher + shadow-<subcommand> tools
docker/             entrypoint (PUID/PGID drop + ownership repair)
config/             bundled service config (e.g. SearXNG)
docs/               landing page + screenshots/clips
docker-compose.yml  shadow + chromadb + searxng + ntfy + bgutil-pot
Dockerfile          python:3.12-slim + Node + Deno + system deps
```

---

## Quick Start

Defaults work out of the box: clone, run, then configure models/search/email inside **Settings**. Only edit `.env` for deployment-level overrides (`APP_BIND`, `APP_PORT`, `AUTH_ENABLED`, `DATABASE_URL`, a pre-seeded admin password, …).

On first setup Shadow creates an admin account (`admin` unless `SHADOW_ADMIN_USER` is set) and prints a temporary password in the terminal — for Docker, see `docker compose logs shadow`. Use it to log in, then change it in **Settings**.

### Docker (recommended)
```bash
git clone https://github.com/haykdevx/dev.git
cd dev
cp .env.example .env        # optional, recommended for explicit defaults
docker compose up -d --build
```
Open `http://localhost:7000` once the containers are healthy. The UI binds to `127.0.0.1` by default; set `APP_PORT` if `7000` is taken. Use `--host 0.0.0.0` only when you intentionally want LAN/reverse-proxy access, and keep authentication plus TLS enabled.

### Native (Linux / macOS)
```bash
git clone https://github.com/haykdevx/dev.git
cd dev
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python setup.py
python -m uvicorn app:app --host 127.0.0.1 --port 7000
```
Requirements: **Python 3.11+**. Cookbook also needs `tmux`. The Music player additionally needs **Deno** + `yt-dlp[default]` (both already in the Docker image).

### Demo mode
Try the Command dashboard without pairing a real PC:
```bash
printf "\nSHADOW_DEMO_MODE=true\n" >> .env
docker compose up -d --build
```
Demo mode exercises the same API shape and approval flow but never touches a real machine. Details: [docs/demo-mode.md](docs/demo-mode.md).

---

## Configuration

All runtime configuration lives in `.env` (copy from `.env.example`). Common keys:

| Key | Purpose |
|---|---|
| `APP_BIND` / `APP_PORT` | bind address / port (default `127.0.0.1:7000`) |
| `AUTH_ENABLED` | require login (keep `true` for any non-loopback bind) |
| `SECURE_COOKIES` | set `true` behind HTTPS |
| `DATABASE_URL` | SQLAlchemy URL (default SQLite under `data/`) |
| `SHADOW_ADMIN_USER` / `SHADOW_ADMIN_PASSWORD` | pre-seed the first-boot admin |
| `ALLOWED_ORIGINS` | comma-separated allowed origins for cookies/CORS |
| `SHADOW_PC_OWNER` + `SHADOW_HOME_AGENT_*` | optional legacy single-device migration adapter |
| `SHADOW_MESH_*` | optional account-isolated MeshCentral Remote Desktop profile |
| `SHADOW_TELEGRAM_BOT_TOKEN` | enables the account-scoped Telegram remote and web inbox |
| `MUSIC_COOKIES_FILE` | path to the YouTube cookies file (default `data/music/cookies.txt`) |

---

## Command-line suite

A `git`-style dispatcher exposes every feature on the shell:

```bash
scripts/shadow                 # list subcommands
scripts/shadow mail list
scripts/shadow calendar today
scripts/shadow backup snapshot
```

Each `shadow-<name>` script is a standalone CLI; symlink `scripts/shadow` onto your `$PATH` to use it from anywhere.

---

## Security model

- Multi-account data is owner-scoped; **keep `AUTH_ENABLED=true`** whenever bound outside loopback, and never expose the app port directly to the public internet — front it with the nginx + TLS setup in [DEPLOY.md](DEPLOY.md).
- Cookie sessions + bcrypt + optional **TOTP 2FA**; scoped, revocable API tokens for paired clients.
- PC control is opt-in and account-owned. Enrollment codes are short-lived and single-use; device tokens are hashed server-side; foreign device IDs are rejected; no user can request or inherit access to another user's PC. The legacy bridge is visible only to the exact `SHADOW_PC_OWNER` username.
- Telegram pairing maps one Telegram identity to one Shadow account. The web inbox returns only that owner's bot conversations; it is not a personal Telegram/MTProto client.
- Secrets and runtime data (`.env`, `data/`, cookies) are git-ignored and never committed.

---

## Acknowledgments

Shadow is a fork of [Odysseus](https://github.com/pewdiepie-archdaemon/odysseus); its upstream MIT license and acknowledgments are preserved. It also builds on the work of [opencode](https://github.com/anomalyco/opencode), [llmfit](https://github.com/AlexsJones/llmfit), and [Tongyi DeepResearch](https://github.com/Alibaba-NLP/DeepResearch). See [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).

## License

MIT — see [LICENSE](LICENSE).

## Star History

<a href="https://www.star-history.com/?repos=haykdevx%2Fdev&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=haykdevx/dev&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=haykdevx/dev&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=haykdevx/dev&type=date&legend=top-left" />
 </picture>
</a>
