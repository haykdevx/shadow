#!/usr/bin/env python3
"""Telegram remote for Shadow PC control with account-scoped pairing."""

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

from src.shadow_access import (
    ShadowAccessError,
    consume_telegram_pair_code,
    telegram_identity,
    unlink_telegram,
)
from src.shadow_pc import ShadowPcError, cancel_action, confirm_action, list_pending, request_action


BOT_TOKEN = os.getenv("SHADOW_TELEGRAM_BOT_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
HELP = """Shadow PC control
/status - linked PC status
/screen - send screenshot
/lock - request screen lock approval
/processes - top processes
/clip get - read clipboard
/clip set <text> - request clipboard write approval
/pending - list your approvals
/approve <id> - approve your pending action
/cancel <id> - cancel your pending action
/unlink - disconnect this Telegram account"""


def _allowlist() -> set[int]:
    values = os.getenv("SHADOW_TELEGRAM_ALLOWED_USER_IDS", "")
    out: set[int] = set()
    for value in values.split(","):
        try:
            out.add(int(value.strip()))
        except ValueError:
            pass
    return out


def _allowlisted(user_id: int) -> bool:
    configured = _allowlist()
    return not configured or user_id in configured


def _identity(user_id: int, permission: str) -> dict[str, Any] | None:
    if not _allowlisted(user_id):
        return None
    identity = telegram_identity(user_id)
    if not identity or not (identity.get("permissions") or {}).get(permission):
        return None
    return identity


def _tg(method: str, *, files: dict[str, Any] | None = None, **params: Any) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=httpx.Timeout(65.0, connect=8.0)) as client:
            response = client.post(
                f"{TG_API}/{method}",
                data=params if files else None,
                json=None if files else params,
                files=files,
            )
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
    _tg(
        "sendPhoto",
        chat_id=str(chat_id),
        caption="Shadow screen",
        files={"photo": ("shadow-screen.png", raw, payload.get("mime") or "image/png")},
    )


def _approval_keyboard(pending_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "Approve", "callback_data": f"pcok:{pending_id}"},
            {"text": "Cancel", "callback_data": f"pcno:{pending_id}"},
        ]]
    }


def _fmt_status(payload: dict[str, Any]) -> str:
    memory = payload.get("memory") or {}
    disk = payload.get("disk") or {}
    load = payload.get("load") or []
    lines = [
        "Shadow PC status",
        f"Host: {payload.get('hostname') or 'unknown'}",
        f"Platform: {payload.get('platform') or 'unknown'}",
        f"Uptime: {int(payload.get('uptime_seconds') or 0)}s",
    ]
    if load:
        lines.append(f"Load: {float(load[0]):.2f}")
    if memory.get("total"):
        lines.append(f"Memory: {memory.get('used', 0) / (1024 ** 3):.1f}/{memory.get('total', 0) / (1024 ** 3):.1f} GB")
    if disk.get("total"):
        lines.append(f"Disk free: {disk.get('free', 0) / (1024 ** 3):.1f}/{disk.get('total', 0) / (1024 ** 3):.1f} GB")
    return "\n".join(lines)


