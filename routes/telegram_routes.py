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
    add_members,
    block_user,
    create_chat,
    create_invite,
    delete_dialog,
    delete_folder,
    delete_messages,
    delete_profile_photo,
    delete_scheduled,
    edit_message,
    forward_messages,
    get_2fa_status,
    get_avatar,
    get_blocked,
    get_chat_permissions,
    get_contacts,
    get_dialogs,
    get_folders,
    get_gifs,
    get_invites,
    get_me_full,
    get_media,
    get_members,
    get_messages,
    get_pinned,
    get_privacy,
    get_profile,
    get_reactions_list,
    get_read_by,
    get_scheduled,
    get_sessions,
    get_shared_media,
    get_status,
    get_sticker_bytes,
    get_stickers,
    get_updates,
    join_target,
    leave_target,
    logout,
    mark_read,
    peer_status,
    pin_message,
    promote_member,
    react,
    remove_member,
    reset_other_sessions,
    reset_session,
    resolve_target,
    restrict_member,
    revoke_invite,
    save_draft,
    save_folder,
    search_dialogs,
    search_messages,
    send_cached_doc,
    send_code,
    send_media,
    send_message,
    send_poll,
    send_scheduled,
    set_2fa,
    set_archived,
    set_chat_permissions,
    set_chat_ttl,
    set_mute,
    set_pinned_dialog,
    set_privacy,
    set_profile_photo,
    set_typing,
    set_unread_mark,
    unblock_user,
    update_profile,
    update_username,
    verify_code,
    verify_password,
    vote_poll,
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
    schedule_date: Optional[int] = None
    quote: Optional[str] = None


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


# Wave 3 request models

# Group 1 — Rich content & compose

class SendStickerRequest(BaseModel):
    peer_id: int
    doc_id: str
    reply_to_id: Optional[int] = None


class SendGifRequest(BaseModel):
    peer_id: int
    doc_id: str
    reply_to_id: Optional[int] = None


class PollRequest(BaseModel):
    peer_id: int
    question: str
    options: List[str]
    multiple: bool = False
    quiz: bool = False
    correct: Optional[int] = None
    public: bool = False


class PollVoteRequest(BaseModel):
    peer_id: int
    message_id: int
    options: List[int]


# Group 2 — Settings & account

class ProfileUpdateRequest(BaseModel):
    first: Optional[str] = None
    last: Optional[str] = None
    bio: Optional[str] = None


class UsernameRequest(BaseModel):
    username: str


class PrivacyRequest(BaseModel):
    key: str
    value: str


class BlockRequest(BaseModel):
    peer_id: int


class TwoFaRequest(BaseModel):
    password: str
    hint: Optional[str] = None
    email: Optional[str] = None
    current: Optional[str] = None


class FolderRequest(BaseModel):
    id: Optional[int] = None
    title: str
    peer_ids: List[int] = []


# Group 3 — Group & channel admin

class MembersAddRequest(BaseModel):
    peer_id: int
    user_ids: List[int]


class MemberRemoveRequest(BaseModel):
    peer_id: int
    user_id: int


class MemberPromoteRequest(BaseModel):
    peer_id: int
    user_id: int
    admin: bool
    rank: Optional[str] = None


class MemberRestrictRequest(BaseModel):
    peer_id: int
    user_id: int
    banned: bool
    until: Optional[int] = None


class ChatPermissionsRequest(BaseModel):
    peer_id: int
    rights: dict


class InviteCreateRequest(BaseModel):
    peer_id: int
    expire: Optional[int] = None
    usage_limit: Optional[int] = None


class InviteRevokeRequest(BaseModel):
    peer_id: int
    link: str


class JoinRequest(BaseModel):
    target: str


class LeaveRequest(BaseModel):
    peer_id: int


# Group 4 — Power messaging

class ScheduledSendRequest(BaseModel):
    peer_id: int
    message_ids: List[int]


class ScheduledDeleteRequest(BaseModel):
    peer_id: int
    message_ids: List[int]


