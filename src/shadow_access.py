"""Per-account access control for Shadow's linked home PC and Telegram bridge."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
import secrets
import threading
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX development fallback
    fcntl = None

from core.atomic_io import atomic_write_json

ACCESS_PATH = Path(os.getenv("SHADOW_PC_ACCESS_PATH", "data/shadow-pc-access.json"))
PAIR_TTL_SECONDS = 10 * 60
VALID_PERMISSIONS = frozenset({"view", "control", "approve"})
_LOCK = threading.RLock()


class ShadowAccessError(RuntimeError):
    """A clean, user-facing access-control failure."""


@contextmanager
def _file_lock():
    """Serialize writes shared by the web and Telegram container processes."""
    ACCESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ACCESS_PATH.with_suffix(ACCESS_PATH.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _username(value: Any) -> str:
    return str(value or "").strip().lower()[:100]


def _default_state() -> dict[str, Any]:
    owner = _username(os.getenv("SHADOW_PC_OWNER"))
    return {
        "version": 1,
        "owner": owner,
        "grants": {},
        "requests": {},
        "telegram": {},
        "pair_codes": {},
    }


def _load() -> dict[str, Any]:
    state = _default_state()
    try:
        raw = json.loads(ACCESS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        raw = {}
    if isinstance(raw, dict):
        for key in ("owner", "grants", "requests", "telegram", "pair_codes"):
            if key in raw:
                state[key] = raw[key]
    state["owner"] = _username(state.get("owner") or os.getenv("SHADOW_PC_OWNER"))
    for key in ("grants", "requests", "telegram", "pair_codes"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    _purge_pair_codes(state)
    return state


def _save(state: dict[str, Any]) -> None:
    ACCESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(ACCESS_PATH), state, indent=2)


def _purge_pair_codes(state: dict[str, Any]) -> None:
    now = time.time()
    state["pair_codes"] = {
        code: row
        for code, row in (state.get("pair_codes") or {}).items()
        if isinstance(row, dict) and float(row.get("expires_at") or 0) > now
    }


def owner_username() -> str:
    with _LOCK:
        return _load()["owner"]


def claim_owner(username: str) -> dict[str, Any]:
    username = _username(username)
    if not username:
        raise ShadowAccessError("A real account is required")
    with _LOCK, _file_lock():
        state = _load()
        if state["owner"] and state["owner"] != username:
            raise ShadowAccessError("The linked PC already has an owner")
        state["owner"] = username
        state["requests"].pop(username, None)
        _save(state)
    return access_summary(username)


def permissions_for(username: str) -> dict[str, bool]:
    username = _username(username)
    with _LOCK:
        state = _load()
    if username and username == state["owner"]:
        return {"view": True, "control": True, "approve": True, "owner": True}
    raw = (state["grants"].get(username) or {}).get("permissions") or {}
    return {
        "view": bool(raw.get("view")),
        "control": bool(raw.get("control")),
        "approve": bool(raw.get("approve")),
        "owner": False,
    }


def require_permission(username: str, permission: str) -> str:
    username = _username(username)
    if permission not in VALID_PERMISSIONS:
        raise ShadowAccessError("Unknown PC permission")
    owner = owner_username()
    if not owner:
        raise ShadowAccessError("Linked-PC ownership is not configured")
    if not permissions_for(username).get(permission):
        raise ShadowAccessError(f"Your account does not have linked-PC {permission} permission")
    return username


def access_summary(username: str) -> dict[str, Any]:
    username = _username(username)
    with _LOCK:
        state = _load()
    perms = permissions_for(username)
    result = {
        "username": username,
        "configured": bool(state["owner"]),
        "permissions": perms,
        "request": state["requests"].get(username),
        "telegram_linked": any(
            _username(row.get("username")) == username
            for row in state["telegram"].values()
            if isinstance(row, dict)
        ),
    }
    if perms["owner"]:
        result["owner"] = state["owner"]
        result["grants"] = [
            {"username": name, **row}
            for name, row in sorted(state["grants"].items())
            if isinstance(row, dict)
        ]
        result["requests"] = [
            {"username": name, **row}
            for name, row in sorted(state["requests"].items())
            if isinstance(row, dict)
        ]
        result["telegram"] = [
            {"telegram_user_id": key, **row}
            for key, row in sorted(state["telegram"].items())
            if isinstance(row, dict)
        ]
    return result


def request_access(username: str, permissions: list[str] | None = None) -> dict[str, Any]:
    username = _username(username)
    if not username:
        raise ShadowAccessError("A real account is required")
    requested = _normalize_permissions(permissions or ["view"])
    with _LOCK, _file_lock():
        state = _load()
        if not state["owner"]:
            raise ShadowAccessError("Linked-PC ownership is not configured")
        if username == state["owner"]:
            return access_summary(username)
        row = {
            "permissions": requested,
            "requested_at": time.time(),
            "status": "pending",
        }
        state["requests"][username] = row
        _save(state)
    return row


def grant_access(owner: str, username: str, permissions: list[str]) -> dict[str, Any]:
    owner = _username(owner)
    username = _username(username)
    with _LOCK, _file_lock():
        state = _load()
        if not state["owner"] or owner != state["owner"]:
            raise ShadowAccessError("Only the linked-PC owner can grant access")
        if not username or username == owner:
            raise ShadowAccessError("Choose another real account")
        normalized = _normalize_permissions(permissions)
        row = {
            "permissions": normalized,
            "granted_by": owner,
            "granted_at": time.time(),
        }
        state["grants"][username] = row
        state["requests"].pop(username, None)
        _save(state)
    return {"username": username, **row}


def revoke_access(owner: str, username: str) -> dict[str, Any]:
    owner = _username(owner)
    username = _username(username)
    with _LOCK, _file_lock():
        state = _load()
        if not state["owner"] or owner != state["owner"]:
            raise ShadowAccessError("Only the linked-PC owner can revoke access")
        state["grants"].pop(username, None)
        state["requests"].pop(username, None)
        state["telegram"] = {
            key: row
            for key, row in state["telegram"].items()
            if _username((row or {}).get("username")) != username
        }
        _save(state)
    return {"ok": True, "username": username}


def create_telegram_pair_code(username: str) -> dict[str, Any]:
    username = require_permission(username, "view")
    code = "-".join([
        secrets.token_hex(2).upper(),
        secrets.token_hex(2).upper(),
        secrets.token_hex(2).upper(),
    ])
    now = time.time()
    with _LOCK, _file_lock():
        state = _load()
        state["pair_codes"][code] = {
            "username": username,
            "created_at": now,
            "expires_at": now + PAIR_TTL_SECONDS,
        }
        _save(state)
    return {"code": code, "expires_at": now + PAIR_TTL_SECONDS}


def consume_telegram_pair_code(code: str, telegram_user_id: int, chat_id: int) -> dict[str, Any]:
    code = str(code or "").strip().upper()
    key = str(int(telegram_user_id))
    with _LOCK, _file_lock():
        state = _load()
        row = state["pair_codes"].pop(code, None)
        if not row:
            raise ShadowAccessError("Pairing code is invalid or expired")
        username = require_permission(row.get("username"), "view")
        state["telegram"][key] = {
            "username": username,
            "chat_id": int(chat_id),
            "paired_at": time.time(),
        }
        _save(state)
    return {"username": username, "permissions": permissions_for(username)}


def telegram_identity(telegram_user_id: int) -> dict[str, Any] | None:
    with _LOCK:
        state = _load()
    row = state["telegram"].get(str(int(telegram_user_id)))
    if not isinstance(row, dict):
        return None
    username = _username(row.get("username"))
    return {"username": username, "permissions": permissions_for(username), **row}


def unlink_telegram(username: str, telegram_user_id: int | None = None) -> dict[str, Any]:
    username = _username(username)
    with _LOCK, _file_lock():
        state = _load()
        state["telegram"] = {
            key: row
            for key, row in state["telegram"].items()
            if not (
                _username((row or {}).get("username")) == username
                and (telegram_user_id is None or key == str(int(telegram_user_id)))
            )
        }
        _save(state)
    return {"ok": True}


def _normalize_permissions(values: list[str]) -> dict[str, bool]:
    requested = {str(value or "").strip().lower() for value in values}
    unknown = requested - VALID_PERMISSIONS
    if unknown:
        raise ShadowAccessError(f"Unknown permissions: {', '.join(sorted(unknown))}")
    if "approve" in requested:
        requested.update({"control", "view"})
    elif "control" in requested:
        requested.add("view")
    return {key: key in requested for key in sorted(VALID_PERMISSIONS)}
