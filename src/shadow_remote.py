"""Account-scoped MeshCentral integration for Shadow Remote Desktop.

MeshCentral remains isolated behind an internal gateway. Shadow stores only a
random per-user MeshCentral password, encrypted with the existing application
key, and exposes short-lived login tokens to authenticated browser sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows server development fallback
    fcntl = None

from core.atomic_io import atomic_write_json
from src.secret_storage import decrypt, encrypt


REMOTE_STATE_PATH = Path(os.getenv("SHADOW_REMOTE_STATE_PATH", "data/shadow-remote.json"))
_LOCK = threading.RLock()


class ShadowRemoteError(RuntimeError):
    """A clean remote-desktop provisioning or launch failure."""


def _owner(value: Any) -> str:
    return str(value or "").strip().lower()[:100]


def _enabled() -> bool:
    return os.getenv("SHADOW_MESH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _gateway_url() -> str:
    return os.getenv("SHADOW_MESH_GATEWAY_URL", "").strip().rstrip("/")


def _gateway_key() -> str:
    return os.getenv("SHADOW_MESH_GATEWAY_KEY", "").strip()


def _public_prefix() -> str:
    prefix = os.getenv("SHADOW_MESH_PUBLIC_PATH", "/remote/").strip() or "/remote/"
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix


def configured() -> bool:
    return _enabled() and bool(_gateway_url()) and len(_gateway_key()) >= 32


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_state() -> dict[str, Any]:
    try:
        value = json.loads(REMOTE_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    value.setdefault("version", 1)
    if not isinstance(value.get("accounts"), dict):
        value["accounts"] = {}
    return value


def _save_state(state: dict[str, Any]) -> None:
    REMOTE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(REMOTE_STATE_PATH), state, indent=2)


def _identity(owner: str) -> tuple[str, str]:
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:20]
    return f"shd_{digest}", f"Shadow {digest}"


def _request(path: str, payload: dict[str, Any] | None = None, *, timeout: float = 35) -> dict[str, Any]:
    if not configured():
        raise ShadowRemoteError("Remote Desktop is not enabled on this Shadow server")
    try:
        response = httpx.post(
            f"{_gateway_url()}{path}",
            json=payload or {},
            headers={"X-Shadow-Gateway-Key": _gateway_key()},
            timeout=timeout,
        )
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ShadowRemoteError(f"Remote Desktop gateway unavailable: {exc}") from exc
    if response.status_code >= 400 or not data.get("ok"):
        message = data.get("error") if isinstance(data, dict) else None
        raise ShadowRemoteError(str(message or f"Remote Desktop gateway returned HTTP {response.status_code}"))
    return data


def _stored_account(owner: str) -> dict[str, Any] | None:
    owner = _owner(owner)
    with _LOCK:
        row = _load_state()["accounts"].get(owner)
    return dict(row) if isinstance(row, dict) else None


def ensure_account(owner: str) -> dict[str, Any]:
    owner = _owner(owner)
    if not owner:
        raise ShadowRemoteError("A real Shadow account is required")
    if not configured():
        raise ShadowRemoteError("Remote Desktop is not enabled on this Shadow server")

    row = _stored_account(owner)
    username, group = _identity(owner)
    if row:
        password = decrypt(str(row.get("password") or ""))
        if not password:
            raise ShadowRemoteError("Remote Desktop credentials could not be decrypted; restore data/.app_key")
        username = str(row.get("username") or username)
        group = str(row.get("group") or group)
    else:
        password = secrets.token_urlsafe(36)

    _request("/provision", {
        "username": username,
        "password": password,
        "group": group,
    })

    if not row:
        now = time.time()
        row = {
            "username": username,
            "password": encrypt(password),
            "group": group,
            "created_at": now,
            "updated_at": now,
        }
        with _LOCK, _file_lock(REMOTE_STATE_PATH):
            state = _load_state()
            existing = state["accounts"].get(owner)
            if isinstance(existing, dict):
                row = existing
            else:
                state["accounts"][owner] = row
                _save_state(state)
    return {
        "username": str(row.get("username") or username),
        "password": decrypt(str(row.get("password") or "")),
        "group": str(row.get("group") or group),
    }


def status(owner: str) -> dict[str, Any]:
    row = _stored_account(owner)
    return {
        "configured": configured(),
        "account_ready": bool(row),
        "public_path": _public_prefix(),
        "security": {
            "account_scoped": True,
            "short_lived_login": True,
            "desktop_only": True,
        },
    }


def _same_origin_path(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    path = parsed.path or ""
    prefix = _public_prefix()
    if not path.startswith(prefix):
        raise ShadowRemoteError("Remote Desktop returned an invalid public URL")
    return path + (f"?{parsed.query}" if parsed.query else "")


def create_invite(owner: str, *, hours: int = 24) -> dict[str, Any]:
    account = ensure_account(owner)
    data = _request("/invite", {
        "group": account["group"],
        "hours": max(1, min(int(hours), 168)),
    })
    return {
        "invite_url": _same_origin_path(str(data.get("invite_url") or "")),
        "expires_in_hours": max(1, min(int(hours), 168)),
    }


def create_session(owner: str) -> dict[str, Any]:
    account = ensure_account(owner)
    data = _request("/session", {
        "username": account["username"],
        "password": account["password"],
        "minutes": 3,
    })
    token_user = str(data.get("token_user") or "")
    token_pass = str(data.get("token_pass") or "")
    if not token_user.startswith("~t:") or len(token_pass) < 12:
        raise ShadowRemoteError("Remote Desktop returned an invalid login token")
    return {
        "login_url": _public_prefix() + "login",
        "token_user": token_user,
        "token_pass": token_pass,
        "expires_in_seconds": 180,
    }


def agent_config(owner: str) -> dict[str, Any]:
    """Return what a device needs to install the remote-desktop agent itself.

    The enrollment installer runs on the target machine with only its device
    token, so it cannot open MeshCentral's invite page. This provisions the
    account (idempotently) and hands back the device-group id that
    MeshCentral's own agent installer and /meshsettings endpoint take,
    scoped to the owner the device belongs to.
    """
    account = ensure_account(owner)
    data = _request("/groupid", {"group": account["group"]})
    group_id = str(data.get("group_id") or "").strip()
    if not group_id:
        raise ShadowRemoteError("Remote Desktop did not return a device group id")
    prefix = _public_prefix()
    return {
        "group_id": group_id,
        # Path only — the installer joins it to the server it enrolled against,
        # so this works behind any hostname or reverse proxy.
        "public_path": prefix,
        "agent_settings_path": f"{prefix}meshsettings",
        "agent_script_path": f"{prefix}meshagents?script=1",
        "agent_binary_path": f"{prefix}meshagents",
    }


def list_remote_devices(owner: str) -> list[dict[str, Any]]:
    account = ensure_account(owner)
    data = _request("/devices", {
        "username": account["username"],
        "password": account["password"],
    })
    rows = data.get("devices")
    if not isinstance(rows, list):
        return []
    clean: list[dict[str, Any]] = []
    for row in rows[:200]:
        if not isinstance(row, dict):
            continue
        clean.append({
            "id": str(row.get("_id") or row.get("id") or "")[:200],
            "name": str(row.get("name") or row.get("hostname") or "Remote PC")[:120],
            "os": str(row.get("osdesc") or row.get("os") or "Unknown OS")[:160],
            "connected": bool(row.get("conn") or row.get("connected")),
        })
    return clean
