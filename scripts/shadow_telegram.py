#!/usr/bin/env python3
"""Telegram remote for Shadow chat and explicitly approved home-PC actions."""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from src.shadow_pc import ShadowPcError, cancel_action, confirm_action, list_pending, request_action


BOT_TOKEN = os.getenv("SHADOW_TELEGRAM_BOT_TOKEN", "").strip()
APP_URL = os.getenv("SHADOW_APP_URL", "http://127.0.0.1:7000").strip().rstrip("/")
APP_TOKEN = os.getenv("SHADOW_APP_API_TOKEN", "").strip()
STATE_PATH = Path(os.getenv("SHADOW_TELEGRAM_STATE", "data/shadow_telegram_sessions.json"))
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
_STATE_LOCK = threading.Lock()


def _allowed() -> set[int]:
    values = os.getenv("SHADOW_TELEGRAM_ALLOWED_USER_IDS", "")
    out = set()
    for value in values.split(","):
        try:
            out.add(int(value.strip()))
        except ValueError:
            pass
    return out


def _load_sessions() -> dict[str, str]:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_sessions(rows: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_PATH)


def _get_session(chat_id: int) -> str | None:
    with _STATE_LOCK:
        return _load_sessions().get(str(chat_id))


def _set_session(chat_id: int, session_id: str) -> None:
    with _STATE_LOCK:
        sessions = _load_sessions()
        sessions[str(chat_id)] = session_id
        _save_sessions(sessions)


def _clear_session(chat_id: int) -> None:
    with _STATE_LOCK:
        sessions = _load_sessions()
        sessions.pop(str(chat_id), None)
        _save_sessions(sessions)


def _tg(method: str, *, files: dict[str, Any] | None = None, **params: Any) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=httpx.Timeout(65.0, connect=8.0)) as client:
            response = client.post(f"{TG_API}/{method}", data=params if files else None, json=None if files else params, files=files)
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[shadow-telegram] {method}: {exc}")
        return {"ok": False}


def _send(chat_id: int, message: str, *, keyboard: dict[str, Any] | None = None) -> None:
    text = str(message or "[no output]")
    while text:
        chunk, text = text[:3900], text[3900:]
        params: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if keyboard and not text:
            params["reply_markup"] = keyboard
        _tg("sendMessage", **params)


def _send_photo(chat_id: int, payload: dict[str, Any]) -> None:
    raw = base64.b64decode(payload.get("image_b64") or "")
    if not raw:
        raise ShadowPcError("Home PC returned an empty screenshot")
    _tg("sendPhoto", chat_id=str(chat_id), files={"photo": ("shadow-screen.png", raw, payload.get("mime") or "image/png")})


def _approval_keyboard(pending_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "Approve", "callback_data": f"pcok:{pending_id}"},
            {"text": "Cancel", "callback_data": f"pcno:{pending_id}"},
        ]]
    }


def _propose(chat_id: int, action: str, args: dict[str, Any] | None = None) -> None:
    try:
        result = request_action(action, args or {}, requested_by=f"telegram:{chat_id}")
        pending = result.get("pending")
        if pending:
            _send(chat_id, f"Approval required: {pending['action']}\nThe action has not executed.", keyboard=_approval_keyboard(pending["id"]))
        else:
            _send(chat_id, json.dumps(result, indent=2, sort_keys=True))
    except ShadowPcError as exc:
        _send(chat_id, f"PC action failed: {exc}")


def _chat(chat_id: int, message: str) -> None:
    if not APP_TOKEN.startswith("ody_"):
        _send(chat_id, "Shadow chat bridge is not configured.")
        return
    session = _get_session(chat_id)
    body: dict[str, Any] = {"message": message}
    if session:
        body["session"] = session
    try:
        with httpx.Client(timeout=httpx.Timeout(140.0, connect=8.0)) as client:
            response = client.post(f"{APP_URL}/api/v1/chat", headers={"Authorization": f"Bearer {APP_TOKEN}"}, json=body)
            payload = response.json()
        if response.status_code >= 400:
            _send(chat_id, f"Shadow chat failed: {payload.get('detail') or payload.get('error') or response.status_code}")
            return
        if payload.get("session_id"):
            _set_session(chat_id, payload["session_id"])
        _send(chat_id, payload.get("response") or "[empty response]")
    except (httpx.HTTPError, ValueError) as exc:
        _send(chat_id, f"Shadow chat is unavailable: {exc}")


