"""Direct Agent Sessions: one model, one workspace, one conversational tool loop.

The lightweight alternative to Autonomous Missions for everyday coding and
file tasks: the user picks a device + workspace + model, types a task, and
the model immediately inspects and edits the real files through the same
policy-checked dispatch path missions use (``src.mission_workspaces.dispatch``
→ ``src.mission_policy.evaluate`` → device relay). There is no planner DAG —
just a bounded, strictly-JSON tool loop with the full conversation preserved,
so follow-up messages continue in context.

Safety is identical to missions: a checkpoint opens before the first
mutation, deletes are soft (workspace trash), every file read/changed and
command executed is recorded, and rollback restores the checkpoint.

Provider failures never strand a session: a failed model call marks the
session ``failed`` with the provider error preserved and ``retryable`` set;
when automatic fallback is enabled the session rotates to the next
configured fallback model instead. Sessions are persisted atomically after
every transition and recovered to a resumable state on startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_json
from src import mission_policy, mission_workspaces
from src.mission_policy import audit
from src.mission_workspaces import (
    WorkspaceApprovalRequired,
    WorkspaceDenied,
    WorkspaceError,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("SHADOW_MISSIONS_DATA", "data/missions"))
SESSIONS_DIR = DATA_DIR / "sessions"

# Budgets — enforced, not advisory.
MAX_STEPS_PER_RUN = 60          # tool-loop rounds per run leg (a follow-up starts a new leg)
MAX_LLM_CALLS = 200             # per session lifetime
MAX_ACTIONS = 500               # relay dispatches per session lifetime
MAX_WALL_SECONDS = 2 * 3600     # per run leg
APPROVAL_WAIT_SECONDS = 24 * 3600
MAX_TRANSCRIPT_ENTRIES = 240
MAX_EVENTS = 1000
MAX_TERMINAL = 120

_LOCK = threading.RLock()

# Live runners (volatile): session_id -> {"task": asyncio.Task, "wake": Event, "loop": loop}
_RUNNERS: dict[str, dict[str, Any]] = {}


class AgentSessionError(RuntimeError):
    """A clean session failure."""


class ProviderError(AgentSessionError):
    """The model provider failed (rate limit, outage, auth)."""


# ── persistence ──────────────────────────────────────────────────────────


def _session_path(session_id: str) -> Path:
    clean = str(session_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,40}", clean):
        raise AgentSessionError("Invalid session id")
    return SESSIONS_DIR / f"{clean}.json"


def _save(session: dict[str, Any]) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session["updated_at"] = time.time()
    atomic_write_json(str(_session_path(session["id"])), session, indent=2)


def load_session(owner: str, session_id: str) -> dict[str, Any]:
    path = _session_path(session_id)
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentSessionError("Session not found") from exc
    if not isinstance(session, dict) or session.get("owner") != str(owner or "").strip().lower():
        raise AgentSessionError("That session does not belong to your account")
    return session


def list_sessions(owner: str, limit: int = 50) -> list[dict[str, Any]]:
    owner = str(owner or "").strip().lower()
    rows: list[dict[str, Any]] = []
    if SESSIONS_DIR.exists():
        for path in SESSIONS_DIR.glob("*.json"):
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(session, dict) and session.get("owner") == owner:
                rows.append({
                    "id": session.get("id"),
                    "task": str(session.get("task") or "")[:200],
                    "status": session.get("status"),
                    "workspace_id": session.get("workspace_id"),
                    "workspace_name": session.get("workspace_name"),
                    "model": (session.get("model") or {}).get("model"),
                    "retryable": bool(session.get("retryable")),
                    "files_changed": len(session.get("files_changed") or []),
                    "pending_approvals": sum(
                        1 for a in session.get("approvals", []) if a.get("status") == "pending"),
                    "created_at": session.get("created_at"),
                    "updated_at": session.get("updated_at"),
                })
    rows.sort(key=lambda r: -float(r.get("created_at") or 0))
    return rows[:limit]


def _event(session: dict[str, Any], kind: str, text: str, **extra: Any) -> None:
    session.setdefault("events", []).append({
        "ts": time.time(), "kind": kind, "text": str(text)[:800], **extra,
    })
    if len(session["events"]) > MAX_EVENTS:
        session["events"] = session["events"][-MAX_EVENTS:]


# ── model resolution (no hardcoded providers) ────────────────────────────


def _resolve_model(target: dict[str, Any]) -> dict[str, Any]:
    endpoint_id = str((target or {}).get("endpoint_id") or "")
    model = str((target or {}).get("model") or "")
    from src.endpoint_resolver import resolve_endpoint_by_id

    resolved = resolve_endpoint_by_id(endpoint_id, model) if endpoint_id else None
    if not resolved:
        raise AgentSessionError("The selected model endpoint is not configured")
    url, model_name, headers = resolved
    return {"url": url, "model": model_name, "headers": headers or {}}


async def _llm(session: dict[str, Any], messages: list[dict[str, str]],
               *, max_tokens: int = 1800) -> str:
    usage = session.setdefault("usage", {"llm_calls": 0, "actions": 0, "by_model": {}})
    if usage["llm_calls"] >= MAX_LLM_CALLS:
        raise AgentSessionError(f"Session LLM budget exhausted ({MAX_LLM_CALLS} calls)")
    target = _resolve_model(session.get("model") or {})
    from src.llm_core import llm_call_async

    started = time.time()
    try:
        raw = await llm_call_async(
            target["url"], target["model"], messages,
            headers=target["headers"], temperature=0.2,
            max_tokens=max_tokens, timeout=180, max_retries=1,
            prompt_type="agent-session",
        )
    except Exception as exc:  # noqa: BLE001 — provider failures are a session state, not a crash
        raise ProviderError(str(exc)[:400]) from exc
    usage["llm_calls"] += 1
    per = usage["by_model"].setdefault(target["model"], {
        "calls": 0, "est_input_tokens": 0, "est_output_tokens": 0, "seconds": 0.0,
    })
    per["calls"] += 1
    per["est_input_tokens"] += sum(len(m.get("content") or "") for m in messages) // 4
    per["est_output_tokens"] += len(raw or "") // 4
    per["seconds"] = round(per["seconds"] + (time.time() - started), 1)
    return raw or ""


def _parse_json_block(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


# ── creation ─────────────────────────────────────────────────────────────


def create_session(
    owner: str,
    workspace_id: str,
    task: str,
    *,
    model: dict[str, str],
    fallbacks: list[dict[str, str]] | None = None,
    auto_fallback: bool = False,
) -> dict[str, Any]:
    owner = str(owner or "").strip().lower()
    task = str(task or "").strip()
    if not task or len(task) < 4:
        raise AgentSessionError("A session needs a real task")
    workspace = mission_workspaces.get_workspace(owner, workspace_id)  # owner check
    if not isinstance(model, dict) or not model.get("endpoint_id") or not model.get("model"):
        raise AgentSessionError("Select a model for this session")
    clean_fallbacks = [
        {"endpoint_id": str(f["endpoint_id"]), "model": str(f["model"])[:120]}
        for f in (fallbacks or [])
        if isinstance(f, dict) and f.get("endpoint_id") and f.get("model")
    ][:5]
    session = {
        "id": secrets.token_urlsafe(9),
        "owner": owner,
        "workspace_id": workspace["id"],
        "workspace_name": workspace.get("name"),
        "device_id": workspace["device_id"],
        "task": task[:4000],
        "model": {"endpoint_id": str(model["endpoint_id"]), "model": str(model["model"])[:120]},
        "fallbacks": clean_fallbacks,
        "auto_fallback": bool(auto_fallback),
        "status": "created",
        "error": "",
        "provider_error": "",
        "retryable": False,
        "transcript": [{"role": "user", "content": task[:4000]}],
        "inbox": [],
        "events": [],
        "terminal": [],
        "approvals": [],
        "files_read": [],
        "files_changed": [],
        "commands": [],
        "usage": {"llm_calls": 0, "actions": 0, "by_model": {}},
        "checkpoint": None,
        "report": "",
        "created_at": time.time(),
    }
    _event(session, "created", f"workspace={workspace['name']} model={model['model']}")
    _save(session)
    audit(owner, "agent_session_created", {
        "session_id": session["id"], "workspace_id": workspace["id"],
        "model": session["model"]["model"],
    })
    return session


# ── dispatch + bookkeeping ───────────────────────────────────────────────


async def _dispatch(session: dict[str, Any], action: str, args: dict[str, Any]) -> dict[str, Any]:
    usage = session.setdefault("usage", {"llm_calls": 0, "actions": 0, "by_model": {}})
    if usage["actions"] >= MAX_ACTIONS:
        raise AgentSessionError(f"Session action budget exhausted ({MAX_ACTIONS})")
    usage["actions"] += 1
    # mode=None: the workspace's *current* mode decides, so flipping the
    # Unattended toggle applies to the very next action of a live session.
    result = await asyncio.to_thread(
        mission_workspaces.dispatch,
        session["owner"], session["workspace_id"], action, args,
        mode=None,
        mission_id=session["id"],
    )
    _record_effects(session, action, args, result)
    return result


def _record_effects(session: dict[str, Any], action: str, args: dict[str, Any],
                    result: dict[str, Any] | None = None) -> None:
    def _add(bucket: str, value: str, cap: int = 400) -> None:
        items = session.setdefault(bucket, [])
        if value and value not in items:
            items.append(value)
            if len(items) > cap:
                del items[: len(items) - cap]

    if action in {"ws_read", "ws_stat"}:
        _add("files_read", str(args.get("path") or ""))
    elif action in {"ws_write", "ws_rename", "ws_delete", "ws_mkdir"}:
        _add("files_changed", str(args.get("path") or ""))
        if action == "ws_rename" and args.get("to"):
            _add("files_changed", str(args.get("to")))
    elif action == "ws_patch":
        for edit in args.get("edits") or []:
            if isinstance(edit, dict):
                _add("files_changed", str(edit.get("path") or ""))
    elif action == "ws_run":
        row = {
            "ts": time.time(),
            "command": str(args.get("command") or "")[:300],
            "returncode": (result or {}).get("returncode"),
            "seconds": (result or {}).get("seconds"),
        }
        session.setdefault("commands", []).append(row)
        if len(session["commands"]) > 200:
            session["commands"] = session["commands"][-200:]
        terminal = session.setdefault("terminal", [])
        terminal.append({
            **row,
            "stdout": str((result or {}).get("stdout") or "")[-6000:],
            "stderr": str((result or {}).get("stderr") or "")[-4000:],
        })
        if len(terminal) > MAX_TERMINAL:
            session["terminal"] = terminal[-MAX_TERMINAL:]


async def _ensure_checkpoint(session: dict[str, Any]) -> str:
    """Create the safety checkpoint before the first mutation."""
    if session.get("checkpoint"):
        return session["checkpoint"]["id"]
    checkpoint_id = f"s-{session['id']}"
    result = await _dispatch(session, "ws_checkpoint", {"checkpoint_id": checkpoint_id})
    session["checkpoint"] = {
        "id": checkpoint_id,
        "git": result.get("git"),
        "created_at": time.time(),
    }
    if result.get("uncommitted_user_work"):
        _event(session, "warning",
               "Uncommitted user changes were present before this session's first edit; "
               "pre-images are captured and unrelated changes will not be reverted.")
    _event(session, "checkpoint", "filesystem checkpoint active (pre-image snapshots)")
    _save(session)
    return checkpoint_id


# ── approvals (ask/auto workspaces; unattended never reaches this) ──────


def _add_approval(session: dict[str, Any], action: str, args: dict[str, Any],
                  exc: WorkspaceApprovalRequired) -> dict[str, Any]:
    approval = {
        "id": secrets.token_urlsafe(8),
        "action": action,
        "args": {k: v for k, v in args.items() if k != "roots"},
        "summary": exc.request.summary,
        "capability": exc.request.capability,
        "risk": exc.request.risk,
        "reason": exc.decision.reason,
        "grant_key": exc.decision.grant_key,
        "status": "pending",
        "created_at": time.time(),
    }
    session.setdefault("approvals", []).append(approval)
    _event(session, "approval_requested", f"{approval['summary']} — {approval['reason']}")
    return approval


def resolve_approval(owner: str, session_id: str, approval_id: str, decision: str) -> dict[str, Any]:
    """Resolve a pending approval. Called only from interactive routes."""
    session = load_session(owner, session_id)
    approval = next((a for a in session.get("approvals", [])
                     if a.get("id") == approval_id and a.get("status") == "pending"), None)
    if not approval:
        raise AgentSessionError("Approval not found or already resolved")
    scope_map = {"allow_once": "once", "allow_session": "mission", "allow_always": "workspace"}
    if decision in scope_map:
        mission_policy.GRANTS.grant(
            owner, scope_map[decision],
            session_id if decision != "allow_always" else "",
            approval["grant_key"],
            workspace_id=session["workspace_id"],
            summary=approval["summary"],
        )
        approval["status"] = "approved"
        approval["scope"] = scope_map[decision]
    elif decision == "decline":
        approval["status"] = "declined"
    elif decision == "stop_session":
        approval["status"] = "declined"
        session["status"] = "stopped"
        _event(session, "stopped", "stopped from an approval request")
    else:
        raise AgentSessionError(f"Unknown decision: {decision}")
    approval["resolved_at"] = time.time()
    _event(session, "approval_" + approval["status"], f"{approval['summary']} ({decision})")
    if session["status"] == "waiting_approval" and not any(
            a.get("status") == "pending" for a in session["approvals"]):
        session["status"] = "running"
    _save(session)
    audit(owner, "agent_session_approval", {
        "session_id": session_id, "approval_id": approval_id,
        "decision": decision, "summary": approval["summary"][:200],
    })
    _wake(session_id)
    if decision == "stop_session":
        stop_session(owner, session_id)
    return approval


async def _wait_for_approval(session: dict[str, Any], approval_id: str) -> dict[str, Any]:
    deadline = time.time() + APPROVAL_WAIT_SECONDS
    runner = _RUNNERS.get(session["id"])
    while time.time() < deadline:
        if runner:
            runner["wake"] = asyncio.Event()
        fresh = load_session(session["owner"], session["id"])
        session["approvals"] = fresh.get("approvals", [])
        row = next((a for a in session["approvals"] if a.get("id") == approval_id), None)
        if row and row.get("status") != "pending":
            return row
        if fresh.get("status") == "stopped":
            raise asyncio.CancelledError()
        if runner:
            try:
                await asyncio.wait_for(runner["wake"].wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(5)
    raise AgentSessionError("Approval timed out after 24h")


def _wake(session_id: str) -> None:
    runner = _RUNNERS.get(session_id)
    if not runner or not isinstance(runner.get("wake"), asyncio.Event):
        return
    loop = runner.get("loop")
    try:
        if loop and loop.is_running():
            loop.call_soon_threadsafe(runner["wake"].set)
        else:
            runner["wake"].set()
    except RuntimeError:
        pass  # the 15s poll fallback still applies


# ── the tool loop ────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are Shadow's coding agent working directly inside one real project workspace.
You inspect and modify real files through tools. Work autonomously toward the user's task.

Respond with ONLY one JSON object per turn — no prose outside the JSON:
- {{"tool": "<name>", "args": {{...}}}} to act. Tools:
  ws_tree{{path?,depth?,limit?}}, ws_stat{{path}}, ws_read{{path}},
  ws_search{{query,mode:"content"|"name"}}, ws_hash{{paths:[..]}},
  ws_diff{{path,text}}, ws_write{{path,text}},
  ws_patch{{edits:[{{path,old,new}}|{{path,text}}]}}, ws_mkdir{{path}},
  ws_rename{{path,to}}, ws_delete{{path}}, ws_run{{command,timeout?}},
  git_info{{}}, git_diff{{target?,path?}}, git_log{{limit?}},
  git_checkout{{branch,create?}}, git_commit{{message}},
  ws_checkpoint{{checkpoint_id}}, ws_restore{{checkpoint_id,paths?}}
- {{"done": true, "report": "factual completion report: what changed, what was verified, what remains"}}
- {{"fail": "why the task cannot be completed"}}

Rules:
- Read files before editing them. Prefer ws_patch with exact old/new strings.
- Run the narrowest verifying command after a change (a test, a compile check).
- Deletes are soft (workspace trash) and edits are checkpointed — but still be precise.
- Never run `git commit` unless the user's task asks for a commit.
- If a tool result says DENIED, that operation is not available — adapt and continue.
- Never claim something succeeded without tool evidence. Steps are limited to {max_steps}; be economical.
- When the user sends a follow-up message it appears in the conversation — continue from current state."""


