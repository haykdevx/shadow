"""Authenticated HTTP surface for the per-account agent browser.

Every route requires an interactive Shadow session (cookie). API tokens and
the internal agent bridge are rejected on purpose: the approval endpoints are
the human side of the browser confirmation gate, so an agent credential must
never be able to approve its own gated action — the same contract as the
home-PC control routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.auth_helpers import require_user
from src.browser_manager import MANAGER, BrowserConfirmContextError, BrowserError


class BrowserActionRequest(BaseModel):
    action: str = Field(..., max_length=40)
    url: str | None = Field(default=None, max_length=4000)
    selector: str | None = Field(default=None, max_length=500)
    text: str | None = Field(default=None, max_length=500)
    value: str | None = Field(default=None, max_length=4000)
    key: str | None = Field(default=None, max_length=40)
    js: str | None = Field(default=None, max_length=4000)
    full_page: bool = False
    max_chars: int | None = Field(default=None, ge=1, le=20000)
    ms: int | None = Field(default=None, ge=1, le=10000)
    index: int | None = Field(default=None, ge=0, le=50)


class BrowseTaskRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=2000)


def _real_user(request: Request) -> str:
    user = require_user(request)
    if not user or getattr(request.state, "api_token", False) or user in {"api", "internal-tool"}:
        raise HTTPException(403, "An interactive Shadow account is required")
    return str(user).strip().lower()


def setup_browser_routes() -> APIRouter:
    router = APIRouter(prefix="/api/browser", tags=["browser"])

    @router.get("/status")
    async def browser_status(request: Request):
        user = _real_user(request)
        try:
            status = await MANAGER.status(user)
        except BrowserError as exc:
            raise HTTPException(400, str(exc)) from exc
        status["history"] = MANAGER.history(user)[-30:]
        return status

    @router.post("/action")
    async def browser_action(payload: BrowserActionRequest, request: Request):
        user = _real_user(request)
        params: dict[str, Any] = payload.model_dump(exclude_none=True)
        action = params.pop("action", "")
        try:
            return await MANAGER.run_action(user, action, params, requested_by=f"web:{user}")
        except BrowserError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/browse")
    async def browser_browse(payload: BrowseTaskRequest, request: Request):
        user = _real_user(request)
        from src.browser_manager import run_browse_task

        try:
            result = await run_browse_task(user, payload.instruction)
        except BrowserError as exc:
            raise HTTPException(400, str(exc)) from exc
        result.pop("shot_path", None)
        if result.get("shot_id"):
            result["shot_url"] = f"/api/browser/shot/{result['shot_id']}"
        return result

    @router.get("/screenshot")
    async def browser_screenshot(request: Request, full_page: bool = False):
        user = _real_user(request)
        try:
            result = await MANAGER.run_action(user, "screenshot", {"full_page": full_page}, requested_by=f"web:{user}")
            path = MANAGER.shot_path(user, result["shot_id"])
        except BrowserError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(str(path), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @router.get("/shot/{shot_id}")
    def browser_shot(shot_id: str, request: Request):
        user = _real_user(request)
        try:
            path = MANAGER.shot_path(user, shot_id)
        except BrowserError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(str(path), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})

    @router.get("/history")
    def browser_history(request: Request):
        user = _real_user(request)
        return {"history": MANAGER.history(user)}

    @router.get("/pending")
    def browser_pending(request: Request):
        user = _real_user(request)
        return {"pending": MANAGER.list_pending(user)}

    @router.post("/confirm/{pending_id}")
    async def browser_confirm(pending_id: str, request: Request):
        user = _real_user(request)
        try:
            return await MANAGER.confirm(user, pending_id)
        except BrowserConfirmContextError as exc:
            raise HTTPException(409, str(exc)) from exc
        except BrowserError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.delete("/pending/{pending_id}")
    def browser_cancel(pending_id: str, request: Request):
        user = _real_user(request)
        try:
            return MANAGER.cancel(user, pending_id)
        except BrowserError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/close")
    async def browser_close(request: Request):
        user = _real_user(request)
        return await MANAGER.close_owner(user)

    @router.post("/wipe")
    async def browser_wipe(request: Request):
        user = _real_user(request)
        try:
            await MANAGER.close_owner(user)
            return MANAGER.wipe_profile(user)
        except BrowserError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
