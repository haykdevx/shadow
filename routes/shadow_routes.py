"""Shadow HUD and private home-PC control routes."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src.shadow_pc import ShadowPcError, cancel_action, confirm_action, list_pending, overview, request_action


class PcActionRequest(BaseModel):
    action: str = Field(..., max_length=60)
    args: dict[str, Any] = Field(default_factory=dict)


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

