"""HTTP surface for Autonomous Missions, Desktop Workspaces, and permissions.

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

from src.auth_helpers import require_user
from src import agent_sessions, mission_engine, mission_workspaces
from src.agent_sessions import AgentSessionError
from src.mission_engine import MissionError
from src.mission_policy import (
    MissionPolicyError,
    arm_full_access,
    disarm_full_access,
    full_access_active,
    list_persistent_rules,
    revoke_persistent_rule,
)
from src.mission_workspaces import (
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


class MissionCreateRequest(BaseModel):
    workspace_id: str = Field(..., min_length=4, max_length=40)
    goal: str = Field(..., min_length=8, max_length=4000)
    mode: str = Field(default="auto", pattern="^(ask|auto|full|unattended)$")
    allow_network: bool = False
    roles: dict[str, dict[str, str]] = Field(default_factory=dict)


class ClarifyRequest(BaseModel):
    answer: str = Field(..., min_length=1, max_length=2000)


class ApprovalRequest(BaseModel):
    decision: str = Field(..., pattern="^(allow_once|allow_mission|allow_always|decline|stop_mission)$")


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
    scope: str = Field(..., pattern="^(once|mission|session|workspace)$")
    workspace_id: str = Field(default="", max_length=40)
    mission_id: str = Field(default="", max_length=40)
    summary: str = Field(default="", max_length=300)


def setup_mission_routes() -> APIRouter:
    router = APIRouter(prefix="/api/missions", tags=["missions"])

    # ── workspaces ──────────────────────────────────────────────────────

    @router.get("/workspaces")
    def workspaces_list(request: Request):
        user = _real_user(request)
        rows = mission_workspaces.list_workspaces(user)
        for row in rows:
            row["full_access"] = bool(full_access_active(user, row.get("device_id") or ""))
        return {"workspaces": rows}

    @router.post("/workspaces")
    def workspaces_create(payload: WorkspaceCreateRequest, request: Request):
        user = _real_user(request)
        from src.shadow_devices import ShadowDeviceError
        try:
            return mission_workspaces.create_workspace(
                user, payload.device_id, payload.root, payload.name, payload.mode)
        except ShadowDeviceError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (WorkspaceError, MissionPolicyError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/workspaces/{workspace_id}")
    def workspaces_remove(workspace_id: str, request: Request):
        try:
            return mission_workspaces.remove_workspace(_real_user(request), workspace_id)
        except WorkspaceError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.put("/workspaces/{workspace_id}/mode")
    def workspaces_mode(workspace_id: str, payload: WorkspaceModeRequest, request: Request):
        user = _real_user(request)
        try:
            workspace = mission_workspaces.get_workspace(user, workspace_id)
            if payload.mode == "full" and not full_access_active(user, workspace["device_id"]):
                raise HTTPException(403, "Arm full access first (password reauthentication required)")
            return mission_workspaces.set_workspace_mode(user, workspace_id, payload.mode)
        except WorkspaceError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/workspaces/{workspace_id}/action")
    async def workspaces_action(workspace_id: str, payload: WorkspaceActionRequest, request: Request):
        """Direct desktop-workspace operation (file tree, read, edit, git…).

        Returns 409 with the approval card payload when the policy engine
        requires explicit approval — the UI then offers allow once / always.
        """
        user = _real_user(request)
        if payload.mode == "full":
            workspace = mission_workspaces.get_workspace(user, workspace_id)
            if not full_access_active(user, workspace["device_id"]):
                raise HTTPException(403, "Full access is not armed for this device")
        try:
            result = await asyncio.to_thread(
                mission_workspaces.dispatch,
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
        except (WorkspaceError, MissionPolicyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — device/relay errors
            raise HTTPException(502, str(exc)[:300]) from exc

    # ── permission policy ──────────────────────────────────────────────

    @router.post("/policy/grants")
    def policy_grant(payload: GrantRequest, request: Request):
        """Record a user-approved grant (allow once / mission / session / always)."""
        user = _real_user(request)
        from src.mission_policy import GRANTS
        try:
            scope_id = payload.mission_id if payload.scope in ("once", "mission") else _session_id(request)
            row = GRANTS.grant(user, payload.scope, scope_id, payload.grant_key,
                               workspace_id=payload.workspace_id, summary=payload.summary)
            return {"ok": True, "grant": row}
        except MissionPolicyError as exc:
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
        try:
            return arm_full_access(user, payload.device_id, password=payload.password,
                                   duration_seconds=payload.duration_seconds)
        except MissionPolicyError as exc:
            raise HTTPException(403, str(exc)) from exc

    @router.delete("/policy/full-access/{device_id}")
    def policy_full_access_off(device_id: str, request: Request):
        disarm_full_access(_real_user(request), device_id)
        return {"ok": True}

    @router.get("/policy/full-access/{device_id}")
    def policy_full_access_status(device_id: str, request: Request):
        row = full_access_active(_real_user(request), device_id)
        return {"armed": bool(row), "expires_at": (row or {}).get("expires_at")}

    # ── agent sessions (direct tool loop, no planner DAG) ─────────────
    # Defined before the /{mission_id} routes so "/sessions" never matches
    # the mission-id path parameter.

    @router.get("/sessions")
    def sessions_list(request: Request):
        return {"sessions": agent_sessions.list_sessions(_real_user(request))}

    @router.post("/sessions")
    async def sessions_create(payload: SessionCreateRequest, request: Request):
        user = _real_user(request)
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
        try:
            return agent_sessions.load_session(_real_user(request), sid)
        except AgentSessionError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/sessions/{sid}/message")
    async def session_message(sid: str, payload: SessionMessageRequest, request: Request):
        user = _real_user(request)
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
        try:
            return agent_sessions.retry_session(_real_user(request), sid)
        except AgentSessionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/sessions/{sid}/retry")
    async def session_retry(sid: str, payload: SessionRetryRequest, request: Request):
        try:
            return agent_sessions.retry_session(
                _real_user(request), sid,
                endpoint_id=payload.endpoint_id, model=payload.model)
        except AgentSessionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/sessions/{sid}/rollback")
    async def session_rollback(sid: str, payload: RollbackRequest, request: Request):
        try:
            return await agent_sessions.rollback_session(
                _real_user(request), sid, paths=payload.paths)
        except AgentSessionError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (WorkspaceDenied, WorkspaceError) as exc:
            raise HTTPException(403, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, str(exc)[:300]) from exc

    @router.post("/sessions/{sid}/approvals/{approval_id}")
    async def session_approval(sid: str, approval_id: str,
                               payload: SessionApprovalRequest, request: Request):
        try:
            return agent_sessions.resolve_approval(
                _real_user(request), sid, approval_id, payload.decision)
        except AgentSessionError as exc:
            raise HTTPException(404, str(exc)) from exc
        except MissionPolicyError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ── missions ────────────────────────────────────────────────────────

    @router.get("")
    def missions_list(request: Request):
        return {"missions": mission_engine.list_missions(_real_user(request))}

    @router.get("/models")
    def missions_models(request: Request):
        return {"available": mission_engine.available_role_targets(_real_user(request)),
                "roles": list(mission_engine.ROLES)}

    @router.post("")
    async def missions_create(payload: MissionCreateRequest, request: Request):
        user = _real_user(request)
        try:
            mission = mission_engine.create_mission(
                user, payload.workspace_id, payload.goal,
                mode=payload.mode, roles=payload.roles,
                allow_network=payload.allow_network,
            )
            await mission_engine.plan_mission(mission)
            return mission_engine.load_mission(user, mission["id"])
        except (MissionError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/{mission_id}")
    def mission_detail(mission_id: str, request: Request):
        try:
            return mission_engine.load_mission(_real_user(request), mission_id)
        except MissionError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/{mission_id}/clarify")
    async def mission_clarify(mission_id: str, payload: ClarifyRequest, request: Request):
        user = _real_user(request)
        try:
            mission = mission_engine.load_mission(user, mission_id)
            if mission.get("status") != "clarifying":
                raise MissionError("Mission is not waiting for a clarification")
            await mission_engine.plan_mission(mission, answer=payload.answer)
            return mission_engine.load_mission(user, mission_id)
        except MissionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/{mission_id}/start")
    async def mission_start(mission_id: str, request: Request):
        try:
            return mission_engine.start_mission(_real_user(request), mission_id)
        except MissionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/{mission_id}/pause")
    async def mission_pause(mission_id: str, request: Request):
        try:
            return mission_engine.pause_mission(_real_user(request), mission_id)
        except MissionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/{mission_id}/resume")
    async def mission_resume(mission_id: str, request: Request):
        try:
            return mission_engine.resume_mission(_real_user(request), mission_id)
        except MissionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/{mission_id}/stop")
    async def mission_stop(mission_id: str, request: Request):
        try:
            return mission_engine.stop_mission(_real_user(request), mission_id)
        except MissionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/{mission_id}/rollback")
    async def mission_rollback(mission_id: str, payload: RollbackRequest, request: Request):
        try:
            return await mission_engine.rollback_mission(
                _real_user(request), mission_id, paths=payload.paths)
        except MissionError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (WorkspaceDenied, WorkspaceError) as exc:
            raise HTTPException(403, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, str(exc)[:300]) from exc

    @router.post("/{mission_id}/approvals/{approval_id}")
    async def mission_approval(mission_id: str, approval_id: str, payload: ApprovalRequest, request: Request):
        try:
            return mission_engine.resolve_approval(
                _real_user(request), mission_id, approval_id, payload.decision,
                session_id=_session_id(request))
        except MissionError as exc:
            raise HTTPException(404, str(exc)) from exc
        except MissionPolicyError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
