"""Autonomous Missions: plan, execute, approve, verify, report.

A mission turns one high-level goal into a dependency-aware task plan,
assigns configured models to roles (planner / researcher / implementer /
reviewer / tester — no provider is hardcoded), runs independent tasks
concurrently, persists every state change durably, pauses for approvals,
survives restarts, and ends with a reviewed diff and a final report.

Every tool call a task makes goes through ``src.mission_workspaces.dispatch``
→ the central policy engine → the device relay. Models never decide their
own permissions; an approval can only be resolved from an interactive
session (route layer enforces it).

Durable state: one JSON document per mission under
``data/missions/missions/<id>.json`` written atomically after every
meaningful transition, so a crash or restart loses at most the in-flight
LLM exchange. On startup :func:`recover_missions` re-marks orphaned running
missions as paused and resets their in-flight tasks so they re-run.
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
MISSIONS_DIR = DATA_DIR / "missions"

ROLES = ("planner", "researcher", "implementer", "reviewer", "tester")

# Budgets — every limit is enforced, not advisory.
MAX_TASKS = 20
MAX_TASK_STEPS = 14          # LLM tool-loop rounds per task
MAX_TASK_ATTEMPTS = 2
MAX_LLM_CALLS = 80           # per mission
MAX_ACTIONS = 250            # per mission (relay dispatches)
MAX_WALL_SECONDS = 4 * 3600
MAX_CONCURRENT_TASKS = 3
APPROVAL_WAIT_SECONDS = 24 * 3600

_LOCK = threading.RLock()

# Live runner registry (volatile): mission_id -> {"task": asyncio.Task,
# "wake": asyncio.Event, "stop": bool, "pause": bool}
_RUNNERS: dict[str, dict[str, Any]] = {}


class MissionError(RuntimeError):
    """A clean mission failure."""


# ── persistence ─────────────────────────────────────────────────────────


def _mission_path(mission_id: str) -> Path:
    clean = str(mission_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,40}", clean):
        raise MissionError("Invalid mission id")
    return MISSIONS_DIR / f"{clean}.json"


def _save_mission(mission: dict[str, Any]) -> None:
    MISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    mission["updated_at"] = time.time()
    atomic_write_json(str(_mission_path(mission["id"])), mission, indent=2)


def load_mission(owner: str, mission_id: str) -> dict[str, Any]:
    path = _mission_path(mission_id)
    try:
        mission = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MissionError("Mission not found") from exc
    if not isinstance(mission, dict) or mission.get("owner") != str(owner or "").strip().lower():
        raise MissionError("That mission does not belong to your account")
    return mission


def list_missions(owner: str, limit: int = 50) -> list[dict[str, Any]]:
    owner = str(owner or "").strip().lower()
    rows: list[dict[str, Any]] = []
    if MISSIONS_DIR.exists():
        for path in MISSIONS_DIR.glob("*.json"):
            try:
                mission = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(mission, dict) and mission.get("owner") == owner:
                rows.append({
                    "id": mission.get("id"),
                    "goal": str(mission.get("goal") or "")[:200],
                    "status": mission.get("status"),
                    "workspace_id": mission.get("workspace_id"),
                    "created_at": mission.get("created_at"),
                    "updated_at": mission.get("updated_at"),
                    "task_counts": _task_counts(mission),
                    "pending_approvals": sum(
                        1 for a in mission.get("approvals", []) if a.get("status") == "pending"
                    ),
                })
    rows.sort(key=lambda r: -float(r.get("created_at") or 0))
    return rows[:limit]


def _task_counts(mission: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in mission.get("tasks", []):
        counts[task.get("status", "?")] = counts.get(task.get("status", "?"), 0) + 1
    return counts


def _event(mission: dict[str, Any], kind: str, text: str, task_id: str = "") -> None:
    mission.setdefault("events", []).append({
        "ts": time.time(), "kind": kind, "text": str(text)[:600],
        **({"task_id": task_id} if task_id else {}),
    })
    if len(mission["events"]) > 800:
        mission["events"] = mission["events"][-800:]


# ── role/model resolution (no hardcoded providers) ──────────────────────


def available_role_targets(owner: str) -> list[dict[str, Any]]:
    """Configured endpoint/model pairs the owner may assign to roles."""
    from routes.magi_routes import _available_models  # established visibility pattern
    return _available_models(owner)


def _resolve_role_target(mission: dict[str, Any], role: str) -> dict[str, Any]:
    roles = mission.get("roles") or {}
    target = roles.get(role) or roles.get("planner") or {}
    endpoint_id = str(target.get("endpoint_id") or "")
    model = str(target.get("model") or "")
    from src.endpoint_resolver import resolve_endpoint_by_id

    resolved = resolve_endpoint_by_id(endpoint_id, model) if endpoint_id else None
    if not resolved:
        raise MissionError(f"No model configured for role '{role}'")
    url, model_name, headers = resolved
    return {"url": url, "model": model_name, "headers": headers or {}}


async def _llm(mission: dict[str, Any], role: str, messages: list[dict[str, str]],
               *, max_tokens: int = 1800, temperature: float = 0.2) -> str:
    usage = mission.setdefault("usage", {"llm_calls": 0, "actions": 0, "by_model": {}})
    if usage["llm_calls"] >= MAX_LLM_CALLS:
        raise MissionError(f"Mission LLM budget exhausted ({MAX_LLM_CALLS} calls)")
    target = _resolve_role_target(mission, role)
    from src.llm_core import llm_call_async

    started = time.time()
    try:
        raw = await llm_call_async(
            target["url"], target["model"], messages,
            headers=target["headers"], temperature=temperature,
            max_tokens=max_tokens, timeout=180, max_retries=1,
            prompt_type=f"mission-{role}",
        )
    except Exception as exc:  # noqa: BLE001 — provider errors are task
        # failures (retryable, bounded), never "internal errors".
        raise MissionError(f"model call failed for role '{role}': {str(exc)[:300]}") from exc
    usage["llm_calls"] += 1
    per = usage["by_model"].setdefault(target["model"], {
        "calls": 0, "est_input_tokens": 0, "est_output_tokens": 0, "seconds": 0.0,
    })
    per["calls"] += 1
    # Estimated tokens (chars/4) — labeled estimated everywhere it is shown.
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


# ── planning ─────────────────────────────────────────────────────────────

_PLAN_PROMPT = """You are the mission planner for an autonomous software-engineering system.
Decompose the goal into 2-{max_tasks} concrete tasks for this repository.

