#!/usr/bin/env python3
"""Discord remote for Shadow PC control with account-scoped pairing.

Mirrors scripts/shadow_telegram.py's command set and pairing model exactly
(/pair, /status, /screen, /lock, /processes, /clip, /browse, /pending,
/approve, /cancel, /unlink) over the official Discord Bot API — a real bot
account added by the user, never a self-bot / user-token client. Discord
gives bots no polling endpoint like Telegram's getUpdates, so this runs the
Gateway (WebSocket) protocol directly: identify, heartbeat, dispatch
MESSAGE_CREATE / INTERACTION_CREATE, and reconnect-with-resume on drops.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from src.shadow_access import (
    ShadowAccessError,
    consume_discord_pair_code,
    discord_identity,
    discord_identity_for_chat,
    unlink_discord,
)
from src.shadow_discord_store import record_message
from src.shadow_pc import ShadowPcError, cancel_action, confirm_action, list_pending, request_action


BOT_TOKEN = os.getenv("SHADOW_DISCORD_BOT_TOKEN", "").strip()
API = "https://discord.com/api/v10"
GATEWAY_VERSION = 10
# GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)
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
/unlink - disconnect this Discord account"""

_application_id: str | None = None


def _identity(user_id: int, permission: str = "view") -> dict[str, Any] | None:
    # Pairing itself is the authorization boundary, same rationale as Telegram:
    # a static deployment-wide allowlist would block legitimate accounts from
    # pairing their own Discord identity, so it's resolved account-by-account.
    return discord_identity(user_id)


async def _dc(client: httpx.AsyncClient, method: str, path: str, *, retry: bool = True, **kwargs: Any) -> httpx.Response:
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bot {BOT_TOKEN}"
    try:
        response = await client.request(method, f"{API}{path}", headers=headers, timeout=20, **kwargs)
    except httpx.HTTPError as exc:
        print(f"[shadow-discord] {method} {path}: {exc}")
        raise
    if response.status_code == 429 and retry:
        # Discord's per-route rate limit. One bounded wait-and-retry is enough
        # here — this bridge sends occasional replies, not bulk traffic.
        try:
            retry_after = float(response.json().get("retry_after", 1.0))
        except (ValueError, json.JSONDecodeError):
            retry_after = 1.0
        await asyncio.sleep(min(retry_after, 10.0) + 0.1)
        return await _dc(client, method, path, retry=False, headers=headers, **kwargs)
    return response


async def _application_id_cached(client: httpx.AsyncClient) -> str:
    global _application_id
    if _application_id:
        return _application_id
    response = await _dc(client, "GET", "/oauth2/applications/@me")
    _application_id = str((response.json() or {}).get("id") or "")
    return _application_id


async def _send(client: httpx.AsyncClient, channel_id: int, message: str, *, components: list[dict[str, Any]] | None = None) -> None:
    text = str(message or "[no output]")
    identity = discord_identity_for_chat(channel_id)
    while text:
        # Discord's message length cap is 2000 chars, well under Telegram's 4096.
        chunk, text = text[:1900], text[1900:]
        payload: dict[str, Any] = {"content": chunk}
        if components and not text:
            payload["components"] = components
        response = await _dc(client, "POST", f"/channels/{channel_id}/messages", json=payload)
        sent = response.json() if response.status_code < 300 else {}
        if identity and sent.get("id"):
            record_message(identity["username"], channel_id, "out", chunk, discord_message_id=int(sent["id"]))


async def _send_photo(client: httpx.AsyncClient, channel_id: int, payload: dict[str, Any]) -> None:
    import base64

    raw = base64.b64decode(payload.get("image_b64") or "")
    if not raw:
        raise ShadowPcError("Home PC returned an empty screenshot")
    files = {"files[0]": ("shadow-screen.png", raw, payload.get("mime") or "image/png")}
    data = {"payload_json": json.dumps({"content": "Shadow screen"})}
    response = await _dc(client, "POST", f"/channels/{channel_id}/messages", data=data, files=files)
    sent = response.json() if response.status_code < 300 else {}
    identity = discord_identity_for_chat(channel_id)
    if identity and sent.get("id"):
        record_message(identity["username"], channel_id, "out", "[Screen capture]", discord_message_id=int(sent["id"]))


def _approval_components(pending_id: str) -> list[dict[str, Any]]:
    return [{
        "type": 1,  # action row
        "components": [
            {"type": 2, "style": 3, "label": "Approve", "custom_id": f"pcok:{pending_id}"},  # success/green
            {"type": 2, "style": 4, "label": "Cancel", "custom_id": f"pcno:{pending_id}"},   # danger/red
        ],
    }]


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


