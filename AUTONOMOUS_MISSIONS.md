# Autonomous Missions, Desktop Workspace & Permission Policy

Shadow's autonomous engineering workspace: open a local project on one of
your own machines, give Shadow a goal, and let it inspect, edit, test, and
manage real files — with every action decided by a server-side policy engine
and re-checked by the device agent. Linux and macOS use the Python relay;
Windows uses the dependency-free native PowerShell/.NET relay. Every device
connects outbound over HTTPS and opens no inbound port.

- UI: **Missions** in the sidebar (route `/missions`) — Agent Sessions,
  Missions, and the Desktop Workspace share the page.
- Backend: `src/mission_policy.py`, `src/mission_workspaces.py`,
  `src/mission_engine.py`, `src/agent_sessions.py`, `routes/mission_routes.py`.
- Device: `companion/workspace_agent.py` on Linux/macOS and the matching
  native implementation in `companion/shadow-device.ps1` on Windows,
  dispatched through the same outbound relay protocol.
- State: `data/missions/` (atomic JSON documents — no SQL schema changes,
  hence no database migrations; this matches the established
  `data/shadow-devices.json` pattern).

## Architecture

```
Browser (missionsPage.js)
   │ cookie-authenticated REST (/api/missions/*)
   ▼
routes/mission_routes.py — interactive sessions ONLY (API tokens & the
   │                       internal agent bridge are rejected outright)
   ▼
src/mission_engine.py ──── plans goals into a task DAG, assigns models to
   │                       roles, runs independent tasks concurrently,
   │                       persists after every transition, pauses on
   │                       approvals, survives restarts, reports
   ▼
src/mission_workspaces.py — every tool call becomes a declared
   │                        ActionRequest → src/mission_policy.evaluate()
   │                        → ALLOW / REQUIRE_APPROVAL / DENY
   ▼
src/shadow_devices.py ──── existing outbound relay (device long-polls over
   │                       HTTPS; no inbound port, SHA-256 device tokens)
   ▼
companion/workspace_agent.py — executes on the user's machine; resolves
                               every path with os.path.realpath and
                               re-checks BOTH the workspace root and the
                               device-local SHADOW_ALLOWED_ROOTS
```

### Missions

A mission is one goal against one workspace. The **planner** model
decomposes it into 2–20 tasks with explicit dependencies (validated: unique
ids, known roles, acyclic). Tasks carry one of five roles — `planner`,
`researcher`, `implementer`, `reviewer`, `tester` — and each role maps to
any configured `{endpoint_id, model}` pair (OpenRouter, OpenAI-compatible,
local llama.cpp/vLLM/Ollama, a scripted stub — **no provider is
hardcoded**). Independent tasks run concurrently (cap 3). Genuinely
blocking ambiguity becomes one clarification question; everything else is
planned through.

Execution is a bounded JSON tool loop per task (max 14 steps, 2 attempts,
80 LLM calls / 250 device actions / 4 h per mission). The model emits
`{"tool": ...}` / `{"done": ...}` / `{"fail": ...}`; malformed output is
re-prompted; an unknown tool is an error result, not an executed action.
One failed task never crashes the mission — dependents are skipped
transitively, independent branches continue, and the mission ends
`completed_with_failures` with the failure history preserved.

Every mission durably records: files read, files changed, commands
executed, per-model usage (calls, **estimated** tokens at chars/4, wall
time), events, approvals, retries, the checkpoint, and the final report
(`## Changes / Evidence / Tests / Risks / Remaining work`), written by the
reviewer model with a deterministic fallback.

**Safety before mutation:** the first mutating action automatically opens a
checkpoint: per-file pre-images are snapshotted on first touch
(`.shadow-checkpoints/<id>/` on the device), uncommitted user work is
detected and flagged, and on a clean git tree the mission moves to a
`shadow/mission-<id>` branch. Rollback is one action — per file or the
whole mission (pre-images restored, created files moved to trash, base
branch restored). This works identically in git and non-git folders.

**Crash recovery:** mission state is written atomically after every
transition. On startup, `recover_missions()` marks orphaned running
missions `paused` and resets in-flight tasks to `ready`; Resume continues
the plan.

