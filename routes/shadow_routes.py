"""Shadow HUD and private home-PC control routes."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src.shadow_automation import (
    automation_templates,
    delete_automation,
    evaluate_automations,
    list_automations,
    save_automation,
)
from src.shadow_pc import (
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


def _model_dump(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _require_real_admin(request: Request) -> None:
    """Require an interactive admin cookie for confirmation endpoints."""
    require_admin(request)
    user = getattr(request.state, "current_user", None)
    if getattr(request.state, "api_token", False) or user in {"api", "internal-tool"}:
        raise HTTPException(403, "An interactive admin session must confirm this action")
    if os.getenv("AUTH_ENABLED", "true").lower() != "false" and not user:
        raise HTTPException(403, "An interactive admin session must confirm this action")


def setup_shadow_routes() -> APIRouter:
    router = APIRouter(prefix="/api/shadow", tags=["shadow"])

    @router.get("/overview")
    def shadow_overview(request: Request):
        require_admin(request)
        return overview()

    @router.get("/pc/pending")
    def pc_pending(request: Request):
        require_admin(request)
        return {"pending": list_pending()}

    @router.get("/timeline")
    def shadow_timeline(request: Request, limit: int = 80):
        require_admin(request)
        return {"events": timeline(limit)}

    @router.get("/runbooks")
    def shadow_runbooks(request: Request):
        require_admin(request)
        return {"runbooks": list_runbooks()}

    @router.post("/runbooks")
    def shadow_save_runbook(payload: RunbookRequest, request: Request):
        require_admin(request)
        try:
            return {"runbook": save_runbook(payload.name, payload.label, payload.description, payload.steps)}
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/runbooks/{name}")
    def shadow_delete_runbook(name: str, request: Request):
        require_admin(request)
        try:
            return delete_runbook(name)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/automations")
    def shadow_automations(request: Request):
        require_admin(request)
        return {"automations": list_automations(), "templates": automation_templates()}

    @router.post("/automations")
    def shadow_save_automation(payload: AutomationRequest, request: Request):
        require_admin(request)
        try:
            return {"automation": save_automation(_model_dump(payload))}
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/automations/{automation_id}")
    def shadow_delete_automation(automation_id: str, request: Request):
        require_admin(request)
        try:
            return delete_automation(automation_id)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/automations/evaluate")
    def shadow_evaluate_automations(request: Request, automation_id: str | None = None):
        require_admin(request)
        try:
            return evaluate_automations(automation_id=automation_id, requested_by="web")
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/watchdog")
    def shadow_watchdog(request: Request):
        require_admin(request)
        return watchdog()

    @router.post("/screen/inspect")
    def shadow_screen_inspect(payload: ScreenInspectRequest, request: Request):
        require_admin(request)
        try:
            screenshot = request_action("screenshot", {}, requested_by="web:screen-inspect")
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
        require_admin(request)
        try:
            return request_action(payload.action, payload.args, requested_by="web")
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/pc/confirm/{pending_id}")
    def pc_confirm(pending_id: str, request: Request):
        _require_real_admin(request)
        try:
            return confirm_action(pending_id)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/pc/pending/{pending_id}")
    def pc_cancel(pending_id: str, request: Request):
        _require_real_admin(request)
        try:
            return cancel_action(pending_id)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router

