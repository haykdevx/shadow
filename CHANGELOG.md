# Shadow Changelog

## Unreleased

### Production hardening (2026-11)

- **Fixed a broken fresh install.** The Remote Desktop merge had deleted
  `.env.example` and the Cookbook model catalog; both are restored, the
  SQLite data directory is created on first start, and the suite passes on a
  fresh clone again.
- **Admin pre-seeding now works in Docker.** `SHADOW_ADMIN_USER` /
  `SHADOW_ADMIN_PASSWORD` were documented but only read by the interactive
  `setup.py`; the app now seeds the first admin from them at startup and
  warns loudly when an unconfigured instance is left in first-visitor setup
  mode.
- **Runtime bugs:** device pairing page raised `NameError` (missing
  `require_admin` import); email auto-summary raised `UnboundLocalError`
  from a shadowed `_q` helper.
- **Login page polish:** no more 401 console errors from theme sync before
  sign-in; the window-snap tip no longer overlaps the Notes intro hint;
  Telegram iframe permission warnings removed.
- **Offline-ready assets:** KaTeX and Mermaid are vendored under
  `static/lib/` instead of loaded from jsdelivr, so math and diagrams render
  on air-gapped or CDN-blocked installs and the CSP no longer needs the CDN
  for styles/fonts.
- **One-command VPS install:** `deploy/vps-install.sh` (Docker + Caddy HTTPS
  + UFW + admin seeding, idempotent re-runs).
- **Repo hygiene:** removed committed backup trees and duplicated modules
  (`services/*` copies now alias the canonical `src/*` implementation), fixed
  stale repository URLs, removed unused imports.
- **CI:** pytest on Python 3.11–3.13, ruff defect rules, JS unit tests,
  `docker compose config`, Dependabot; `requirements-dev.txt` for a fast
  test install; healthcheck on the `shadow` container.

### Agent Sessions & Unattended mode

- Added **Agent Sessions**: a direct, conversational tool loop (no planner
  DAG) for everyday coding tasks — pick a workspace and a model, type the
  task, and watch the live activity timeline, terminal output, changed-file
  diffs, and final report; follow-up messages continue in context, with
  Stop / Resume / Retry / Rollback controls.
- Added the persistent **Unattended** permission mode: per-workspace, never
  asks for approval — project edits, commands, tests, package installation,
  network access, and safe git run immediately while privilege escalation,
  destructive git, secrets paths, and anything outside the workspace are
  refused with a concise error. Survives restarts; persistent UI indicator
  with one-click disable.
- Provider failures now preserve the session as failed + retryable with the
  provider error shown, optional automatic fallback to other configured
  models, and one-action retry on another model.
- Added a live end-to-end check (`scripts/e2e_agent_workspace_check.py`)
  covering the full workspace contract incl. unattended execution, escape
  rejection, and rollback against a real server + relay agent.
- `SHADOW_AUTH_PATH` can now point isolated test instances at their own
  account store.

### Product identity

- Rebranded the visible workspace, login, companion identity, and PWA metadata
  as Shadow while retaining upstream-compatible internal identifiers.
- Added a compact HUD home surface for linked-PC status and quick actions.

### Private PC control

- Added a Tailscale-oriented home-PC companion with an allowlisted action API.
- Added immediate read-only status, process, screenshot, and clipboard access.
- Added short-lived explicit approvals for lock, typing, keypress, clipboard
  writes, media, volume, and application control.
- Added native `pc_control` agent tooling with non-admin blocking.

### Remote access

- Added an optional Telegram bridge that reuses Shadow's existing owner-scoped
  chat API and supports callback approval for PC lock requests.
- Added hardened VPS, Caddy, Tailscale, boot service, backup, and secret
  handling documentation.

### Upstream foundation

Shadow is a fork of [Odysseus](https://github.com/pewdiepie-archdaemon/odysseus).
The upstream MIT license and acknowledgments remain in this repository.

