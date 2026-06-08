"""Private home-PC bridge client with explicit confirmation for mutations."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


READ_ACTIONS = frozenset({
    "status",
    "processes",
    "screenshot",
    "clipboard_get",
    "windows",
    "file_list",
    "file_read",
    "file_search",
})
WRITE_ACTIONS = frozenset({
    "clipboard_set",
    "media",
    "volume",
    "app_launch",
    "app_focus",
    "app_close",
    "kill_process",
    "shell",
    "file_write",
    "lock",
    "sleep",
    "shutdown",
    "type_text",
    "keypress",
    "mouse_move",
    "mouse_click",
    "runbook",
})
ALL_ACTIONS = READ_ACTIONS | WRITE_ACTIONS

AUDIT_PATH = Path(os.getenv("SHADOW_PC_AUDIT_PATH", "data/shadow-pc-audit.jsonl"))
RUNBOOKS_PATH = Path(os.getenv("SHADOW_PC_RUNBOOKS_PATH", "data/shadow-runbooks.json"))
MAX_AUDIT_LINES = 500
MAX_RUNBOOK_STEPS = 16
STATUS_CACHE_SECONDS = float(os.getenv("SHADOW_PC_STATUS_CACHE_SECONDS", "3"))

RUNBOOKS: dict[str, dict[str, Any]] = {
    "health_report": {
        "label": "Health report",
        "description": "Refresh status, processes, windows, screenshot, and clipboard status.",
        "steps": [
            {"action": "status", "args": {}},
            {"action": "processes", "args": {"limit": 12}},
            {"action": "windows", "args": {}},
            {"action": "screenshot", "args": {}},
            {"action": "clipboard_get", "args": {}},
        ],
    },
    "lock_report": {
        "label": "Lock and report",
        "description": "Capture status/screen, then lock the PC.",
        "steps": [
            {"action": "status", "args": {}},
            {"action": "screenshot", "args": {}},
            {"action": "lock", "args": {}},
        ],
    },
    "work_mode": {
        "label": "Work mode",
        "description": "Open the allowlisted terminal and browser, then set volume to 40%.",
        "steps": [
            {"action": "app_launch", "args": {"app": "terminal"}},
            {"action": "app_launch", "args": {"app": "browser"}},
            {"action": "volume", "args": {"percent": 40}},
        ],
    },
}

_ACTION_RISK = {
    "status": "low",
    "processes": "low",
    "screenshot": "low",
    "clipboard_get": "low",
    "windows": "low",
    "file_list": "low",
    "file_read": "low",
    "file_search": "low",
    "clipboard_set": "medium",
    "media": "medium",
    "volume": "medium",
    "app_launch": "medium",
    "app_focus": "medium",
    "type_text": "medium",
    "keypress": "medium",
    "mouse_move": "medium",
    "mouse_click": "medium",
    "app_close": "high",
    "kill_process": "high",
    "file_write": "high",
    "shell": "high",
    "lock": "high",
    "sleep": "critical",
    "shutdown": "critical",
    "runbook": "high",
}
_RISK_WEIGHT = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_DANGEROUS_SHELL = re.compile(
    r"(?:^|[\s;&|()])(?:sudo|su|rm|rmdir|mv|chmod|chown|dd|mkfs|mount|umount|kill|killall|pkill|shutdown|reboot|poweroff|systemctl|service|:>)\b|(?:^|[\s;&|()])rm\s+-[^\n]*[rf]",
    re.IGNORECASE,
)
_OVERVIEW_CACHE: dict[str, Any] = {"ts": 0.0, "pc": None}
_OVERVIEW_LOCK = threading.RLock()

_PENDING: dict[str, dict[str, Any]] = {}
_LOCK = threading.RLock()
_AUDIT_LOCK = threading.RLock()


class ShadowPcError(RuntimeError):
    """A clean, user-facing bridge failure."""


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _demo_enabled() -> bool:
    return _truthy("SHADOW_DEMO_MODE")


def _demo_status() -> dict[str, Any]:
    return {
        "ok": True,
        "demo": True,
        "hostname": "shadow-demo",
        "platform": "Shadow Demo Linux",
        "os": {"system": "Linux", "release": "demo"},
        "uptime_seconds": 45678,
        "load": [0.42, 0.37, 0.31],
        "cpu": {"percent": 18.4, "count": 8},
        "memory": {"total": 16 * 1024**3, "used": 6 * 1024**3, "available": 10 * 1024**3},
        "disk": {"total": 512 * 1024**3, "used": 211 * 1024**3, "free": 301 * 1024**3},
        "network": {"interfaces": [{"name": "tailscale0", "rx_bytes": 18420000, "tx_bytes": 7310000}], "rx_bytes": 18420000, "tx_bytes": 7310000},
        "gpu": [{"name": "Demo GPU", "util_percent": 12, "memory_used_mib": 1400, "memory_total_mib": 8192, "temp_c": 48}],
    }


def _demo_screen() -> dict[str, Any]:
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
<defs><linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="#080b10"/><stop offset="1" stop-color="#151b24"/></linearGradient></defs>
<rect width="1280" height="720" fill="url(#g)"/>
<rect x="70" y="70" width="1140" height="560" fill="#0b0f14" stroke="#e05f6a" stroke-width="2" rx="18"/>
<text x="98" y="126" fill="#e05f6a" font-family="monospace" font-size="28" font-weight="700">SHADOW COMMAND DEMO</text>
<text x="98" y="178" fill="#8fc8dc" font-family="monospace" font-size="20">No home PC is connected. All controls are simulated.</text>
<rect x="98" y="228" width="350" height="160" fill="#111822" stroke="#2e5261" rx="10"/>
<text x="124" y="278" fill="#8fc8dc" font-family="monospace" font-size="18">CPU 18.4%</text>
<text x="124" y="326" fill="#8fc8dc" font-family="monospace" font-size="18">RAM 6 / 16 GB</text>
<text x="124" y="374" fill="#8fc8dc" font-family="monospace" font-size="18">GPU 48C</text>
<rect x="494" y="228" width="520" height="160" fill="#111822" stroke="#2e5261" rx="10"/>
<text x="520" y="278" fill="#e05f6a" font-family="monospace" font-size="18">Approval gates are active in demo mode.</text>
<text x="520" y="326" fill="#8fc8dc" font-family="monospace" font-size="18">Try runbooks, timeline, files, and MAGI.</text>
</svg>'''.strip()
    return {
        "ok": True,
        "demo": True,
        "mime": "image/svg+xml",
        "width": 1280,
        "height": 720,
        "image_b64": base64.b64encode(svg.encode("utf-8")).decode("ascii"),
    }


def _demo_read_action(action: str, args: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return _demo_status()
    if action == "processes":
        return {"ok": True, "demo": True, "processes": [
            "PID COMMAND %CPU %MEM",
            "101 shadow-ui 8.4 12.1",
            "202 ollama 5.7 18.3",
            "303 firefox 3.2 9.8",
            "404 code 2.1 7.4",
        ]}
    if action == "screenshot":
        return _demo_screen()
    if action == "clipboard_get":
        return {"ok": True, "demo": True, "text": "Shadow demo clipboard"}
    if action == "windows":
        return {"ok": True, "demo": True, "windows": [
            {"id": "0x001", "desktop": "0", "pid": "303", "class": "firefox.Firefox", "host": "shadow-demo", "title": "Shadow - Command"},
            {"id": "0x002", "desktop": "0", "pid": "404", "class": "code.Code", "host": "shadow-demo", "title": "shadow-workspace - VS Code"},
        ]}
    if action == "file_list":
        path = str(args.get("path") or "/demo")
        return {"ok": True, "demo": True, "path": path, "roots": ["/demo"], "entries": [
            {"name": "README.md", "path": "/demo/README.md", "type": "file", "size": 2048, "modified": int(time.time())},
            {"name": "projects", "path": "/demo/projects", "type": "dir", "size": 4096, "modified": int(time.time())},
        ]}
    if action == "file_read":
        path = str(args.get("path") or "/demo/README.md")
        return {"ok": True, "demo": True, "path": path, "name": Path(path).name, "size": 128, "truncated": False, "binary": False, "mime": "text/markdown", "text": "# Shadow Demo\n\nThis is simulated file content. Connect a home agent for real files."}
    if action == "file_search":
        return {"ok": True, "demo": True, "path": str(args.get("path") or "/demo"), "query": str(args.get("query") or ""), "truncated": False, "results": [
            {"name": "README.md", "path": "/demo/README.md", "type": "file", "size": 2048, "modified": int(time.time())},
        ]}
    raise ShadowPcError(f"Unsupported demo read action: {action}")


def _demo_confirmed_action(action: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "demo": True, "action": action, "args": _redact_args(args), "message": "Demo mode simulated this state-changing action."}


def _ttl_seconds() -> int:
    try:
        return max(30, min(int(os.getenv("SHADOW_PC_CONFIRM_TTL_SECONDS", "300")), 3600))
    except ValueError:
        return 300


def _agent_url() -> str:
    return os.getenv("SHADOW_HOME_AGENT_URL", "").strip().rstrip("/")


def _agent_token() -> str:
    return os.getenv("SHADOW_HOME_AGENT_TOKEN", "").strip()


def _agent_socket() -> str:
    return os.getenv("SHADOW_HOME_AGENT_SOCKET", "").strip()


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
    if _demo_enabled():
        return True
    return bool((_agent_socket() or _agent_url()) and len(_agent_token()) >= 32)


def _validated_agent_url() -> str:
    if _demo_enabled():
        return "http://shadow-demo-agent"
    if not (_agent_socket() or _agent_url()) or len(_agent_token()) < 32:
        raise ShadowPcError("Home PC bridge is not configured")
    if _agent_socket():
        if not os.path.isabs(_agent_socket()):
            raise ShadowPcError("SHADOW_HOME_AGENT_SOCKET must be an absolute path")
        return "http://shadow-home-agent"
    return validate_agent_url(_agent_url())


def _clean_args(args: Any) -> dict[str, Any]:
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise ShadowPcError("Action args must be a JSON object")
    return args


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in (args or {}).items():
        low = str(key).lower()
        if any(word in low for word in ("token", "secret", "password", "key")):
            redacted[key] = "<redacted>"
        elif isinstance(value, str) and len(value) > 240:
            redacted[key] = value[:240] + "..."
        else:
            redacted[key] = value
    return redacted


def _risk_for(action: str, args: dict[str, Any] | None = None) -> str:
    action = str(action or "").strip().lower()
    args = args or {}
    if action == "shell" and _DANGEROUS_SHELL.search(str(args.get("command") or "")):
        return "critical"
    if action == "file_write" and str(args.get("path") or "").strip() in {"/", "~", ""}:
        return "critical"
    if action == "runbook":
        spec = _all_runbooks().get(str(args.get("name") or "").strip().lower())
        return _runbook_risk(spec) if spec else "high"
    return _ACTION_RISK.get(action, "high")


def _max_risk(values: list[str]) -> str:
    if not values:
        return "low"
    return max(values, key=lambda item: _RISK_WEIGHT.get(item, 3))


def _audit(
    action: str,
    status: str,
    *,
    requested_by: str = "system",
    principal: str | None = None,
    args: dict[str, Any] | None = None,
    detail: str = "",
) -> None:
    record = {
        "ts": time.time(),
        "action": action,
        "status": status,
        "risk": _risk_for(action, args or {}),
        "requested_by": str(requested_by or "unknown")[:100],
        "principal": str(principal or "")[:100],
        "args": _redact_args(args or {}),
        "detail": str(detail or "")[:500],
    }
    try:
        with _AUDIT_LOCK:
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            rows = []
            if AUDIT_PATH.exists():
                rows = AUDIT_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()[-(MAX_AUDIT_LINES - 1):]
            rows.append(json.dumps(record, separators=(",", ":"), sort_keys=True))
            AUDIT_PATH.write_text("\n".join(rows) + "\n", encoding="utf-8")
    except OSError:
        pass


def timeline(limit: int = 80, principal: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 80), 300))
    try:
        lines = AUDIT_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict) and (principal is None or item.get("principal") == principal):
            rows.append(item)
            if len(rows) >= limit:
                break
    return rows


def _runbook_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9_-]+", "_", str(value or "").strip().lower()).strip("_")
    if not key or len(key) > 60:
        raise ShadowPcError("Runbook name must be 1-60 chars: letters, numbers, _ or -")
    return key


def _normalize_step(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ShadowPcError("Runbook steps must be JSON objects")
    action = str(step.get("action") or "").strip().lower()
    if action not in ALL_ACTIONS or action == "runbook":
        raise ShadowPcError(f"Unsupported runbook step action: {action or '(missing)'}")
    args = _clean_args(step.get("args") or {})
    return {"action": action, "args": args}


def _validate_runbook_spec(spec: dict[str, Any], *, key: str, builtin: bool = False) -> dict[str, Any]:
    steps = [_normalize_step(step) for step in spec.get("steps") or []]
    if not steps:
        raise ShadowPcError("Runbook requires at least one step")
    if len(steps) > MAX_RUNBOOK_STEPS:
        raise ShadowPcError(f"Runbook cannot exceed {MAX_RUNBOOK_STEPS} steps")
    return {
        "label": str(spec.get("label") or key).strip()[:80],
        "description": str(spec.get("description") or "").strip()[:500],
        "steps": steps,
        "builtin": builtin,
        "created_at": float(spec.get("created_at") or time.time()),
        "updated_at": float(spec.get("updated_at") or time.time()),
    }


def _custom_runbooks() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(RUNBOOKS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    rows = raw.get("runbooks") if isinstance(raw, dict) else raw
    if not isinstance(rows, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in rows.items():
        try:
            key = _runbook_key(str(name))
            if key in RUNBOOKS or not isinstance(spec, dict):
                continue
            out[key] = _validate_runbook_spec(spec, key=key, builtin=False)
        except ShadowPcError:
            continue
    return out


def _write_custom_runbooks(rows: dict[str, dict[str, Any]]) -> None:
    RUNBOOKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    serializable = {
        key: {
            "label": spec.get("label") or key,
            "description": spec.get("description") or "",
            "steps": spec.get("steps") or [],
            "created_at": spec.get("created_at") or time.time(),
            "updated_at": spec.get("updated_at") or time.time(),
        }
        for key, spec in sorted(rows.items())
    }
    RUNBOOKS_PATH.write_text(json.dumps({"runbooks": serializable}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _all_runbooks() -> dict[str, dict[str, Any]]:
    builtins = {key: _validate_runbook_spec(spec, key=key, builtin=True) for key, spec in RUNBOOKS.items()}
    return {**builtins, **_custom_runbooks()}


def _runbook_risk(spec: dict[str, Any] | None) -> str:
    if not spec:
        return "high"
    return _max_risk([_risk_for(step.get("action", ""), step.get("args") or {}) for step in spec.get("steps") or []])


def list_runbooks() -> list[dict[str, Any]]:
    rows = []
    for name, spec in _all_runbooks().items():
        rows.append({
            "name": name,
            "label": spec["label"],
            "description": spec["description"],
            "steps": [step["action"] for step in spec["steps"]],
            "step_specs": spec["steps"],
            "builtin": bool(spec.get("builtin")),
            "risk": _runbook_risk(spec),
            "updated_at": spec.get("updated_at"),
        })
    return sorted(rows, key=lambda row: (not row["builtin"], row["name"]))


def save_runbook(name: str, label: str, description: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    key = _runbook_key(name)
    if key in RUNBOOKS:
        raise ShadowPcError("Built-in runbooks cannot be overwritten")
    rows = _custom_runbooks()
    now = time.time()
    existing = rows.get(key) or {}
    rows[key] = _validate_runbook_spec({
        "label": label,
        "description": description,
        "steps": steps,
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }, key=key, builtin=False)
    _write_custom_runbooks(rows)
    _audit("runbook.save", "executed", args={"name": key}, detail=rows[key]["label"])
    return next(row for row in list_runbooks() if row["name"] == key)


def delete_runbook(name: str) -> dict[str, Any]:
    key = _runbook_key(name)
    if key in RUNBOOKS:
        raise ShadowPcError("Built-in runbooks cannot be deleted")
    rows = _custom_runbooks()
    if key not in rows:
        raise ShadowPcError(f"Unknown custom runbook: {name or '(missing)'}")
    removed = rows.pop(key)
    _write_custom_runbooks(rows)
    _audit("runbook.delete", "executed", args={"name": key}, detail=removed.get("label", key))
    return {"ok": True, "name": key}


def _execute_runbook(name: str, *, requested_by: str, principal: str | None = None) -> dict[str, Any]:
    key = _runbook_key(name)
    spec = _all_runbooks().get(key)
    if not spec:
        raise ShadowPcError(f"Unknown runbook: {name or '(missing)'}")
    results = []
    for step in spec["steps"]:
        action = step["action"]
        args = dict(step.get("args") or {})
        try:
            if _demo_enabled():
                result = _demo_read_action(action, args) if action in READ_ACTIONS else _demo_confirmed_action(action, args)
            else:
                result = _call_home_agent(action, args, confirmed=True)
            results.append({"action": action, "risk": _risk_for(action, args), "ok": True, "result": result})
        except ShadowPcError as exc:
            results.append({"action": action, "risk": _risk_for(action, args), "ok": False, "error": str(exc)})
            break
    ok = all(item.get("ok") for item in results)
    _audit("runbook", "executed" if ok else "failed", requested_by=requested_by, principal=principal, args={"name": key}, detail=spec["label"])
    return {"ok": ok, "name": key, "label": spec["label"], "risk": _runbook_risk(spec), "results": results}


def watchdog(principal: str | None = None) -> dict[str, Any]:
    data = overview(principal=principal)
    data["timeline"] = timeline(12, principal=principal)
    data["runbooks"] = list_runbooks()
    data["watchdog"] = {
        "ok": bool(data.get("online")),
        "configured": bool(data.get("configured")),
        "pending_count": len(data.get("pending") or []),
        "last_event": (data["timeline"][0] if data["timeline"] else None),
    }
    return data


def _call_home_agent(action: str, args: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
    url = _validated_agent_url()
    transport = httpx.HTTPTransport(uds=_agent_socket()) if _agent_socket() else None
    try:
        with httpx.Client(transport=transport, timeout=httpx.Timeout(15.0, connect=4.0)) as client:
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


def request_action(
    action: str,
    args: Any = None,
    *,
    requested_by: str = "web",
    principal: str | None = None,
) -> dict[str, Any]:
    action = str(action or "").strip().lower()
    if action not in ALL_ACTIONS:
        raise ShadowPcError(f"Unsupported home PC action: {action or '(missing)'}")
    clean_args = _clean_args(args)
    _validated_agent_url()
    if action in READ_ACTIONS:
        try:
            result = _demo_read_action(action, clean_args) if _demo_enabled() else _call_home_agent(action, clean_args, confirmed=False)
            _audit(action, "executed", requested_by=requested_by, principal=principal, args=clean_args)
            return result
        except ShadowPcError as exc:
            _audit(action, "failed", requested_by=requested_by, principal=principal, args=clean_args, detail=str(exc))
            raise
    now = time.time()
    item = {
        "id": secrets.token_urlsafe(12),
        "action": action,
        "args": clean_args,
        "risk": _risk_for(action, clean_args),
        "requested_by": str(requested_by or "unknown")[:100],
        "principal": str(principal or "")[:100],
        "created_at": now,
        "expires_at": now + _ttl_seconds(),
    }
    with _LOCK:
        _purge_expired()
        _PENDING[item["id"]] = item
    _audit(action, "pending", requested_by=requested_by, principal=principal, args=clean_args)
    return {"status": "pending_confirmation", "pending": dict(item)}


def list_pending(principal: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        _purge_expired()
        return [
            dict(item)
            for item in sorted(_PENDING.values(), key=lambda row: row["created_at"])
            if principal is None or item.get("principal") == principal
        ]


def confirm_action(pending_id: str, principal: str | None = None) -> dict[str, Any]:
    with _LOCK:
        _purge_expired()
        key = str(pending_id or "")
        item = _PENDING.get(key)
        if item and principal is not None and item.get("principal") != principal:
            raise ShadowPcError("Pending action belongs to another account")
        if item:
            _PENDING.pop(key, None)
    if not item:
        raise ShadowPcError("Pending action was not found or has expired")
    try:
        if item["action"] == "runbook":
            result = _execute_runbook(
                str(item.get("args", {}).get("name") or ""),
                requested_by=item.get("requested_by", "web"),
                principal=item.get("principal"),
            )
        else:
            result = _demo_confirmed_action(item["action"], item["args"]) if _demo_enabled() else _call_home_agent(item["action"], item["args"], confirmed=True)
            _audit(
                item["action"],
                "executed",
                requested_by=item.get("requested_by", "web"),
                principal=item.get("principal"),
                args=item.get("args") or {},
            )
        return {"status": "executed", "action": item["action"], "result": result}
    except ShadowPcError as exc:
        _audit(
            item["action"],
            "failed",
            requested_by=item.get("requested_by", "web"),
            principal=item.get("principal"),
            args=item.get("args") or {},
            detail=str(exc),
        )
        raise


def cancel_action(pending_id: str, principal: str | None = None) -> dict[str, Any]:
    with _LOCK:
        _purge_expired()
        key = str(pending_id or "")
        item = _PENDING.get(key)
        if item and principal is not None and item.get("principal") != principal:
            raise ShadowPcError("Pending action belongs to another account")
        if item:
            _PENDING.pop(key, None)
    if not item:
        raise ShadowPcError("Pending action was not found or has expired")
    _audit(
        item["action"],
        "cancelled",
        requested_by=item.get("requested_by", "web"),
        principal=item.get("principal"),
        args=item.get("args") or {},
    )
    return {"status": "cancelled", "action": item["action"]}


def _status_cache_ttl() -> float:
    try:
        return max(0.0, min(float(os.getenv("SHADOW_PC_STATUS_CACHE_SECONDS", str(STATUS_CACHE_SECONDS))), 30.0))
    except ValueError:
        return 3.0


def _status_snapshot() -> tuple[dict[str, Any], float]:
    ttl = _status_cache_ttl()
    now = time.time()
    with _OVERVIEW_LOCK:
        cached = _OVERVIEW_CACHE.get("pc")
        cached_ts = float(_OVERVIEW_CACHE.get("ts") or 0.0)
        if cached is not None and ttl > 0 and now - cached_ts <= ttl:
            return dict(cached), now - cached_ts
    pc = _demo_status() if _demo_enabled() else _call_home_agent("status", {}, confirmed=False)
    with _OVERVIEW_LOCK:
        _OVERVIEW_CACHE["pc"] = dict(pc)
        _OVERVIEW_CACHE["ts"] = now
    return pc, 0.0


def overview(principal: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "configured": configured(),
        "online": False,
        "demo": _demo_enabled(),
        "pending": list_pending(principal=principal),
        "actions": {"read": sorted(READ_ACTIONS), "confirm": sorted(WRITE_ACTIONS)},
        "runbooks": list_runbooks(),
        "risk_levels": _ACTION_RISK,
    }
    if not configured():
        return result
    try:
        pc, age = _status_snapshot()
        result["pc"] = pc
        result["online"] = True
        result["cache"] = {"status_age_seconds": round(age, 2)}
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
    action = str(payload.get("action") or "").strip().lower()
    principal = requested_by.partition(":")[2].strip().lower() if ":" in requested_by else ""
    try:
        from src.shadow_access import ShadowAccessError, require_permission

        require_permission(principal, "view" if action in READ_ACTIONS else "control")
        result = request_action(
            action,
            payload.get("args"),
            requested_by=requested_by,
            principal=principal,
        )
    except (ShadowPcError, ShadowAccessError) as exc:
        return {"error": str(exc), "exit_code": 1}
    if result.get("status") == "pending_confirmation":
        pending = result["pending"]
        return {
            "output": (
                f"Home PC action `{pending['action']}` is waiting for explicit approval "
                f"(risk: `{pending.get('risk', 'high')}`). Pending id: `{pending['id']}`. "
                "The action has NOT executed."
            ),
            "exit_code": 0,
            "pending_confirmation": pending,
        }
    return {"output": json.dumps(result, indent=2, sort_keys=True), "exit_code": 0, "result": result}