async def _execute_tool(session: dict[str, Any], action: str, args: dict[str, Any]) -> str:
    """Run one tool call; pauses durably when an approval is required."""
    while True:
        try:
            result = await _dispatch(session, action, args)
            return json.dumps(result, ensure_ascii=False, default=str)
        except WorkspaceApprovalRequired as exc:
            approval = _add_approval(session, action, args, exc)
            session["status"] = "waiting_approval"
            _save(session)
            resolved = await _wait_for_approval(session, approval["id"])
            if session["status"] == "waiting_approval":
                session["status"] = "running"
            if resolved.get("status") == "approved":
                _event(session, "approval_consumed", approval["summary"])
                continue
            return f"DENIED BY USER: {approval['summary']} — adapt your approach or finish."
        except WorkspaceDenied as exc:
            return f"DENIED BY POLICY: {exc.decision.reason}"
        except (WorkspaceError, AgentSessionError) as exc:
            return f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001 — relay/device errors arrive as plain exceptions
            return f"ERROR: {str(exc)[:400]}"


def _drain_inbox(session: dict[str, Any]) -> bool:
    """Move queued follow-up messages into the transcript. True if any."""
    fresh = load_session(session["owner"], session["id"])
    inbox = [m for m in fresh.get("inbox", []) if str(m or "").strip()]
    if not inbox:
        return False
    for message in inbox:
        session.setdefault("transcript", []).append(
            {"role": "user", "content": str(message)[:4000]})
        _event(session, "user_message", str(message)[:200])
    session["inbox"] = []
    _save(session)
    return True


