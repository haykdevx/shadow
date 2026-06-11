# Shadow — Status (AFTER)

Re-recorded 2026-06-11 at the end of the browser/stabilization/MAGI pass.
The BEFORE state (recorded 2026-06-10) is preserved at the bottom for diffing.

## Verification evidence (all run on this box, this checkout)

- `.venv/bin/python -m pytest -q` → **903 passed, 2 skipped, 0 failed**
  (BEFORE: 9 failed, 851 passed). Stable across consecutive runs.
- `node --check` on `static/js/commandPage.js`, `static/js/magi.js` → clean.
- `python -m py_compile` on every changed Python file → clean.
- `docker compose config -q` → valid.
- Authenticated e2e smoke against a live `uvicorn` instance (temp user,
  auth.json backed up and restored byte-for-byte afterwards): **19/19 checks
  pass**. Three initially failed because this host's upstream DNS sinkholes
  `example.com` to `0.0.0.17` (environment, not code — verified with
  `dig @8.8.8.8`); re-run against `wikipedia.org` all pass:
  - unauthenticated `/api/browser/*` and `/api/magi/deliberate` → 401
  - navigate / read / screenshot (JPEG) round-trip
  - `eval` gated → `pending_confirmation` → approve via cookie route → runs
  - double-confirm → 404 (approval consumed atomically)
  - gate on page A, navigate to page B, confirm → **409** (page-drift guard)
  - pending list never exposes raw params or full page URL
  - `169.254.169.254` navigation blocked; shot-id traversal rejected
  - file-queue browse task (the Telegram path) executed by the in-app worker
  - **live MAGI deliberation** (OpenRouter free models): unanimous
    CONDITIONAL → auto-escalated to judge (`judge_escalated`), weighted
    tally `{CONDITIONAL: 2.5}`, diversity warning fired (2 distinct models
    across 3 units), 0 malfunctions
  - **live evidence-grounded deliberation**: browser fetched
    `wikipedia.org` (2022 chars), all units received the shared evidence
    block, unanimous grounded APPROVE

## What was built / fixed in this pass

### Agent browser (was: half-wired; now: complete)
Native in-process tool (`browser`) — native tools receive `owner`, MCP stdio
tools do not, and per-user isolation is non-negotiable.
- `src/browser_manager.py`: per-owner persistent Chromium profile
  (`data/browser/profiles/<slug>`, chmod 0700), action set (navigate, read,
  links, click, fill, press, eval, screenshot, tabs, download, …),
  pure `classify_risk()` gate (eval always; secret-field fills; risky
  click/press words and URLs; unknown actions), URL policy (http/https only,
  allow/deny lists, private-host + DNS-resolution SSRF checks, post-redirect
  re-check, subresource route guard), per-owner rate limit, LRU context
  eviction + idle reaper, redacted logs/history.
- Confirmation gate: gated actions become pending approvals (in-memory, TTL).
  Approval only via interactive cookie routes — agents/API tokens cannot
  self-approve. Approval is consumed atomically (no double-fire) and is
  voided with 409 if the page navigated since gating (`_page_context` drift
  guard added in the hardening pass).
- Registered like every other native tool (schemas, parsing aliases, index,
  admin-only security lists, dispatch, `do_browser`), admin-only on
  multi-user installs.
- Command-center Browser panel (`static/js/commandPage.js`): live capture,
  command box (URL / search / `click <text>` / `js <code>` / read / back),
  approve/deny pending rows, history.
- Telegram `/browse <url|search>`: file-backed task queue over shared
  `data/` (`src/browser_tasks.py`) — no new network auth surface; summary +
  screenshot photo reply.
- MAGI optional evidence source (below). Playwright chromium baked into the
  Docker image; env knobs documented in `.env.example`.

### Stabilization
- All 9 baseline test failures root-caused and fixed (2 stale topic-analyzer
  tests vs. an intentional cross-tenant fix — tests updated + 2 new security
  guards; 7 order-dependent failures caused by tests stomping `sys.modules`
  with MagicMocks at collection time — conftest now pre-imports the real
  modules and the offending test files restore what they replace).

### MAGI upgrade
- Confidence-weighted voting (opt-in `weighted`): stances ranked by summed
  confidence; exact tie → judge.
- All-CONDITIONAL and CONDITIONAL-winner outcomes now escalate to the judge
  for condition synthesis (was: counted as plain consensus).