### Agent Sessions (direct, no planner DAG)

Simple coding/file tasks should not pay for an oversized planner. An **Agent
Session** (`src/agent_sessions.py`) is one model + one workspace + one
conversational JSON tool loop: pick a device/workspace, pick any configured
model, type the task, press Run. The model immediately inspects and edits the
real files through the exact same policy-checked dispatch path missions use.

The session view shows a chronological activity timeline, live terminal
output (command, exit code, stdout/stderr), the changed-file list with
per-file revert, a unified diff viewer, and the final factual report.
Follow-up messages join the same conversation — mid-run they are queued and
drained between steps; after completion they continue the session in context.
Stop, Resume, Retry, and Rollback are one click each.

Safety is identical to missions: a checkpoint opens automatically before the
first mutation, deletes are soft (workspace trash), every file read/changed
and command executed is recorded durably, and sessions interrupted by a
restart are recovered as `stopped` + retryable — never stranded `running`.

Provider failures are session state, not crashes: the provider error is
shown verbatim, the record is preserved as `failed` + retryable, **Retry with
another model** is one action, and when *automatic fallback* is enabled the
session rotates to the next configured fallback model by itself.

### Desktop Workspace

The same policy-checked path powers a desktop-style explorer per
workspace: file tree, filename + content search, open/edit/save (with
sha256 conflict detection against external edits), create/rename/upload/
download, soft delete (workspace trash), unified-diff preview with
accept/reject, git status/branches/history/diff/commit, and a command
runner (10-minute cap, 200 KB output cap). Binary files are detected and
not editable. A workspace is one explicitly authorized folder on one
enrolled device for one account.

## Permission modes (Codex-style)

Selected per workspace, shown beside every mission; the modal supports
↑/↓ + Enter + Escape with the current mode highlighted.

| | `ask` — Ask for approval | `auto` — Approve for me (default) | `unattended` — Unattended | `full` — Full access |
|---|---|---|---|---|
| Read / inspect workspace | allowed | allowed | allowed | allowed |
| Edit files in workspace | ask | allowed | allowed | allowed |
| Ordinary project commands | ask | allowed | allowed | allowed |
| Dependency installation | ask | ask | **allowed** | allowed |
| Network access | ask | ask (unless granted for the mission) | **allowed** | allowed |
| Outside the workspace | ask | ask | **DENY** (no prompt) | allowed **within device roots** |
| Privilege escalation / power / security settings / destructive git / secrets | ask | **ask** | **DENY** (no prompt) | allowed |
| Cross-user / cross-device | DENY | DENY | DENY | **DENY** |

`unattended` is the persistent autonomy mode: the policy engine never
returns REQUIRE_APPROVAL for it — everything is either an immediate ALLOW or
a concise DENY the model adapts to. It is enabled once per workspace,
stored on the workspace record (survives restarts), indicated by a
persistent UNATTENDED banner, and disabled with one click. The hard deny
list is not negotiable: paths outside the workspace/`SHADOW_ALLOWED_ROOTS`,
symlink/traversal escapes, cross-account access, privilege escalation,
power control, security settings, destructive git (`reset --hard`, force
push, `clean -fd`, history rewrites), root-level filesystem destruction,
credentials/secrets paths (unless the workspace root itself is that
directory), and Shadow's own credential files.

`full` requires password reauthentication, is never on by default, takes an
optional time limit (15 min–8 h, default 1 h), shows a persistent
`FULL ACCESS` banner, writes immutable audit records
(`data/missions/audit.log`, append-only, 0600), is keyed to exactly one
owner+device pair, and still cannot leave the device agent's
`SHADOW_ALLOWED_ROOTS`.

### Approval decisions

Every `REQUIRE_APPROVAL` becomes a card offering:

- **Allow once** — consumed by the next matching action.
- **Allow for this mission** — expires with the mission.
- **Always allow this action in this workspace** — a persistent rule,
  visible and revocable under *Permission rules*.
- **Decline** — the model is told and adapts.
- **Stop mission** — emergency stop.