class ChatTtlRequest(BaseModel):
    peer_id: int
    seconds: int


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
    async def dialogs(request: Request, limit: int = 50, folder: int = 0, folder_id: int = 0):
        user = _require_user(request)
        limit = max(1, min(limit, 200))
        folder = folder if folder in (0, 1) else 0
        try:
            items = await get_dialogs(user, limit=limit, folder=folder, folder_id=folder_id)
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
        around_id: int = 0,
    ):
        user = _require_user(request)
        limit = max(1, min(limit, 200))
        try:
            msgs, has_more = await get_messages(
                user, peer_id, limit=limit, before_id=before_id, around_id=around_id
            )
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
                schedule_date=body.schedule_date,
                quote=body.quote,
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
        schedule_date: str = Form(""),
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

        sched: Optional[int] = None
        if schedule_date and schedule_date.strip():
            try:
                sched = int(schedule_date.strip())
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
                schedule_date=sched,
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

    # ==================================================================
    # Wave 3 — Group 1: Rich content & compose
    # ==================================================================

    # ------------------------------------------------------------------
    # 34. Stickers
    # ------------------------------------------------------------------

    @router.get("/stickers")
    async def stickers(request: Request):
        user = _require_user(request)
        try:
            return await get_stickers(user)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 35. Serve a sticker/gif document
    # ------------------------------------------------------------------

    @router.get("/sticker/{doc_id}")
    async def sticker(request: Request, doc_id: str):
        user = _require_user(request)
        try:
            result = await get_sticker_bytes(user, doc_id)
            if result is None:
                raise HTTPException(status_code=404, detail="Sticker not found")
            data, content_type = result
            return Response(
                content=data,
                media_type=content_type,
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
    # 36. GIFs
    # ------------------------------------------------------------------

    @router.get("/gifs")
    async def gifs(request: Request):
        user = _require_user(request)
        try:
            return await get_gifs(user)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 37. Send sticker
    # ------------------------------------------------------------------

    @router.post("/send-sticker")
    async def send_sticker(request: Request, body: SendStickerRequest):
        user = _require_user(request)
        try:
            msg = await send_cached_doc(user, body.peer_id, body.doc_id, reply_to_id=body.reply_to_id)
            return {"ok": True, "message": msg}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 38. Send gif
    # ------------------------------------------------------------------

    @router.post("/send-gif")
    async def send_gif(request: Request, body: SendGifRequest):
        user = _require_user(request)
        try:
            msg = await send_cached_doc(user, body.peer_id, body.doc_id, reply_to_id=body.reply_to_id)
            return {"ok": True, "message": msg}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 39. Create poll
    # ------------------------------------------------------------------

    @router.post("/poll")
    async def poll(request: Request, body: PollRequest):
        user = _require_user(request)
        question = (body.question or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question is required")
        options = [o for o in (body.options or []) if (o or "").strip()]
        if len(options) < 2:
            raise HTTPException(status_code=400, detail="at least two options are required")
        try:
            msg = await send_poll(
                user,
                body.peer_id,
                question,
                options,
                multiple=body.multiple,
                quiz=body.quiz,
                correct=body.correct,
                public=body.public,
            )
            return {"ok": True, "message": msg}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 40. Vote in a poll
    # ------------------------------------------------------------------

    @router.post("/poll/vote")
    async def poll_vote(request: Request, body: PollVoteRequest):
        user = _require_user(request)
        try:
            msg = await vote_poll(user, body.peer_id, body.message_id, body.options)
            return {"ok": True, "message": msg}
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ==================================================================
    # Wave 3 — Group 2: Settings & account
    # ==================================================================

    # ------------------------------------------------------------------
    # 41. Me (full profile)
    # ------------------------------------------------------------------

    @router.get("/me")
    async def me(request: Request):
        user = _require_user(request)
        try:
            return await get_me_full(user)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 42. Update profile
    # ------------------------------------------------------------------

    @router.post("/profile/update")
    async def profile_update(request: Request, body: ProfileUpdateRequest):
        user = _require_user(request)
        try:
            return await update_profile(user, first=body.first, last=body.last, bio=body.bio)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 43. Update username
    # ------------------------------------------------------------------

    @router.post("/profile/username")
    async def profile_username(request: Request, body: UsernameRequest):
        user = _require_user(request)
        try:
            return await update_username(user, body.username or "")
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 44. Set / delete profile photo
    # ------------------------------------------------------------------

    @router.post("/profile/photo")
    async def profile_photo_set(request: Request, file: UploadFile = None):
        user = _require_user(request)
        if file is None:
            raise HTTPException(status_code=400, detail="file is required")
        data = await file.read()
        fname = file.filename or "photo.jpg"
        try:
            return await set_profile_photo(user, fname, data)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.delete("/profile/photo")
    async def profile_photo_delete(request: Request):
        user = _require_user(request)
        try:
            return await delete_profile_photo(user)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 45. Privacy
    # ------------------------------------------------------------------

    @router.get("/privacy")
    async def privacy_get(request: Request):
        user = _require_user(request)
        try:
            return await get_privacy(user)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/privacy")
    async def privacy_set(request: Request, body: PrivacyRequest):
        user = _require_user(request)
        try:
            return await set_privacy(user, body.key, body.value)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 46. Sessions
    # ------------------------------------------------------------------

    @router.get("/sessions")
    async def sessions_get(request: Request):
        user = _require_user(request)
        try:
            return await get_sessions(user)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.delete("/sessions/{hash}")
    async def sessions_delete(request: Request, hash: str):
        user = _require_user(request)
        try:
            return await reset_session(user, hash)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/sessions/reset-others")
    async def sessions_reset_others(request: Request):
        user = _require_user(request)
        try:
            return await reset_other_sessions(user)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 47. Blocked users
    # ------------------------------------------------------------------

    @router.get("/blocked")
    async def blocked_get(request: Request):
        user = _require_user(request)
        try:
            return await get_blocked(user)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/block")
    async def block(request: Request, body: BlockRequest):
        user = _require_user(request)
        try:
            return await block_user(user, body.peer_id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/unblock")
    async def unblock(request: Request, body: BlockRequest):
        user = _require_user(request)
        try:
            return await unblock_user(user, body.peer_id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 48. Two-step verification (2FA)
    # ------------------------------------------------------------------

    @router.get("/2fa/status")
    async def twofa_status(request: Request):
        user = _require_user(request)
        try:
            return await get_2fa_status(user)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/2fa/set")
    async def twofa_set(request: Request, body: TwoFaRequest):
        user = _require_user(request)
        if not body.password:
            raise HTTPException(status_code=400, detail="password is required")
        try:
            return await set_2fa(
                user,
                body.password,
                hint=body.hint,
                email=body.email,
                current=body.current,
            )
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 49. Chat folders
    # ------------------------------------------------------------------

    @router.get("/folders")
    async def folders_get(request: Request):
        user = _require_user(request)
        try:
            return await get_folders(user)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/folders")
    async def folders_save(request: Request, body: FolderRequest):
        user = _require_user(request)
        title = (body.title or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title is required")
        try:
            return await save_folder(user, title, body.peer_ids or [], folder_id=body.id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.delete("/folders/{folder_id}")
    async def folders_delete(request: Request, folder_id: int):
        user = _require_user(request)
        try:
            return await delete_folder(user, folder_id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ==================================================================
    # Wave 3 — Group 3: Group & channel admin
    # ==================================================================

    # ------------------------------------------------------------------
    # 50. Members list
    # ------------------------------------------------------------------

    @router.get("/members/{peer_id}")
    async def members(request: Request, peer_id: int, limit: int = 100, offset: int = 0, q: str = ""):
        user = _require_user(request)
        limit = max(1, min(limit, 200))
        try:
            return await get_members(user, peer_id, limit=limit, offset=offset, q=(q or "").strip())
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 51. Add members
    # ------------------------------------------------------------------

    @router.post("/members/add")
    async def members_add(request: Request, body: MembersAddRequest):
        user = _require_user(request)
        if not body.user_ids:
            raise HTTPException(status_code=400, detail="user_ids is required")
        try:
            return await add_members(user, body.peer_id, body.user_ids)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 52. Remove member
    # ------------------------------------------------------------------

    @router.post("/members/remove")
    async def members_remove(request: Request, body: MemberRemoveRequest):
        user = _require_user(request)
        try:
            return await remove_member(user, body.peer_id, body.user_id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 53. Promote member
    # ------------------------------------------------------------------

    @router.post("/members/promote")
    async def members_promote(request: Request, body: MemberPromoteRequest):
        user = _require_user(request)
        try:
            return await promote_member(
                user, body.peer_id, body.user_id, body.admin, rank=body.rank or ""
            )
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 54. Restrict member
    # ------------------------------------------------------------------

    @router.post("/members/restrict")
    async def members_restrict(request: Request, body: MemberRestrictRequest):
        user = _require_user(request)
        try:
            return await restrict_member(
                user, body.peer_id, body.user_id, body.banned, until=body.until
            )
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 55. Chat permissions
    # ------------------------------------------------------------------

    @router.get("/chat/permissions/{peer_id}")
    async def chat_permissions_get(request: Request, peer_id: int):
        user = _require_user(request)
        try:
            return await get_chat_permissions(user, peer_id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/chat/permissions")
    async def chat_permissions_set(request: Request, body: ChatPermissionsRequest):
        user = _require_user(request)
        try:
            return await set_chat_permissions(user, body.peer_id, body.rights or {})
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 56. Invite links
    # ------------------------------------------------------------------

    @router.get("/invites/{peer_id}")
    async def invites_get(request: Request, peer_id: int):
        user = _require_user(request)
        try:
            return await get_invites(user, peer_id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/invites")
    async def invites_create(request: Request, body: InviteCreateRequest):
        user = _require_user(request)
        try:
            return await create_invite(
                user, body.peer_id, expire=body.expire, usage_limit=body.usage_limit
            )
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/invites/revoke")
    async def invites_revoke(request: Request, body: InviteRevokeRequest):
        user = _require_user(request)
        try:
            return await revoke_invite(user, body.peer_id, body.link)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 57. Resolve / join / leave
    # ------------------------------------------------------------------

    @router.get("/resolve/{username_target}")
    async def resolve(request: Request, username_target: str):
        user = _require_user(request)
        try:
            return await resolve_target(user, username_target)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.post("/join")
    async def join(request: Request, body: JoinRequest):
        user = _require_user(request)
        target = (body.target or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="target is required")
        try:
            return await join_target(user, target)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/leave")
    async def leave(request: Request, body: LeaveRequest):
        user = _require_user(request)
        try:
            return await leave_target(user, body.peer_id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ==================================================================
    # Wave 3 — Group 4: Power messaging
    # ==================================================================

    # ------------------------------------------------------------------
    # 58. Scheduled messages
    # ------------------------------------------------------------------

    @router.get("/scheduled/{peer_id}")
    async def scheduled_get(request: Request, peer_id: int):
        user = _require_user(request)
        try:
            return await get_scheduled(user, peer_id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.post("/scheduled/send")
    async def scheduled_send(request: Request, body: ScheduledSendRequest):
        user = _require_user(request)
        if not body.message_ids:
            raise HTTPException(status_code=400, detail="message_ids is required")
        try:
            return await send_scheduled(user, body.peer_id, body.message_ids)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @router.delete("/scheduled")
    async def scheduled_delete(request: Request, body: ScheduledDeleteRequest):
        user = _require_user(request)
        if not body.message_ids:
            raise HTTPException(status_code=400, detail="message_ids is required")
        try:
            return await delete_scheduled(user, body.peer_id, body.message_ids)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 59. Reactions list
    # ------------------------------------------------------------------

    @router.get("/reactions/{peer_id}/{message_id}")
    async def reactions(request: Request, peer_id: int, message_id: int):
        user = _require_user(request)
        try:
            return await get_reactions_list(user, peer_id, message_id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 60. Read-by
    # ------------------------------------------------------------------

    @router.get("/read-by/{peer_id}/{message_id}")
    async def read_by(request: Request, peer_id: int, message_id: int):
        user = _require_user(request)
        try:
            return await get_read_by(user, peer_id, message_id)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # ------------------------------------------------------------------
    # 61. Chat TTL (auto-delete)
    # ------------------------------------------------------------------

    @router.post("/chat/ttl")
    async def chat_ttl(request: Request, body: ChatTtlRequest):
        user = _require_user(request)
        try:
            return await set_chat_ttl(user, body.peer_id, body.seconds)
        except TelegramNotConfiguredError:
            raise _not_configured_error()
        except TelegramNotAuthorizedError:
            raise _not_authorized_error()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    return router