async def _propose(client: httpx.AsyncClient, channel_id: int, username: str, action: str, args: dict[str, Any] | None = None) -> None:
    try:
        result = request_action(
            action,
            args or {},
            requested_by=f"discord:{username}",
            principal=username,
        )
        pending = result.get("pending")
        if pending:
            await _send(
                client, channel_id,
                f"Approval required: {pending['action']}\nThis has NOT executed.\nid={pending['id']}",
                components=_approval_components(pending["id"]),
            )
        else:
            await _send(client, channel_id, json.dumps(result, indent=2, sort_keys=True))
    except ShadowPcError as exc:
        await _send(client, channel_id, f"PC action failed: {exc}")


async def _handle_clip(client: httpx.AsyncClient, channel_id: int, username: str, rest: str) -> None:
    sub, _, value = rest.partition(" ")
    sub = sub.lower().strip()
    if sub == "get":
        try:
            result = request_action("clipboard_get", {}, requested_by=f"discord:{username}", principal=username)
            await _send(client, channel_id, result.get("text") or "[clipboard empty]")
        except ShadowPcError as exc:
            await _send(client, channel_id, f"Clipboard read failed: {exc}")
    elif sub == "set" and value.strip():
        await _propose(client, channel_id, username, "clipboard_set", {"text": value})
    else:
        await _send(client, channel_id, "Usage: /clip get  OR  /clip set <text>")


async def _handle_pair(client: httpx.AsyncClient, channel_id: int, user_id: int, code: str, message: dict[str, Any] | None = None) -> None:
    try:
        result = consume_discord_pair_code(code, user_id, channel_id)
        source = message or {}
        record_message(
            result["username"], channel_id, "in", str(source.get("content") or f"/pair {code}"),
            discord_message_id=int(source["id"]) if source.get("id") else None,
            chat=source.get("channel"), sender=source.get("author"),
        )
        await _send(client, channel_id, f"Paired with Shadow account {result['username']}.\n\n{HELP}")
    except ShadowAccessError:
        await _send(client, channel_id, "Pairing code is invalid or expired.")


async def _handle_browse(client: httpx.AsyncClient, channel_id: int, username: str, instruction: str) -> None:
    """Queue a browse task for the app's browser worker and relay the result.

    Same file-backed queue Telegram uses (src.browser_tasks) — this process
    only shares the data/ volume with the app, not an HTTP path to it.
    """
    from src.browser_tasks import enqueue, wait_result

    if not instruction:
        await _send(client, channel_id, "Usage: /browse <url or search terms>")
        return
    await _send(client, channel_id, "Browsing…")
    task_id = enqueue(username, instruction)
    result = await asyncio.to_thread(wait_result, task_id, timeout_seconds=90)
    if result is None:
        await _send(client, channel_id, "Browse task timed out — is the Shadow app running?")
        return
    if not result.get("ok"):
        await _send(client, channel_id, f"Browse failed: {result.get('error') or 'unknown error'}")
        return
    title = result.get("title") or result.get("url") or "page"
    text = (result.get("text") or "").strip()
    summary = text[:900] + ("…" if len(text) > 900 else "")
    await _send(client, channel_id, f"{title}\n{result.get('url') or ''}\n\n{summary}".strip())
    shot_path = result.get("shot_path") or ""
    if shot_path and Path(shot_path).is_file():
        try:
            files = {"files[0]": ("shadow-browse.jpg", Path(shot_path).read_bytes(), "image/jpeg")}
            data = {"payload_json": json.dumps({"content": str(title)[:200]})}
            response = await _dc(client, "POST", f"/channels/{channel_id}/messages", data=data, files=files)
            sent = response.json() if response.status_code < 300 else {}
            if sent.get("id"):
                record_message(username, channel_id, "out", "[Browse screenshot]", discord_message_id=int(sent["id"]))
        except OSError:
            pass  # screenshot is best-effort; the text result already went out