- Evidence grounding (opt-in `evidence`/`evidence_query`): one shared
  browser fetch before deliberation, identical untrusted-framed block to all
  units; retrieval failure degrades to evidence-free with `evidence_error`
  surfaced — never aborts.
- Model-diversity validation: result carries `model_diversity`
  {unique_models, total_units, warning}; duplicated-model installs get a
  visible warning (markdown, SSE event, UI label), never a block.
- Resolution renamed `judge_deadlock` → `judge_escalated`; payload spec and
  UI toggles (weighted / evidence) updated. One unit's failure still never
  aborts a deliberation (verified live and in tests).

### Hardening pass (boundaries)
- Browser: page-drift 409 guard; pending display never carries raw
  params/full URLs; screenshot/tabs polling exempted from the action rate
  limit so the 10s UI poll can't starve interactive actions; browse-task
  worker got a 110s hard cap per task so one stuck page can't stall the
  queue.
- Telegram bridge: one malformed update can no longer crash the main loop
  (callbacks ran inline; now wrapped), message-handler threads report
  failures back to the chat instead of dying silently, poison `update_id`s
  are skipped past.
- app.py: the global 45s request-timeout middleware was killing legitimate
  long MAGI deliberations (`timeout_seconds` ≤ 600) and browse tasks —
  found live when a healthy deliberation 504'd; `/api/magi/deliberate` and
  `/api/browser` are now exempt (both enforce their own internal deadlines).
- Tests: 903 total; new coverage for drift guard, redaction, diversity,
  weighted ties, conditional escalation, evidence propagation.

## Known limitations (honest)

- Pending browser approvals are in-memory: a server restart drops them
  (fail-closed — the gated action simply never runs; re-request it).
- DNS-rebinding remains a theoretical residual SSRF vector: main-frame
  navigations are re-checked post-redirect, but subresource requests are
  policy-checked by hostname only. Full protection needs a proxy layer.
- The DNS SSRF check fails open on resolver errors (deliberate: Playwright
  then surfaces the real error) — combined with a hostile resolver this is
  the same residual class as above.
- Browse-task queue trusts the sidecar's `owner` field; the trust boundary
  is the shared `data/` volume (0700), same model as the existing shadow-*
  bridges. The worker runs tasks serially.
- MAGI MALFUNCTION path is covered by unit tests with fake models; the live
  smoke exercised the happy paths (forcing a live malfunction requires a
  deliberately broken endpoint).
- Free-tier OpenRouter models are slow; non-stream deliberations can take
  >60s. Use `/deliberate/stream` for UX.
- `builtin_browser` (optional npx Playwright-MCP server) still exists and is
  toggle-mapped together with the native tool; it never starts unless
  `@playwright/mcp` is pre-cached.
- This host's upstream DNS currently sinkholes `example.com` (and possibly
  other domains) to `0.0.0.17` — affects any browsing feature, unrelated to
  Shadow.

## Git state

- Branch `feat/command-page`. This pass's work is uncommitted alongside the
  pre-existing MeshCentral remote-desktop WIP (kept untouched).
- `tests/bombadil-spec.ts` (pre-existing untracked artifact) — removed in
  the polish phase.

---

# Appendix: Ground-Truth Status (BEFORE, recorded 2026-06-10)

- `uvicorn app:app` started cleanly; expected degraded warnings without
  docker services (ChromaDB at `localhost:8100`, `python-magic`).
- Test suite: **9 failed, 851 passed, 2 skipped** — 2 stale topic-analyzer
  tests, 7 order-dependent failures from cross-test `sys.modules` pollution
  (`test_research_query_fallback`, `test_scheduler_restart_doublefire`,
  `test_security_regressions::test_auth_manager_migrates_legacy_admin_role`).
- MAGI: structured verdicts, repair → MALFUNCTION, fan-out, VOTE/DEBATE/JUDGE,
  deadlock escalation, SSE — but no evidence pre-step, no weighted vote,
  all-CONDITIONAL never escalated, no diversity validation.
- **Agent browser: half-wired.** Only an optional `npx @playwright/mcp`
  server (`src/builtin_mcp.py:78`), not cached so it never started; no
  engine, no per-user profiles, no gates, no policy, no panel, no Telegram
  path, no MAGI hook; `playwright` absent from venv/requirements/Dockerfile.
- Working: auth, chat + agent loop, PC command center (gated approvals),
  Telegram PC bot, embedded tweb client, music player (VPS), desktop Qt
  browser (separate human-facing track), MeshCentral remote desktop (WIP,
  uncommitted).
