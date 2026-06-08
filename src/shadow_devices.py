"""Account-owned Shadow devices and durable outbound relay jobs.

Each browser account enrolls its own machines with a short-lived, single-use
code. Devices poll Shadow over HTTPS, so home machines do not expose inbound
ports. Device credentials are stored as SHA-256 hashes and every lookup is
scoped to the authenticated owner.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows server development fallback
    fcntl = None

from core.atomic_io import atomic_write_json


DEVICES_PATH = Path(os.getenv("SHADOW_DEVICES_PATH", "data/shadow-devices.json"))
JOBS_PATH = Path(os.getenv("SHADOW_DEVICE_JOBS_PATH", "data/shadow-device-jobs.json"))
ENROLL_TTL_SECONDS = 10 * 60
ONLINE_SECONDS = 90
JOB_TTL_SECONDS = 15 * 60
_LOCK = threading.RLock()


class ShadowDeviceError(RuntimeError):
    """A clean device enrollment or dispatch failure."""


def _owner(value: Any) -> str:
    return str(value or "").strip().lower()[:100]


def _device_id(value: Any) -> str:
    return str(value or "").strip()[:80]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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


def _load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return dict(default)
    return raw if isinstance(raw, dict) else dict(default)


def _save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(path), state, indent=2)


def _device_state() -> dict[str, Any]:
    state = _load(DEVICES_PATH, {"version": 1, "devices": {}, "enrollments": {}, "selected": {}})
    for key in ("devices", "enrollments", "selected"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    now = time.time()
    state["enrollments"] = {
        code: row
        for code, row in state["enrollments"].items()
        if isinstance(row, dict) and float(row.get("expires_at") or 0) > now
    }
    return state


def _legacy_device(owner: str) -> dict[str, Any] | None:
    legacy_owner = _owner(os.getenv("SHADOW_PC_OWNER"))
    token = os.getenv("SHADOW_HOME_AGENT_TOKEN", "").strip()
    target = os.getenv("SHADOW_HOME_AGENT_SOCKET", "").strip() or os.getenv("SHADOW_HOME_AGENT_URL", "").strip()
    if not owner or owner != legacy_owner or not target or len(token) < 32:
        return None
    return {
        "id": "legacy-home",
        "owner": owner,
        "name": os.getenv("SHADOW_LEGACY_DEVICE_NAME", "Home PC").strip() or "Home PC",
        "platform": "Linux",
        "transport": "legacy",
        "created_at": 0,
        "last_seen": time.time(),
        "online": True,
        "primary": True,
        "capabilities": [],
        "legacy": True,
    }


def _public_device(row: dict[str, Any], *, selected: bool = False) -> dict[str, Any]:
    last_seen = float(row.get("last_seen") or 0)
    return {
        "id": row.get("id"),
        "owner": row.get("owner"),
        "name": row.get("name") or "Shadow device",
        "platform": row.get("platform") or "Unknown",
        "transport": row.get("transport") or "relay",
        "created_at": row.get("created_at"),
        "last_seen": last_seen or None,
        "online": bool(row.get("legacy")) or (last_seen > 0 and time.time() - last_seen <= ONLINE_SECONDS),
        "selected": selected,
        "capabilities": row.get("capabilities") if isinstance(row.get("capabilities"), list) else [],
        "legacy": bool(row.get("legacy")),
    }


def create_enrollment(owner: str) -> dict[str, Any]:
    owner = _owner(owner)
    if not owner:
        raise ShadowDeviceError("A real Shadow account is required")
    code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
    now = time.time()
    with _LOCK, _file_lock(DEVICES_PATH):
        state = _device_state()
        state["enrollments"][code] = {
            "owner": owner,
            "created_at": now,
            "expires_at": now + ENROLL_TTL_SECONDS,
        }
        _save(DEVICES_PATH, state)
    return {"code": code, "expires_at": now + ENROLL_TTL_SECONDS}


def enroll_device(code: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    code = str(code or "").strip().upper()
    metadata = metadata if isinstance(metadata, dict) else {}
    with _LOCK, _file_lock(DEVICES_PATH):
        state = _device_state()
        enrollment = state["enrollments"].pop(code, None)
        if not enrollment:
            raise ShadowDeviceError("Enrollment code is invalid or expired")
        owner = _owner(enrollment.get("owner"))
        device_id = str(uuid.uuid4())
        token = "shddev_" + secrets.token_urlsafe(32)
        now = time.time()
        row = {
            "id": device_id,
            "owner": owner,
            "name": str(metadata.get("name") or metadata.get("hostname") or "Shadow device").strip()[:100],
            "platform": str(metadata.get("platform") or "Unknown").strip()[:100],
            "transport": "relay",
            "created_at": now,
            "last_seen": now,
            "token_hash": _hash_token(token),
            "token_prefix": token[:12],
            "capabilities": [
                str(value)[:60]
                for value in (metadata.get("capabilities") or [])
                if isinstance(value, str)
            ][:100],
        }
        state["devices"][device_id] = row
        state["selected"].setdefault(owner, device_id)
        _save(DEVICES_PATH, state)
    return {"device": _public_device(row, selected=True), "token": token}


def authenticate_device(token: str) -> dict[str, Any]:
    token = str(token or "").strip()
    if not token.startswith("shddev_") or len(token) < 24:
        raise ShadowDeviceError("Invalid device credential")
    digest = _hash_token(token)
    with _LOCK:
        state = _device_state()
    for row in state["devices"].values():
        if isinstance(row, dict) and secrets.compare_digest(str(row.get("token_hash") or ""), digest):
            return dict(row)
    raise ShadowDeviceError("Invalid device credential")


def touch_device(device_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    device_id = _device_id(device_id)
    metadata = metadata if isinstance(metadata, dict) else {}
    with _LOCK, _file_lock(DEVICES_PATH):
        state = _device_state()
        row = state["devices"].get(device_id)
        if not isinstance(row, dict):
            raise ShadowDeviceError("Device was not found")
        row["last_seen"] = time.time()
        if metadata.get("name"):
            row["name"] = str(metadata["name"]).strip()[:100]
        if metadata.get("platform"):
            row["platform"] = str(metadata["platform"]).strip()[:100]
        if isinstance(metadata.get("capabilities"), list):
            row["capabilities"] = [str(value)[:60] for value in metadata["capabilities"] if isinstance(value, str)][:100]
        _save(DEVICES_PATH, state)
        return _public_device(row)


def list_devices(owner: str) -> list[dict[str, Any]]:
    owner = _owner(owner)
    with _LOCK:
        state = _device_state()
    selected = _device_id(state["selected"].get(owner))
    rows = [
        _public_device(row, selected=row.get("id") == selected)
        for row in state["devices"].values()
        if isinstance(row, dict) and _owner(row.get("owner")) == owner
    ]
    legacy = _legacy_device(owner)
    if legacy:
        legacy["selected"] = not selected or selected == legacy["id"]
        rows.insert(0, legacy)
    if rows and not any(row.get("selected") for row in rows):
        rows[0]["selected"] = True
    return sorted(rows, key=lambda row: (not row.get("selected"), not row.get("online"), str(row.get("name") or "").lower()))


def get_device(owner: str, device_id: str | None = None) -> dict[str, Any]:
    owner = _owner(owner)
    if not owner:
        raise ShadowDeviceError("A real Shadow account is required")
    rows = list_devices(owner)
    wanted = _device_id(device_id)
    if wanted:
        row = next((item for item in rows if item.get("id") == wanted), None)
        if not row:
            raise ShadowDeviceError("That device does not belong to your account")
        return row
    row = next((item for item in rows if item.get("selected")), None) or (rows[0] if rows else None)
    if not row:
        raise ShadowDeviceError("No PC is connected to this account. Add a device in Command.")
    return row


def select_device(owner: str, device_id: str) -> dict[str, Any]:
    owner = _owner(owner)
    row = get_device(owner, device_id)
    with _LOCK, _file_lock(DEVICES_PATH):
        state = _device_state()
        state["selected"][owner] = row["id"]
        _save(DEVICES_PATH, state)
    return {**row, "selected": True}


def remove_device(owner: str, device_id: str) -> dict[str, Any]:
    owner = _owner(owner)
    device_id = _device_id(device_id)
    if device_id == "legacy-home":
        raise ShadowDeviceError("The migrated home PC is managed by server configuration")
    with _LOCK, _file_lock(DEVICES_PATH):
        state = _device_state()
        row = state["devices"].get(device_id)
        if not isinstance(row, dict) or _owner(row.get("owner")) != owner:
            raise ShadowDeviceError("That device does not belong to your account")
        state["devices"].pop(device_id, None)
        if state["selected"].get(owner) == device_id:
            state["selected"].pop(owner, None)
        _save(DEVICES_PATH, state)
    return {"ok": True, "id": device_id}


def _job_state() -> dict[str, Any]:
    state = _load(JOBS_PATH, {"version": 1, "jobs": {}})
    if not isinstance(state.get("jobs"), dict):
        state["jobs"] = {}
    cutoff = time.time() - JOB_TTL_SECONDS
    state["jobs"] = {
        key: row
        for key, row in state["jobs"].items()
        if isinstance(row, dict) and float(row.get("created_at") or 0) > cutoff
    }
    return state


def dispatch_action(
    owner: str,
    device_id: str,
    action: str,
    args: dict[str, Any],
    *,
    confirmed: bool,
    timeout: float = 25,
) -> dict[str, Any]:
    device = get_device(owner, device_id)
    if device.get("transport") != "relay":
        raise ShadowDeviceError("This device does not use the outbound relay")
    if not device.get("online"):
        raise ShadowDeviceError("Device is offline. Start the Shadow device service on that PC.")
    job_id = secrets.token_urlsafe(16)
    now = time.time()
    with _LOCK, _file_lock(JOBS_PATH):
        state = _job_state()
        state["jobs"][job_id] = {
            "id": job_id,
            "owner": _owner(owner),
            "device_id": device["id"],
            "action": str(action)[:60],
            "args": args if isinstance(args, dict) else {},
            "confirmed": bool(confirmed),
            "status": "queued",
            "created_at": now,
            "expires_at": now + max(5, min(float(timeout), 60)),
        }
        _save(JOBS_PATH, state)
    deadline = time.time() + max(5, min(float(timeout), 60))
    while time.time() < deadline:
        time.sleep(0.25)
        with _LOCK:
            state = _job_state()
            row = state["jobs"].get(job_id)
        if not row:
            break
        if row.get("status") == "completed":
            result = row.get("result")
            return result if isinstance(result, dict) else {"ok": True, "result": result}
        if row.get("status") == "failed":
            raise ShadowDeviceError(str(row.get("error") or "Device action failed"))
    raise ShadowDeviceError("Device did not answer before the action timeout")


def poll_job(device: dict[str, Any], timeout: float = 25) -> dict[str, Any] | None:
    device_id = _device_id(device.get("id"))
    deadline = time.time() + max(1, min(float(timeout), 30))
    touch_device(device_id)
    while time.time() < deadline:
        with _LOCK, _file_lock(JOBS_PATH):
            state = _job_state()
            now = time.time()
            candidates = sorted(
                (
                    row for row in state["jobs"].values()
                    if isinstance(row, dict)
                    and row.get("device_id") == device_id
                    and row.get("status") == "queued"
                    and float(row.get("expires_at") or 0) > now
                ),
                key=lambda row: float(row.get("created_at") or 0),
            )
            if candidates:
                row = candidates[0]
                row["status"] = "dispatched"
                row["dispatched_at"] = now
                _save(JOBS_PATH, state)
                return {
                    "id": row["id"],
                    "action": row["action"],
                    "args": row.get("args") or {},
                    "confirmed": row.get("confirmed") is True,
                }
            _save(JOBS_PATH, state)
        time.sleep(0.5)
    return None


def complete_job(device: dict[str, Any], job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job_id or "").strip()
    with _LOCK, _file_lock(JOBS_PATH):
        state = _job_state()
        row = state["jobs"].get(job_id)
        if not isinstance(row, dict) or row.get("device_id") != device.get("id"):
            raise ShadowDeviceError("Relay job was not found for this device")
        if payload.get("error"):
            row["status"] = "failed"
            row["error"] = str(payload.get("error"))[:1000]
        else:
            row["status"] = "completed"
            row["result"] = payload.get("result") if isinstance(payload.get("result"), dict) else {"ok": True}
        row["completed_at"] = time.time()
        _save(JOBS_PATH, state)
    touch_device(str(device.get("id")))
    return {"ok": True}
