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
    telegram_identity_for_chat,
    unlink_telegram,
)
from src.shadow_telegram_store import record_message
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
/browse <url or search> - run a private browser task (summary + screenshot)
/pending - list your approvals
/approve <id> - approve your pending action
/cancel <id> - cancel your pending action
/unlink - disconnect this Telegram account"""


def _identity(user_id: int, permission: str = "view") -> dict[str, Any] | None:
    # Pairing itself is the authorization boundary. A static deployment-wide
    # allowlist would prevent legitimate Shadow accounts from pairing their own
    # Telegram identity, so linked identities are resolved account-by-account.
    return telegram_identity(user_id)


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
    identity = telegram_identity_for_chat(chat_id)
    while text:
        chunk, text = text[:3900], text[3900:]
        params: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if keyboard and not text:
            params["reply_markup"] = keyboard
        sent = _tg("sendMessage", **params)
        if identity and sent.get("ok"):
            result = sent.get("result") or {}
            record_message(identity["username"], chat_id, "out", chunk, telegram_message_id=result.get("message_id"))


def _send_photo(chat_id: int, payload: dict[str, Any]) -> None:
    raw = base64.b64decode(payload.get("image_b64") or "")
    if not raw:
        raise ShadowPcError("Home PC returned an empty screenshot")
    sent = _tg(
        "sendPhoto",
        chat_id=str(chat_id),
        caption="Shadow screen",
        files={"photo": ("shadow-screen.png", raw, payload.get("mime") or "image/png")},
    )
    identity = telegram_identity_for_chat(chat_id)
    if identity and sent.get("ok"):
        result = sent.get("result") or {}
        record_message(identity["username"], chat_id, "out", "[Screen capture]", telegram_message_id=result.get("message_id"))


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


def _handle_pair(chat_id: int, user_id: int, code: str, message: dict[str, Any] | None = None) -> None:
    try:
        result = consume_telegram_pair_code(code, user_id, chat_id)
        source = message or {}
        record_message(result["username"], chat_id, "in", str(source.get("text") or f"/pair {code}"), telegram_message_id=source.get("message_id"), chat=source.get("chat"), sender=source.get("from"))
        _send(chat_id, f"Paired with Shadow account {result['username']}.\n\n{HELP}")
    except ShadowAccessError:
        _send(chat_id, "Pairing code is invalid or expired.")


def _handle_browse(chat_id: int, username: str, instruction: str) -> None:
    """Queue a browse task for the app's browser worker and relay the result.

    The bot only shares the data/ volume with the app, so this goes through
    the file-backed queue in src.browser_tasks rather than HTTP.
    """
    from src.browser_tasks import enqueue, wait_result

    if not instruction:
        _send(chat_id, "Usage: /browse <url or search terms>")
        return
    _send(chat_id, "Browsing…")
    task_id = enqueue(username, instruction)
    result = wait_result(task_id, timeout_seconds=90)
    if result is None:
        _send(chat_id, "Browse task timed out — is the Shadow app running?")
        return
    if not result.get("ok"):
        _send(chat_id, f"Browse failed: {result.get('error') or 'unknown error'}")
        return
    title = result.get("title") or result.get("url") or "page"
    text = (result.get("text") or "").strip()
    summary = text[:900] + ("…" if len(text) > 900 else "")
    _send(chat_id, f"{title}\n{result.get('url') or ''}\n\n{summary}".strip())
    shot_path = result.get("shot_path") or ""
    if shot_path and Path(shot_path).is_file():
        try:
            sent = _tg(
                "sendPhoto",
                chat_id=str(chat_id),
                caption=str(title)[:200],
                files={"photo": ("shadow-browse.jpg", Path(shot_path).read_bytes(), "image/jpeg")},
            )
            if sent.get("ok"):
                record_message(username, chat_id, "out", "[Browse screenshot]",
                               telegram_message_id=(sent.get("result") or {}).get("message_id"))
        except OSError:
            pass  # screenshot is best-effort; the text result already went out


def _handle_message(message: dict[str, Any]) -> None:
    chat_id = int(message.get("chat", {}).get("id", 0))
    user_id = int(message.get("from", {}).get("id", 0))
    body = str(message.get("text") or "").strip()
    if not chat_id or not body:
        return

    cmd, _, rest = body.partition(" ")
    cmd = cmd.lower()
    if cmd == "/pair" and rest.strip():
        _handle_pair(chat_id, user_id, rest.strip(), message)
        return

    permission = "approve" if cmd in {"/pending", "/approve", "/cancel"} else (
        "control" if cmd in {"/lock"} or (cmd == "/clip" and rest.strip().lower().startswith("set ")) else "view"
    )
    identity = _identity(user_id, permission)
    if not identity:
        _send(chat_id, "Not authorized user.")
        return
    username = identity["username"]
    record_message(username, chat_id, "in", body, telegram_message_id=message.get("message_id"), chat=message.get("chat"), sender=message.get("from"))

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
    elif cmd == "/browse":
        _handle_browse(chat_id, username, rest.strip())
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


def _dispatch_message(message: dict[str, Any]) -> None:
    """Thread entrypoint: surface handler crashes instead of dying silently."""
    try:
        _handle_message(message)
    except Exception as exc:  # noqa: BLE001
        print(f"[shadow-telegram] message handler failed: {exc!r}")
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id:
            try:
                _send(int(chat_id), "Sorry — that command failed on the Shadow side.")
            except Exception:  # noqa: BLE001 — best-effort notification only
                pass


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
            try:
                offset = max(offset, int(update.get("update_id") or 0) + 1)
            except (TypeError, ValueError):
                offset += 1  # poison update_id: still move past it
            # One malformed update must never take down the whole bridge.
            try:
                if update.get("callback_query"):
                    _handle_callback(update["callback_query"])
                elif update.get("message"):
                    threading.Thread(target=_dispatch_message, args=(update["message"],), daemon=True).start()
            except Exception as exc:  # noqa: BLE001
                print(f"[shadow-telegram] update {update.get('update_id')} failed: {exc!r}")


if __name__ == "__main__":
    main()