async def _handle_message(client: httpx.AsyncClient, message: dict[str, Any]) -> None:
    if (message.get("author") or {}).get("bot"):
        return  # never react to other bots (or ourselves) — avoids reply loops
    channel_id = int(message.get("channel_id") or 0)
    user_id = int((message.get("author") or {}).get("id") or 0)
    body = str(message.get("content") or "").strip()
    if not channel_id or not user_id or not body:
        return

    cmd, _, rest = body.partition(" ")
    cmd = cmd.lower()
    if cmd == "/pair" and rest.strip():
        await _handle_pair(client, channel_id, user_id, rest.strip(), message)
        return

    permission = "approve" if cmd in {"/pending", "/approve", "/cancel"} else (
        "control" if cmd in {"/lock"} or (cmd == "/clip" and rest.strip().lower().startswith("set ")) else "view"
    )
    identity = _identity(user_id, permission)
    if not identity:
        await _send(client, channel_id, "Not authorized user. Pair first with /pair <code> from Shadow's Command console.")
        return
    username = identity["username"]
    record_message(
        username, channel_id, "in", body,
        discord_message_id=int(message["id"]) if message.get("id") else None,
        chat=message.get("channel"), sender=message.get("author"),
    )

    if cmd in {"/start", "/help"}:
        await _send(client, channel_id, HELP)
    elif cmd == "/status":
        try:
            result = request_action("status", {}, requested_by=f"discord:{username}", principal=username)
            await _send(client, channel_id, _fmt_status(result))
        except ShadowPcError as exc:
            await _send(client, channel_id, f"PC status unavailable: {exc}")
    elif cmd == "/screen":
        try:
            result = request_action("screenshot", {}, requested_by=f"discord:{username}", principal=username)
            await _send_photo(client, channel_id, result)
        except ShadowPcError as exc:
            await _send(client, channel_id, f"Screenshot failed: {exc}")
    elif cmd == "/lock":
        await _propose(client, channel_id, username, "lock")
    elif cmd == "/processes":
        try:
            result = request_action("processes", {"limit": 12}, requested_by=f"discord:{username}", principal=username)
            await _send(client, channel_id, "\n".join(result.get("processes") or []) or "[no process output]")
        except ShadowPcError as exc:
            await _send(client, channel_id, f"Process list failed: {exc}")
    elif cmd == "/clip":
        await _handle_clip(client, channel_id, username, rest.strip())
    elif cmd == "/browse":
        await _handle_browse(client, channel_id, username, rest.strip())
    elif cmd == "/pending":
        await _send(client, channel_id, _fmt_pending(list_pending(principal=username)))
    elif cmd == "/approve" and rest.strip():
        try:
            result = confirm_action(rest.strip(), principal=username)
            await _send(client, channel_id, f"Executed: {result['action']}")
        except ShadowPcError as exc:
            await _send(client, channel_id, f"Approval failed: {exc}")
    elif cmd == "/cancel" and rest.strip():
        try:
            result = cancel_action(rest.strip(), principal=username)
            await _send(client, channel_id, f"Cancelled: {result['action']}")
        except ShadowPcError as exc:
            await _send(client, channel_id, f"Cancel failed: {exc}")
    elif cmd == "/unlink":
        unlink_discord(username, user_id)
        await _send(client, channel_id, "Discord account unlinked.")
    else:
        await _send(client, channel_id, HELP)


async def _handle_interaction(client: httpx.AsyncClient, interaction: dict[str, Any]) -> None:
    # Discord requires an ACK within 3 seconds. Deferred-update keeps the
    # original message (with its buttons) exactly as-is; the actual result
    # goes out as a normal followup message, matching Telegram's
    # answerCallbackQuery-then-send pattern.
    await _dc(
        client, "POST", f"/interactions/{interaction['id']}/{interaction['token']}/callback",
        json={"type": 6},
    )
    user = (interaction.get("member") or {}).get("user") or interaction.get("user") or {}
    user_id = int(user.get("id") or 0)
    channel_id = int(interaction.get("channel_id") or 0)
    identity = _identity(user_id, "approve")
    if not identity:
        if channel_id:
            await _send(client, channel_id, "Not authorized user.")
        return
    username = identity["username"]
    custom_id = str((interaction.get("data") or {}).get("custom_id") or "")
    action, _, pending_id = custom_id.partition(":")
    try:
        if action == "pcok":
            result = confirm_action(pending_id, principal=username)
            await _send(client, channel_id, f"Executed: {result['action']}")
        elif action == "pcno":
            result = cancel_action(pending_id, principal=username)
            await _send(client, channel_id, f"Cancelled: {result['action']}")
    except ShadowPcError as exc:
        await _send(client, channel_id, f"Approval failed: {exc}")


async def _dispatch_message(client: httpx.AsyncClient, message: dict[str, Any]) -> None:
    """Task entrypoint: surface handler crashes instead of dying silently."""
    try:
        await _handle_message(client, message)
    except Exception as exc:  # noqa: BLE001
        print(f"[shadow-discord] message handler failed: {exc!r}")
        channel_id = message.get("channel_id")
        if channel_id:
            try:
                await _send(client, int(channel_id), "Sorry — that command failed on the Shadow side.")
            except Exception:  # noqa: BLE001 — best-effort notification only
                pass


