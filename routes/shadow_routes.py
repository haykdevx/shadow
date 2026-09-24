"""Shadow Command, per-user device enrollment, and Telegram inbox routes."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from src.auth_helpers import require_user
from src.shadow_access import (
    ShadowAccessError,
    access_summary,
    create_discord_pair_code,
    create_telegram_pair_code,
    unlink_discord,
    unlink_telegram,
)
from src.shadow_automation import (
    automation_templates,
    delete_automation,
    evaluate_automations,
    list_automations,
    save_automation,
)
from src.shadow_devices import (
    ShadowDeviceError,
    authenticate_device,
    complete_job,
    create_enrollment,
    enroll_device,
    list_devices,
    poll_job,
    remove_device,
    select_device,
    touch_device,
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
from src.shadow_remote import (
    agent_config as remote_agent_config,
    ShadowRemoteError,
    create_invite as create_remote_invite,
    create_session as create_remote_session,
    list_remote_devices,
    status as remote_status,
)
from src.shadow_telegram_store import list_chats, mark_read, messages, owns_chat, record_message
from src.shadow_discord_store import (
    list_chats as list_discord_chats,
    mark_read as mark_discord_read,
    messages as discord_messages,
    owns_chat as owns_discord_chat,
    record_message as record_discord_message,
)


BASE_DIR = Path(__file__).resolve().parents[1]


class PcActionRequest(BaseModel):
    action: str = Field(..., max_length=60)
    args: dict[str, Any] = Field(default_factory=dict)
    device_id: str | None = Field(default=None, max_length=80)


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
    device_id: str | None = Field(default=None, max_length=80)


class DeviceEnrollRequest(BaseModel):
    code: str = Field(..., min_length=8, max_length=80)
    name: str = Field(default="", max_length=100)
    hostname: str = Field(default="", max_length=100)
    platform: str = Field(default="Unknown", max_length=100)
    agent_version: str = Field(default="", max_length=40)
    capabilities: list[str] = Field(default_factory=list, max_length=100)


class DevicePollRequest(BaseModel):
    timeout: float = Field(default=25, ge=1, le=30)
    name: str = Field(default="", max_length=100)
    platform: str = Field(default="", max_length=100)
    agent_version: str = Field(default="", max_length=40)
    capabilities: list[str] = Field(default_factory=list, max_length=100)


class DeviceResultRequest(BaseModel):
    job_id: str = Field(..., min_length=8, max_length=100)
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=1000)


class TelegramSendRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


class DiscordSendRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1900)


def _model_dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def _real_user(request: Request) -> str:
    user = require_user(request)
    if not user or getattr(request.state, "api_token", False) or user in {"api", "internal-tool"}:
        raise HTTPException(403, "An interactive Shadow account is required")
    return str(user).strip().lower()


def _device_from_request(request: Request) -> dict[str, Any]:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Device credential required")
    try:
        return authenticate_device(auth[7:])
    except ShadowDeviceError as exc:
        raise HTTPException(401, str(exc)) from exc


def _origin(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}".rstrip("/")


def _setup_commands(origin: str, code: str) -> dict[str, str]:
    linux = f"curl -fsSL {origin}/api/shadow/device/install/unix | bash -s -- --server {origin} --code {code}"
    ps_origin = origin.replace("'", "''")
    ps_code = code.replace("'", "''")
    script = (
        f"& curl.exe --fail --show-error --silent --connect-timeout 15 --max-time 180 --retry 3 --retry-delay 2 '{ps_origin}/api/shadow/device/install/windows' -o $env:TEMP\\shadow-install.ps1;"
        "if($LASTEXITCODE -ne 0){throw 'Installer download failed.'};"
        f"& $env:TEMP\\shadow-install.ps1 -Server '{ps_origin}' -Code '{ps_code}'"
    )
    windows = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "{script}"'
    return {"linux": linux, "macos": linux, "windows": windows}


def _telegram_send(chat_id: int, text: str) -> dict[str, Any]:
    token = os.getenv("SHADOW_TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "Telegram bot is not configured")
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=20,
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Telegram send failed: {exc}") from exc
    if not payload.get("ok"):
        raise HTTPException(502, str(payload.get("description") or "Telegram rejected the message"))
    return payload.get("result") or {}


def _discord_send(channel_id: int, text: str) -> dict[str, Any]:
    token = os.getenv("SHADOW_DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "Discord bot is not configured")
    try:
        response = httpx.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": text[:1900]},
            timeout=20,
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Discord send failed: {exc}") from exc
    if response.status_code >= 300:
        raise HTTPException(502, str(payload.get("message") or "Discord rejected the message"))
    return payload or {}


def setup_shadow_routes() -> APIRouter:
    router = APIRouter(prefix="/api/shadow", tags=["shadow"])

    @router.get("/devices")
    def shadow_devices(request: Request):
        user = _real_user(request)
        return {"devices": list_devices(user), "username": user}

    @router.post("/devices/enrollment")
    def shadow_device_enrollment(request: Request):
        user = _real_user(request)
        enrollment = create_enrollment(user)
        enrollment["commands"] = _setup_commands(_origin(request), enrollment["code"])
        return enrollment

    @router.post("/devices/{device_id}/select")
    def shadow_device_select(device_id: str, request: Request):
        try:
            return {"device": select_device(_real_user(request), device_id)}
        except ShadowDeviceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/devices/{device_id}")
    def shadow_device_remove(device_id: str, request: Request):
        try:
            return remove_device(_real_user(request), device_id)
        except ShadowDeviceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/remote/status")
    def shadow_remote_status(request: Request):
        user = _real_user(request)
        payload = remote_status(user)
        if payload["configured"] and payload["account_ready"]:
            try:
                payload["devices"] = list_remote_devices(user)
            except ShadowRemoteError as exc:
                payload["devices"] = []
                payload["warning"] = str(exc)
        else:
            payload["devices"] = []
        return payload

    @router.post("/remote/setup")
    def shadow_remote_setup(request: Request):
        try:
            return create_remote_invite(_real_user(request))
        except ShadowRemoteError as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.post("/remote/session")
    def shadow_remote_session(request: Request):
        try:
            return create_remote_session(_real_user(request))
        except ShadowRemoteError as exc:
            raise HTTPException(503, str(exc)) from exc

    # Backward-compatible read endpoint. It no longer exposes grants or requests.
    @router.get("/access")
    def shadow_access(request: Request):
        user = _real_user(request)
        summary = access_summary(user)
        return {
            "username": user,
            "devices": list_devices(user),
            "telegram_linked": summary.get("telegram_linked", False),
            "discord_linked": summary.get("discord_linked", False),
        }

    @router.post("/device/enroll")
    def shadow_device_enroll(payload: DeviceEnrollRequest):
        try:
            values = _model_dump(payload)
            code = values.pop("code")
            return enroll_device(code, values)
        except ShadowDeviceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/device/poll")
    def shadow_device_poll(payload: DevicePollRequest, request: Request):
        device = _device_from_request(request)
        try:
            touch_device(device["id"], {"name": payload.name, "platform": payload.platform, "agent_version": payload.agent_version, "capabilities": payload.capabilities})
            job = poll_job(device, payload.timeout)
            return {"ok": True, "job": job}
        except ShadowDeviceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/device/result")
    def shadow_device_result(payload: DeviceResultRequest, request: Request):
        device = _device_from_request(request)
        try:
            return complete_job(device, payload.job_id, {"result": payload.result, "error": payload.error})
        except ShadowDeviceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/device/remote-config")
    def shadow_device_remote_config(request: Request):
        """Remote-desktop join details for the calling device's own account.

        Authenticated by the device token issued at enrollment, so the
        installer can finish remote-desktop setup in the same run instead of
        making the user open an invite link on every machine.
        """
        device = _device_from_request(request)
        try:
            config = remote_agent_config(device.get("owner"))
        except ShadowRemoteError as exc:
            # Remote Desktop being off is not an install failure — the agent
            # itself is already enrolled and working.
            return {"ok": False, "configured": False, "error": str(exc)}
        return {"ok": True, "configured": True, **config}

    @router.get("/device/install/{platform_name}", response_class=PlainTextResponse)
    def shadow_device_installer(platform_name: str):
        filename = "install-shadow-device.ps1" if platform_name == "windows" else "install-shadow-device.sh"
        path = BASE_DIR / "scripts" / filename
        if not path.exists():
            raise HTTPException(404, "Installer not found")
        return path.read_text(encoding="utf-8")

    @router.get("/device/source/{filename}", response_class=PlainTextResponse)
    def shadow_device_source(filename: str):
        agent_path = BASE_DIR / "companion" / "shadow-device.ps1"
        if filename == "shadow-device.ps1.gz":
            if not agent_path.exists():
                raise HTTPException(404, "Source file not found")
            payload = gzip.compress(agent_path.read_bytes(), compresslevel=9)
            return Response(
                content=payload,
                media_type="application/gzip",
                headers={"Cache-Control": "no-store"},
            )
        allowed = {
            "relay_agent.py": BASE_DIR / "companion" / "relay_agent.py",
            "home_agent.py": BASE_DIR / "companion" / "home_agent.py",
            "workspace_agent.py": BASE_DIR / "companion" / "workspace_agent.py",
            "shadow-device.ps1": agent_path,
        }
        path = allowed.get(filename)
        if not path or not path.exists():
            raise HTTPException(404, "Source file not found")
        return path.read_text(encoding="utf-8")

    @router.post("/telegram/pair-code")
    def shadow_telegram_pair_code(request: Request):
        try:
            return create_telegram_pair_code(_real_user(request))
        except ShadowAccessError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/telegram/link")
    def shadow_telegram_unlink(request: Request):
        return unlink_telegram(_real_user(request))

    @router.get("/telegram/status")
    def shadow_telegram_status(request: Request):
        user = _real_user(request)
        return {
            "configured": bool(os.getenv("SHADOW_TELEGRAM_BOT_TOKEN", "").strip()),
            "linked": bool(access_summary(user).get("telegram_linked")),
            "username": user,
        }

    @router.get("/telegram/chats")
    def shadow_telegram_chats(request: Request):
        return {"chats": list_chats(_real_user(request))}

    @router.get("/telegram/chats/{chat_id}/messages")
    def shadow_telegram_messages(chat_id: int, request: Request, limit: int = 200):
        user = _real_user(request)
        if not owns_chat(user, chat_id):
            raise HTTPException(404, "Telegram chat not found")
        return {"messages": messages(user, chat_id, limit)}

    @router.post("/telegram/chats/{chat_id}/read")
    def shadow_telegram_read(chat_id: int, request: Request):
        user = _real_user(request)
        if not owns_chat(user, chat_id):
            raise HTTPException(404, "Telegram chat not found")
        return mark_read(user, chat_id)

    @router.post("/telegram/chats/{chat_id}/send")
    def shadow_telegram_send(chat_id: int, payload: TelegramSendRequest, request: Request):
        user = _real_user(request)
        if not owns_chat(user, chat_id):
            raise HTTPException(404, "Telegram chat not found")
        sent = _telegram_send(chat_id, payload.text)
        record_message(user, chat_id, "out", payload.text, telegram_message_id=sent.get("message_id"))
        return {"ok": True, "message": sent}

    @router.post("/discord/pair-code")
    def shadow_discord_pair_code(request: Request):
        try:
            return create_discord_pair_code(_real_user(request))
        except ShadowAccessError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/discord/link")
    def shadow_discord_unlink(request: Request):
        return unlink_discord(_real_user(request))

    @router.get("/discord/status")
    def shadow_discord_status(request: Request):
        user = _real_user(request)
        return {
            "configured": bool(os.getenv("SHADOW_DISCORD_BOT_TOKEN", "").strip()),
            "linked": bool(access_summary(user).get("discord_linked")),
            "username": user,
        }

    @router.get("/discord/chats")
    def shadow_discord_chats(request: Request):
        return {"chats": list_discord_chats(_real_user(request))}

    @router.get("/discord/chats/{channel_id}/messages")
    def shadow_discord_messages(channel_id: int, request: Request, limit: int = 200):
        user = _real_user(request)
        if not owns_discord_chat(user, channel_id):
            raise HTTPException(404, "Discord chat not found")
        return {"messages": discord_messages(user, channel_id, limit)}

    @router.post("/discord/chats/{channel_id}/read")
    def shadow_discord_read(channel_id: int, request: Request):
        user = _real_user(request)
        if not owns_discord_chat(user, channel_id):
            raise HTTPException(404, "Discord chat not found")
        return mark_discord_read(user, channel_id)

    @router.post("/discord/chats/{channel_id}/send")
    def shadow_discord_send(channel_id: int, payload: DiscordSendRequest, request: Request):
        user = _real_user(request)
        if not owns_discord_chat(user, channel_id):
            raise HTTPException(404, "Discord chat not found")
        sent = _discord_send(channel_id, payload.text)
        record_discord_message(user, channel_id, "out", payload.text, discord_message_id=int(sent["id"]) if sent.get("id") else None)
        return {"ok": True, "message": sent}

    @router.get("/overview")
    def shadow_overview(request: Request, device_id: str | None = None):
        return overview(principal=_real_user(request), device_id=device_id)

    @router.get("/pc/pending")
    def pc_pending(request: Request):
        return {"pending": list_pending(principal=_real_user(request))}

    @router.get("/timeline")
    def shadow_timeline(request: Request, limit: int = 80):
        return {"events": timeline(limit, principal=_real_user(request))}

    @router.get("/runbooks")
    def shadow_runbooks(request: Request):
        _real_user(request)
        return {"runbooks": list_runbooks()}

    @router.post("/runbooks")
    def shadow_save_runbook(payload: RunbookRequest, request: Request):
        _real_user(request)
        try:
            return {"runbook": save_runbook(payload.name, payload.label, payload.description, payload.steps)}
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/runbooks/{name}")
    def shadow_delete_runbook(name: str, request: Request):
        _real_user(request)
        try:
            return delete_runbook(name)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/automations")
    def shadow_automations(request: Request):
        _real_user(request)
        return {"automations": list_automations(), "templates": automation_templates()}

    @router.post("/automations")
    def shadow_save_automation(payload: AutomationRequest, request: Request):
        _real_user(request)
        try:
            return {"automation": save_automation(_model_dump(payload))}
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/automations/{automation_id}")
    def shadow_delete_automation(automation_id: str, request: Request):
        _real_user(request)
        try:
            return delete_automation(automation_id)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/automations/evaluate")
    def shadow_evaluate_automations(request: Request, automation_id: str | None = None):
        user = _real_user(request)
        try:
            return evaluate_automations(automation_id=automation_id, requested_by=f"web:{user}", principal=user)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/watchdog")
    def shadow_watchdog(request: Request):
        return watchdog(principal=_real_user(request))

    @router.post("/screen/inspect")
    def shadow_screen_inspect(payload: ScreenInspectRequest, request: Request):
        user = _real_user(request)
        try:
            screenshot = request_action("screenshot", {}, requested_by=f"web:{user}:screen-inspect", principal=user, device_id=payload.device_id)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "ok": True,
            "prompt": payload.prompt,
            "screenshot": screenshot,
            "analysis": "Screen captured. Send it through the configured vision model from chat for semantic analysis.",
            "needs_vision_model": True,
        }

    @router.post("/pc/action")
    def pc_action(payload: PcActionRequest, request: Request):
        user = _real_user(request)
        try:
            return request_action(payload.action, payload.args, requested_by=f"web:{user}", principal=user, device_id=payload.device_id)
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/pc/confirm/{pending_id}")
    def pc_confirm(pending_id: str, request: Request):
        try:
            return confirm_action(pending_id, principal=_real_user(request))
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/pc/pending/{pending_id}")
    def pc_cancel(pending_id: str, request: Request):
        try:
            return cancel_action(pending_id, principal=_real_user(request))
        except ShadowPcError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
