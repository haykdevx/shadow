# Autonomous Missions, Desktop Workspace & Permission Policy

Shadow's autonomous engineering workspace: open a local project on one of
your own machines, give Shadow a goal, and let it inspect, edit, test, and
manage real files — with every action decided by a server-side policy engine
and re-checked by the device agent. Works on Windows, Linux, and macOS via
the Python device agent; nothing on the device is ever exposed to the
internet.

- UI: **Missions** in the sidebar (route `/missions`).
- Backend: `src/mission_policy.py`, `src/mission_workspaces.py`,
  `src/mission_engine.py`, `routes/mission_routes.py`.
- Device: `companion/workspace_agent.py` (dispatched through the existing
  outbound relay; installed automatically by the device installers).
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

| | `ask` — Ask for approval | `auto` — Approve for me (default) | `full` — Full access |
|---|---|---|---|
| Read / inspect workspace | allowed | allowed | allowed |
| Edit files in workspace | ask | allowed | allowed |
| Ordinary project commands | ask | allowed | allowed |
| Network access | ask | ask (unless granted for the mission) | allowed |
| Outside the workspace | ask | ask | allowed **within device roots** |
| Destructive / privileged / installers / secrets / security settings | ask | **ask** | allowed |
| Cross-user / cross-device | DENY | DENY | **DENY** |

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

The workspace needs the **Python device agent** (`relay_agent.py` +
`home_agent.py` + `workspace_agent.py`). Devices enrolled before this
feature: re-run the installer (or drop `workspace_agent.py` next to the
agent) and restart it; the agent then advertises `ws_*` capabilities.

- **Linux**: Command → Add device → run the one-line installer. Installs to
  `~/.local/share/shadow-device`, runs as a systemd user service
  (`shadow-device.service`).
- **macOS**: same one-liner; installs to
  `~/Library/Application Support/Shadow/device-agent`, runs via launchd
  (`io.shadow.device`).
- **Windows**: the default one-liner installs the dependency-free
  PowerShell agent, which supports PC control but **not** workspaces. For
  workspaces install Python 3.10+ and run the Python agent:
  ```powershell
  mkdir $env:LOCALAPPDATA\ShadowDevice; cd $env:LOCALAPPDATA\ShadowDevice
  curl.exe -O https://YOUR-SHADOW/api/shadow/device/source/relay_agent.py
  curl.exe -O https://YOUR-SHADOW/api/shadow/device/source/home_agent.py
  curl.exe -O https://YOUR-SHADOW/api/shadow/device/source/workspace_agent.py
  $env:SHADOW_ALLOWED_ROOTS="C:\Users\you\Projects"
  python relay_agent.py --server https://YOUR-SHADOW --enroll CODE --name "My PC"
  python relay_agent.py   # keep running (Task Scheduler / NSSM for autostart)
  ```
- **Every OS**: set `SHADOW_ALLOWED_ROOTS` (path-separator-joined list) on
  the device to the outermost folders Shadow may ever touch. Workspaces
  must live inside it; it defaults to your home directory — narrow it.

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

## Test report (2026-06-11)

- Full suite: **1037 passed, 2 skipped, 0 failed** (903 before this
  feature; **+134 new tests**, all green; chat/MAGI/devices untouched).
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

- The dependency-free Windows PowerShell agent does not implement
  workspace actions (use the Python agent on Windows, above).
- Mission token counts are estimates (chars/4) — providers' usage fields
  are not yet aggregated.
- `researcher` works repo-locally; it has no web access (the agent browser
  remains a separate, separately-gated tool).
- Approvals wait up to 24 h, then the task fails (resumable).
- One relay job ≤ 10 min; longer builds should be split or run detached.
- Uploads/downloads through the workspace UI are text-oriented (≤1.5 MB).