def _fmt_pending(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No pending PC actions."
    lines = ["Pending PC actions"]
    now = time.time()
    for row in rows:
        ttl = max(0, int(float(row.get("expires_at") or now) - now))
        lines.append(f"- {row.get('action')}  id={row.get('id')}  expires={ttl}s")
    return "\n".join(lines)


def _propose(chat_id: int, username: str, action: str, args: dict[str, Any] | None = None) -> None:
    try:
        result = request_action(
            action,
            args or {},
            requested_by=f"telegram:{username}",
            principal=username,
        )
        pending = result.get("pending")
        if pending:
            _send(
                chat_id,
                f"Approval required: {pending['action']}\nThis has NOT executed.\nid={pending['id']}",
                keyboard=_approval_keyboard(pending["id"]),
            )
        else:
            _send(chat_id, json.dumps(result, indent=2, sort_keys=True))
    except ShadowPcError as exc:
        _send(chat_id, f"PC action failed: {exc}")


def _handle_clip(chat_id: int, username: str, rest: str) -> None:
    sub, _, value = rest.partition(" ")
    sub = sub.lower().strip()
    if sub == "get":
        try:
            result = request_action(
                "clipboard_get",
                {},
                requested_by=f"telegram:{username}",
                principal=username,
            )
            _send(chat_id, result.get("text") or "[clipboard empty]")
        except ShadowPcError as exc:
            _send(chat_id, f"Clipboard read failed: {exc}")
    elif sub == "set" and value.strip():
        _propose(chat_id, username, "clipboard_set", {"text": value})
    else:
        _send(chat_id, "Usage: /clip get  OR  /clip set <text>")


def _handle_pair(chat_id: int, user_id: int, code: str) -> None:
    if not _allowlisted(user_id):
        _send(chat_id, "Not authorized user.")
        return
    try:
        result = consume_telegram_pair_code(code, user_id, chat_id)
        _send(chat_id, f"Paired with Shadow account {result['username']}.\n\n{HELP}")
    except ShadowAccessError:
        _send(chat_id, "Pairing code is invalid or expired.")


def _handle_message(message: dict[str, Any]) -> None:
    chat_id = int(message.get("chat", {}).get("id", 0))
    user_id = int(message.get("from", {}).get("id", 0))
    body = str(message.get("text") or "").strip()
    if not chat_id or not body:
        return

    cmd, _, rest = body.partition(" ")
    cmd = cmd.lower()
    if cmd == "/pair" and rest.strip():
        _handle_pair(chat_id, user_id, rest.strip())
        return

    permission = "approve" if cmd in {"/pending", "/approve", "/cancel"} else (
        "control" if cmd in {"/lock"} or (cmd == "/clip" and rest.strip().lower().startswith("set ")) else "view"
    )
    identity = _identity(user_id, permission)
    if not identity:
        _send(chat_id, "Not authorized user.")
        return
    username = identity["username"]

    if cmd in {"/start", "/help"}:
        _send(chat_id, HELP)
    elif cmd == "/status":
        try:
            result = request_action("status", {}, requested_by=f"telegram:{username}", principal=username)
            _send(chat_id, _fmt_status(result))
        except ShadowPcError as exc:
            _send(chat_id, f"PC status unavailable: {exc}")
    elif cmd == "/screen":
        try:
            result = request_action("screenshot", {}, requested_by=f"telegram:{username}", principal=username)
            _send_photo(chat_id, result)
        except ShadowPcError as exc:
            _send(chat_id, f"Screenshot failed: {exc}")
    elif cmd == "/lock":
        _propose(chat_id, username, "lock")
    elif cmd == "/processes":
        try:
            result = request_action("processes", {"limit": 12}, requested_by=f"telegram:{username}", principal=username)
            _send(chat_id, "\n".join(result.get("processes") or []) or "[no process output]")
        except ShadowPcError as exc:
            _send(chat_id, f"Process list failed: {exc}")
    elif cmd == "/clip":
        _handle_clip(chat_id, username, rest.strip())
    elif cmd == "/pending":
        _send(chat_id, _fmt_pending(list_pending(principal=username)))
    elif cmd == "/approve" and rest.strip():
        try:
            result = confirm_action(rest.strip(), principal=username)
            _send(chat_id, f"Executed: {result['action']}")
        except ShadowPcError as exc:
            _send(chat_id, f"Approval failed: {exc}")
    elif cmd == "/cancel" and rest.strip():
        try:
            result = cancel_action(rest.strip(), principal=username)
            _send(chat_id, f"Cancelled: {result['action']}")
        except ShadowPcError as exc:
            _send(chat_id, f"Cancel failed: {exc}")
    elif cmd == "/unlink":
        unlink_telegram(username, user_id)
        _send(chat_id, "Telegram account unlinked.")
    else:
        _send(chat_id, HELP)


def _handle_callback(callback: dict[str, Any]) -> None:
    callback_id = callback.get("id")
    user_id = int(callback.get("from", {}).get("id", 0))
    chat_id = int(callback.get("message", {}).get("chat", {}).get("id", 0))
    if callback_id:
        _tg("answerCallbackQuery", callback_query_id=callback_id)
    identity = _identity(user_id, "approve")
    if not identity:
        if chat_id:
            _send(chat_id, "Not authorized user.")
        return
    username = identity["username"]
    action, _, pending_id = str(callback.get("data") or "").partition(":")
    try:
        if action == "pcok":
            result = confirm_action(pending_id, principal=username)
            _send(chat_id, f"Executed: {result['action']}")
        elif action == "pcno":
            result = cancel_action(pending_id, principal=username)
            _send(chat_id, f"Cancelled: {result['action']}")
    except ShadowPcError as exc:
        _send(chat_id, f"Approval failed: {exc}")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set SHADOW_TELEGRAM_BOT_TOKEN")
    print("Shadow Telegram PC-control bridge online")
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
