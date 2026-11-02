# Shadow — Status

Recorded 2026-11-02 after the production-hardening pass on branch
`claude/bold-einstein-da41j3`. Everything below was run on this checkout.

## Verification evidence

- `python -m pytest -q` → **1131 passed, 3 skipped, 0 failed** on a fresh
  clone (before this pass: 6 failed / collection crash without `data/`).
- `ruff check .` (defect rules: syntax, undefined names, unused imports) → clean.
- `node --test tests/js/*.test.cjs` → 27/27 pass.
- `node --check` on every file under `static/js` and `static/app.js` → clean.
- `python scripts/public_readiness_check.py --strict` → passed.
- `docker compose config -q` → valid.
- Browser smoke (`scripts/e2e_ui_smoke.js`, Chromium via Playwright) against a
  live `uvicorn` with a seeded admin: login → workspace in ~1 s (DOMContentLoaded
  ≈ 0.9 s, 200 KB transferred), all 13 workspace views open with **0 page
  errors, 0 failed requests, 0 same-origin HTTP errors**, no main-thread
  long task ≥ 120 ms. Math/diagram assets now load same-origin.

## What this pass fixed

### Fresh install was broken
The Remote Desktop merge (`b87c528`) deleted `.env.example` and
`services/hwfit/data/hf_models.json`. `cp .env.example .env` failed for every
documented install path and Cookbook had no catalog. Both restored.
`core/database.py` now creates the SQLite directory before opening the engine.

### Admin pre-seeding did nothing in Docker
`SHADOW_ADMIN_USER` / `SHADOW_ADMIN_PASSWORD` were only read by the
interactive `setup.py`. A plain `docker compose up` left the instance in
first-visitor setup mode. `core.auth.bootstrap_admin_from_env` now seeds the
admin at startup (tests in `tests/test_admin_bootstrap_env.py`); without a
password the server logs a loud warning.

### Runtime bugs
- `companion/routes.py`: `require_admin` was never imported → device pairing
  page 500'd.
- `routes/email_pollers.py`: a nested `import quote as _q` shadowed the
  module-level IMAP folder quoter → `UnboundLocalError` in auto-summary.

### UI polish
- Login page: theme sync no longer calls authenticated endpoints before
  sign-in (removed two 401 console errors per visit).
- Window-snap tip no longer overlaps the Notes intro hint.
- Telegram iframe no longer declares unrecognized permission features.
- KaTeX and Mermaid vendored under `static/lib/` (CSP `style-src`/`font-src`
  no longer reference the CDN).

### Repo and CI
- Removed committed backup trees (1.3 MB) and seven duplicated modules
  (`services/*` copies alias `src/*`; the diverged `memory.py` pair merged).
- Fixed stale `haykdevx/dev` URLs; removed debug `console.log`s; 90 unused imports.
- CI now runs pytest (3.11–3.13), ruff, the JS tests and `docker compose
  config` instead of syntax checks only. Dependabot enabled.
- `deploy/vps-install.sh`: one-command VPS install (Docker, Caddy HTTPS, UFW,
  admin seeding, health wait; idempotent re-runs).

## Known limitations (honest)

- `src/search/{core,content,providers}.py` and `services/search/*` have
  diverged and both are still imported; merging them needs a behavioural
  review, not a mechanical alias.
- `requirements.txt` is still unpinned; Dependabot now tracks it, but a lock
  file would make Docker builds reproducible.
- `static/style.css` (36 k lines, 1.3 k `!important`) and the 17 JS modules
  over 2 000 lines remain the main maintainability risk; the `innerHTML`
  audit called out in ROADMAP has not been done.
- 234 `except Exception: pass` sites in the backend still swallow failures
  silently.
- Pending browser approvals are in-memory and lost on restart (fail-closed).
- The Pyodide code runner still loads from jsdelivr on demand.