Rules:
- Output ONLY a JSON object, no prose.
- Schema: {{"clarification": null or "one genuinely blocking question",
  "tasks": [{{"id": "t1", "title": "...", "role": "researcher|implementer|reviewer|tester",
  "goal": "specific, verifiable instruction", "depends_on": ["t0", ...]}}]}}
- Ask a clarification ONLY when the goal is impossible to start safely without it.
- Use "researcher" for read-only investigation, "implementer" for edits,
  "tester" for running tests/builds, "reviewer" to inspect the final diff.
- Every mission should normally end with one tester task and one reviewer task
  that depend on the implementation tasks.
- Dependencies must form a DAG over the listed ids. Independent tasks will run
  concurrently."""


def _validate_plan(tasks: Any) -> list[dict[str, Any]]:
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError(f"plan must contain 1-{MAX_TASKS} tasks")
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for row in tasks:
        if not isinstance(row, dict):
            raise ValueError("each task must be an object")
        task_id = str(row.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", task_id) or task_id in seen:
            raise ValueError(f"bad or duplicate task id: {task_id!r}")
        seen.add(task_id)
        role = str(row.get("role") or "implementer").strip().lower()
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        deps = [str(d).strip() for d in (row.get("depends_on") or []) if str(d).strip()]
        cleaned.append({
            "id": task_id,
            "title": str(row.get("title") or task_id)[:160],
            "role": role,
            "goal": str(row.get("goal") or "")[:2000],
            "depends_on": deps,
            "status": "pending",
            "attempts": 0,
            "max_attempts": MAX_TASK_ATTEMPTS,
            "logs": [],
            "result": "",
            "error": "",
        })
    ids = {t["id"] for t in cleaned}
    for task in cleaned:
        unknown = [d for d in task["depends_on"] if d not in ids]
        if unknown:
            raise ValueError(f"task {task['id']} depends on unknown {unknown}")
    # cycle check (Kahn)
    indegree = {t["id"]: len(t["depends_on"]) for t in cleaned}
    queue = [tid for tid, deg in indegree.items() if deg == 0]
    visited = 0
    children: dict[str, list[str]] = {}
    for task in cleaned:
        for dep in task["depends_on"]:
            children.setdefault(dep, []).append(task["id"])
    while queue:
        node = queue.pop()
        visited += 1
        for child in children.get(node, []):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(cleaned):
        raise ValueError("task dependencies contain a cycle")
    return cleaned


async def plan_mission(mission: dict[str, Any], *, answer: str = "") -> None:
    """Run the planner model; populate tasks or a clarification question."""
    context = await _repo_context(mission)
    messages = [
        {"role": "system", "content": _PLAN_PROMPT.format(max_tasks=MAX_TASKS)},
        {"role": "user", "content": (
            f"GOAL:\n{mission['goal']}\n\nREPOSITORY CONTEXT:\n{context}"
            + (f"\n\nUSER CLARIFICATION ANSWER:\n{answer}" if answer else "")
        )},
    ]
    last_error = ""
    for attempt in range(2):
        raw = await _llm(mission, "planner", messages, max_tokens=2200)
        try:
            payload = _parse_json_block(raw)
            question = payload.get("clarification")
            if question and not answer:
                mission["status"] = "clarifying"
                mission["clarification"] = str(question)[:500]
                _event(mission, "clarify", mission["clarification"])
                _save_mission(mission)
                return
            mission["tasks"] = _validate_plan(payload.get("tasks"))
            mission["clarification"] = None
            mission["status"] = "ready"
            _event(mission, "planned", f"{len(mission['tasks'])} tasks planned")
            _save_mission(mission)
            return
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            messages.append({"role": "assistant", "content": raw[:4000]})
            messages.append({"role": "user", "content":
                             f"Invalid plan ({exc}). Reply with ONLY the corrected JSON object."})
    raise MissionError(f"Planner produced no valid plan: {last_error}")


async def _repo_context(mission: dict[str, Any]) -> str:
    """Small read-only repo snapshot for the planner (tree + git status)."""
    parts: list[str] = []
    try:
        tree = await _dispatch(mission, "ws_tree", {"depth": 2, "limit": 120}, task_id="plan")
        parts.append("FILE TREE (depth 2):\n" + _render_tree(tree.get("entries") or [], 0))
    except (WorkspaceError, WorkspaceDenied, WorkspaceApprovalRequired, MissionError) as exc:
        parts.append(f"(tree unavailable: {exc})")
    try:
        git = await _dispatch(mission, "git_info", {}, task_id="plan")
        if git.get("is_repo"):
            parts.append(f"GIT: branch={git.get('branch')} dirty={git.get('dirty')} "
                         f"changes={len(git.get('status') or [])}")
    except (WorkspaceError, WorkspaceDenied, WorkspaceApprovalRequired, MissionError):
        pass
    return "\n\n".join(parts)[:6000]


def _render_tree(entries: list[dict[str, Any]], level: int) -> str:
    lines: list[str] = []
    for entry in entries[:80]:
        lines.append("  " * level + entry.get("name", "?") + ("/" if entry.get("type") == "dir" else ""))
        if entry.get("children"):
            lines.append(_render_tree(entry["children"], level + 1))
    return "\n".join(filter(None, lines))


# ── creation ─────────────────────────────────────────────────────────────


def create_mission(
    owner: str,
    workspace_id: str,
    goal: str,
    *,
    mode: str = "auto",
    roles: dict[str, dict[str, str]] | None = None,
    allow_network: bool = False,
) -> dict[str, Any]:
    owner = str(owner or "").strip().lower()
    goal = str(goal or "").strip()
    if not goal or len(goal) < 8:
        raise MissionError("A mission needs a real goal")
    workspace = mission_workspaces.get_workspace(owner, workspace_id)  # owner check
    if mode not in ("ask", "auto", "full"):
        raise MissionError("Unknown permission mode")
    roles = roles if isinstance(roles, dict) else {}
    cleaned_roles = {}
    for role in ROLES:
        target = roles.get(role)
        if isinstance(target, dict) and target.get("endpoint_id") and target.get("model"):
            cleaned_roles[role] = {"endpoint_id": str(target["endpoint_id"]),
                                   "model": str(target["model"])[:120]}
    if "planner" not in cleaned_roles:
        raise MissionError("Assign at least a planner model")
    mission = {
        "id": secrets.token_urlsafe(9),
        "owner": owner,
        "workspace_id": workspace["id"],
        "device_id": workspace["device_id"],
        "goal": goal[:4000],
        "mode": mode,
        "allow_network": bool(allow_network),
        "roles": cleaned_roles,
        "status": "created",
        "clarification": None,
        "tasks": [],
        "approvals": [],
        "events": [],
        "files_read": [],
        "files_changed": [],
        "commands": [],
        "usage": {"llm_calls": 0, "actions": 0, "by_model": {}},
        "checkpoint": None,
        "report": "",
        "created_at": time.time(),
    }
    _event(mission, "created", f"mode={mode} workspace={workspace['name']}")
    _save_mission(mission)
    audit(owner, "mission_created", {"mission_id": mission["id"], "workspace_id": workspace["id"], "mode": mode})
    return mission


# ── dispatch + approvals ─────────────────────────────────────────────────


async def _dispatch(mission: dict[str, Any], action: str, args: dict[str, Any],
                    *, task_id: str) -> dict[str, Any]:
    """Policy-checked relay dispatch with mission bookkeeping.

    Raises WorkspaceApprovalRequired upward — the task loop turns it into a
    durable approval and pauses.
    """
    usage = mission.setdefault("usage", {"llm_calls": 0, "actions": 0, "by_model": {}})
    if usage["actions"] >= MAX_ACTIONS:
        raise MissionError(f"Mission action budget exhausted ({MAX_ACTIONS})")
    usage["actions"] += 1
    result = await asyncio.to_thread(
        mission_workspaces.dispatch,
        mission["owner"], mission["workspace_id"], action, args,
        mode=mission.get("mode") or "auto",
        mission_id=mission["id"],
        mission_network_approved=bool(mission.get("allow_network")),
    )
    _record_effects(mission, action, args, task_id)
    return result


def _record_effects(mission: dict[str, Any], action: str, args: dict[str, Any], task_id: str) -> None:
    def _add(bucket: str, value: str, cap: int = 300) -> None:
        items = mission.setdefault(bucket, [])
        if value and value not in items:
            items.append(value)
            if len(items) > cap:
                del items[: len(items) - cap]

    if action in {"ws_read", "ws_stat"}:
        _add("files_read", str(args.get("path") or ""))
    elif action in {"ws_write", "ws_rename", "ws_delete"}:
        _add("files_changed", str(args.get("path") or ""))
    elif action == "ws_patch":
        for edit in args.get("edits") or []:
            if isinstance(edit, dict):
                _add("files_changed", str(edit.get("path") or ""))
    elif action == "ws_run":
        mission.setdefault("commands", []).append({
            "ts": time.time(), "task_id": task_id,
            "command": str(args.get("command") or "")[:300],
        })
        if len(mission["commands"]) > 200:
            mission["commands"] = mission["commands"][-200:]


def _add_approval(mission: dict[str, Any], task_id: str, action: str,
                  args: dict[str, Any], exc: WorkspaceApprovalRequired) -> dict[str, Any]:
    approval = {
        "id": secrets.token_urlsafe(8),
        "task_id": task_id,
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
    mission.setdefault("approvals", []).append(approval)
    _event(mission, "approval_requested", f"{approval['summary']} — {approval['reason']}", task_id)
    return approval


def resolve_approval(owner: str, mission_id: str, approval_id: str, decision: str,
                     *, session_id: str = "") -> dict[str, Any]:
    """Resolve a pending approval. Called only from interactive routes.

    decision: allow_once | allow_mission | allow_always | decline | stop_mission
    """
    mission = load_mission(owner, mission_id)
    approval = next((a for a in mission.get("approvals", [])
                     if a.get("id") == approval_id and a.get("status") == "pending"), None)
    if not approval:
        raise MissionError("Approval not found or already resolved")
    scope_map = {"allow_once": "once", "allow_mission": "mission", "allow_always": "workspace"}
    if decision in scope_map:
        # Resolve the store through the module so the same instance that
        # evaluate() consults receives the grant.
        mission_policy.GRANTS.grant(
            owner, scope_map[decision],
            mission_id if decision != "allow_always" else "",
            approval["grant_key"],
            workspace_id=mission["workspace_id"],
            summary=approval["summary"],
        )
        approval["status"] = "approved"
        approval["scope"] = scope_map[decision]
    elif decision == "decline":
        approval["status"] = "declined"
    elif decision == "stop_mission":
        approval["status"] = "declined"
        mission["status"] = "cancelled"
        _event(mission, "cancelled", "stopped from an approval request")
    else:
        raise MissionError(f"Unknown decision: {decision}")
    approval["resolved_at"] = time.time()
    _event(mission, "approval_" + approval["status"],
           f"{approval['summary']} ({decision})", approval.get("task_id", ""))
    if mission["status"] == "paused_approval" and not any(
            a.get("status") == "pending" for a in mission["approvals"]):
        mission["status"] = "running"
    _save_mission(mission)
    audit(owner, "mission_approval", {"mission_id": mission_id, "approval_id": approval_id,
                                      "decision": decision, "summary": approval["summary"][:200]})
    _wake(mission_id)
    if decision == "stop_mission":
        stop_mission(owner, mission_id)
    return approval


# ── task execution (the tool loop) ──────────────────────────────────────

_WORKER_PROMPT = """You are the {role} on an autonomous engineering mission.
Work ONLY toward your task goal. You operate on a real repository through tools.

