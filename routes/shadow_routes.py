"""Shadow HUD and account-scoped private home-PC control routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.auth_helpers import require_user
from src.shadow_access import (
    ShadowAccessError,
    access_summary,
    claim_owner,
    create_telegram_pair_code,
    grant_access,
    owner_username,
    request_access,
    require_permission,
    revoke_access,
    unlink_telegram,
)
from src.shadow_automation import (
    automation_templates,
    delete_automation,
    evaluate_automations,
    list_automations,
    save_automation,
)
from src.shadow_pc import (
    READ_ACTIONS,
    ShadowPcError,
    cancel_action,
    confirm_action,
    delete_runbook,
    list_pending,
    list_runbooks,
    overview,
    request_action,
    save_runbook,
    timeline,
    watchdog,
)


class PcActionRequest(BaseModel):
    action: str = Field(..., max_length=60)
    args: dict[str, Any] = Field(default_factory=dict)


class RunbookRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    label: str = Field(..., min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    steps: list[dict[str, Any]] = Field(..., min_length=1, max_length=16)


class AutomationRequest(BaseModel):
    id: str | None = Field(default=None, max_length=80)
    name: str = Field(..., min_length=1, max_length=100)
    enabled: bool = True
    trigger: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    cooldown_seconds: int = Field(default=300, ge=0, le=86400)


class ScreenInspectRequest(BaseModel):
    prompt: str = Field(default="Describe the current screen and point out anything important.", max_length=1000)


class AccessRequest(BaseModel):
    permissions: list[str] = Field(default_factory=lambda: ["view"], max_length=3)


class AccessGrant(BaseModel):
    permissions: list[str] = Field(..., min_length=1, max_length=3)


def _model_dump(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _real_user(request: Request) -> str:
    """Require a named browser account; PC control never accepts bearer/tool identities."""
    user = require_user(request)
    if (
        not user
        or getattr(request.state, "api_token", False)
        or user in {"api", "internal-tool"}
    ):
        raise HTTPException(403, "An interactive Shadow account is required for linked-PC access")
    return str(user).strip().lower()


def _pc_user(request: Request, permission: str) -> str:
    user = _real_user(request)
    try:
        return require_permission(user, permission)
    except ShadowAccessError as exc:
        raise HTTPException(403, str(exc)) from exc


def _owner_user(request: Request) -> str:
    user = _real_user(request)
    if not owner_username() or user != owner_username():
        raise HTTPException(403, "Only the linked-PC owner can change this setting")
    return user


def _access_error(exc: ShadowAccessError) -> HTTPException:
    return HTTPException(400, str(exc))


def setup_shadow_routes() -> APIRouter:
    router = APIRouter(prefix="/api/shadow", tags=["shadow"])

    @router.get("/access")
    def shadow_access(request: Request):
        return access_summary(_real_user(request))

    @router.post("/access/request")
    def shadow_request_access(payload: AccessRequest, request: Request):
        try:
            return {"request": request_access(_real_user(request), payload.permissions)}
        except ShadowAccessError as exc:
            raise _access_error(exc) from exc

    @router.post("/access/claim")
    def shadow_claim_owner(request: Request):
        user = _real_user(request)
        auth_manager = getattr(request.app.state, "auth_manager", None)
        if not auth_manager or not auth_manager.is_admin(user):
            raise HTTPException(403, "Only an administrator can claim an unconfigured linked PC")
        try:
            return claim_owner(user)
        except ShadowAccessError as exc:
            raise _access_error(exc) from exc

    @router.post("/access/grants/{username}")
    def shadow_grant_access(username: str, payload: AccessGrant, request: Request):
        try:
            return {"grant": grant_access(_owner_user(request), username, payload.permissions)}
        except ShadowAccessError as exc:
            raise _access_error(exc) from exc

    @router.delete("/access/grants/{username}")
    def shadow_revoke_access(username: str, request: Request):
        try:
            return revoke_access(_owner_user(request), username)
        except ShadowAccessError as exc:
            raise _access_error(exc) from exc

    @router.post("/telegram/pair-code")
    def shadow_telegram_pair_code(request: Request):
        try:
            return create_telegram_pair_code(_pc_user(request, "view"))
        except ShadowAccessError as exc:
            raise _access_error(exc) from exc

    @router.delete("/telegram/link")
    def shadow_telegram_unlink(request: Request):
        return unlink_telegram(_real_user(request))

    @router.get("/overview")
    def shadow_overview(request: Request):
        user = _pc_user(request, "view")
        return overview(principal=user)

    @router.get("/pc/pending")
    def pc_pending(request: Request):
        user = _pc_user(request, "approve")
        return {"pending": list_pending(principal=user)}

    @router.get("/timeline")
    def shadow_timeline(request: Request, limit: int = 80):
        user = _pc_user(request, "view")
        return {"events": timeline(limit, principal=user)}

    @router.get("/runbooks")
    def shadow_runbooks(request: Request):
        _pc_user(request, "view")
        return {"runbooks": list_runbooks()}

    @router.post("/runbooks")
    def shadow_save_runbook(payload: RunbookRequest, request: Request):
        _owner_user(request)
        try:
            return {"runbook": save_runbook(payload.name, payload.label, payload.description, payload.steps)}
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/runbooks/{name}")
    def shadow_delete_runbook(name: str, request: Request):
        _owner_user(request)
        try:
            return delete_runbook(name)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/automations")
    def shadow_automations(request: Request):
        _pc_user(request, "view")
        return {"automations": list_automations(), "templates": automation_templates()}

    @router.post("/automations")
    def shadow_save_automation(payload: AutomationRequest, request: Request):
        _owner_user(request)
        try:
            return {"automation": save_automation(_model_dump(payload))}
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/automations/{automation_id}")
    def shadow_delete_automation(automation_id: str, request: Request):
        _owner_user(request)
        try:
            return delete_automation(automation_id)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/automations/evaluate")
    def shadow_evaluate_automations(request: Request, automation_id: str | None = None):
        user = _pc_user(request, "control")
        try:
            return evaluate_automations(
                automation_id=automation_id,
                requested_by=f"web:{user}",
                principal=user,
            )
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/watchdog")
    def shadow_watchdog(request: Request):
        user = _pc_user(request, "view")
        return watchdog(principal=user)

    @router.post("/screen/inspect")
    def shadow_screen_inspect(payload: ScreenInspectRequest, request: Request):
        user = _pc_user(request, "view")
        try:
            screenshot = request_action(
                "screenshot",
                {},
                requested_by=f"web:{user}:screen-inspect",
                principal=user,
            )
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "ok": True,
            "prompt": payload.prompt,
            "screenshot": screenshot,
            "analysis": (
                "Screen captured. Vision-model analysis is intentionally routed through the existing "
                "chat/model stack next; this endpoint returns the deterministic capture payload now."
            ),
            "needs_vision_model": True,
        }

    @router.post("/pc/action")
    def pc_action(payload: PcActionRequest, request: Request):
        action = payload.action.strip().lower()
        user = _pc_user(request, "view" if action in READ_ACTIONS else "control")
        try:
            return request_action(
                action,
                payload.args,
                requested_by=f"web:{user}",
                principal=user,
            )
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/pc/confirm/{pending_id}")
    def pc_confirm(pending_id: str, request: Request):
        user = _pc_user(request, "approve")
        try:
            return confirm_action(pending_id, principal=user)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/pc/pending/{pending_id}")
    def pc_cancel(pending_id: str, request: Request):
        user = _pc_user(request, "approve")
        try:
            return cancel_action(pending_id, principal=user)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