def _trim_transcript(session: dict[str, Any]) -> None:
    transcript = session.get("transcript") or []
    if len(transcript) <= MAX_TRANSCRIPT_ENTRIES:
        return
    # Keep the first user task and the most recent exchanges.
    head, tail = transcript[:1], transcript[-(MAX_TRANSCRIPT_ENTRIES - 2):]
    session["transcript"] = head + [
        {"role": "user", "content": "(older tool exchanges trimmed for length)"}
    ] + tail


def _rotate_fallback(session: dict[str, Any]) -> bool:
    """Switch to the next fallback model. True when one was available."""
    fallbacks = session.get("fallbacks") or []
    if not fallbacks:
        return False
    nxt = fallbacks.pop(0)
    previous = (session.get("model") or {}).get("model")
    session["model"] = nxt
    _event(session, "model_fallback", f"{previous} unavailable → switching to {nxt.get('model')}")
    _save(session)
    return True


async def _run_loop(session: dict[str, Any]) -> None:
    started = time.time()
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT.format(max_steps=MAX_STEPS_PER_RUN)},
        *[{"role": m["role"], "content": m["content"]} for m in session.get("transcript", [])],
    ]
    repaired_last_step = False

    for _step in range(MAX_STEPS_PER_RUN):
        if time.time() - started > MAX_WALL_SECONDS:
            raise AgentSessionError("Session wall-clock budget exhausted")
        if _drain_inbox(session):
            messages = [
                {"role": "system", "content": _SYSTEM_PROMPT.format(max_steps=MAX_STEPS_PER_RUN)},
                *[{"role": m["role"], "content": m["content"]} for m in session["transcript"]],
            ]

        try:
            raw = await _llm(session, messages)
        except ProviderError as exc:
            session["provider_error"] = str(exc)[:500]
            _event(session, "provider_error", str(exc)[:300])
            if session.get("auto_fallback") and _rotate_fallback(session):
                continue
            raise

        session.setdefault("transcript", []).append({"role": "assistant", "content": raw[:8000]})
        messages.append({"role": "assistant", "content": raw[:8000]})
        try:
            payload = _parse_json_block(raw)
            repaired_last_step = False
        except (ValueError, json.JSONDecodeError):
            if repaired_last_step:
                # Already repaired once in a row; surface a hard error event
                # and keep going with a final warning rather than looping.
                _event(session, "malformed_output", "model produced invalid JSON twice in a row")
            repaired_last_step = True
            repair = ("Your reply was not one valid JSON object. "
                      "Reply with ONLY the corrected JSON object — no prose.")
            session["transcript"].append({"role": "user", "content": repair})
            messages.append({"role": "user", "content": repair})
            _save(session)
            continue

        if payload.get("done"):
            session["status"] = "completed"
            session["report"] = str(payload.get("report") or payload.get("summary") or "")[:20000]
            if not session["report"]:
                session["report"] = _fallback_report(session)
            session["retryable"] = False
            _event(session, "completed", session["report"][:200])
            _trim_transcript(session)
            _save(session)
            return
        if payload.get("fail"):
            session["status"] = "failed"
            session["error"] = str(payload["fail"])[:500]
            session["retryable"] = True
            _event(session, "failed", session["error"])
            _trim_transcript(session)
            _save(session)
            return

        action = str(payload.get("tool") or "").strip()
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        if action not in mission_workspaces.WORKSPACE_ACTIONS:
            note = f"Unknown tool: {action or '(missing)'}"
            session["transcript"].append({"role": "user", "content": note})
            messages.append({"role": "user", "content": note})
            _save(session)
            continue
        if action in {"ws_write", "ws_patch", "ws_delete", "ws_rename", "ws_restore"}:
            args.setdefault("checkpoint_id", await _ensure_checkpoint(session))

        _event(session, "tool", mission_workspaces._summary_for(action, args), action=action)
        outcome = await _execute_tool(session, action, args)
        result_note = f"RESULT of {action}:\n{outcome[:7000]}"
        session["transcript"].append({"role": "user", "content": result_note})
        messages.append({"role": "user", "content": result_note})
        _trim_transcript(session)
        _save(session)

    raise AgentSessionError(f"Session exceeded its step budget ({MAX_STEPS_PER_RUN})")


