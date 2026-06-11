"""Authorized desktop workspaces and the policy-checked dispatch path.

A workspace is one explicitly authorized folder on one enrolled device,
owned by one Shadow account. Every file/git/command action targeting it is
declared as a :class:`src.mission_policy.ActionRequest`, decided by the
policy engine, and only then dispatched through the existing outbound device
relay (`src.shadow_devices.dispatch_action`). The device agent re-enforces
root containment independently, so the server is never the only guard.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_json
from src import shadow_devices
from src.mission_policy import (
    ALLOW,
    DENY,
    MODES,
    ActionRequest,
    Decision,
    MissionPolicyError,
    audit,
    canonicalize_path,
    evaluate,
    classify_command,
)

DATA_DIR = Path(os.getenv("SHADOW_MISSIONS_DATA", "data/missions"))
WORKSPACES_PATH = DATA_DIR / "workspaces.json"
_LOCK = threading.RLock()


class WorkspaceError(RuntimeError):
    """A clean workspace failure."""


class WorkspaceApprovalRequired(RuntimeError):
    """Raised when the policy engine requires explicit human approval."""

    def __init__(self, decision: Decision, request: ActionRequest):
        super().__init__(decision.reason)
        self.decision = decision
        self.request = request


class WorkspaceDenied(RuntimeError):
    """Raised when the policy engine denies an action outright."""

    def __init__(self, decision: Decision):
        super().__init__(decision.reason)
        self.decision = decision


# ── registry ─────────────────────────────────────────────────────────────


def _owner(value: Any) -> str:
    return str(value or "").strip().lower()[:100]


def _state() -> dict[str, Any]:
    try:
        raw = json.loads(WORKSPACES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "workspaces": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("workspaces"), dict):
        return {"version": 1, "workspaces": {}}
    return raw


def _save(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(WORKSPACES_PATH), state, indent=2)


def create_workspace(owner: str, device_id: str, root: str, name: str = "",
                     mode: str = "auto") -> dict[str, Any]:
    owner = _owner(owner)
    if not owner:
        raise WorkspaceError("A real Shadow account is required")
    device = shadow_devices.get_device(owner, device_id)  # raises on cross-user
    root_canonical = canonicalize_path(root)
    if not root_canonical or root_canonical in (".", "/", ".."):
        raise WorkspaceError("Workspace root must be a concrete folder")
    if mode not in MODES:
        raise WorkspaceError("Unknown permission mode")
    row = {
        "id": secrets.token_urlsafe(9),
        "owner": owner,
        "device_id": device["id"],
        "device_name": device.get("name"),
        "name": str(name or "").strip()[:100] or root_canonical.rstrip("/").rsplit("/", 1)[-1],
        "root": root_canonical,
        "mode": mode,
        "created_at": time.time(),
    }
    with _LOCK:
        state = _state()
        for existing in state["workspaces"].values():
            if (isinstance(existing, dict) and existing.get("owner") == owner
                    and existing.get("device_id") == device["id"]
                    and existing.get("root") == root_canonical):
                return dict(existing)
        state["workspaces"][row["id"]] = row
        _save(state)
    audit(owner, "workspace_authorized", {"workspace_id": row["id"], "device_id": device["id"], "root": root_canonical})
    return row


def list_workspaces(owner: str) -> list[dict[str, Any]]:
    owner = _owner(owner)
    with _LOCK:
        state = _state()
    return sorted(
        (dict(row) for row in state["workspaces"].values()
         if isinstance(row, dict) and row.get("owner") == owner),
        key=lambda r: -float(r.get("created_at") or 0),
    )


def get_workspace(owner: str, workspace_id: str) -> dict[str, Any]:
    owner = _owner(owner)
    with _LOCK:
        state = _state()
    row = state["workspaces"].get(str(workspace_id or ""))
    if not isinstance(row, dict) or row.get("owner") != owner:
        raise WorkspaceError("That workspace does not belong to your account")
    return dict(row)


def set_workspace_mode(owner: str, workspace_id: str, mode: str) -> dict[str, Any]:
    if mode not in MODES:
        raise WorkspaceError("Unknown permission mode")
    row = get_workspace(owner, workspace_id)
    with _LOCK:
        state = _state()
        state["workspaces"][row["id"]]["mode"] = mode
        _save(state)
        row = dict(state["workspaces"][row["id"]])
    audit(_owner(owner), "workspace_mode_changed", {"workspace_id": row["id"], "mode": mode})
    return row


def remove_workspace(owner: str, workspace_id: str) -> dict[str, Any]:
    row = get_workspace(owner, workspace_id)
    with _LOCK:
        state = _state()
        state["workspaces"].pop(row["id"], None)
        _save(state)
    audit(_owner(owner), "workspace_revoked", {"workspace_id": row["id"]})
    return {"ok": True, "id": row["id"]}


# ── action declaration ───────────────────────────────────────────────────

# action -> (capability, mutating, base risk)
_ACTION_DECL: dict[str, tuple[str, bool, str]] = {
    "ws_tree": ("fs_read", False, "low"),
    "ws_stat": ("fs_read", False, "low"),
    "ws_read": ("fs_read", False, "low"),
    "ws_search": ("fs_read", False, "low"),
    "ws_hash": ("fs_read", False, "low"),
    "ws_diff": ("fs_read", False, "low"),
    "git_info": ("git_read", False, "low"),
    "git_log": ("git_read", False, "low"),
    "git_diff": ("git_read", False, "low"),
    "ws_write": ("fs_write", True, "medium"),
    "ws_mkdir": ("fs_write", True, "low"),
    "ws_rename": ("fs_write", True, "medium"),
    "ws_patch": ("fs_write", True, "medium"),
    "ws_delete": ("fs_delete", True, "medium"),
    "ws_run": ("shell", True, "medium"),
    "git_commit": ("git_write", True, "medium"),
    "git_checkout": ("git_write", True, "medium"),
    "ws_checkpoint": ("fs_write", True, "low"),
    "ws_restore": ("fs_write", True, "medium"),
}

WORKSPACE_ACTIONS = frozenset(_ACTION_DECL)


def _summary_for(action: str, args: dict[str, Any]) -> str:
    if action == "ws_run":
        return f"Run command: {str(args.get('command') or '')[:160]}"
    if action == "ws_patch":
        paths = ", ".join(str(e.get("path") or "?") for e in (args.get("edits") or [])[:5] if isinstance(e, dict))
        return f"Edit files: {paths[:160]}"
    if action.startswith("git_"):
        return f"Git {action[4:]}: {str(args.get('branch') or args.get('message') or args.get('target') or '')[:120]}".strip()
    target = str(args.get("path") or args.get("to") or "")[:160]
    return f"{action.replace('ws_', 'Workspace ').replace('_', ' ')} {target}".strip()


def _paths_in_args(action: str, args: dict[str, Any]) -> list[str]:
    paths = []
    for key in ("path", "to", "cwd"):
        if args.get(key):
            paths.append(str(args[key]))
    if action == "ws_patch":
        for edit in args.get("edits") or []:
            if isinstance(edit, dict) and edit.get("path"):
                paths.append(str(edit["path"]))
    if action == "ws_hash":
        paths.extend(str(p) for p in (args.get("paths") or []))
    return paths


def build_request(owner: str, workspace: dict[str, Any], action: str,
                  args: dict[str, Any], *, mission_id: str = "") -> ActionRequest:
    """Declare an ActionRequest for a workspace action (server-side check).

    ``outside_roots`` is a *syntactic* verdict here; the device agent
    repeats containment against the real filesystem.
    """
    if action not in _ACTION_DECL:
        raise WorkspaceError(f"Unknown workspace action: {action}")
    capability, mutating, risk = _ACTION_DECL[action]
    root = str(workspace.get("root") or "")
    outside = False
    primary_path = ""
    for raw in _paths_in_args(action, args):
        try:
            canonical = canonicalize_path(raw)
        except MissionPolicyError:
            outside = True
            continue
        if not primary_path:
            primary_path = canonical
        # Absolute paths must stay inside the workspace root; relative paths
        # must not climb out (the agent resolves them against the root).
        if canonical.startswith("/") or canonical[1:3] in (":/", ":\\"):
            from src.mission_policy import path_within_root
            if not path_within_root(canonical, root):
                outside = True
        elif canonical == ".." or canonical.startswith("../"):
            outside = True
    command = str(args.get("command") or "") if action == "ws_run" else ""
    network = bool(classify_command(command)["network"]) if command else False
    return ActionRequest(
        capability=capability,
        summary=_summary_for(action, args),
        owner=_owner(owner),
        device_id=str(workspace.get("device_id") or ""),
        workspace_id=str(workspace.get("id") or ""),
        mutating=mutating,
        risk=risk,
        network=network,
        outside_roots=outside,
        command=command,
        path=primary_path,
        mission_id=mission_id,
        detail={"workspace_root": root},
    )


def dispatch(
    owner: str,
    workspace_id: str,
    action: str,
    args: dict[str, Any] | None = None,
    *,
    mode: str | None = None,
    mission_id: str = "",
    session_id: str = "",
    mission_network_approved: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Policy-check then relay one workspace action to the device agent.

    Raises :class:`WorkspaceApprovalRequired` / :class:`WorkspaceDenied`
    when the policy engine does not allow the action outright.
    """
    workspace = get_workspace(owner, workspace_id)
    args = dict(args or {})
    request = build_request(owner, workspace, action, args, mission_id=mission_id)
    decision = evaluate(
        request,
        mode=mode or str(workspace.get("mode") or "auto"),
        mission_id=mission_id,
        session_id=session_id,
        mission_network_approved=mission_network_approved,
    )
    if decision.verdict == DENY:
        audit(owner, "action_denied", {"workspace_id": workspace["id"], "action": action,
                                       "reason": decision.reason, "rule": decision.rule})
        raise WorkspaceDenied(decision)
    if decision.verdict != ALLOW:
        raise WorkspaceApprovalRequired(decision, request)

    args["roots"] = [workspace["root"]]
    if timeout is None:
        timeout = 615 if action == "ws_run" else 90 if action in {"ws_patch", "ws_restore", "ws_search", "git_diff"} else 45
    result = shadow_devices.dispatch_action(
        owner,
        workspace["device_id"],
        action,
        args,
        confirmed=True,  # policy/approval already decided; the agent re-checks roots
        timeout=timeout,
    )
    audit(owner, "action_executed", {
        "workspace_id": workspace["id"], "action": action,
        "summary": request.summary[:200], "rule": decision.rule,
        "mission_id": mission_id or None,
    })
    return result