async def _dispatch_interaction(client: httpx.AsyncClient, interaction: dict[str, Any]) -> None:
    try:
        await _handle_interaction(client, interaction)
    except Exception as exc:  # noqa: BLE001
        print(f"[shadow-discord] interaction handler failed: {exc!r}")


async def _gateway_url(client: httpx.AsyncClient) -> str:
    try:
        response = await _dc(client, "GET", "/gateway/bot")
        url = (response.json() or {}).get("url")
        if url:
            return url
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        pass
    return "wss://gateway.discord.gg"


async def _run_connection(client: httpx.AsyncClient, url: str, resume: dict[str, Any] | None) -> dict[str, Any] | None:
    """One Gateway connection lifecycle. Returns resume state for the next
    attempt (session_id/seq/resume_url), or None to force a fresh identify."""
    seq: int | None = resume.get("seq") if resume else None
    session_id: str | None = resume.get("session_id") if resume else None
    resume_url: str | None = resume.get("resume_url") if resume else None

    async with websockets.connect(f"{(resume_url or url)}?v={GATEWAY_VERSION}&encoding=json", max_size=2**23) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("op") != 10:
            raise ConnectionClosed(None, None)
        heartbeat_interval = float(hello["d"]["heartbeat_interval"]) / 1000.0

        stop = asyncio.Event()
        last_ack = True

        async def _heartbeat() -> None:
            nonlocal last_ack
            await asyncio.sleep(heartbeat_interval * random.random())
            while not stop.is_set():
                if not last_ack:
                    return  # zombied connection — let the outer loop reconnect
                last_ack = False
                await ws.send(json.dumps({"op": 1, "d": seq}))
                await asyncio.sleep(heartbeat_interval)

        heartbeat_task = asyncio.create_task(_heartbeat())
        try:
            if session_id:
                await ws.send(json.dumps({"op": 6, "d": {"token": BOT_TOKEN, "session_id": session_id, "seq": seq}}))
            else:
                await ws.send(json.dumps({
                    "op": 2,
                    "d": {
                        "token": BOT_TOKEN,
                        "intents": INTENTS,
                        "properties": {"os": "linux", "browser": "shadow", "device": "shadow"},
                    },
                }))

            async for raw in ws:
                event = json.loads(raw)
                op = event.get("op")
                if event.get("s") is not None:
                    seq = event["s"]
                if op == 0:  # Dispatch
                    t = event.get("t")
                    d = event.get("d") or {}
                    if t == "READY":
                        session_id = d.get("session_id")
                        resume_url = d.get("resume_gateway_url") or resume_url
                        print(f"Shadow Discord PC-control bridge online as {(d.get('user') or {}).get('username', '?')}")
                    elif t == "RESUMED":
                        print("[shadow-discord] resumed session")
                    elif t == "MESSAGE_CREATE":
                        asyncio.create_task(_dispatch_message(client, d))
                    elif t == "INTERACTION_CREATE":
                        asyncio.create_task(_dispatch_interaction(client, d))
                elif op == 1:  # server asked for an immediate heartbeat
                    await ws.send(json.dumps({"op": 1, "d": seq}))
                elif op == 7:  # Reconnect — resume if we can
                    await ws.close()
                    break
                elif op == 9:  # Invalid Session
                    can_resume = bool(event.get("d"))
                    if not can_resume:
                        # Can't resume — a stale resume_gateway_url would just
                        # send the next attempt back into the same invalid
                        # session, so fall all the way back to a fresh identify
                        # against the plain gateway URL.
                        session_id = None
                        seq = None
                        resume_url = None
                    await asyncio.sleep(1 + random.random() * 4)
                    await ws.close()
                    break
                elif op == 11:  # Heartbeat ACK
                    last_ack = True
        finally:
            stop.set()
            heartbeat_task.cancel()

    return {"session_id": session_id, "seq": seq, "resume_url": resume_url}


async def _main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set SHADOW_DISCORD_BOT_TOKEN")
    delay = 2.0
    async with httpx.AsyncClient() as client:
        await _application_id_cached(client)
        url = await _gateway_url(client)
        resume: dict[str, Any] | None = None
        while True:
            try:
                resume = await _run_connection(client, url, resume)
                delay = 2.0  # a clean cycle resets backoff
            except (ConnectionClosed, OSError, asyncio.TimeoutError, KeyError) as exc:
                print(f"[shadow-discord] gateway dropped: {exc!r}; reconnecting in {delay:.0f}s")
                await asyncio.sleep(delay)
                delay = min(delay * 1.7, 30.0)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