def _fallback_report(session: dict[str, Any]) -> str:
    changed = ", ".join(session.get("files_changed") or []) or "none"
    commands = len(session.get("commands") or [])
    return (f"Files changed: {changed}. Commands executed: {commands}. "
            f"See the activity timeline for details.")


async def _runner(session_id: str, owner: str) -> None:
    session = load_session(owner, session_id)
    try:
        session["status"] = "running"
        session["error"] = ""
        session["retryable"] = False
        _event(session, "started", f"model={((session.get('model') or {}).get('model'))}")
        _save(session)
        await _run_loop(session)
    except asyncio.CancelledError:
        session["status"] = "stopped"
        session["retryable"] = True
        _event(session, "stopped", "runner stopped")
        _save(session)
        raise
    except ProviderError as exc:
        session["status"] = "failed"
        session["error"] = f"model provider failed: {exc}"
        session["retryable"] = True
        _event(session, "failed", session["error"])
        _save(session)
    except (AgentSessionError, WorkspaceError) as exc:
        session["status"] = "failed"
        session["error"] = str(exc)[:500]
        session["retryable"] = True
        _event(session, "failed", session["error"])
        _save(session)
    except Exception as exc:  # noqa: BLE001 — one crash must not lose the record
        logger.exception("Agent session %s crashed", session_id)
        session["status"] = "failed"
        session["error"] = f"internal error: {str(exc)[:300]}"
        session["retryable"] = True
        _event(session, "failed", session["error"])
        _save(session)
    finally:
        _RUNNERS.pop(session_id, None)
        audit(owner, "agent_session_finished", {
            "session_id": session_id, "status": session.get("status"),
        })