Grant keys are derived from the action's *danger classification* (e.g.
`shell:pip-install,pkg-fetch`), not its exact text — "always allow
installing dependencies" works, while a newly dangerous command never
inherits an old grant. Session/mission grants are in-memory by design: a
restart drops them (fail closed). Approvals can only be resolved by an
interactive cookie session — API tokens and the internal agent bridge get
403, so **a model can never approve its own action**.

## Enforcement architecture

Every tool call is declared (`src/mission_policy.ActionRequest`):
capability (closed list of 12), owner, device, workspace, mutating flag,
risk, network flag, outside-roots flag, raw command, target path,
human-readable summary. `evaluate()` is pure and deterministic; models
never see or influence it.

Path hygiene is layered:

1. **Server (syntactic):** unicode NFC, percent-encoding rejected, null
   bytes rejected, `\` unified, `.`/`..` resolved, UNC shares refused
   unless the root authorizes them, Windows containment is
   case-insensitive.
2. **Device (authoritative):** `os.path.realpath` resolution — symlink
   escapes are caught against the real filesystem — and containment in
   BOTH the per-job workspace root and the device-local
   `SHADOW_ALLOWED_ROOTS`. A compromised server still cannot direct the
   agent outside what the device owner configured locally.
3. Mutating agent actions additionally require the relay job to carry
   `confirmed=true`, which only the policy/approval path sets.

Dangerous-command classification (regex tables in `mission_policy.py`)
covers: recursive/forced deletes, device writes, formatting; sudo/doas/
runas/UAC; shutdown/reboot/sleep; kill/taskkill; git force-push,
hard-reset, clean, history rewrites; system + language package managers
and curl-pipe-sh; firewall/users/passwords/services/registry; SSH keys,
cloud credentials, `.env` files, GPG, browser profiles, password stores,
system directories, and Shadow's own credential files.

## Threat model

| Threat | Mitigation |
|---|---|
| Model self-escalation (granting its own permissions) | Policy engine is server-side; approval routes reject agent/API credentials; grants are keyed per owner |
| Prompt-injected destructive command | Classification + gating happen outside the model; `auto` mode asks; declined actions return as text, not execution |
| Path traversal / encodings / null bytes | Rejected syntactically server-side and re-checked on-device |
| Symlink escape | `os.path.realpath` containment on the device |
| Compromised/buggy server directing the agent | Agent-local `SHADOW_ALLOWED_ROOTS` is an independent boundary; mutations need `confirmed=true` |
| Cross-user / cross-device access | Workspaces bind owner+device; every lookup is owner-scoped; structural DENY in all modes incl. full |
| Stolen API token approving actions | 403 on every mission/workspace/policy route |
| Full-access abuse | Password reauth, time limit, persistent indicator, immutable audit, per-device scope |
| Runaway mission (cost/loops) | Hard budgets: steps, attempts, LLM calls, device actions, wall clock, command timeout, output caps |
| Data loss from edits | First-touch pre-image checkpoint, soft deletes to trash, conflict detection, per-file/full rollback, git branch when clean |
| Restart mid-mission | Atomic persistence + startup recovery to a resumable paused state |
| Internet exposure of the device | Outbound-only relay (unchanged); no new listening sockets anywhere |

Residual risks (honest): `auto` mode runs *ordinary* commands without
approval — a workspace's own build scripts are trusted once you run them
(same trust as running `make` yourself); the dangerous-command tables are
deny-lists and cannot be exhaustive (mode `ask` exists for zero-trust
review); approvals consumed by classification mean two same-class commands
are interchangeable under a mission-scoped grant; estimated token counts
are estimates.

## Device agent setup

Command generates a one-line, account-scoped installer for every supported OS.
Devices enrolled before workspace support should run the installer's repair/update
path once so they advertise the `ws_*` and `git_*` capabilities.

- **Linux**: installs the Python relay to `~/.local/share/shadow-device` and
  runs it as the enabled systemd user service `shadow-device.service`.
- **macOS**: installs the same Python relay to
  `~/Library/Application Support/Shadow/device-agent` and runs it through
  the `io.shadow.device` launch agent.
- **Windows 10/11**: installs the native PowerShell 5.1/.NET relay. It has
  the same workspace, git, command, checkpoint, and rollback contract and
  requires no Python, pip, Node, package manager, administrator rights, or
  third-party runtime. The enrollment token is protected with current-user
  DPAPI and the relay starts through HKCU Run (Startup-folder fallback).
- **Every OS**: `SHADOW_ALLOWED_ROOTS` is the device-local outer boundary.
  The default is the current user's home/profile directory; narrow it for
  shared machines. Every job also carries one explicit workspace root, and
  the device requires the path to be inside both boundaries.

## API specification

All routes under `/api/missions`, interactive cookie session required
(401 anonymous / 403 token):

| Method & path | Purpose |
|---|---|
| `GET/POST /workspaces` | List / authorize a folder (`device_id`, `root`, `name`, `mode`) |
| `DELETE /workspaces/{id}` | Revoke authorization |
| `PUT /workspaces/{id}/mode` | `ask\|auto\|full` (full requires armed full access) |
| `POST /workspaces/{id}/action` | One workspace action: `{action, args}`. Returns `{ok, result}` or `{approval_required, summary, reason, risk, grant_key}` |
| `POST /policy/grants` | Record a user decision: `{grant_key, scope: once\|mission\|session\|workspace, workspace_id, mission_id, summary}` |
| `GET /policy/rules`, `DELETE /policy/rules/{id}` | List / revoke persistent rules |
| `POST /policy/full-access` | Arm: `{device_id, password, duration_seconds?}` |
| `GET/POST /sessions` | List / create+start an agent session: `{workspace_id, task, endpoint_id, model, fallbacks?, auto_fallback?}` |
| `GET /sessions/{id}` | Full session record (timeline, terminal, files, report) |
| `POST /sessions/{id}/message` | Follow-up message (queued mid-run; restarts a finished session in context) |
| `POST /sessions/{id}/stop\|resume` | Stop / resume |
| `POST /sessions/{id}/retry` | Retry, optionally `{endpoint_id, model}` to switch models |
| `POST /sessions/{id}/rollback` | `{paths?}` — per-file or full rollback |
| `POST /sessions/{id}/approvals/{aid}` | `{decision: allow_once\|allow_session\|allow_always\|decline\|stop_session}` |
| `GET/DELETE /policy/full-access/{device_id}` | Status / disarm |
| `GET /models` | Configured `{endpoint_id, models[]}` pairs + role names |
| `POST /` (missions) | Create + plan: `{workspace_id, goal, mode, allow_network, roles{role: {endpoint_id, model}}}` |
| `GET /`, `GET /{id}` | List / full mission record |
| `POST /{id}/clarify` | Answer the planner's question |
| `POST /{id}/start\|pause\|resume\|stop` | Lifecycle (stop = emergency stop) |
| `POST /{id}/rollback` | `{paths?: [..]}` — per-file or full rollback |
| `POST /{id}/approvals/{aid}` | `{decision: allow_once\|allow_mission\|allow_always\|decline\|stop_mission}` |

Workspace actions (`action` values): `ws_tree, ws_stat, ws_read, ws_search,
ws_hash, ws_diff, ws_write, ws_mkdir, ws_rename, ws_delete, ws_patch,
ws_run, git_info, git_log, git_diff, git_commit, git_checkout,
ws_checkpoint, ws_restore`.

## Test report (2026-06-12)

- Full suite: **1112 passed, 2 skipped, 0 failed** (1041 before the
  unattended/agent-session work; **+71 new tests**, all green).
  - `tests/test_unattended_mode.py` (45): every project-work class allowed
    without asking; every hard-deny class (privilege escalation, power,
    security settings, root-level destruction, destructive git, secrets,
    Shadow credentials, outside-roots, cross-tenant) denied without a
    prompt; REQUIRE_APPROVAL proven unreachable; workspace-root-covers-
    secret nuance; persistence across registry reloads; dispatch-level
    allow/deny integration.
  - `tests/test_agent_sessions.py` (18): full loop to completion with
    checkpoint-before-mutation ordering, terminal recording, malformed-JSON
    repair, unknown-tool rejection, policy-denial adaptation, provider
    failure → failed+retryable, auto-fallback rotation, opt-in-only
    fallback, retry on another model, follow-up context continuity,
    stop/resume, rollback, crash recovery, cross-user isolation.
  - `tests/test_mission_routes_auth.py` (+15 cases): anonymous 401 and
    API-token/agent-identity 403 on every session route.
- Earlier mission-suite coverage (described below) is unchanged and green.
- Live end-to-end (`scripts/e2e_agent_workspace_check.py` — real uvicorn
  server, real Linux relay agent, dedicated temporary workspace, isolated
  account store): **33/33** — tree/read/write/patch on disk, command exit
  codes, checkpoint → rollback restoring pre-images and removing created
  files, relative/absolute path-escape rejection, symlink escape stopped by
  the device agent's realpath check, cross-account workspace and device
  authorization rejected, installer-class command running with **no
  approval in unattended mode** while the identical command in `auto` mode
  returns an approval card, `sudo rm -rf /` denied without a prompt, and a
  complete agent session against a scripted OpenAI-compatible endpoint that
  wrote a real file through the relay, ran a verification command, reported,
  and was rolled back.
  - `tests/test_mission_policy.py` (52): all three modes, session-grant
    expiry, dangerous-command and secrets detection, internet-access
    gating, cross-user grant scoping, full-access arming/expiry/audit,
    Windows/POSIX/UNC path handling, structural DENYs.
  - `tests/test_workspace_agent.py` (30): traversal/absolute/null-byte/
    symlink escapes, server-root vs local-root intersection, confirmed
    flag, conflict detection, all-or-nothing patches, soft delete,
    checkpoint/rollback in git and non-git repos, command timeouts,
    git argument injection.
  - `tests/test_mission_engine.py` (27): plan validation (cycles, dup ids,
    unknown deps/roles), clarification round-trip, concurrency,
    dependency ordering, partial failure + transitive skip, bounded
    retries, malformed model output, checkpoint-before-mutation, budgets,
    approval pause→grant→resume and decline-adapt, cross-user approval
    rejection, crash recovery + resume, full/per-file rollback.
  - `tests/test_mission_routes_auth.py` (25): anonymous 401 and API-token /
    agent-identity 403 on every protected route, cross-user 404s,
    password-gated full access, owner-scoped persistent rules.
- Static: `py_compile` on all new/changed Python, `node --check` on JS,
  `docker compose config -q` — clean.
- Live end-to-end (real server, real relay, real standalone agent copy,
  real git repo): **16/16** workspace checks (auth rejections, tree/read/
  write/search, on-device pytest, traversal gated, dangerous command →
  approval card → session grant → executes, ask-mode write gated, git via
  relay, unarmed full mode → 403) and **17/17** mission demo checks: goal
  planned into a 3-task DAG by a locally-registered OpenAI-compatible
  endpoint, run paused at `pip install` approval, resumed by interactive
  approval, **edited utils.py/test_utils.py on disk, pytest passed (2
  tests) on the device**, usage + checkpoint + report recorded, then
  **full rollback restored the repo** and the mission was marked
  `rolled_back`.
- A second live run against OpenRouter free models exhausted the
  provider's daily quota mid-mission and demonstrated graceful partial
  failure: failed tasks bounded, dependents skipped, deterministic
  fallback report, mission record preserved.

## Known limitations

- Windows workspace execution depends on built-in PowerShell 5.1; git actions additionally require Git when the selected folder is a Git repository. File edits, commands, checkpoints, and rollback do not require Git.
- Mission token counts are estimates (chars/4) — providers' usage fields
  are not yet aggregated.
- `researcher` works repo-locally; it has no web access (the agent browser
  remains a separate, separately-gated tool).
- Approvals wait up to 24 h, then the task fails (resumable).
- One relay job ≤ 10 min; longer builds should be split or run detached.
- Uploads/downloads through the workspace UI are text-oriented (≤1.5 MB).
