"""Private home-PC bridge client with explicit confirmation for mutations."""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx


READ_ACTIONS = frozenset({"status", "processes", "screenshot", "clipboard_get"})
WRITE_ACTIONS = frozenset({
    "clipboard_set",
    "media",
    "volume",
    "app_launch",
    "app_focus",
    "app_close",
    "lock",
    "type_text",
    "keypress",
})
ALL_ACTIONS = READ_ACTIONS | WRITE_ACTIONS

_PENDING: dict[str, dict[str, Any]] = {}
_LOCK = threading.RLock()


class ShadowPcError(RuntimeError):
    """A clean, user-facing bridge failure."""


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _ttl_seconds() -> int:
    try:
        return max(30, min(int(os.getenv("SHADOW_PC_CONFIRM_TTL_SECONDS", "300")), 3600))
    except ValueError:
        return 300


def _agent_url() -> str:
    return os.getenv("SHADOW_HOME_AGENT_URL", "").strip().rstrip("/")


def _agent_token() -> str:
    return os.getenv("SHADOW_HOME_AGENT_TOKEN", "").strip()


def _private_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    tailscale = ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")
    return ip.is_loopback or ip.is_private or tailscale


def validate_agent_url(url: str) -> str:
    """Reject public home-agent endpoints unless the operator explicitly opts in."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ShadowPcError("SHADOW_HOME_AGENT_URL must use http or https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ShadowPcError("SHADOW_HOME_AGENT_URL must not contain credentials, query, or fragment")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ShadowPcError("SHADOW_HOME_AGENT_URL is missing a host")
    if _truthy("SHADOW_HOME_AGENT_ALLOW_PUBLIC"):
        return url.rstrip("/")
    if host == "localhost" or host.endswith(".ts.net") or _private_ip(host):
        return url.rstrip("/")
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, parsed.port or 80)}
    except OSError as exc:
        raise ShadowPcError(f"Cannot resolve private home-agent host {host}: {exc}") from exc
    if addresses and all(_private_ip(ip) for ip in addresses):
        return url.rstrip("/")
    raise ShadowPcError(
        "Refusing a public home-agent URL. Use loopback, a private address, or Tailscale; "
        "set SHADOW_HOME_AGENT_ALLOW_PUBLIC=true only if a separate secure tunnel enforces access."
    )


def configured() -> bool:
    return bool(_agent_url() and _agent_token())


def _clean_args(args: Any) -> dict[str, Any]:
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise ShadowPcError("Action args must be a JSON object")
    return args


def _call_home_agent(action: str, args: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
    if not configured():
        raise ShadowPcError("Home PC bridge is not configured")
    url = validate_agent_url(_agent_url())
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=4.0)) as client:
            response = client.post(
                f"{url}/v1/action",
                headers={"Authorization": f"Bearer {_agent_token()}"},
                json={"action": action, "args": args, "confirmed": confirmed},
            )
    except httpx.HTTPError as exc:
        raise ShadowPcError(f"Home PC bridge is unreachable: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise ShadowPcError(f"Home PC bridge returned HTTP {response.status_code}") from exc
    if response.status_code >= 400:
        raise ShadowPcError(str(payload.get("error") or payload.get("detail") or "Home PC action failed"))
    return payload


def _purge_expired() -> None:
    now = time.time()
    expired = [key for key, item in _PENDING.items() if item["expires_at"] <= now]
    for key in expired:
        _PENDING.pop(key, None)


def request_action(action: str, args: Any = None, *, requested_by: str = "web") -> dict[str, Any]:
    action = str(action or "").strip().lower()
    if action not in ALL_ACTIONS:
        raise ShadowPcError(f"Unsupported home PC action: {action or '(missing)'}")
    clean_args = _clean_args(args)
    if action in READ_ACTIONS:
        return _call_home_agent(action, clean_args, confirmed=False)
    now = time.time()
    item = {
        "id": secrets.token_urlsafe(12),
        "action": action,
        "args": clean_args,
        "requested_by": str(requested_by or "unknown")[:100],
        "created_at": now,
        "expires_at": now + _ttl_seconds(),
    }
    with _LOCK:
        _purge_expired()
        _PENDING[item["id"]] = item
    return {"status": "pending_confirmation", "pending": dict(item)}


def list_pending() -> list[dict[str, Any]]:
    with _LOCK:
        _purge_expired()
        return [dict(item) for item in sorted(_PENDING.values(), key=lambda row: row["created_at"])]


def confirm_action(pending_id: str) -> dict[str, Any]:
    with _LOCK:
        _purge_expired()
        item = _PENDING.pop(str(pending_id or ""), None)
    if not item:
        raise ShadowPcError("Pending action was not found or has expired")
    result = _call_home_agent(item["action"], item["args"], confirmed=True)
    return {"status": "executed", "action": item["action"], "result": result}


def cancel_action(pending_id: str) -> dict[str, Any]:
    with _LOCK:
        _purge_expired()
        item = _PENDING.pop(str(pending_id or ""), None)
    if not item:
        raise ShadowPcError("Pending action was not found or has expired")
    return {"status": "cancelled", "action": item["action"]}


def overview() -> dict[str, Any]:
    result: dict[str, Any] = {
        "configured": configured(),
        "online": False,
        "pending": list_pending(),
        "actions": {"read": sorted(READ_ACTIONS), "confirm": sorted(WRITE_ACTIONS)},
    }
    if not configured():
        return result
    try:
        result["pc"] = _call_home_agent("status", {}, confirmed=False)
        result["online"] = True
    except ShadowPcError as exc:
        result["error"] = str(exc)
    return result


def tool_action(content: str, *, requested_by: str) -> dict[str, Any]:
    """Parse the native agent-tool payload and return formatter-friendly output."""
    import json

    try:
        payload = json.loads(content or "{}")
    except json.JSONDecodeError:
        return {"error": "pc_control expects JSON arguments", "exit_code": 1}
    try:
        result = request_action(payload.get("action"), payload.get("args"), requested_by=requested_by)
    except ShadowPcError as exc:
        return {"error": str(exc), "exit_code": 1}
    if result.get("status") == "pending_confirmation":
        pending = result["pending"]
        return {
            "output": (
                f"Home PC action `{pending['action']}` is waiting for explicit approval. "
                f"Pending id: `{pending['id']}`. The action has NOT executed."
            ),
            "exit_code": 0,
            "pending_confirmation": pending,
        }
    return {"output": json.dumps(result, indent=2, sort_keys=True), "exit_code": 0, "result": result}