# ── lifecycle API (used by routes) ───────────────────────────────────────


def start_session(owner: str, session_id: str) -> dict[str, Any]:
    session = load_session(owner, session_id)
    if session_id in _RUNNERS:
        raise AgentSessionError("Session is already running")
    if session["status"] in ("completed", "rolled_back") and not session.get("inbox"):
        raise AgentSessionError("Session is finished — send a follow-up message to continue")
    loop = asyncio.get_running_loop()  # callers must be on the event loop (async routes)
    runner_task = loop.create_task(_runner(session_id, owner))
    _RUNNERS[session_id] = {"task": runner_task, "wake": asyncio.Event(), "loop": loop}
    return {"ok": True, "status": "running"}


def send_message(owner: str, session_id: str, text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        raise AgentSessionError("Message is empty")
    session = load_session(owner, session_id)
    session.setdefault("inbox", []).append(text[:4000])
    _save(session)
    running = session_id in _RUNNERS
    return {"ok": True, "queued": True, "running": running}


def stop_session(owner: str, session_id: str) -> dict[str, Any]:
    session = load_session(owner, session_id)
    runner = _RUNNERS.pop(session_id, None)
    if runner and isinstance(runner.get("task"), asyncio.Task):
        runner["task"].cancel()
    if session["status"] in ("running", "waiting_approval", "created"):
        session["status"] = "stopped"
        session["retryable"] = True
        _event(session, "stopped", "stopped by user")
        _save(session)
    audit(owner, "agent_session_stopped", {"session_id": session_id})
    return {"ok": True, "status": session["status"]}


def retry_session(owner: str, session_id: str,
                  *, endpoint_id: str = "", model: str = "") -> dict[str, Any]:
    """Retry a failed/stopped session, optionally on a different model."""
    session = load_session(owner, session_id)
    if session_id in _RUNNERS:
        raise AgentSessionError("Session is already running")
    if session["status"] not in ("failed", "stopped", "created", "waiting_approval"):
        raise AgentSessionError(f"Session cannot retry from status '{session['status']}'")
    if endpoint_id and model:
        session["model"] = {"endpoint_id": str(endpoint_id), "model": str(model)[:120]}
        _event(session, "model_changed", f"retrying with {model}")
    session["provider_error"] = ""
    _save(session)
    return start_session(owner, session_id)


async def rollback_session(owner: str, session_id: str,
                           *, paths: list[str] | None = None) -> dict[str, Any]:
    """Per-file (``paths``) or full session rollback via the agent checkpoint."""
    session = load_session(owner, session_id)
    if session_id in _RUNNERS:
        raise AgentSessionError("Stop the session before rolling back")
    checkpoint = session.get("checkpoint")
    if not checkpoint:
        raise AgentSessionError("This session made no checkpointed changes")
    args: dict[str, Any] = {"checkpoint_id": checkpoint["id"]}
    if paths:
        args["paths"] = [str(p) for p in paths][:100]
    result = await asyncio.to_thread(
        mission_workspaces.dispatch,
        owner, session["workspace_id"], "ws_restore", args,
        mode=None, mission_id=session_id,
    )
    if not paths:
        session["status"] = "rolled_back"
    _event(session, "rolled_back",
           f"restored={len(result.get('restored') or [])} "
           f"removed={len(result.get('removed_created') or [])}"
           + (f" (paths={len(paths)})" if paths else " (full)"))
    _save(session)
    audit(owner, "agent_session_rollback", {"session_id": session_id, "full": not paths})
    return result


def recover_sessions() -> int:
    """Startup hook: sessions running when the server died become resumable
    instead of being stranded in 'running' forever."""
    recovered = 0
    if not SESSIONS_DIR.exists():
        return 0
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(session, dict):
            continue
        if session.get("status") in ("running", "waiting_approval", "created") \
                and session.get("id") not in _RUNNERS:
            session["status"] = "stopped"
            session["retryable"] = True
            _event(session, "recovered", "server restarted; session stopped and is resumable")
            try:
                _save(session)
                recovered += 1
            except AgentSessionError:
                continue
    if recovered:
        logger.info("Recovered %d interrupted agent session(s)", recovered)
    return recovered
