"""MTProto Telegram user-account client manager (Telethon).

One ``TelegramClient`` is kept alive per Shadow user and cached in
``_clients``.  The client connects lazily on first use and reconnects
automatically from the saved ``.session`` file, so authenticated sessions
survive server restarts.

All public coroutines raise ``TelegramNotConfiguredError`` when the operator
has not supplied API credentials, and ``TelegramNotAuthorizedError`` when the
Shadow user has not completed login yet.  Route handlers catch those and map
them to the correct HTTP status codes (400 / 409).
"""
from __future__ import annotations

import asyncio
import collections
import html
import io
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_api_id() -> Optional[int]:
    raw = os.environ.get("TELEGRAM_API_ID", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _get_api_hash() -> Optional[str]:
    val = os.environ.get("TELEGRAM_API_HASH", "").strip()
    return val or None


def is_configured() -> bool:
    return _get_api_id() is not None and _get_api_hash() is not None


def _sessions_dir() -> Path:
    p = Path(os.environ.get("TELEGRAM_SESSIONS_PATH", "data/telegram-sessions"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_dir() -> Path:
    p = Path(os.environ.get("TELEGRAM_CACHE_PATH", "data/telegram-cache"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(username: str) -> str:
    """Safe lowercase alnum+dash slug from a Shadow username."""
    return re.sub(r"[^a-z0-9-]", "-", username.lower()).strip("-") or "user"


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class TelegramNotConfiguredError(Exception):
    """Operator has not set TELEGRAM_API_ID / TELEGRAM_API_HASH."""


class TelegramNotAuthorizedError(Exception):
    """This Shadow user has not logged into Telegram yet."""


# ---------------------------------------------------------------------------
# Per-user state
# ---------------------------------------------------------------------------

# user_slug -> TelegramClient (connected)
_clients: Dict[str, Any] = {}

# user_slug -> {"phone": str, "phone_code_hash": str}
_pending_logins: Dict[str, Dict[str, str]] = {}

# user_slug -> deque of {"cursor": int, "type": str, ...event fields...}
_event_queues: Dict[str, collections.deque] = {}

# Monotonic cursor per user
_event_cursors: Dict[str, int] = {}


def _next_cursor(slug: str) -> int:
    _event_cursors[slug] = _event_cursors.get(slug, 0) + 1
    return _event_cursors[slug]


def _push_event(slug: str, event: Dict[str, Any]) -> None:
    if slug not in _event_queues:
        _event_queues[slug] = collections.deque(maxlen=500)
    cursor = _next_cursor(slug)
    event["cursor"] = cursor
    _event_queues[slug].append(event)


# ---------------------------------------------------------------------------
# Client factory / lifecycle
# ---------------------------------------------------------------------------

def _make_client(slug: str) -> Any:
    """Create (but do NOT connect) a TelegramClient for *slug*."""
    from telethon import TelegramClient  # type: ignore

    api_id = _get_api_id()
    api_hash = _get_api_hash()
    if api_id is None or api_hash is None:
        raise TelegramNotConfiguredError("TELEGRAM_API_ID / TELEGRAM_API_HASH not set")

    session_path = str(_sessions_dir() / f"{slug}.session")
    client = TelegramClient(session_path, api_id, api_hash)
    return client


def _register_handlers(client: Any, slug: str) -> None:
    """Register Telethon update handlers that push events into the user's queue."""
    from telethon import events  # type: ignore

    @client.on(events.NewMessage)
    async def _on_new_message(event):
        try:
            msg = await _normalize_message(event.message, client)
            peer_id = _peer_id_from_event(event)
            _push_event(slug, {"type": "message", "peer_id": peer_id, "message": msg})
        except Exception as exc:  # noqa: BLE001
            logger.debug("NewMessage handler error: %s", exc)

    @client.on(events.MessageEdited)
    async def _on_message_edited(event):
        try:
            msg = await _normalize_message(event.message, client)
            peer_id = _peer_id_from_event(event)
            _push_event(slug, {"type": "edit", "peer_id": peer_id, "message": msg})
        except Exception as exc:  # noqa: BLE001
            logger.debug("MessageEdited handler error: %s", exc)

    @client.on(events.MessageRead)
    async def _on_message_read(event):
        try:
            peer_id = _peer_id_from_event(event)
            _push_event(slug, {"type": "read", "peer_id": peer_id})
        except Exception as exc:  # noqa: BLE001
            logger.debug("MessageRead handler error: %s", exc)

    @client.on(events.UserUpdate)
    async def _on_user_update(event):
        """Push typing events when a user is typing in any chat."""
        try:
            # event.typing is True when the user is typing
            if not getattr(event, "typing", False):
                return
            peer_id = _peer_id_from_event(event)
            user_id = 0
            name = ""
            try:
                sender = await event.get_input_user()
                if sender is not None:
                    from telethon import utils as _tgu  # type: ignore
                    user_id = int(_tgu.get_peer_id(sender))
            except Exception:
                pass
            try:
                user_entity = await event.get_user()
                if user_entity is not None:
                    fname = getattr(user_entity, "first_name", None) or ""
                    lname = getattr(user_entity, "last_name", None) or ""
                    name = (fname + " " + lname).strip() or getattr(user_entity, "username", None) or str(user_id)
            except Exception:
                pass
            _push_event(slug, {
                "type": "typing",
                "peer_id": peer_id,
                "user_id": user_id,
                "name": name,
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug("UserUpdate handler error: %s", exc)


def _peer_id_from_event(event: Any) -> int:
    """Extract a numeric peer id from a Telethon event."""
    try:
        chat = event.chat_id
        if chat is not None:
            return int(chat)
    except Exception:
        pass
    try:
        from telethon import utils  # type: ignore

        return int(utils.get_peer_id(event.peer_id))
    except Exception:
        return 0


async def _get_client(slug: str, *, require_auth: bool = True) -> Any:
    """Return a connected TelegramClient, connecting if necessary.

    Raises TelegramNotConfiguredError / TelegramNotAuthorizedError as appropriate.
    """
    if not is_configured():
        raise TelegramNotConfiguredError("TELEGRAM_API_ID / TELEGRAM_API_HASH not set")

    if slug not in _clients:
        client = _make_client(slug)
        await client.connect()
        _register_handlers(client, slug)
        _clients[slug] = client
    else:
        client = _clients[slug]
        if not client.is_connected():
            await client.connect()

    if require_auth and not await client.is_user_authorized():
        raise TelegramNotAuthorizedError("not_authorized")

    return client


# ---------------------------------------------------------------------------
# HTML entity renderer
# ---------------------------------------------------------------------------

def _entities_to_html(text: str, entities: Any) -> str:
    """Render Telethon message entities to safe HTML.

    Strategy: build a list of (offset, is_open, priority, tag_html) tuples,
    sort them, then walk the text inserting tags at the right positions.
    Overlapping entities are handled best-effort (outer opens first, inner
    closes last).  Never raises.
    """
    try:
        from telethon.tl.types import (  # type: ignore
            MessageEntityBold,
            MessageEntityItalic,
            MessageEntityCode,
            MessageEntityPre,
            MessageEntityStrike,
            MessageEntityUnderline,
            MessageEntitySpoiler,
            MessageEntityTextUrl,
            MessageEntityUrl,
            MessageEntityMention,
            MessageEntityMentionName,
        )

        if not entities:
            return html.escape(text).replace("\n", "\n")

        # Build list of (char_offset, sort_key, html_fragment)
        # sort_key: opens at offset (ascending), closes at offset+length
        # We collect open/close events, then insert them character by character.
        events_list: List[Tuple[int, int, str]] = []
        # (position, 0=open/1=close, tag)
        tag_events: List[Tuple[int, int, str]] = []

        for ent in entities:
            offset = ent.offset
            length = ent.length
            end = offset + length

            open_tag: Optional[str] = None
            close_tag: Optional[str] = None

            if isinstance(ent, MessageEntityBold):
                open_tag, close_tag = "<b>", "</b>"
            elif isinstance(ent, MessageEntityItalic):
                open_tag, close_tag = "<i>", "</i>"
            elif isinstance(ent, MessageEntityCode):
                open_tag, close_tag = "<code>", "</code>"
            elif isinstance(ent, MessageEntityPre):
                open_tag, close_tag = "<pre>", "</pre>"
            elif isinstance(ent, MessageEntityStrike):
                open_tag, close_tag = "<s>", "</s>"
            elif isinstance(ent, MessageEntityUnderline):
                open_tag, close_tag = "<u>", "</u>"
            elif isinstance(ent, MessageEntitySpoiler):
                open_tag, close_tag = '<span class="tg-spoiler">', "</span>"
            elif isinstance(ent, MessageEntityTextUrl):
                href = html.escape(getattr(ent, "url", "") or "", quote=True)
                open_tag = f'<a href="{href}" rel="noopener noreferrer" target="_blank">'
                close_tag = "</a>"
            elif isinstance(ent, MessageEntityUrl):
                # The URL is the text itself; we'll escape the text portion
                open_tag = None  # handled below specially
                close_tag = None
                # Grab the raw url text
                raw_url = text[offset:end]
                escaped_url = html.escape(raw_url, quote=True)
                tag_events.append((offset, 0, f'<a href="{escaped_url}" rel="noopener noreferrer" target="_blank">'))
                tag_events.append((end, 1, "</a>"))
            elif isinstance(ent, (MessageEntityMention, MessageEntityMentionName)):
                open_tag, close_tag = "<a>", "</a>"

            if open_tag is not None and close_tag is not None:
                tag_events.append((offset, 0, open_tag))
                tag_events.append((end, 1, close_tag))

        # Sort: opens before text at same position (key=0), closes after (key=1)
        # For opens at same position, we want outer first → sort by end descending (priority)
        # We'll do a simpler sort: (position, is_close) so opens come before closes at same pos
        tag_events.sort(key=lambda x: (x[0], x[1]))

        # Build the result by walking UTF-16 code units (Telegram entity offsets are UTF-16)
        # Convert text to list of UTF-16 code units for offset tracking
        try:
            utf16_bytes = text.encode("utf-16-le")
            utf16_units = len(utf16_bytes) // 2
        except Exception:
            utf16_units = len(text)

        # Map utf-16 offset → python string index
        utf16_to_py: List[int] = []
        py_idx = 0
        for ch in text:
            utf16_to_py.append(py_idx)
            # Characters outside BMP take 2 UTF-16 units
            if ord(ch) > 0xFFFF:
                utf16_to_py.append(py_idx)  # surrogate pair, both units map to same char
            py_idx += 1
        utf16_to_py.append(py_idx)  # sentinel

        def _utf16_to_py(u16: int) -> int:
            if u16 < len(utf16_to_py):
                return utf16_to_py[u16]
            return len(text)

        # Rebuild tag_events with python indices
        py_tag_events: List[Tuple[int, int, str]] = []
        for (u16_pos, is_close, tag) in tag_events:
            py_pos = _utf16_to_py(u16_pos)
            py_tag_events.append((py_pos, is_close, tag))

        py_tag_events.sort(key=lambda x: (x[0], x[1]))

        # Walk text, inserting tags
        result_parts: List[str] = []
        prev = 0
        for (py_pos, is_close, tag) in py_tag_events:
            if py_pos > prev:
                result_parts.append(html.escape(text[prev:py_pos]))
                prev = py_pos
            result_parts.append(tag)

        if prev < len(text):
            result_parts.append(html.escape(text[prev:]))

        return "".join(result_parts)

    except Exception as exc:
        logger.debug("_entities_to_html error: %s", exc)
        return html.escape(text or "")


# ---------------------------------------------------------------------------
# Message normalization
# ---------------------------------------------------------------------------

async def _normalize_message(message: Any, client: Any, read_max_id: Optional[int] = None) -> Dict[str, Any]:
    """Convert a Telethon Message to the contract JSON shape.

    ``read_max_id`` is the peer's ``read_outbox_max_id`` (highest outgoing
    message id the peer has read). Pass it in so read-state is computed without
    a per-message network call; fetch it once per thread in ``get_messages``.
    """
    from telethon.tl.types import (  # type: ignore
        MessageMediaPhoto,
        MessageMediaDocument,
        MessageMediaGeo,
        MessageMediaContact,
        MessageMediaPoll,
        MessageMediaVenue,
        MessageMediaWebPage,
        DocumentAttributeAudio,
        DocumentAttributeVideo,
        DocumentAttributeSticker,
        DocumentAttributeFilename,
        DocumentAttributeImageSize,
        ReactionEmoji,
    )

    # Sender name
    sender_info: Optional[Dict[str, Any]] = None
    try:
        sender = await message.get_sender()
        if sender is not None:
            sid = getattr(sender, "id", None)
            fname = getattr(sender, "first_name", None) or ""
            lname = getattr(sender, "last_name", None) or ""
            title = getattr(sender, "title", None) or ""
            uname = getattr(sender, "username", None) or ""
            if fname or lname:
                sname = (fname + " " + lname).strip()
            elif title:
                sname = title
            elif uname:
                sname = uname
            else:
                sname = str(sid)
            sender_info = {"id": int(sid) if sid is not None else 0, "name": sname}
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # Media — enriched
    # -----------------------------------------------------------------------
    media_info: Optional[Dict[str, Any]] = None
    if message.media:
        mtype: Optional[str] = None
        caption = message.message or ""
        filename: Optional[str] = None
        url: Optional[str] = None
        mime: Optional[str] = None
        width: Optional[int] = None
        height: Optional[int] = None
        duration: Optional[float] = None
        size: Optional[int] = None

        if isinstance(message.media, MessageMediaPhoto):
            mtype = "photo"
            peer_id = _peer_id_from_message(message)
            url = f"/api/telegram/media/{peer_id}/{message.id}"
            # Try to get photo dimensions from sizes
            try:
                photo = message.media.photo
                for sz in reversed(getattr(photo, "sizes", [])):
                    w = getattr(sz, "w", None)
                    h = getattr(sz, "h", None)
                    if w and h:
                        width, height = int(w), int(h)
                        break
            except Exception:
                pass

        elif isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            mime = getattr(doc, "mime_type", "") or ""
            size_val = getattr(doc, "size", None)
            if size_val is not None:
                try:
                    size = int(size_val)
                except Exception:
                    pass

            # Inspect attributes to determine type
            has_voice = False
            has_video = False
            has_sticker = False
            has_audio = False
            is_animated = False

            for attr in getattr(doc, "attributes", []):
                if isinstance(attr, DocumentAttributeAudio):
                    if getattr(attr, "voice", False):
                        has_voice = True
                    else:
                        has_audio = True
                    dur = getattr(attr, "duration", None)
                    if dur is not None:
                        try:
                            duration = float(dur)
                        except Exception:
                            pass
                elif isinstance(attr, DocumentAttributeVideo):
                    has_video = True
                    w = getattr(attr, "w", None)
                    h = getattr(attr, "h", None)
                    if w:
                        width = int(w)
                    if h:
                        height = int(h)
                    dur = getattr(attr, "duration", None)
                    if dur is not None:
                        try:
                            duration = float(dur)
                        except Exception:
                            pass
                    if getattr(attr, "round_message", False) or getattr(attr, "supports_streaming", False):
                        pass  # still video
                elif isinstance(attr, DocumentAttributeSticker):
                    has_sticker = True
                elif isinstance(attr, DocumentAttributeFilename):
                    fn = getattr(attr, "file_name", None)
                    if fn:
                        filename = fn
                elif isinstance(attr, DocumentAttributeImageSize):
                    w = getattr(attr, "w", None)
                    h = getattr(attr, "h", None)
                    if w:
                        width = int(w)
                    if h:
                        height = int(h)

            # Determine GIF: video attr with mime=video/mp4 and document has "animated" in name,
            # or mime is image/gif, or doc has animated attribute
            # Telethon: gif documents often have DocumentAttributeVideo + mime video/mp4
            # and a filename ending in .gif or the animated flag
            try:
                # Check for animated gif flag in sticker
                from telethon.tl.types import DocumentAttributeAnimated  # type: ignore
                for attr in getattr(doc, "attributes", []):
                    if isinstance(attr, DocumentAttributeAnimated):
                        is_animated = True
                        break
            except Exception:
                pass

            if has_voice:
                mtype = "voice"
            elif has_sticker or mime == "image/webp":
                mtype = "sticker"
            elif (has_video and is_animated) or mime == "image/gif":
                mtype = "gif"
            elif has_video:
                mtype = "video"
            elif has_audio:
                mtype = "audio"
            else:
                mtype = "document"

            peer_id = _peer_id_from_message(message)
            url = f"/api/telegram/media/{peer_id}/{message.id}"

        elif isinstance(message.media, MessageMediaWebPage):
            mtype = "webpage"
        elif isinstance(message.media, MessageMediaGeo):
            mtype = "geo"
        elif isinstance(message.media, MessageMediaContact):
            mtype = "contact"
        elif isinstance(message.media, MessageMediaPoll):
            mtype = "poll"
        elif isinstance(message.media, MessageMediaVenue):
            mtype = "venue"
        else:
            mtype = "other"

        media_info = {
            "type": mtype,
            "url": url,
            "mime": mime,
            "filename": filename,
            "size": size,
            "width": width,
            "height": height,
            "duration": duration,
            "caption": caption,
        }

    # Service message (channel migration, pin, join, etc.)
    service_text: Optional[str] = None
    try:
        from telethon.tl.types import MessageService  # type: ignore
        if isinstance(message, MessageService):
            action = message.action
            service_text = type(action).__name__.replace("MessageAction", "")
    except Exception:
        pass

    # reply_to_id
    reply_to_id: Optional[int] = None
    try:
        if message.reply_to and hasattr(message.reply_to, "reply_to_msg_id"):
            reply_to_id = message.reply_to.reply_to_msg_id
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # text + text_html
    # -----------------------------------------------------------------------
    text = message.message or ""
    if media_info and media_info.get("type") in ("photo", "video", "audio", "voice", "gif", "sticker", "document"):
        # caption is already in media; clear text to avoid duplication
        if text == (media_info.get("caption") or ""):
            text = ""
        media_info["caption"] = media_info.get("caption") or ""

    entities = getattr(message, "entities", None) or []
    try:
        # Use message.message for HTML (the full text before stripping)
        raw_text_for_html = message.message or ""
        text_html = _entities_to_html(raw_text_for_html, entities)
    except Exception:
        text_html = html.escape(text)

    # -----------------------------------------------------------------------
    # reply_quote
    # -----------------------------------------------------------------------
    reply_quote: Optional[Dict[str, Any]] = None
    try:
        if reply_to_id:
            replied = await message.get_reply_message()
            if replied is not None:
                rq_text = (replied.message or "")[:120]
                rq_sender = await replied.get_sender()
                rq_name = ""
                if rq_sender is not None:
                    rf = getattr(rq_sender, "first_name", None) or ""
                    rl = getattr(rq_sender, "last_name", None) or ""
                    rt = getattr(rq_sender, "title", None) or ""
                    ru = getattr(rq_sender, "username", None) or ""
                    if rf or rl:
                        rq_name = (rf + " " + rl).strip()
                    elif rt:
                        rq_name = rt
                    elif ru:
                        rq_name = ru
                reply_quote = {"name": rq_name, "text": rq_text}
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # forward_from
    # -----------------------------------------------------------------------
    forward_from: Optional[Dict[str, Any]] = None
    try:
        fwd = message.forward
        if fwd is not None:
            fwd_name = ""
            # Try to get sender name from forward header
            try:
                fwd_sender = await fwd.get_sender()
                if fwd_sender is not None:
                    ff = getattr(fwd_sender, "first_name", None) or ""
                    fl = getattr(fwd_sender, "last_name", None) or ""
                    ft = getattr(fwd_sender, "title", None) or ""
                    fu = getattr(fwd_sender, "username", None) or ""
                    if ff or fl:
                        fwd_name = (ff + " " + fl).strip()
                    elif ft:
                        fwd_name = ft
                    elif fu:
                        fwd_name = fu
            except Exception:
                pass
            if not fwd_name:
                # Fallback: from_name attribute on forward header
                fwd_name = getattr(fwd, "from_name", None) or ""
            forward_from = {"name": fwd_name}
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # reactions
    # -----------------------------------------------------------------------
    reactions: List[Dict[str, Any]] = []
    try:
        msg_reactions = getattr(message, "reactions", None)
        if msg_reactions is not None:
            results = getattr(msg_reactions, "results", None) or []
            # recent_reactions to detect chosen
            recent = getattr(msg_reactions, "recent_reactions", None) or []
            # chosen_order (Telethon 1.24+) or fall back
            chosen_set: set = set()
            try:
                for r in recent:
                    peer_r = getattr(r, "peer_id", None)
                    if peer_r is not None:
                        # We don't have "me" id easily here; use chosen flag
                        pass
                # Use the chosen flag on ReactionCount if available
                for rc in results:
                    if getattr(rc, "chosen", False) or getattr(rc, "chosen_order", None) is not None:
                        emo = getattr(rc, "reaction", None)
                        if emo is not None:
                            emoticon = getattr(emo, "emoticon", None) or ""
                            chosen_set.add(emoticon)
            except Exception:
                pass

            for rc in results:
                emo = getattr(rc, "reaction", None)
                emoticon = ""
                if emo is not None:
                    emoticon = getattr(emo, "emoticon", None) or ""
                count = getattr(rc, "count", 0) or 0
                chosen = getattr(rc, "chosen", False) or (emoticon in chosen_set)
                reactions.append({"emoji": emoticon, "count": int(count), "chosen": bool(chosen)})
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # pinned
    # -----------------------------------------------------------------------
    pinned = bool(getattr(message, "pinned", False))

    # -----------------------------------------------------------------------
    # read (outgoing only)
    # -----------------------------------------------------------------------
    read: Optional[bool] = None
    try:
        if getattr(message, "out", False) and read_max_id is not None:
            read = message.id <= read_max_id
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # grouped_id
    # -----------------------------------------------------------------------
    grouped_id: Optional[int] = None
    try:
        gid = getattr(message, "grouped_id", None)
        if gid is not None:
            grouped_id = int(gid)
    except Exception:
        pass

    return {
        "id": message.id,
        "out": bool(getattr(message, "out", False)),
        "text": text,
        "text_html": text_html,
        "date": int(message.date.timestamp()) if message.date else 0,
        "edited": message.edit_date is not None,
        "sender": sender_info,
        "reply_to_id": reply_to_id,
        "reply_quote": reply_quote,
        "forward_from": forward_from,
        "reactions": reactions,
        "pinned": pinned,
        "read": read,
        "grouped_id": grouped_id,
        "media": media_info,
        "service": service_text,
    }


def _peer_id_from_message(message: Any) -> int:
    """Return a message's canonical *marked* peer id (matches dialog ids)."""
    try:
        from telethon import utils  # type: ignore

        return int(utils.get_peer_id(message.peer_id))
    except Exception:
        pass
    try:
        return int(message.chat_id)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Public API — called by route handlers
# ---------------------------------------------------------------------------

async def get_status(username: str) -> Dict[str, Any]:
    """Return configured/authorized status + me info."""
    configured = is_configured()
    if not configured:
        return {"configured": False, "authorized": False, "me": None}

    slug = _slug(username)
    try:
        client = await _get_client(slug, require_auth=False)
        authorized = await client.is_user_authorized()
        me_info = None
        if authorized:
            me = await client.get_me()
            me_info = _me_to_dict(me)
        return {"configured": True, "authorized": authorized, "me": me_info}
    except TelegramNotConfiguredError:
        return {"configured": False, "authorized": False, "me": None}
    except Exception as exc:
        logger.warning("get_status error for %s: %s", username, exc)
        return {"configured": True, "authorized": False, "me": None}


def _me_to_dict(me: Any) -> Dict[str, Any]:
    fname = getattr(me, "first_name", None) or ""
    lname = getattr(me, "last_name", None) or ""
    name = (fname + " " + lname).strip() or getattr(me, "username", None) or str(me.id)
    return {
        "id": int(me.id),
        "name": name,
        "username": getattr(me, "username", None),
        "phone": getattr(me, "phone", None),
    }


async def send_code(username: str, phone: str) -> None:
    """Send a login code to the given phone number."""
    if not is_configured():
        raise TelegramNotConfiguredError("TELEGRAM_API_ID / TELEGRAM_API_HASH not set")
    slug = _slug(username)
    client = await _get_client(slug, require_auth=False)
    result = await client.send_code_request(phone)
    _pending_logins[slug] = {
        "phone": phone,
        "phone_code_hash": result.phone_code_hash,
    }


async def verify_code(username: str, code: str) -> Dict[str, Any]:
    """Verify the login code.  Returns {"authorized": True, "me": {...}}
    or {"needs_password": True}."""
    if not is_configured():
        raise TelegramNotConfiguredError("TELEGRAM_API_ID / TELEGRAM_API_HASH not set")
    slug = _slug(username)
    pending = _pending_logins.get(slug)
    if not pending:
        raise ValueError("No pending login for this user. Call send-code first.")

    from telethon.errors import (  # type: ignore
        SessionPasswordNeededError,
        PhoneCodeInvalidError,
        PhoneCodeExpiredError,
        FloodWaitError,
        PhoneNumberInvalidError,
    )

    client = await _get_client(slug, require_auth=False)
    try:
        me = await client.sign_in(
            phone=pending["phone"],
            code=code,
            phone_code_hash=pending["phone_code_hash"],
        )
        _pending_logins.pop(slug, None)
        return {"authorized": True, "me": _me_to_dict(me)}
    except SessionPasswordNeededError:
        return {"needs_password": True}
    except PhoneCodeInvalidError:
        raise ValueError("Invalid code. Please check and try again.")
    except PhoneCodeExpiredError:
        raise ValueError("Code expired. Please request a new code.")
    except FloodWaitError as exc:
        raise ValueError(f"Too many attempts. Try again in {exc.seconds} seconds.")
    except PhoneNumberInvalidError:
        raise ValueError("Invalid phone number.")


async def verify_password(username: str, password: str) -> Dict[str, Any]:
    """Complete 2FA login with a cloud password."""
    if not is_configured():
        raise TelegramNotConfiguredError("TELEGRAM_API_ID / TELEGRAM_API_HASH not set")
    slug = _slug(username)

    from telethon.errors import (  # type: ignore
        PasswordHashInvalidError,
        FloodWaitError,
    )

    client = await _get_client(slug, require_auth=False)
    try:
        me = await client.sign_in(password=password)
        _pending_logins.pop(slug, None)
        return {"authorized": True, "me": _me_to_dict(me)}
    except PasswordHashInvalidError:
        raise ValueError("Incorrect password.")
    except FloodWaitError as exc:
        raise ValueError(f"Too many attempts. Try again in {exc.seconds} seconds.")


async def logout(username: str) -> None:
    """Log out, disconnect, and delete the session file."""
    slug = _slug(username)
    client = _clients.pop(slug, None)
    if client is not None:
        try:
            await client.log_out()
        except Exception:
            pass
        try:
            await client.disconnect()
        except Exception:
            pass

    # Delete session file
    session_path = _sessions_dir() / f"{slug}.session"
    try:
        session_path.unlink(missing_ok=True)
    except Exception:
        pass

    # Clear avatar/media cache for this user (best-effort)
    # We don't delete other users' caches.
    _pending_logins.pop(slug, None)
    _event_queues.pop(slug, None)
    _event_cursors.pop(slug, None)


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

async def get_dialogs(username: str, limit: int = 50, folder: int = 0) -> List[Dict[str, Any]]:
    slug = _slug(username)
    client = await _get_client(slug)

    import time as _time
    from telethon.tl.types import (  # type: ignore
        User,
        Chat,
        Channel,
        DraftMessage,
    )
    from telethon import utils  # type: ignore

    # Telethon supports folder param: 0=main, 1=archived
    dialogs = await client.get_dialogs(limit=limit, folder=folder)
    result: List[Dict[str, Any]] = []
    now_ts = int(_time.time())
    for d in dialogs:
        entity = d.entity
        if entity is None:
            continue

        # Determine type
        if isinstance(entity, User):
            dtype = "bot" if getattr(entity, "bot", False) else "user"
        elif isinstance(entity, Chat):
            dtype = "group"
        elif isinstance(entity, Channel):
            dtype = "channel" if getattr(entity, "broadcast", False) else "group"
        else:
            dtype = "user"

        # Title
        if isinstance(entity, User):
            fname = getattr(entity, "first_name", None) or ""
            lname = getattr(entity, "last_name", None) or ""
            title = (fname + " " + lname).strip() or getattr(entity, "username", None) or str(entity.id)
        else:
            title = getattr(entity, "title", None) or str(entity.id)

        # Last message text
        last_msg = ""
        last_date = 0
        if d.message:
            last_msg = d.message.message or ""
            if d.message.date:
                last_date = int(d.message.date.timestamp())

        # Peer id — use Telethon's canonical *marked* id (users positive,
        # chats negative, channels -100… prefixed). This is the same scheme
        # event.chat_id and get_peer_id(message.peer_id) produce, so dialog
        # ids match realtime-event peer ids and resolve via get_entity().
        peer_id = utils.get_peer_id(entity)

        # --- Wave 2 enrichment ---
        # muted: check notify_settings.mute_until > now (best-effort)
        muted = False
        try:
            ns = d.dialog.notify_settings
            mute_until = getattr(ns, "mute_until", None)
            if mute_until is not None:
                try:
                    mute_ts = int(mute_until.timestamp()) if hasattr(mute_until, "timestamp") else int(mute_until)
                    muted = mute_ts > now_ts
                except Exception:
                    pass
        except Exception:
            pass

        # archived: folder_id == 1
        archived = False
        try:
            archived = getattr(d.dialog, "folder_id", 0) == 1
        except Exception:
            pass

        # draft: dialog draft message text
        draft_text = ""
        try:
            draft = d.draft
            if draft is not None and isinstance(draft, DraftMessage):
                draft_text = draft.text or ""
        except Exception:
            pass

        # unread_mark: Dialog.unread_mark
        unread_mark = False
        try:
            unread_mark = bool(getattr(d.dialog, "unread_mark", False))
        except Exception:
            pass

        result.append({
            "id": peer_id,
            "title": title,
            "username": getattr(entity, "username", None),
            "type": dtype,
            "unread": d.unread_count or 0,
            "last_message": last_msg[:300],
            "last_date": last_date,
            "pinned": bool(d.pinned),
            "verified": bool(getattr(entity, "verified", False)),
            "has_photo": entity.photo is not None,
            # Wave 2
            "muted": muted,
            "archived": archived,
            "draft": draft_text,
            "unread_mark": unread_mark,
        })

    return result


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

async def _read_outbox_max_id(client: Any, entity: Any) -> Optional[int]:
    """The highest outgoing message id the peer has read (one RPC, per thread)."""
    try:
        from telethon.tl import functions  # type: ignore

        res = await client(functions.messages.GetPeerDialogsRequest(peers=[entity]))
        dialogs = getattr(res, "dialogs", None) or []
        if dialogs:
            return getattr(dialogs[0], "read_outbox_max_id", None)
    except Exception:
        return None
    return None


async def get_messages(
    username: str,
    peer_id: int,
    limit: int = 50,
    before_id: int = 0,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Return (messages_oldest_first, has_more)."""
    slug = _slug(username)
    client = await _get_client(slug)

    entity = await client.get_entity(peer_id)
    kwargs: Dict[str, Any] = {"limit": limit + 1}
    if before_id:
        kwargs["max_id"] = before_id

    msgs = await client.get_messages(entity, **kwargs)
    has_more = len(msgs) > limit
    msgs = list(msgs[:limit])
    msgs.reverse()  # oldest first

    # Fetch the peer's read-outbox watermark ONCE for the whole thread so
    # read-state on outgoing bubbles costs zero extra per-message round-trips.
    read_max = await _read_outbox_max_id(client, entity)

    normalized = []
    for m in msgs:
        try:
            normalized.append(await _normalize_message(m, client, read_max_id=read_max))
        except Exception as exc:
            logger.debug("normalize_message(%s) error: %s", m.id, exc)

    return normalized, has_more


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

async def send_message(
    username: str,
    peer_id: int,
    text: str,
    reply_to_id: Optional[int] = None,
) -> Dict[str, Any]:
    slug = _slug(username)
    client = await _get_client(slug)

    entity = await client.get_entity(peer_id)
    msg = await client.send_message(
        entity,
        text,
        reply_to=reply_to_id,
        parse_mode="md",
    )
    return await _normalize_message(msg, client)


# ---------------------------------------------------------------------------
# Mark read
# ---------------------------------------------------------------------------

async def mark_read(username: str, peer_id: int) -> None:
    slug = _slug(username)
    client = await _get_client(slug)
    entity = await client.get_entity(peer_id)
    await client.send_read_acknowledge(entity)


# ---------------------------------------------------------------------------
# Updates (long-poll drain)
# ---------------------------------------------------------------------------

def get_updates(username: str, cursor: int) -> Dict[str, Any]:
    """Drain pending events with cursor > given cursor.  Synchronous."""
    slug = _slug(username)
    queue = _event_queues.get(slug, collections.deque())
    events_list = [e for e in queue if e.get("cursor", 0) > cursor]
    # Remove "cursor" field from each event's copy before returning
    result = []
    for e in events_list:
        item = {k: v for k, v in e.items() if k != "cursor"}
        result.append(item)
    new_cursor = max((e["cursor"] for e in events_list), default=cursor) if events_list else cursor
    return {"cursor": new_cursor, "events": result}


# ---------------------------------------------------------------------------
# Avatar
# ---------------------------------------------------------------------------

async def get_avatar(username: str, peer_id: int) -> Optional[bytes]:
    """Return avatar bytes (JPEG) or None."""
    slug = _slug(username)
    client = await _get_client(slug)

    cache_path = _cache_dir() / f"avatar_{peer_id}.jpg"
    if cache_path.exists():
        return cache_path.read_bytes()

    try:
        entity = await client.get_entity(peer_id)
        photo_bytes = await client.download_profile_photo(entity, file=bytes)
        if photo_bytes:
            cache_path.write_bytes(photo_bytes)
            return photo_bytes
    except Exception as exc:
        logger.debug("get_avatar(%s) error: %s", peer_id, exc)

    return None


# ---------------------------------------------------------------------------
# Media download
# ---------------------------------------------------------------------------

async def get_media(username: str, peer_id: int, message_id: int) -> Optional[Tuple[bytes, str]]:
    """Return (bytes, content_type) or None."""
    slug = _slug(username)
    client = await _get_client(slug)

    # Check cache
    cache_dir = _cache_dir()
    # Try common extensions
    for ext in ("", ".jpg", ".mp4", ".mp3", ".ogg", ".pdf", ".zip"):
        candidate = cache_dir / f"{peer_id}_{message_id}{ext}"
        if candidate.exists():
            ct = _content_type(str(candidate))
            return candidate.read_bytes(), ct

    # Fetch the message
    try:
        entity = await client.get_entity(peer_id)
        msgs = await client.get_messages(entity, ids=message_id)
        if not msgs or msgs[0] is None:
            return None
        msg = msgs[0] if not isinstance(msgs, list) else msgs[0]

        # Determine file extension from mime type
        mime = ""
        try:
            from telethon.tl.types import MessageMediaDocument  # type: ignore
            if isinstance(msg.media, MessageMediaDocument):
                mime = getattr(msg.media.document, "mime_type", "") or ""
        except Exception:
            pass

        ext_suffix = _ext_from_mime(mime) or ""
        cache_path = cache_dir / f"{peer_id}_{message_id}{ext_suffix}"

        data = await client.download_media(msg, file=bytes)
        if data is None:
            return None

        cache_path.write_bytes(data)
        ct = _content_type(str(cache_path)) if ext_suffix else (mime or "application/octet-stream")
        return data, ct
    except Exception as exc:
        logger.debug("get_media(%s,%s) error: %s", peer_id, message_id, exc)
        return None


def _ext_from_mime(mime: str) -> str:
    if not mime:
        return ""
    exts = mimetypes.guess_all_extensions(mime)
    if exts:
        # Prefer common extensions
        for pref in (".jpg", ".jpeg", ".png", ".mp4", ".mp3", ".ogg", ".webp", ".pdf"):
            if pref in exts:
                return pref
        return exts[0]
    return ""


def _content_type(path: str) -> str:
    ct, _ = mimetypes.guess_type(path)
    return ct or "application/octet-stream"


# ---------------------------------------------------------------------------
# Wave 1 — new client functions
# ---------------------------------------------------------------------------

async def forward_messages(
    username: str,
    from_peer_id: int,
    message_ids: List[int],
    to_peer_id: int,
) -> None:
    """Forward messages from one chat to another."""
    slug = _slug(username)
    client = await _get_client(slug)
    from_entity = await client.get_entity(from_peer_id)
    to_entity = await client.get_entity(to_peer_id)
    await client.forward_messages(to_entity, message_ids, from_entity)


async def edit_message(
    username: str,
    peer_id: int,
    message_id: int,
    text: str,
) -> Dict[str, Any]:
    """Edit a message (Markdown parse mode)."""
    slug = _slug(username)
    client = await _get_client(slug)
    entity = await client.get_entity(peer_id)
    msg = await client.edit_message(entity, message_id, text, parse_mode="md")
    return await _normalize_message(msg, client)


async def delete_messages(
    username: str,
    peer_id: int,
    message_ids: List[int],
    revoke: bool = True,
) -> None:
    """Delete messages, optionally for everyone (revoke=True)."""
    slug = _slug(username)
    client = await _get_client(slug)
    entity = await client.get_entity(peer_id)
    await client.delete_messages(entity, message_ids, revoke=revoke)


async def react(
    username: str,
    peer_id: int,
    message_id: int,
    emoji: str,
) -> None:
    """Set (or clear) a reaction on a message.

    Pass empty string to clear the current reaction.
    """
    slug = _slug(username)
    client = await _get_client(slug)

    from telethon import functions, types  # type: ignore

    entity = await client.get_entity(peer_id)
    reaction_list = [types.ReactionEmoji(emoticon=emoji)] if emoji else []
    await client(
        functions.messages.SendReactionRequest(
            peer=entity,
            msg_id=message_id,
            reaction=reaction_list,
        )
    )


async def set_typing(
    username: str,
    peer_id: int,
    cancel: bool = False,
) -> None:
    """Send typing indicator (or cancel it).  Best-effort, errors silently ignored."""
    slug = _slug(username)
    try:
        client = await _get_client(slug)
        entity = await client.get_entity(peer_id)
        action = "cancel" if cancel else "typing"
        async with client.action(entity, action):
            pass
    except Exception as exc:
        logger.debug("set_typing error: %s", exc)


async def send_media(
    username: str,
    peer_id: int,
    files: List[Tuple[str, bytes, str]],  # (filename, data, mime)
    caption: str = "",
    reply_to: Optional[int] = None,
    voice: bool = False,
) -> Dict[str, Any]:
    """Send one or more files.  For voice, tries voice_note=True; falls back to document."""
    slug = _slug(username)
    client = await _get_client(slug)
    entity = await client.get_entity(peer_id)

    # Build file-like objects from raw bytes
    file_objs = []
    for fname, data, mime in files:
        buf = io.BytesIO(data)
        buf.name = fname  # Telethon uses .name for filename detection
        file_objs.append(buf)

    single = file_objs[0] if len(file_objs) == 1 else file_objs

    try:
        sent = await client.send_file(
            entity,
            single,
            caption=caption or None,
            reply_to=reply_to,
            voice_note=voice,
            force_document=False,
            parse_mode="md" if caption else None,
        )
    except Exception as exc:
        if voice:
            # Fallback: send as regular document
            logger.debug("send_media voice_note failed (%s), falling back to document", exc)
            # Reset buffer positions
            for buf in file_objs:
                buf.seek(0)
            sent = await client.send_file(
                entity,
                single,
                caption=caption or None,
                reply_to=reply_to,
                voice_note=False,
                force_document=True,
                parse_mode="md" if caption else None,
            )
        else:
            raise

    # sent may be a single message or a list (album); return the last/representative
    if isinstance(sent, list):
        representative = sent[-1]
    else:
        representative = sent

    return await _normalize_message(representative, client)


async def peer_status(username: str, peer_id: int) -> Dict[str, Any]:
    """Return online/presence info for a peer."""
    slug = _slug(username)
    client = await _get_client(slug)

    from telethon.tl.types import (  # type: ignore
        User,
        Channel,
        Chat,
        UserStatusOnline,
        UserStatusOffline,
        UserStatusRecently,
        UserStatusLastWeek,
        UserStatusLastMonth,
    )

    entity = await client.get_entity(peer_id)

    if isinstance(entity, User):
        status = getattr(entity, "status", None)
        if isinstance(status, UserStatusOnline):
            return {"online": True, "last_seen": None, "label": "online"}
        elif isinstance(status, UserStatusOffline):
            ts = None
            label = "offline"
            try:
                ts = int(status.was_online.timestamp())
                label = f"last seen {ts}"
            except Exception:
                pass
            return {"online": False, "last_seen": ts, "label": label}
        elif isinstance(status, UserStatusRecently):
            return {"online": False, "last_seen": None, "label": "last seen recently"}
        elif isinstance(status, UserStatusLastWeek):
            return {"online": False, "last_seen": None, "label": "last seen last week"}
        elif isinstance(status, UserStatusLastMonth):
            return {"online": False, "last_seen": None, "label": "last seen last month"}
        else:
            return {"online": False, "last_seen": None, "label": "unknown"}
    else:
        # Group or channel
        count = None
        try:
            if isinstance(entity, Channel):
                count = getattr(entity, "participants_count", None)
            elif isinstance(entity, Chat):
                count = getattr(entity, "participants_count", None)
        except Exception:
            pass
        label = f"{count} members" if count is not None else "group"
        return {"online": False, "last_seen": None, "label": label}


# ---------------------------------------------------------------------------
# Wave 2 — Chat management functions
# ---------------------------------------------------------------------------

async def set_mute(username: str, peer_id: int, mute: bool) -> None:
    """Mute or unmute a dialog's notifications."""
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import functions, types  # type: ignore

    entity = await client.get_entity(peer_id)
    input_peer = await client.get_input_entity(entity)
    await client(
        functions.account.UpdateNotifySettingsRequest(
            peer=types.InputNotifyPeer(peer=input_peer),
            settings=types.InputPeerNotifySettings(
                mute_until=2**31 - 1 if mute else 0,
            ),
        )
    )


async def set_pinned_dialog(username: str, peer_id: int, pin: bool) -> None:
    """Pin or unpin a dialog."""
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import functions  # type: ignore

    entity = await client.get_entity(peer_id)
    input_peer = await client.get_input_entity(entity)
    await client(
        functions.messages.TogglePinnedDialogRequest(
            peer=input_peer,
            pinned=pin,
        )
    )


async def set_archived(username: str, peer_id: int, archive: bool) -> None:
    """Archive or unarchive a dialog (move to/from folder 1)."""
    slug = _slug(username)
    client = await _get_client(slug)

    entity = await client.get_entity(peer_id)
    await client.edit_folder(entity, folder=1 if archive else 0)


async def set_unread_mark(username: str, peer_id: int, unread: bool) -> None:
    """Mark a dialog as unread or clear the unread mark."""
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import functions  # type: ignore

    entity = await client.get_entity(peer_id)
    input_peer = await client.get_input_entity(entity)
    await client(
        functions.messages.MarkDialogUnreadRequest(
            peer=input_peer,
            unread=unread,
        )
    )


async def delete_dialog(
    username: str,
    peer_id: int,
    leave: bool = False,
    just_clear: bool = False,
) -> None:
    """Delete or clear a dialog.

    just_clear=True  → clear history only (DeleteHistoryRequest).
    leave=True       → leave the group/channel and delete dialog.
    Default          → delete_dialog with revoke=False.
    """
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import functions  # type: ignore

    entity = await client.get_entity(peer_id)

    if just_clear:
        input_peer = await client.get_input_entity(entity)
        await client(
            functions.messages.DeleteHistoryRequest(
                peer=input_peer,
                max_id=0,
                just_clear=True,
                revoke=False,
            )
        )
    else:
        # delete_dialog handles leave for channels/groups and clears for users
        await client.delete_dialog(entity, revoke=False)


async def pin_message(
    username: str,
    peer_id: int,
    message_id: int,
    pin: bool,
) -> None:
    """Pin or unpin a message in a chat."""
    slug = _slug(username)
    client = await _get_client(slug)

    entity = await client.get_entity(peer_id)
    if pin:
        await client.pin_message(entity, message_id)
    else:
        await client.unpin_message(entity, message_id)


async def get_pinned(
    username: str,
    peer_id: int,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return pinned messages (oldest first)."""
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import types  # type: ignore

    entity = await client.get_entity(peer_id)
    msgs = await client.get_messages(
        entity,
        filter=types.InputMessagesFilterPinned(),
        limit=limit,
    )
    result = []
    for m in reversed(list(msgs)):
        try:
            result.append(await _normalize_message(m, client))
        except Exception as exc:
            logger.debug("get_pinned normalize error: %s", exc)
    return result


async def search_messages(
    username: str,
    query: str,
    peer_id: int = 0,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """Search messages in a specific peer or globally.

    Returns a list of result dicts:
    {peer_id, peer_title, peer_type, has_photo, message:{...normalized...}}
    """
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import utils  # type: ignore
    from telethon.tl.types import User, Channel, Chat  # type: ignore

    results: List[Dict[str, Any]] = []

    if peer_id:
        entity = await client.get_entity(peer_id)
        msgs = await client.get_messages(entity, search=query, limit=limit)
        # Determine peer info
        peer_title = _entity_title(entity)
        peer_type = _entity_type(entity)
        has_photo = entity.photo is not None
        for m in msgs:
            try:
                norm = await _normalize_message(m, client)
                results.append({
                    "peer_id": peer_id,
                    "peer_title": peer_title,
                    "peer_type": peer_type,
                    "has_photo": has_photo,
                    "message": norm,
                })
            except Exception as exc:
                logger.debug("search_messages normalize error: %s", exc)
    else:
        # Global search — iterate via iter_messages(None, search=query)
        try:
            async for m in client.iter_messages(None, search=query, limit=limit):
                try:
                    chat = await m.get_chat()
                    if chat is None:
                        continue
                    cpid = int(utils.get_peer_id(chat))
                    peer_title = _entity_title(chat)
                    peer_type = _entity_type(chat)
                    has_photo = chat.photo is not None
                    norm = await _normalize_message(m, client)
                    results.append({
                        "peer_id": cpid,
                        "peer_title": peer_title,
                        "peer_type": peer_type,
                        "has_photo": has_photo,
                        "message": norm,
                    })
                except Exception as exc:
                    logger.debug("search_messages global item error: %s", exc)
        except Exception as exc:
            logger.warning("search_messages global error: %s", exc)

    return results


def _entity_title(entity: Any) -> str:
    from telethon.tl.types import User  # type: ignore
    if isinstance(entity, User):
        fname = getattr(entity, "first_name", None) or ""
        lname = getattr(entity, "last_name", None) or ""
        return (fname + " " + lname).strip() or getattr(entity, "username", None) or str(entity.id)
    return getattr(entity, "title", None) or str(entity.id)


def _entity_type(entity: Any) -> str:
    from telethon.tl.types import User, Chat, Channel  # type: ignore
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "channel" if getattr(entity, "broadcast", False) else "group"
    return "user"


async def search_dialogs(
    username: str,
    query: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Search dialogs locally (title match) + global contacts search.

    Returns a deduplicated list in dialog shape (same keys as get_dialogs).
    """
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import functions, utils  # type: ignore
    from telethon.tl.types import User, Chat, Channel  # type: ignore

    seen_ids: set = set()
    results: List[Dict[str, Any]] = []

    def _make_dialog_shape(entity: Any, extra: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        try:
            pid = int(utils.get_peer_id(entity))
            if pid in seen_ids:
                return None
            seen_ids.add(pid)
            etype = _entity_type(entity)
            title = _entity_title(entity)
            d = {
                "id": pid,
                "title": title,
                "username": getattr(entity, "username", None),
                "type": etype,
                "unread": 0,
                "last_message": "",
                "last_date": 0,
                "pinned": False,
                "verified": bool(getattr(entity, "verified", False)),
                "has_photo": entity.photo is not None,
                "muted": False,
                "archived": False,
                "draft": "",
                "unread_mark": False,
            }
            if extra:
                d.update(extra)
            return d
        except Exception:
            return None

    # 1. Local dialogs — title match
    try:
        local_dialogs = await client.get_dialogs(limit=200)
        q_lower = query.lower()
        for d in local_dialogs:
            if len(results) >= limit:
                break
            entity = d.entity
            if entity is None:
                continue
            title = _entity_title(entity)
            uname = getattr(entity, "username", None) or ""
            if q_lower in title.lower() or q_lower in uname.lower():
                shape = _make_dialog_shape(entity)
                if shape:
                    results.append(shape)
    except Exception as exc:
        logger.debug("search_dialogs local error: %s", exc)

    # 2. Global contacts search
    try:
        res = await client(functions.contacts.SearchRequest(q=query, limit=limit))
        entities_map: Dict[int, Any] = {}
        for u in getattr(res, "users", []):
            entities_map[u.id] = u
        for c in getattr(res, "chats", []):
            entities_map[c.id] = c

        for entity in entities_map.values():
            if len(results) >= limit:
                break
            shape = _make_dialog_shape(entity)
            if shape:
                results.append(shape)
    except Exception as exc:
        logger.debug("search_dialogs global error: %s", exc)

    return results[:limit]


async def get_profile(username: str, peer_id: int) -> Dict[str, Any]:
    """Return enriched profile info for a peer."""
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import functions, utils  # type: ignore
    from telethon.tl.types import (  # type: ignore
        User,
        Channel,
        Chat,
        UserStatusOnline,
        UserStatusOffline,
        UserStatusRecently,
        UserStatusLastWeek,
        UserStatusLastMonth,
    )

    entity = await client.get_entity(peer_id)
    pid = int(utils.get_peer_id(entity))
    etype = _entity_type(entity)
    title = _entity_title(entity)
    uname = getattr(entity, "username", None)
    verified = bool(getattr(entity, "verified", False))
    has_photo = entity.photo is not None

    bio = ""
    phone = None
    members_count = None
    online = False
    last_seen = None
    status_label = "unknown"
    common_chats_count = 0

    if isinstance(entity, User):
        # Presence
        status = getattr(entity, "status", None)
        if isinstance(status, UserStatusOnline):
            online = True
            status_label = "online"
        elif isinstance(status, UserStatusOffline):
            try:
                last_seen = int(status.was_online.timestamp())
                status_label = f"last seen {last_seen}"
            except Exception:
                status_label = "offline"
        elif isinstance(status, UserStatusRecently):
            status_label = "last seen recently"
        elif isinstance(status, UserStatusLastWeek):
            status_label = "last seen last week"
        elif isinstance(status, UserStatusLastMonth):
            status_label = "last seen last month"

        phone = getattr(entity, "phone", None)

        try:
            full = await client(functions.users.GetFullUserRequest(id=entity))
            full_user = getattr(full, "full_user", None)
            if full_user is not None:
                bio = getattr(full_user, "about", None) or ""
                common_chats_count = getattr(full_user, "common_chats_count", 0) or 0
        except Exception as exc:
            logger.debug("get_profile GetFullUser error: %s", exc)

    elif isinstance(entity, Channel):
        members_count = getattr(entity, "participants_count", None)
        try:
            full = await client(functions.channels.GetFullChannelRequest(channel=entity))
            full_chat = getattr(full, "full_chat", None)
            if full_chat is not None:
                bio = getattr(full_chat, "about", None) or ""
                if members_count is None:
                    members_count = getattr(full_chat, "participants_count", None)
        except Exception as exc:
            logger.debug("get_profile GetFullChannel error: %s", exc)

    elif isinstance(entity, Chat):
        members_count = getattr(entity, "participants_count", None)
        try:
            full = await client(functions.messages.GetFullChatRequest(chat_id=entity.id))
            full_chat = getattr(full, "full_chat", None)
            if full_chat is not None:
                bio = getattr(full_chat, "about", None) or ""
                if members_count is None:
                    members_count = getattr(full_chat, "participants_count", None)
        except Exception as exc:
            logger.debug("get_profile GetFullChat error: %s", exc)

    return {
        "id": pid,
        "title": title,
        "username": uname,
        "bio": bio,
        "phone": phone,
        "type": etype,
        "verified": verified,
        "members_count": members_count,
        "online": online,
        "last_seen": last_seen,
        "status_label": status_label,
        "has_photo": has_photo,
        "common_chats_count": common_chats_count,
    }


_SHARED_MEDIA_FILTER_MAP = {
    "photo": "InputMessagesFilterPhotos",
    "video": "InputMessagesFilterVideo",
    "file": "InputMessagesFilterDocument",
    "link": "InputMessagesFilterUrl",
    "voice": "InputMessagesFilterVoice",
}


async def get_shared_media(
    username: str,
    peer_id: int,
    kind: str = "photo",
    limit: int = 30,
    before_id: int = 0,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Return shared media items.  Returns (items, has_more)."""
    slug = _slug(username)
    client = await _get_client(slug)
    import telethon.types as _ttypes  # type: ignore

    filter_name = _SHARED_MEDIA_FILTER_MAP.get(kind, "InputMessagesFilterPhotos")
    filter_cls = getattr(_ttypes, filter_name, None)
    if filter_cls is None:
        from telethon import types as _tltypes  # type: ignore
        filter_cls = getattr(_tltypes, filter_name, _tltypes.InputMessagesFilterPhotos)

    entity = await client.get_entity(peer_id)
    fetch_limit = limit + 1
    kwargs: Dict[str, Any] = {"filter": filter_cls(), "limit": fetch_limit}
    if before_id:
        kwargs["max_id"] = before_id

    msgs = await client.get_messages(entity, **kwargs)
    has_more = len(msgs) > limit
    msgs = list(msgs[:limit])

    items: List[Dict[str, Any]] = []
    for m in msgs:
        try:
            norm = await _normalize_message(m, client)
            items.append({
                "message_id": m.id,
                "date": norm["date"],
                "media": norm.get("media"),
                "text": norm.get("text", ""),
            })
        except Exception as exc:
            logger.debug("get_shared_media normalize error: %s", exc)

    return items, has_more


async def create_chat(
    username: str,
    kind: str,
    title: str,
    about: str = "",
    user_ids: Optional[List[int]] = None,
) -> int:
    """Create a basic group, supergroup, or broadcast channel.

    Returns the new chat's marked peer_id.

    kind values:
      "group"        → basic group (CreateChatRequest, max ~200 members)
      "group_super"  → supergroup / megagroup (CreateChannelRequest megagroup=True)
      "channel"      → broadcast channel (CreateChannelRequest broadcast=True)
    """
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import functions, utils  # type: ignore

    user_ids = user_ids or []

    if kind == "group":
        # Resolve user entities
        users = []
        for uid in user_ids:
            try:
                users.append(await client.get_input_entity(uid))
            except Exception:
                pass
        if not users:
            raise ValueError("At least one user_id is required for a basic group")
        result = await client(
            functions.messages.CreateChatRequest(
                users=users,
                title=title,
            )
        )
        # result is Updates; find the Chat peer
        try:
            chats = getattr(result, "chats", [])
            if chats:
                return int(utils.get_peer_id(chats[0]))
        except Exception:
            pass
        return 0
    else:
        # Supergroup or broadcast channel
        megagroup = kind == "group_super"
        result = await client(
            functions.channels.CreateChannelRequest(
                title=title,
                about=about or "",
                megagroup=megagroup,
                broadcast=not megagroup,
            )
        )
        try:
            chats = getattr(result, "chats", [])
            if chats:
                return int(utils.get_peer_id(chats[0]))
        except Exception:
            pass
        return 0


async def get_contacts(username: str) -> List[Dict[str, Any]]:
    """Return the user's contact list."""
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import functions, utils  # type: ignore

    result = await client(functions.contacts.GetContactsRequest(hash=0))
    contacts: List[Dict[str, Any]] = []
    for u in getattr(result, "users", []):
        try:
            fname = getattr(u, "first_name", None) or ""
            lname = getattr(u, "last_name", None) or ""
            name = (fname + " " + lname).strip() or getattr(u, "username", None) or str(u.id)
            contacts.append({
                "id": int(utils.get_peer_id(u)),
                "name": name,
                "username": getattr(u, "username", None),
                "phone": getattr(u, "phone", None),
                "has_photo": u.photo is not None,
            })
        except Exception as exc:
            logger.debug("get_contacts item error: %s", exc)
    return contacts


async def save_draft(username: str, peer_id: int, text: str) -> None:
    """Save (or clear) a draft message for a dialog."""
    slug = _slug(username)
    client = await _get_client(slug)
    from telethon import functions  # type: ignore

    entity = await client.get_entity(peer_id)
    input_peer = await client.get_input_entity(entity)
    await client(
        functions.messages.SaveDraftRequest(
            peer=input_peer,
            message=text or "",
        )
    )