HELP = """Shadow remote
/ask <message> - talk to your configured Shadow model
/status - linked home PC summary
/screen - capture the linked home PC screen
/lock - request screen lock
/pending - list pending PC approvals
/new - start a fresh Telegram chat session
Plain text is sent to Shadow chat."""


def _handle_message(message: dict[str, Any]) -> None:
    chat_id = int(message.get("chat", {}).get("id", 0))
    user_id = int(message.get("from", {}).get("id", 0))
    body = str(message.get("text") or "").strip()
    if not chat_id or not body:
        return
    if user_id not in _allowed():
        _send(chat_id, "Not authorized user.")
        return
    cmd, _, rest = body.partition(" ")
    cmd = cmd.lower()
    if cmd in {"/start", "/help"}:
        _send(chat_id, HELP)
    elif cmd == "/status":
        try:
            _send(chat_id, json.dumps(request_action("status", {}, requested_by=f"telegram:{chat_id}"), indent=2, sort_keys=True))
        except ShadowPcError as exc:
            _send(chat_id, f"PC status unavailable: {exc}")
    elif cmd == "/screen":
        try:
            _send_photo(chat_id, request_action("screenshot", {}, requested_by=f"telegram:{chat_id}"))
        except ShadowPcError as exc:
            _send(chat_id, f"Screenshot failed: {exc}")
    elif cmd == "/lock":
        _propose(chat_id, "lock")
    elif cmd == "/pending":
        rows = list_pending()
        _send(chat_id, "\n".join(f"- {row['action']} ({row['id']})" for row in rows) or "No pending PC actions.")
    elif cmd == "/new":
        _clear_session(chat_id)
        _send(chat_id, "Started a fresh Shadow chat.")
    elif cmd == "/ask":
        _chat(chat_id, rest.strip()) if rest.strip() else _send(chat_id, "Usage: /ask <message>")
    elif cmd.startswith("/"):
        _send(chat_id, HELP)
    else:
        _chat(chat_id, body)


def _handle_callback(callback: dict[str, Any]) -> None:
    callback_id = callback.get("id")
    user_id = int(callback.get("from", {}).get("id", 0))
    chat_id = int(callback.get("message", {}).get("chat", {}).get("id", 0))
    if callback_id:
        _tg("answerCallbackQuery", callback_query_id=callback_id)
    if user_id not in _allowed():
        if chat_id:
            _send(chat_id, "Not authorized user.")
        return
    action, _, pending_id = str(callback.get("data") or "").partition(":")
    try:
        if action == "pcok":
            result = confirm_action(pending_id)
            _send(chat_id, f"Executed: {result['action']}")
        elif action == "pcno":
            result = cancel_action(pending_id)
            _send(chat_id, f"Cancelled: {result['action']}")
    except ShadowPcError as exc:
        _send(chat_id, f"Approval failed: {exc}")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set SHADOW_TELEGRAM_BOT_TOKEN")
    if not _allowed():
        raise SystemExit("Set SHADOW_TELEGRAM_ALLOWED_USER_IDS")
    print("Shadow Telegram bridge online")
    offset = 0
    while True:
        payload = _tg("getUpdates", timeout=30, offset=offset, allowed_updates=json.dumps(["message", "callback_query"]))
        if not payload.get("ok"):
            time.sleep(3)
            continue
        for update in payload.get("result", []):
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            if update.get("callback_query"):
                _handle_callback(update["callback_query"])
            elif update.get("message"):
                threading.Thread(target=_handle_message, args=(update["message"],), daemon=True).start()


if __name__ == "__main__":
    main()

