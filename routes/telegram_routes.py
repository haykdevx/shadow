"""MTProto Telegram user-account routes.

Prefix: /api/telegram
Auth:   every endpoint requires a valid Shadow session (get_current_user).
        If the Shadow user has no active Telethon session, data endpoints
        return HTTP 409 with detail "not_authorized" so the UI can show
        the login screen.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from src.auth_helpers import get_current_user
from src.shadow_telegram_client import (
    TelegramNotAuthorizedError,
    TelegramNotConfiguredError,
    create_chat,
    delete_dialog,
    delete_messages,
    edit_message,
    forward_messages,
    get_avatar,
    get_contacts,
    get_dialogs,
    get_media,
    get_messages,
    get_pinned,
    get_profile,
    get_shared_media,
    get_status,
    get_updates,
    logout,
    mark_read,
    peer_status,
    pin_message,
    react,
    save_draft,
    search_dialogs,
    search_messages,
    send_code,
    send_media,
    send_message,
    set_archived,
    set_mute,
    set_pinned_dialog,
    set_typing,
    set_unread_mark,
    verify_code,
    verify_password,
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SendCodeRequest(BaseModel):
    phone: str


class VerifyCodeRequest(BaseModel):
    code: str


class VerifyPasswordRequest(BaseModel):
    password: str


class SendMessageRequest(BaseModel):
    peer_id: int
    text: str
    reply_to_id: Optional[int] = None


class MarkReadRequest(BaseModel):
    peer_id: int


class ForwardRequest(BaseModel):
    from_peer_id: int
    message_ids: List[int]
    to_peer_id: int


class EditMessageRequest(BaseModel):
    peer_id: int
    message_id: int
    text: str


class DeleteMessagesRequest(BaseModel):
    peer_id: int
    message_ids: List[int]
    revoke: bool = True


class ReactRequest(BaseModel):
    peer_id: int
    message_id: int
    emoji: str = ""


class TypingRequest(BaseModel):
    peer_id: int
    cancel: bool = False


# Wave 2 request models

class MuteRequest(BaseModel):
    peer_id: int
    mute: bool


class PinDialogRequest(BaseModel):
    peer_id: int
    pin: bool


class ArchiveRequest(BaseModel):
    peer_id: int
    archive: bool


class UnreadMarkRequest(BaseModel):
    peer_id: int
    unread: bool


class DeleteDialogRequest(BaseModel):
    peer_id: int
    leave: bool = False
    just_clear: bool = False


class PinMessageRequest(BaseModel):
    peer_id: int
    message_id: int
    pin: bool


class CreateChatRequest(BaseModel):
    kind: str  # "group" | "group_super" | "channel"
    title: str
    about: str = ""
    user_ids: List[int] = []


class DraftRequest(BaseModel):
    peer_id: int
    text: str = ""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _require_user(request: Request) -> str:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _not_configured_error() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail="Telegram API credentials not configured on this server.",
    )


def _not_authorized_error() -> HTTPException:
    return HTTPException(status_code=409, detail="not_authorized")


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def setup_telegram_routes() -> APIRouter:
    router = APIRouter(prefix="/api/telegram", tags=["telegram"])

    # ------------------------------------------------------------------
    # 1. Status
    # ------------------------------------------------------------------

    @router.get("/status")
    async def status(request: Request):
        user = _require_user(request)
        result = await get_status(user)
        return result

    # ------------------------------------------------------------------
    # 2. Login — send code
    # ------------------------------------------------------------------

    @router.post("/login/send-code")
    async def login_send_code(request: Request, body: SendCodeRequest):
        user = _require_user(request)
        phone = (body.phone or "").strip()
        if not phone:
            raise HTTPException(status_code=400, detail="phone is required")
        try:
            await send_code(user, phone)
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # 3. Login — verify code
    # ------------------------------------------------------------------

    @router.post("/login/verify")
    async def login_verify(request: Request, body: VerifyCodeRequest):
        user = _require_user(request)
        code = (body.code or "").strip()
        if not code:
            raise HTTPException(status_code=400, detail="code is required")
        try:
            result = await verify_code(user, code)
            if result.get("needs_password"):
                return {"ok": True, "needs_password": True}
            return {"ok": True, "authorized": True, "me": result.get("me")}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # 4. Login — 2FA password
    # ------------------------------------------------------------------

    @router.post("/login/password")
    async def login_password(request: Request, body: VerifyPasswordRequest):
        user = _require_user(request)
        password = body.password or ""
        if not password:
            raise HTTPException(status_code=400, detail="password is required")
        try:
            result = await verify_password(user, password)
            return {"ok": True, "authorized": True, "me": result.get("me")}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # 5. Logout
    # ------------------------------------------------------------------

    @router.post("/logout")
    async def do_logout(request: Request):
        user = _require_user(request)
        try:
            await logout(user)
        except Exception:
            pass  # Best-effort logout; always return ok
        return {"ok": True}

    # ------------------------------------------------------------------
    # 6. Dialogs
    # ------------------------------------------------------------------

    @router.get("/dialogs")
    async def dialogs(request: Request, limit: int = 50, folder: int = 0):
        user = _require_user(request)
        limit = max(1, min(limit, 200))
        folder = folder if folder in (0, 1) else 0
        try:
            items = await get_dialogs(user, limit=limit, folder=folder)
            return {"dialogs": items}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 7. Messages
    # ------------------------------------------------------------------

    @router.get("/messages/{peer_id}")
    async def messages(
        request: Request,
        peer_id: int,
        limit: int = 50,
        before_id: int = 0,
    ):
        user = _require_user(request)
        limit = max(1, min(limit, 200))
        try:
            msgs, has_more = await get_messages(user, peer_id, limit=limit, before_id=before_id)
            return {"messages": msgs, "has_more": has_more}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 8. Send
    # ------------------------------------------------------------------

    @router.post("/send")
    async def send(request: Request, body: SendMessageRequest):
        user = _require_user(request)
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        try:
            msg = await send_message(
                user,
                body.peer_id,
                text,
                reply_to_id=body.reply_to_id,
            )
            return {"ok": True, "message": msg}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 9. Mark read
    # ------------------------------------------------------------------

    @router.post("/read")
    async def read(request: Request, body: MarkReadRequest):
        user = _require_user(request)
        try:
            await mark_read(user, body.peer_id)
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 10. Updates (event drain)
    # ------------------------------------------------------------------

    @router.get("/updates")
    async def updates(request: Request, cursor: int = 0):
        user = _require_user(request)
        # Verify user is authenticated before returning updates
        status = await get_status(user)
        if not status.get("authorized"):
            if not status.get("configured"):
                raise _not_configured_error()
            raise _not_authorized_error()
        result = get_updates(user, cursor)
        return result

    # ------------------------------------------------------------------
    # 11. Avatar
    # ------------------------------------------------------------------

    @router.get("/avatar/{peer_id}")
    async def avatar(request: Request, peer_id: int):
        user = _require_user(request)
        try:
            data = await get_avatar(user, peer_id)
            if data is None:
                raise HTTPException(status_code=404, detail="No avatar")
            return Response(
                content=data,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except HTTPException:
            raise
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 12. Media
    # ------------------------------------------------------------------

    @router.get("/media/{peer_id}/{message_id}")
    async def media(request: Request, peer_id: int, message_id: int):
        user = _require_user(request)
        try:
            result = await get_media(user, peer_id, message_id)
            if result is None:
                raise HTTPException(status_code=404, detail="Media not found")
            data, content_type = result

            async def _iter():
                yield data

            return StreamingResponse(
                _iter(),
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=3600"},
            )
        except HTTPException:
            raise
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 13. Forward  (Wave 1)
    # ------------------------------------------------------------------

    @router.post("/forward")
    async def forward(request: Request, body: ForwardRequest):
        user = _require_user(request)
        if not body.message_ids:
            raise HTTPException(status_code=400, detail="message_ids is required")
        try:
            await forward_messages(
                user,
                body.from_peer_id,
                body.message_ids,
                body.to_peer_id,
            )
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 14. Edit message  (Wave 1)
    # ------------------------------------------------------------------

    @router.post("/edit")
    async def edit(request: Request, body: EditMessageRequest):
        user = _require_user(request)
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        try:
            msg = await edit_message(user, body.peer_id, body.message_id, text)
            return {"ok": True, "message": msg}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 15. Delete messages  (Wave 1)
    # ------------------------------------------------------------------

    @router.post("/delete")
    async def delete(request: Request, body: DeleteMessagesRequest):
        user = _require_user(request)
        if not body.message_ids:
            raise HTTPException(status_code=400, detail="message_ids is required")
        try:
            await delete_messages(
                user,
                body.peer_id,
                body.message_ids,
                revoke=body.revoke,
            )
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 16. React  (Wave 1)
    # ------------------------------------------------------------------

    @router.post("/react")
    async def do_react(request: Request, body: ReactRequest):
        user = _require_user(request)
        try:
            await react(user, body.peer_id, body.message_id, body.emoji)
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 17. Typing indicator  (Wave 1)
    # ------------------------------------------------------------------

    @router.post("/typing")
    async def typing(request: Request, body: TypingRequest):
        user = _require_user(request)
        try:
            await set_typing(user, body.peer_id, cancel=body.cancel)
        except Exception:
            pass  # best-effort; never fail the client
        return {"ok": True}

    # ------------------------------------------------------------------
    # 18. Send media  (Wave 1)
    # ------------------------------------------------------------------

    @router.post("/send-media")
    async def send_media_route(
        request: Request,
        peer_id: str = Form(...),
        caption: str = Form(""),
        reply_to_id: str = Form(""),
        voice: str = Form("0"),
        files: List[UploadFile] = None,
    ):
        user = _require_user(request)
        if not files:
            raise HTTPException(status_code=400, detail="At least one file is required")
        try:
            pid = int(peer_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="peer_id must be an integer")

        reply_to: Optional[int] = None
        if reply_to_id and reply_to_id.strip():
            try:
                reply_to = int(reply_to_id.strip())
            except ValueError:
                pass

        is_voice = voice.strip() == "1"

        # Read all file bytes
        file_tuples = []
        for f in files:
            data = await f.read()
            fname = f.filename or "upload"
            mime = f.content_type or "application/octet-stream"
            file_tuples.append((fname, data, mime))

        try:
            msg = await send_media(
                user,
                pid,
                file_tuples,
                caption=caption or "",
                reply_to=reply_to,
                voice=is_voice,
            )
            return {"ok": True, "message": msg}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 19. Peer status  (Wave 1)
    # ------------------------------------------------------------------

    @router.get("/peer-status/{peer_id}")
    async def get_peer_status(request: Request, peer_id: int):
        user = _require_user(request)
        try:
            result = await peer_status(user, peer_id)
            return result
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 20. Mute  (Wave 2)
    # ------------------------------------------------------------------

    @router.post("/chat/mute")
    async def chat_mute(request: Request, body: MuteRequest):
        user = _require_user(request)
        try:
            await set_mute(user, body.peer_id, body.mute)
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 21. Pin dialog  (Wave 2)
    # ------------------------------------------------------------------

    @router.post("/chat/pin")
    async def chat_pin(request: Request, body: PinDialogRequest):
        user = _require_user(request)
        try:
            await set_pinned_dialog(user, body.peer_id, body.pin)
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 22. Archive  (Wave 2)
    # ------------------------------------------------------------------

    @router.post("/chat/archive")
    async def chat_archive(request: Request, body: ArchiveRequest):
        user = _require_user(request)
        try:
            await set_archived(user, body.peer_id, body.archive)
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 23. Mark unread  (Wave 2)
    # ------------------------------------------------------------------

    @router.post("/chat/mark-unread")
    async def chat_mark_unread(request: Request, body: UnreadMarkRequest):
        user = _require_user(request)
        try:
            await set_unread_mark(user, body.peer_id, body.unread)
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 24. Delete dialog  (Wave 2)
    # ------------------------------------------------------------------

    @router.post("/chat/delete")
    async def chat_delete(request: Request, body: DeleteDialogRequest):
        user = _require_user(request)
        try:
            await delete_dialog(user, body.peer_id, leave=body.leave, just_clear=body.just_clear)
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 25. Pin message  (Wave 2)
    # ------------------------------------------------------------------

    @router.post("/message/pin")
    async def message_pin(request: Request, body: PinMessageRequest):
        user = _require_user(request)
        try:
            await pin_message(user, body.peer_id, body.message_id, body.pin)
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 26. Get pinned messages  (Wave 2)
    # ------------------------------------------------------------------

    @router.get("/pinned/{peer_id}")
    async def pinned_messages(request: Request, peer_id: int, limit: int = 20):
        user = _require_user(request)
        limit = max(1, min(limit, 100))
        try:
            msgs = await get_pinned(user, peer_id, limit=limit)
            return {"messages": msgs}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 27. Search messages  (Wave 2)
    # ------------------------------------------------------------------

    @router.get("/search")
    async def search(
        request: Request,
        q: str = "",
        peer_id: int = 0,
        limit: int = 30,
    ):
        user = _require_user(request)
        q = (q or "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="q is required")
        limit = max(1, min(limit, 100))
        try:
            results = await search_messages(user, q, peer_id=peer_id, limit=limit)
            return {"results": results}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 28. Search dialogs  (Wave 2)
    # ------------------------------------------------------------------

    @router.get("/search-dialogs")
    async def search_dialogs_route(
        request: Request,
        q: str = "",
        limit: int = 20,
    ):
        user = _require_user(request)
        q = (q or "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="q is required")
        limit = max(1, min(limit, 50))
        try:
            items = await search_dialogs(user, q, limit=limit)
            return {"dialogs": items}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 29. Profile  (Wave 2)
    # ------------------------------------------------------------------

    @router.get("/profile/{peer_id}")
    async def profile(request: Request, peer_id: int):
        user = _require_user(request)
        try:
            result = await get_profile(user, peer_id)
            return result
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 30. Shared media  (Wave 2)
    # ------------------------------------------------------------------

    @router.get("/shared-media/{peer_id}")
    async def shared_media(
        request: Request,
        peer_id: int,
        kind: str = "photo",
        limit: int = 30,
        before_id: int = 0,
    ):
        user = _require_user(request)
        kind = kind if kind in ("photo", "video", "file", "link", "voice") else "photo"
        limit = max(1, min(limit, 100))
        try:
            items, has_more = await get_shared_media(
                user, peer_id, kind=kind, limit=limit, before_id=before_id
            )
            return {"items": items, "has_more": has_more}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 31. Create chat  (Wave 2)
    # ------------------------------------------------------------------

    @router.post("/chat/create")
    async def chat_create(request: Request, body: CreateChatRequest):
        user = _require_user(request)
        kind = (body.kind or "").strip()
        if kind not in ("group", "group_super", "channel"):
            raise HTTPException(status_code=400, detail="kind must be 'group', 'group_super', or 'channel'")
        title = (body.title or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title is required")
        try:
            new_peer_id = await create_chat(
                user,
                kind=kind,
                title=title,
                about=body.about or "",
                user_ids=body.user_ids or [],
            )
            return {"ok": True, "peer_id": new_peer_id}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 32. Contacts  (Wave 2)
    # ------------------------------------------------------------------

    @router.get("/contacts")
    async def contacts(request: Request):
        user = _require_user(request)
        try:
            items = await get_contacts(user)
            return {"contacts": items}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 33. Save draft  (Wave 2)
    # ------------------------------------------------------------------

    @router.post("/draft")
    async def draft(request: Request, body: DraftRequest):
        user = _require_user(request)
        try:
            await save_draft(user, body.peer_id, body.text or "")
            return {"ok": True}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    return router
