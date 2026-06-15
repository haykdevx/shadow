"""HTTP surface for desktop workspaces, direct agent sessions, and permissions.

Every route requires an interactive Shadow session (cookie). API tokens and
the internal agent bridge are rejected on purpose: approvals, permission-mode
changes, and full-access arming are the *human* side of the confirmation
gates, so an agent credential must never be able to grant itself anything —
the same contract as the home-PC control and agent-browser routes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.auth_helpers import require_privilege, require_user
from src import agent_sessions, workspace_service
from src.agent_sessions import AgentSessionError
from src.workspace_policy import (
    WorkspacePolicyError,
    arm_full_access,
    disarm_full_access,
    full_access_active,
    list_persistent_rules,
    read_audit_log,
    revoke_persistent_rule,
)
from src.workspace_service import (
    WorkspaceApprovalRequired,
    WorkspaceDenied,
    WorkspaceError,
)


def _real_user(request: Request) -> str:
    user = require_user(request)
    if not user or getattr(request.state, "api_token", False) or user in {"api", "internal-tool"}:
        raise HTTPException(403, "An interactive Shadow account is required")
    return str(user).strip().lower()


def _session_id(request: Request) -> str:
    # Session-scoped grants expire with the cookie session.
    return str(request.cookies.get("shadow_session") or "")[:32]


def _require_computer_access(request: Request) -> str:
    """Gate every workspace-agent write/dispatch path on one privilege.

    Computer access grants filesystem and command execution on an enrolled
    device — strictly more powerful than ``can_use_bash`` — so it is opt-in
    per account (admins always have it via ``ADMIN_PRIVILEGES``).
    """
    return require_privilege(request, "can_use_computer")


class WorkspaceCreateRequest(BaseModel):
    device_id: str = Field(..., min_length=4, max_length=80)
    root: str = Field(..., min_length=1, max_length=1000)
    name: str = Field(default="", max_length=100)
    mode: str = Field(default="auto", pattern="^(ask|auto|full|unattended)$")


class WorkspaceModeRequest(BaseModel):
    mode: str = Field(..., pattern="^(ask|auto|full|unattended)$")


class WorkspaceActionRequest(BaseModel):
    action: str = Field(..., max_length=40)
    args: dict[str, Any] = Field(default_factory=dict)
    mode: str | None = Field(default=None, pattern="^(ask|auto|full|unattended)$")


class RollbackRequest(BaseModel):
    paths: list[str] | None = Field(default=None, max_length=100)


class FullAccessRequest(BaseModel):
    device_id: str = Field(..., min_length=4, max_length=80)
    password: str = Field(..., min_length=1, max_length=200)
    duration_seconds: int | None = Field(default=None, ge=60, le=8 * 3600)


class SessionCreateRequest(BaseModel):
    workspace_id: str = Field(..., min_length=4, max_length=40)
    task: str = Field(..., min_length=4, max_length=4000)
    endpoint_id: str = Field(..., min_length=1, max_length=80)
    model: str = Field(..., min_length=1, max_length=120)
    fallbacks: list[dict[str, str]] = Field(default_factory=list, max_length=5)
    auto_fallback: bool = False


class SessionMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


class SessionRetryRequest(BaseModel):
    endpoint_id: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=120)


class SessionApprovalRequest(BaseModel):
    decision: str = Field(..., pattern="^(allow_once|allow_session|allow_always|decline|stop_session)$")


class GrantRequest(BaseModel):
    grant_key: str = Field(..., min_length=3, max_length=300)
    scope: str = Field(..., pattern="^(once|session|workspace)$")
    workspace_id: str = Field(default="", max_length=40)
    summary: str = Field(default="", max_length=300)


def setup_workspace_agent_routes() -> APIRouter:
    router = APIRouter(prefix="/api/workspace-agent", tags=["workspace-agent"])

    # ── workspaces ──────────────────────────────────────────────────────

    @router.get("/workspaces")
    def workspaces_list(request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        rows = workspace_service.list_workspaces(user)
        for row in rows:
            row["full_access"] = bool(full_access_active(user, row.get("device_id") or ""))
        return {"workspaces": rows}

    @router.post("/workspaces")
    def workspaces_create(payload: WorkspaceCreateRequest, request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        from src.shadow_devices import ShadowDeviceError
        try:
            return workspace_service.create_workspace(
                user, payload.device_id, payload.root, payload.name, payload.mode)
        except ShadowDeviceError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (WorkspaceError, WorkspacePolicyError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/workspaces/{workspace_id}")
    def workspaces_remove(workspace_id: str, request: Request):
        try:
            return workspace_service.remove_workspace(_real_user(request), workspace_id)
        except WorkspaceError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.put("/workspaces/{workspace_id}/mode")
    def workspaces_mode(workspace_id: str, payload: WorkspaceModeRequest, request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        try:
            workspace = workspace_service.get_workspace(user, workspace_id)
            if payload.mode == "full" and not full_access_active(user, workspace["device_id"]):
                raise HTTPException(403, "Arm full access first (password reauthentication required)")
            return workspace_service.set_workspace_mode(user, workspace_id, payload.mode)
        except WorkspaceError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/workspaces/{workspace_id}/action")
    async def workspaces_action(workspace_id: str, payload: WorkspaceActionRequest, request: Request):
        """Direct desktop-workspace operation (file tree, read, edit, git…).

        Returns 409 with the approval card payload when the policy engine
        requires explicit approval — the UI then offers allow once / always.
        """
        user = _real_user(request)
        _require_computer_access(request)
        if payload.mode == "full":
            workspace = workspace_service.get_workspace(user, workspace_id)
            if not full_access_active(user, workspace["device_id"]):
                raise HTTPException(403, "Full access is not armed for this device")
        try:
            result = await asyncio.to_thread(
                workspace_service.dispatch,
                user, workspace_id, payload.action, payload.args,
                mode=payload.mode, session_id=_session_id(request),
            )
            return {"ok": True, "result": result}
        except WorkspaceApprovalRequired as exc:
            return {
                "ok": False,
                "approval_required": True,
                "summary": exc.request.summary,
                "reason": exc.decision.reason,
                "capability": exc.request.capability,
                "risk": exc.request.risk,
                "grant_key": exc.decision.grant_key,
            }
        except WorkspaceDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (WorkspaceError, WorkspacePolicyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — device/relay errors
            raise HTTPException(502, str(exc)[:300]) from exc

    # ── permission policy ──────────────────────────────────────────────

    @router.post("/policy/grants")
    def policy_grant(payload: GrantRequest, request: Request):
        """Record a user-approved grant (allow once / session / always)."""
        user = _real_user(request)
        from src.workspace_policy import GRANTS
        try:
            scope_id = _session_id(request)
            row = GRANTS.grant(user, payload.scope, scope_id, payload.grant_key,
                               workspace_id=payload.workspace_id, summary=payload.summary)
            return {"ok": True, "grant": row}
        except WorkspacePolicyError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/policy/rules")
    def policy_rules(request: Request):
        return {"rules": list_persistent_rules(_real_user(request))}

    @router.delete("/policy/rules/{rule_id}")
    def policy_rule_delete(rule_id: str, request: Request):
        if not revoke_persistent_rule(_real_user(request), rule_id):
            raise HTTPException(404, "Rule not found")
        return {"ok": True}

    @router.post("/policy/full-access")
    def policy_full_access(payload: FullAccessRequest, request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        try:
            return arm_full_access(user, payload.device_id, password=payload.password,
                                   duration_seconds=payload.duration_seconds)
        except WorkspacePolicyError as exc:
            raise HTTPException(403, str(exc)) from exc

    @router.delete("/policy/full-access/{device_id}")
    def policy_full_access_off(device_id: str, request: Request):
        disarm_full_access(_real_user(request), device_id)
        return {"ok": True}

    @router.get("/policy/full-access/{device_id}")
    def policy_full_access_status(device_id: str, request: Request):
        row = full_access_active(_real_user(request), device_id)
        return {"armed": bool(row), "expires_at": (row or {}).get("expires_at")}

    # ── agent sessions (direct conversational tool loop) ───────────────

    @router.get("/sessions")
    def sessions_list(request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        return {"sessions": agent_sessions.list_sessions(user)}

    @router.post("/sessions")
    async def sessions_create(payload: SessionCreateRequest, request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        try:
            session = agent_sessions.create_session(
                user, payload.workspace_id, payload.task,
                model={"endpoint_id": payload.endpoint_id, "model": payload.model},
                fallbacks=payload.fallbacks,
                auto_fallback=payload.auto_fallback,
            )
            agent_sessions.start_session(user, session["id"])
            return agent_sessions.load_session(user, session["id"])
        except (AgentSessionError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/sessions/{sid}")
    def session_detail(sid: str, request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        try:
            return agent_sessions.load_session(user, sid)
        except AgentSessionError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/sessions/{sid}/message")
    async def session_message(sid: str, payload: SessionMessageRequest, request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        try:
            result = agent_sessions.send_message(user, sid, payload.text)
            if not result.get("running"):
                try:
                    agent_sessions.start_session(user, sid)
                except AgentSessionError:
                    pass  # e.g. raced with another start; the inbox is durable
            return result
        except AgentSessionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/sessions/{sid}/stop")
    async def session_stop(sid: str, request: Request):
        try:
            return agent_sessions.stop_session(_real_user(request), sid)
        except AgentSessionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/sessions/{sid}/resume")
    async def session_resume(sid: str, request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        try:
            return agent_sessions.retry_session(user, sid)
        except AgentSessionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/sessions/{sid}/retry")
    async def session_retry(sid: str, payload: SessionRetryRequest, request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        try:
            return agent_sessions.retry_session(
                user, sid,
                endpoint_id=payload.endpoint_id, model=payload.model)
        except AgentSessionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/sessions/{sid}/rollback")
    async def session_rollback(sid: str, payload: RollbackRequest, request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        try:
            return await agent_sessions.rollback_session(
                user, sid, paths=payload.paths)
        except AgentSessionError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (WorkspaceDenied, WorkspaceError) as exc:
            raise HTTPException(403, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, str(exc)[:300]) from exc

    @router.post("/sessions/{sid}/approvals/{approval_id}")
    async def session_approval(sid: str, approval_id: str,
                               payload: SessionApprovalRequest, request: Request):
        user = _real_user(request)
        _require_computer_access(request)
        try:
            return agent_sessions.resolve_approval(
                user, sid, approval_id, payload.decision)
        except AgentSessionError as exc:
            raise HTTPException(404, str(exc)) from exc
        except WorkspacePolicyError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ── audit log ────────────────────────────────────────────────────────

    @router.get("/audit")
    def audit_log(request: Request, limit: int = 100, before: float | None = None, event: str = ""):
        """Recent workspace-agent audit records.

        Admins see every account's activity; everyone else sees only their
        own — the same scoping ``list_workspaces``/``list_sessions`` use.
        """
        user = _real_user(request)
        auth_mgr = getattr(request.app.state, "auth_manager", None)
        try:
            is_admin = bool(auth_mgr and auth_mgr.is_admin(user))
        except Exception:
            is_admin = False
        records = read_audit_log(
            owner=None if is_admin else user,
            event=event.strip()[:80],
            before=before,
            limit=limit,
        )
        return {"events": records, "is_admin": is_admin}

    return router
