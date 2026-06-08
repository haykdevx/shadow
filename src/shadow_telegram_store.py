"""Owner-scoped Telegram bot conversation history for the Shadow inbox."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from core.atomic_io import atomic_write_json


STORE_PATH = Path(os.getenv("SHADOW_TELEGRAM_STORE_PATH", "data/shadow-telegram-chats.json"))
MAX_MESSAGES_PER_CHAT = 1000
_LOCK = threading.RLock()


@contextmanager
def _file_lock():
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STORE_PATH.with_suffix(STORE_PATH.suffix + ".lock").open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _owner(value: Any) -> str:
    return str(value or "").strip().lower()[:100]


def _load() -> dict[str, Any]:
    try:
        state = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("chats"), dict):
        state["chats"] = {}
    return state


def _save(state: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(STORE_PATH), state, indent=2)


def _key(owner: str, chat_id: int) -> str:
    return f"{_owner(owner)}:{int(chat_id)}"


def record_message(
    owner: str,
    chat_id: int,
    direction: str,
    text: str,
    *,
    telegram_message_id: int | None = None,
    chat: dict[str, Any] | None = None,
    sender: dict[str, Any] | None = None,
) -> dict[str, Any]:
    owner = _owner(owner)
    if not owner:
        return {}
    chat = chat if isinstance(chat, dict) else {}
    sender = sender if isinstance(sender, dict) else {}
    now = time.time()
    message = {
        "id": str(uuid.uuid4()),
        "telegram_message_id": telegram_message_id,
        "direction": "out" if direction == "out" else "in",
        "text": str(text or "")[:50_000],
        "created_at": now,
        "sender": {
            "id": sender.get("id"),
            "name": " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip(),
            "username": sender.get("username"),
        },
    }
    key = _key(owner, chat_id)
    with _LOCK, _file_lock():
        state = _load()
        row = state["chats"].get(key)
        if not isinstance(row, dict):
            row = {
                "owner": owner,
                "chat_id": int(chat_id),
                "title": chat.get("title")
                or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])).strip()
                or chat.get("username")
                or f"Telegram {chat_id}",
                "username": chat.get("username"),
                "type": chat.get("type") or "private",
                "messages": [],
                "unread": 0,
            }
            state["chats"][key] = row
        messages = row.get("messages") if isinstance(row.get("messages"), list) else []
        if telegram_message_id is not None and any(
            item.get("telegram_message_id") == telegram_message_id and item.get("direction") == message["direction"]
            for item in messages
            if isinstance(item, dict)
        ):
            return message
        messages.append(message)
        row["messages"] = messages[-MAX_MESSAGES_PER_CHAT:]
        row["updated_at"] = now
        row["last_message"] = message["text"][:240]
        if message["direction"] == "in":
            row["unread"] = int(row.get("unread") or 0) + 1
        _save(state)
    return message


def list_chats(owner: str) -> list[dict[str, Any]]:
    owner = _owner(owner)
    with _LOCK:
        state = _load()
    rows = []
    for row in state["chats"].values():
        if not isinstance(row, dict) or _owner(row.get("owner")) != owner:
            continue
        rows.append({key: value for key, value in row.items() if key != "messages"})
    return sorted(rows, key=lambda row: float(row.get("updated_at") or 0), reverse=True)


def messages(owner: str, chat_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with _LOCK:
        state = _load()
    row = state["chats"].get(_key(owner, chat_id))
    if not isinstance(row, dict) or _owner(row.get("owner")) != _owner(owner):
        return []
    values = row.get("messages") if isinstance(row.get("messages"), list) else []
    return values[-max(1, min(int(limit), 500)):]


def owns_chat(owner: str, chat_id: int) -> bool:
    with _LOCK:
        state = _load()
    row = state["chats"].get(_key(owner, chat_id))
    return isinstance(row, dict) and _owner(row.get("owner")) == _owner(owner)


def mark_read(owner: str, chat_id: int) -> dict[str, Any]:
    key = _key(owner, chat_id)
    with _LOCK, _file_lock():
        state = _load()
        row = state["chats"].get(key)
        if not isinstance(row, dict) or _owner(row.get("owner")) != _owner(owner):
            return {"ok": False}
        row["unread"] = 0
        _save(state)
    return {"ok": True}
