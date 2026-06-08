# Shadow Command Page

`/command` is a dedicated authenticated dashboard for the private home-PC bridge.
It replaces the old empty-chat `Shadow // home link` card.

## Endpoint Model

Frontend calls stay on the existing Shadow bridge surface. Access-management endpoints are:

- `GET /api/shadow/access`
- `POST /api/shadow/access/request`
- `POST /api/shadow/access/claim`
- `POST /api/shadow/access/grants/{username}`
- `DELETE /api/shadow/access/grants/{username}`
- `POST /api/shadow/telegram/pair-code`
- `DELETE /api/shadow/telegram/link`

PC-control endpoints are:

- `GET /api/shadow/overview`
- `GET /api/shadow/pc/pending`
- `GET /api/shadow/timeline`
- `GET /api/shadow/runbooks`
- `POST /api/shadow/runbooks`
- `DELETE /api/shadow/runbooks/{name}`
- `GET /api/shadow/automations`
- `POST /api/shadow/automations`
- `DELETE /api/shadow/automations/{automation_id}`
- `POST /api/shadow/automations/evaluate`
- `POST /api/shadow/screen/inspect`
- `GET /api/shadow/watchdog`
- `POST /api/shadow/pc/action`
- `POST /api/shadow/pc/confirm/{pending_id}`
- `DELETE /api/shadow/pc/pending/{pending_id}`

Read actions execute immediately:

- `status`
- `processes`
- `screenshot`
- `clipboard_get`
- `windows`
- `file_list`
- `file_read`
- `file_search`

State-changing actions create a server-side pending approval first:

- `clipboard_set`
- `media`
- `volume`
- `app_launch`
- `app_focus`
- `app_close`
- `kill_process`
- `shell`
- `file_write`
- `lock`
- `sleep`
- `shutdown`
- `type_text`
- `keypress`
- `mouse_move`
- `mouse_click`
- `runbook`

The companion rejects those write actions unless the request includes
`confirmed=true`; the web server only sends that after the same Shadow account
that created the action confirms it with `approve` permission.

## Account Permissions

`SHADOW_PC_OWNER` identifies the single linked-PC owner. Every other authenticated
account starts with no device access and sees a request-access state instead of
telemetry. The owner can grant `view`, `control`, and `approve`; stronger grants
include weaker ones. Pending actions and audit timelines are principal-scoped, and
the AI `pc_control` tool enforces the same permissions. Telegram uses a single-use,
ten-minute pairing code and inherits the paired account's current grants. State is
persisted in `data/shadow-pc-access.json` with atomic writes and cross-process locking.

## File Scope

File browsing/search/read/write are restricted to `SHADOW_ALLOWED_ROOTS`.
If unset, the companion falls back to the home directory of the user running
`companion/home_agent.py`.

## AI Panel Boundary

The `+ Add panel (AI)` control is intentionally spec-based. It composes from
already-loaded read-only dashboard data (`vitals`, `processes`, `windows`) and
does not render arbitrary model HTML or gain access to shell/control endpoints.
## Audit, Runbooks, and Mouse Control

Every read/write bridge request records a redacted audit event in
`data/shadow-pc-audit.jsonl`. The Command page timeline shows recent pending,
cancelled, executed, and failed actions.

Runbooks are action bundles. Built-ins (`health_report`, `lock_report`, and
`work_mode`) ship with the app, and the linked-PC owner can create custom runbooks in the
Command page. Custom runbooks are saved in `data/shadow-runbooks.json`. Running
any runbook still creates one server-side pending approval before execution, and
the pending row displays the max risk of its steps.

The Screen panel supports Shift-click remote mouse clicks. The browser maps the
click into screenshot coordinates and sends `mouse_click`; the server still
requires confirmation before forwarding it to the home PC.

## Automations

Automations are saved in `data/shadow-automations.json`. They support manual and
metric-threshold triggers (`cpu_percent`, `memory_percent`, `disk_used_percent`,
`disk_free_gb`, `load_1`, `gpu_temp_c`, `gpu_percent`). Automations do not
bypass confirmation: when one fires it requests a runbook or PC action, then the
normal approval queue controls execution.

## Screen Inspection

`POST /api/shadow/screen/inspect` captures the screen on demand and returns a
structured payload. It is deliberately not part of the polling loop, so the page
only screenshots when the user clicks Capture or Inspect. Vision-model analysis
is left behind the existing chat/model stack boundary; the deterministic capture
endpoint is live and safe.

## Performance

`GET /api/shadow/overview` caches host status briefly via
`SHADOW_PC_STATUS_CACHE_SECONDS` (default 3s) so the dashboard does not hammer
the home bridge while panels refresh.