Respond with ONLY one JSON object per turn — no prose outside JSON:
- {{"tool": "<name>", "args": {{...}}}} to act. Tools:
  ws_tree{{path?,depth?}}, ws_read{{path}}, ws_search{{query,mode:"content"|"name"}},
  ws_write{{path,text}}, ws_patch{{edits:[{{path,old,new}}|{{path,text}}]}},
  ws_delete{{path}}, ws_run{{command,timeout?}}, ws_diff{{path,text}},
  git_info{{}}, git_diff{{target?,path?}}, git_log{{limit?}}, git_commit{{message}}
- {{"done": true, "summary": "what you accomplished, with evidence"}}
- {{"fail": "why this task cannot be completed"}}

Rules: prefer ws_patch with exact old/new for edits; read before you write;
run the narrowest verifying command; never invent file contents; if an action
is denied or needs approval you will see that in the result — adapt or finish.
Steps are limited to {max_steps}; be economical."""


async def _run_task(mission: dict[str, Any], task: dict[str, Any]) -> None:
    task["status"] = "running"
    task["attempts"] = int(task.get("attempts") or 0) + 1
    _event(mission, "task_started", task["title"], task["id"])
    _save_mission(mission)

    transcript: list[dict[str, str]] = [
        {"role": "system", "content": _WORKER_PROMPT.format(role=task["role"], max_steps=MAX_TASK_STEPS)},
        {"role": "user", "content": (
            f"MISSION GOAL:\n{mission['goal']}\n\nYOUR TASK ({task['id']}): {task['title']}\n"
            f"{task['goal']}\n\nCompleted prerequisite results:\n" + (
                "\n".join(f"- {t['id']} {t['title']}: {str(t.get('result') or '')[:300]}"
                          for t in mission["tasks"] if t["id"] in task["depends_on"]) or "(none)")
        )},
    ]

    for step in range(MAX_TASK_STEPS):
        await _pause_gate(mission)
        raw = await _llm(mission, task["role"], transcript, max_tokens=1600)
        transcript.append({"role": "assistant", "content": raw[:6000]})
        try:
            payload = _parse_json_block(raw)
        except (ValueError, json.JSONDecodeError):
            transcript.append({"role": "user", "content":
                               "Your reply was not a single valid JSON object. Try again."})
            continue

        if payload.get("done"):
            task["status"] = "done"
            task["result"] = str(payload.get("summary") or "completed")[:2000]
            _event(mission, "task_done", task["result"][:200], task["id"])
            _save_mission(mission)
            return
        if payload.get("fail"):
            raise MissionError(str(payload["fail"])[:500])

        action = str(payload.get("tool") or "").strip()
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        if action not in mission_workspaces.WORKSPACE_ACTIONS:
            transcript.append({"role": "user", "content": f"Unknown tool: {action}"})
            continue
        if action in {"ws_write", "ws_patch", "ws_delete", "ws_rename", "ws_restore"}:
            args.setdefault("checkpoint_id", await _ensure_checkpoint(mission))

        outcome = await _execute_with_approval(mission, task, action, args)
        task.setdefault("logs", []).append({
            "ts": time.time(), "step": step, "action": action,
            "summary": str(outcome)[:300],
        })
        if len(task["logs"]) > 60:
            task["logs"] = task["logs"][-60:]
        _save_mission(mission)
        transcript.append({"role": "user", "content": f"RESULT of {action}:\n{outcome[:7000]}"})

    raise MissionError(f"Task exceeded its step budget ({MAX_TASK_STEPS})")


async def _execute_with_approval(mission: dict[str, Any], task: dict[str, Any],
                                 action: str, args: dict[str, Any]) -> str:
    """Run one tool call; on REQUIRE_APPROVAL pause durably until resolved."""
    while True:
        try:
            result = await _dispatch(mission, action, args, task_id=task["id"])
            return json.dumps(result, ensure_ascii=False, default=str)
        except WorkspaceApprovalRequired as exc:
            approval = _add_approval(mission, task["id"], action, args, exc)
            mission["status"] = "paused_approval"
            task["status"] = "blocked"
            _save_mission(mission)
            resolved = await _wait_for_approval(mission, approval["id"])
            task["status"] = "running"
            mission_fresh = load_mission(mission["owner"], mission["id"])
            if mission_fresh.get("status") == "cancelled":
                raise asyncio.CancelledError()
            if mission["status"] == "paused_approval":
                mission["status"] = "running"
            if resolved.get("status") == "approved":
                _event(mission, "approval_consumed", approval["summary"], task["id"])
                continue  # grant now covers it; retry
            return f"DENIED BY USER: {approval['summary']} — adapt your approach or finish."
        except WorkspaceDenied as exc:
            return f"DENIED BY POLICY: {exc.decision.reason}"
        except (WorkspaceError, MissionError) as exc:
            return f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001 — relay/device errors come as plain exceptions
            return f"ERROR: {str(exc)[:400]}"


async def _wait_for_approval(mission: dict[str, Any], approval_id: str) -> dict[str, Any]:
    deadline = time.time() + APPROVAL_WAIT_SECONDS
    runner = _RUNNERS.get(mission["id"])
    while time.time() < deadline:
        # Arm the wake event BEFORE reading state: a resolution arriving
        # between the read and the wait then sets the event we actually
        # await (events are level-triggered), instead of a stale one.
        if runner:
            runner["wake"] = asyncio.Event()
        fresh = load_mission(mission["owner"], mission["id"])
        mission["approvals"] = fresh.get("approvals", [])
        row = next((a for a in mission["approvals"] if a.get("id") == approval_id), None)
        if row and row.get("status") != "pending":
            return row
        if fresh.get("status") == "cancelled":
            raise asyncio.CancelledError()
        if runner:
            try:
                await asyncio.wait_for(runner["wake"].wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(5)
    raise MissionError("Approval timed out after 24h")


def _wake(mission_id: str) -> None:
    runner = _RUNNERS.get(mission_id)
    if not runner or not isinstance(runner.get("wake"), asyncio.Event):
        return
    loop = runner.get("loop")
    try:
        if loop and loop.is_running():
            # Routes run in worker threads; Event.set is not thread-safe.
            loop.call_soon_threadsafe(runner["wake"].set)
        else:
            runner["wake"].set()
    except RuntimeError:
        pass  # the 15s poll fallback in _wait_for_approval still applies


async def _pause_gate(mission: dict[str, Any]) -> None:
    runner = _RUNNERS.get(mission["id"])
    while runner and runner.get("pause"):
        if mission["status"] != "paused":
            mission["status"] = "paused"
            _save_mission(mission)
        await asyncio.sleep(2)
    if mission["status"] == "paused":
        mission["status"] = "running"


async def _ensure_checkpoint(mission: dict[str, Any]) -> str:
    """Create the safety checkpoint before the first mutation."""
    if mission.get("checkpoint"):
        return mission["checkpoint"]["id"]
    checkpoint_id = f"m-{mission['id']}"
    result = await _dispatch(mission, "ws_checkpoint", {"checkpoint_id": checkpoint_id}, task_id="safety")
    mission["checkpoint"] = {
        "id": checkpoint_id,
        "git": result.get("git"),
        "created_at": time.time(),
    }
    if result.get("uncommitted_user_work"):
        _event(mission, "warning",
               "Uncommitted user changes detected before the mission started; "
               "they are captured in the checkpoint pre-images and will not be overwritten blindly.")
    git = result.get("git") or {}
    if git and not git.get("dirty"):
        try:
            branch = f"shadow/mission-{mission['id']}"
            await _dispatch(mission, "git_checkout", {"branch": branch, "create": True}, task_id="safety")
            mission["checkpoint"]["branch"] = branch
            mission["checkpoint"]["base_branch"] = git.get("branch")
            _event(mission, "checkpoint", f"working on branch {branch} (base {git.get('branch')})")
        except Exception as exc:  # noqa: BLE001 — branch is best-effort; pre-images are the guarantee
            _event(mission, "checkpoint", f"git branch skipped: {str(exc)[:120]}")
    else:
        _event(mission, "checkpoint", "filesystem checkpoint active (pre-image snapshots)")
    _save_mission(mission)
    return checkpoint_id


# ── the mission runner ───────────────────────────────────────────────────


async def _runner(mission_id: str, owner: str) -> None:
    mission = load_mission(owner, mission_id)
    started = time.time()
    try:
        mission["status"] = "running"
        _event(mission, "started", "mission runner online")
        _save_mission(mission)
        while True:
            if time.time() - started > MAX_WALL_SECONDS:
                raise MissionError("Mission wall-clock budget exhausted")
            await _pause_gate(mission)
            tasks = mission["tasks"]
            done_ids = {t["id"] for t in tasks if t["status"] == "done"}
            # Tasks whose dependencies failed can never run — propagate to a
            # fixpoint so a failure cascades through the whole downstream DAG.
            changed = True
            while changed:
                changed = False
                failed_ids = {t["id"] for t in tasks if t["status"] in ("failed", "skipped")}
                for task in tasks:
                    if task["status"] in ("pending", "ready") and any(d in failed_ids for d in task["depends_on"]):
                        task["status"] = "skipped"
                        task["error"] = "skipped: a dependency failed"
                        _event(mission, "task_skipped", task["title"], task["id"])
                        changed = True
            ready = [
                t for t in tasks
                if t["status"] in ("pending", "ready")
                and all(d in done_ids for d in t["depends_on"])
            ]
            if not ready:
                if all(t["status"] in ("done", "failed", "skipped") for t in tasks):
                    break
                if not any(t["status"] in ("running", "blocked") for t in tasks):
                    break  # nothing ready, nothing running: plan is stuck
                await asyncio.sleep(1)
                continue

            batch = ready[:MAX_CONCURRENT_TASKS]
            results = await asyncio.gather(
                *[_run_task_guard(mission, task) for task in batch],
                return_exceptions=False,
            )
            del results
            _save_mission(mission)

        failed = [t for t in mission["tasks"] if t["status"] == "failed"]
        mission["status"] = "completed" if not failed else "completed_with_failures"
        await _final_review(mission)
        _event(mission, "finished", mission["status"])
        _save_mission(mission)
        audit(owner, "mission_finished", {"mission_id": mission_id, "status": mission["status"]})
    except asyncio.CancelledError:
        fresh_status = "cancelled"
        try:
            fresh_status = load_mission(owner, mission_id).get("status") or "cancelled"
        except MissionError:
            pass
        mission["status"] = "cancelled" if fresh_status != "paused" else "paused"
        _event(mission, "cancelled", "runner cancelled")
        _save_mission(mission)
        raise
    except MissionError as exc:
        mission["status"] = "failed"
        mission["error"] = str(exc)[:500]
        _event(mission, "failed", str(exc))
        _save_mission(mission)
    except Exception as exc:  # noqa: BLE001 — one failure must not lose the record
        logger.exception("Mission %s crashed", mission_id)
        mission["status"] = "failed"
        mission["error"] = f"internal error: {str(exc)[:300]}"
        _event(mission, "failed", mission["error"])
        _save_mission(mission)
    finally:
        _RUNNERS.pop(mission_id, None)


async def _run_task_guard(mission: dict[str, Any], task: dict[str, Any]) -> None:
    """One failed task never crashes the mission; bounded retries apply."""
    try:
        await _run_task(mission, task)
    except asyncio.CancelledError:
        task["status"] = "ready"  # safe to re-run after resume
        raise
    except (MissionError, WorkspaceError) as exc:
        if task["attempts"] < task.get("max_attempts", MAX_TASK_ATTEMPTS):
            task["status"] = "ready"
            task["error"] = f"attempt {task['attempts']} failed: {exc}"
            _event(mission, "task_retry", f"{task['title']}: {exc}", task["id"])
        else:
            task["status"] = "failed"
            task["error"] = str(exc)[:500]
            _event(mission, "task_failed", f"{task['title']}: {exc}", task["id"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Task %s crashed", task["id"])
        task["status"] = "failed"
        task["error"] = f"internal error: {str(exc)[:300]}"
        _event(mission, "task_failed", task["error"], task["id"])
    _save_mission(mission)


async def _final_review(mission: dict[str, Any]) -> None:
    """Review the final diff and produce the report (LLM, with fallback)."""
    diff_text = ""
    try:
        diff = await _dispatch(mission, "git_diff", {}, task_id="review")
        diff_text = str(diff.get("diff") or "")[:12000]
    except Exception:  # noqa: BLE001
        diff_text = "(diff unavailable)"
    summary = {
        "goal": mission["goal"],
        "status": mission["status"],
        "tasks": [{"id": t["id"], "title": t["title"], "role": t["role"],
                   "status": t["status"], "result": str(t.get("result") or "")[:300],
                   "error": str(t.get("error") or "")[:200]} for t in mission["tasks"]],
        "files_changed": mission.get("files_changed", []),
        "commands": [c.get("command") for c in mission.get("commands", [])][-20:],
        "approvals": [{"summary": a["summary"], "status": a["status"]}
                      for a in mission.get("approvals", [])],
    }
    try:
        raw = await _llm(mission, "reviewer", [
            {"role": "system", "content":
             "Write the final mission report in markdown with sections: "
             "## Changes, ## Evidence, ## Tests, ## Risks, ## Remaining work. "
             "Be specific and honest; never claim success without evidence."},
            {"role": "user", "content":
             f"MISSION RECORD:\n{json.dumps(summary, indent=1)[:9000]}\n\nFINAL DIFF:\n{diff_text}"},
        ], max_tokens=1800)
        mission["report"] = raw.strip()[:20000]
    except Exception as exc:  # noqa: BLE001 — deterministic fallback report
        done = sum(1 for t in mission["tasks"] if t["status"] == "done")
        mission["report"] = (
            f"## Changes\n- files: {', '.join(mission.get('files_changed') or []) or 'none'}\n\n"
            f"## Evidence\n- {done}/{len(mission['tasks'])} tasks done\n\n"
            f"## Tests\n- commands run: {len(mission.get('commands') or [])}\n\n"
            f"## Risks\n- automated report failed: {str(exc)[:200]}\n\n"
            f"## Remaining work\n- review task results manually"
        )


# ── lifecycle API (used by routes) ───────────────────────────────────────


def start_mission(owner: str, mission_id: str) -> dict[str, Any]:
    mission = load_mission(owner, mission_id)
    if mission["status"] not in ("ready", "paused", "failed", "created"):
        raise MissionError(f"Mission cannot start from status '{mission['status']}'")
    if not mission.get("tasks"):
        raise MissionError("Plan the mission first")
    if mission_id in _RUNNERS:
        raise MissionError("Mission is already running")
    # Crash/restart safety: tasks stuck in 'running'/'blocked' re-run.
    for task in mission["tasks"]:
        if task["status"] in ("running", "blocked"):
            task["status"] = "ready"
    _save_mission(mission)
    loop = asyncio.get_running_loop()  # callers must be on the event loop (async routes)
    runner_task = loop.create_task(_runner(mission_id, mission["owner"]))
    _RUNNERS[mission_id] = {"task": runner_task, "wake": asyncio.Event(), "pause": False, "loop": loop}
    return {"ok": True, "status": "running"}


def pause_mission(owner: str, mission_id: str) -> dict[str, Any]:
    load_mission(owner, mission_id)  # ownership check
    runner = _RUNNERS.get(mission_id)
    if not runner:
        raise MissionError("Mission is not running")
    runner["pause"] = True
    return {"ok": True, "status": "pausing"}


def resume_mission(owner: str, mission_id: str) -> dict[str, Any]:
    mission = load_mission(owner, mission_id)
    runner = _RUNNERS.get(mission_id)
    if runner:
        runner["pause"] = False
        _wake(mission_id)
        return {"ok": True, "status": "running"}
    if mission["status"] in ("paused", "paused_approval", "running"):
        # running-but-no-runner = recovered after restart
        mission["status"] = "paused"
        _save_mission(mission)
        return start_mission(owner, mission_id)
    raise MissionError(f"Mission cannot resume from status '{mission['status']}'")


def stop_mission(owner: str, mission_id: str) -> dict[str, Any]:
    mission = load_mission(owner, mission_id)
    runner = _RUNNERS.pop(mission_id, None)
    if runner and isinstance(runner.get("task"), asyncio.Task):
        runner["task"].cancel()
    if mission["status"] not in ("completed", "completed_with_failures", "failed"):
        mission["status"] = "cancelled"
        _event(mission, "cancelled", "emergency stop")
        _save_mission(mission)
    audit(owner, "mission_stopped", {"mission_id": mission_id})
    return {"ok": True, "status": mission["status"]}


async def rollback_mission(owner: str, mission_id: str, *, paths: list[str] | None = None) -> dict[str, Any]:
    """Per-file (``paths``) or full mission rollback via the agent checkpoint."""
    mission = load_mission(owner, mission_id)
    if mission_id in _RUNNERS:
        raise MissionError("Stop the mission before rolling back")
    checkpoint = mission.get("checkpoint")
    if not checkpoint:
        raise MissionError("This mission made no checkpointed changes")
    args: dict[str, Any] = {"checkpoint_id": checkpoint["id"]}
    if paths:
        args["paths"] = [str(p) for p in paths][:100]
    result = await asyncio.to_thread(
        mission_workspaces.dispatch,
        owner, mission["workspace_id"], "ws_restore", args,
        mode="auto", mission_id=mission_id,
    )
    if not paths:
        mission["status"] = "rolled_back"
        base = (checkpoint or {}).get("base_branch")
        if base:
            try:
                await asyncio.to_thread(
                    mission_workspaces.dispatch,
                    owner, mission["workspace_id"], "git_checkout", {"branch": base},
                    mode="auto", mission_id=mission_id,
                )
            except Exception as exc:  # noqa: BLE001
                _event(mission, "warning", f"could not return to {base}: {str(exc)[:120]}")
    _event(mission, "rolled_back", f"restored={len(result.get('restored') or [])} "
                                   f"removed={len(result.get('removed_created') or [])}"
                                   + (f" (paths={len(paths)})" if paths else " (full)"))
    _save_mission(mission)
    audit(owner, "mission_rollback", {"mission_id": mission_id, "full": not paths})
    return result


def recover_missions() -> int:
    """Startup hook: missions that were running when the server died become
    resumable instead of being silently lost."""
    recovered = 0
    if not MISSIONS_DIR.exists():
        return 0
    for path in MISSIONS_DIR.glob("*.json"):
        try:
            mission = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(mission, dict):
            continue
        if mission.get("status") in ("running", "paused_approval", "planning") \
                and mission.get("id") not in _RUNNERS:
            mission["status"] = "paused"
            for task in mission.get("tasks", []):
                if task.get("status") in ("running", "blocked"):
                    task["status"] = "ready"
            _event(mission, "recovered", "server restarted; mission paused and is resumable")
            try:
                _save_mission(mission)
                recovered += 1
            except MissionError:
                continue
    if recovered:
        logger.info("Recovered %d interrupted mission(s) to paused state", recovered)
    return recovered
