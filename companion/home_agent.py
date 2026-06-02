"""Allowlisted home-PC control service for a private Tailscale link."""

from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

READ_ACTIONS = frozenset({"status", "processes", "screenshot", "clipboard_get"})
WRITE_ACTIONS = frozenset({"clipboard_set", "media", "volume", "app_launch", "app_focus", "app_close", "lock", "type_text", "keypress"})
ALL_ACTIONS = READ_ACTIONS | WRITE_ACTIONS


class HomeAgentError(RuntimeError):
    """A clean home-agent action failure."""


def _run(argv: list[str], *, input_text: str | None = None, timeout: float = 8) -> str:
    try:
        proc = subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise HomeAgentError(f"{argv[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise HomeAgentError(f"{argv[0]} timed out") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise HomeAgentError(f"{argv[0]} failed: {detail[:400]}")
    return (proc.stdout or "").strip()


def _run_first(candidates: list[list[str]], *, input_text: str | None = None, timeout: float = 8) -> str:
    errors = []
    for argv in candidates:
        if shutil.which(argv[0]) is None:
            continue
        try:
            return _run(argv, input_text=input_text, timeout=timeout)
        except HomeAgentError as exc:
            errors.append(str(exc))
    raise HomeAgentError("; ".join(errors) or "No supported desktop utility is installed")


def _clipboard_set(text: str) -> dict[str, Any]:
    """Clipboard providers may intentionally stay alive after receiving data."""
    candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    errors = []
    for argv in candidates:
        if shutil.which(argv[0]) is None:
            continue
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                start_new_session=True,
            )
            try:
                _, stderr = proc.communicate(text, timeout=1.5)
            except subprocess.TimeoutExpired:
                # wl-copy/xclip can remain alive as the clipboard owner. That
                # is success after stdin was delivered, not an action failure.
                return {"ok": True, "provider": argv[0], "resident": True}
            if proc.returncode == 0:
                return {"ok": True, "provider": argv[0], "resident": False}
            errors.append(f"{argv[0]}: {(stderr or '').strip()[:240]}")
        except OSError as exc:
            errors.append(f"{argv[0]}: {exc}")
    raise HomeAgentError("; ".join(errors) or "Install wl-clipboard, xclip, or xsel")


def _mem_info() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError):
        return {}
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    return {"total": total, "used": max(0, total - available), "available": available}


def _status() -> dict[str, Any]:
    disk = shutil.disk_usage("/")
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError):
        uptime = 0
    try:
        load = list(os.getloadavg())
    except OSError:
        load = []
    return {
        "ok": True,
        "hostname": platform.node(),
        "platform": platform.platform(),
        "uptime_seconds": round(uptime),
        "load": load,
        "memory": _mem_info(),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
    }


def _processes(args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit", 15)), 50))
    output = _run(["ps", "-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"], timeout=5)
    return {"ok": True, "processes": output.splitlines()[: limit + 1]}


def _screenshot() -> dict[str, Any]:
    fd, name = tempfile.mkstemp(prefix="shadow-screen-", suffix=".png")
    os.close(fd)
    path = Path(name)
    candidates = [
        ["grim", str(path)],
        ["gnome-screenshot", "-f", str(path)],
        ["spectacle", "-b", "-n", "-o", str(path)],
        ["import", "-window", "root", str(path)],
    ]
    errors = []
    try:
        for argv in candidates:
            if shutil.which(argv[0]) is None:
                continue
            try:
                _run(argv, timeout=12)
                if path.exists() and path.stat().st_size:
                    mime = mimetypes.guess_type(str(path))[0] or "image/png"
                    return {
                        "ok": True,
                        "mime": mime,
                        "image_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
                    }
            except HomeAgentError as exc:
                errors.append(str(exc))
        raise HomeAgentError("; ".join(errors) or "Install grim, gnome-screenshot, spectacle, or ImageMagick import")
    finally:
        path.unlink(missing_ok=True)


def _parse_apps() -> dict[str, list[str]]:
    apps: dict[str, list[str]] = {}
    for item in os.getenv("SHADOW_ALLOWED_APPS", "").split(","):
        name, sep, command = item.strip().partition("=")
        if sep and name.strip() and command.strip():
            apps[name.strip().lower()] = shlex.split(command.strip())
    return apps


def _need_text(args: dict[str, Any], key: str, *, limit: int = 4000) -> str:
    value = str(args.get(key) or "").strip()
    if not value:
        raise HomeAgentError(f"{key} is required")
    return value[:limit]


def execute_action(action: str, args: Any = None, *, confirmed: bool = False) -> dict[str, Any]:
    action = str(action or "").strip().lower()
    if action not in ALL_ACTIONS:
        raise HomeAgentError(f"Unsupported action: {action or '(missing)'}")
    if action in WRITE_ACTIONS and not confirmed:
        raise HomeAgentError("State-changing actions require confirmed=true")
    args = args if isinstance(args, dict) else {}

    if action == "status":
        return _status()
    if action == "processes":
        return _processes(args)
    if action == "screenshot":
        return _screenshot()
    if action == "clipboard_get":
        text = _run_first([["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]])
        return {"ok": True, "text": text[:100_000]}
    if action == "clipboard_set":
        return _clipboard_set(str(args.get("text") or "")[:100_000])
    if action == "lock":
        _run_first([["loginctl", "lock-session"], ["gnome-screensaver-command", "-l"], ["xdg-screensaver", "lock"]])
        return {"ok": True}
    if action == "type_text":
        _run(["xdotool", "type", "--clearmodifiers", "--", _need_text(args, "text")])
        return {"ok": True}
    if action == "keypress":
        key = _need_text(args, "key", limit=80)
        if not all(ch.isalnum() or ch in "+_- " for ch in key):
            raise HomeAgentError("keypress contains unsupported characters")
        _run(["xdotool", "key", "--clearmodifiers", key])
        return {"ok": True}
    if action == "media":
        command = _need_text(args, "command", limit=20).lower()
        if command not in {"play", "pause", "play-pause", "next", "previous", "stop"}:
            raise HomeAgentError("media command must be play, pause, play-pause, next, previous, or stop")
        _run(["playerctl", command])
        return {"ok": True}
    if action == "volume":
        value = max(0, min(int(args.get("percent", 50)), 150))
        _run_first([
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{value}%"],
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"],
        ])
        return {"ok": True, "percent": value}
    if action == "app_launch":
        app = _need_text(args, "app", limit=60).lower()
        argv = _parse_apps().get(app)
        if not argv:
            raise HomeAgentError(f"App '{app}' is not in SHADOW_ALLOWED_APPS")
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, start_new_session=True)
        return {"ok": True, "app": app}
    if action in {"app_focus", "app_close"}:
        title = _need_text(args, "title", limit=120)
        _run(["wmctrl", "-a" if action == "app_focus" else "-c", title])
        return {"ok": True, "title": title}
    raise HomeAgentError(f"Unsupported action: {action}")


class Handler(BaseHTTPRequestHandler):
    server_version = "ShadowHomeAgent/1.0"

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        expected = os.getenv("SHADOW_HOME_AGENT_TOKEN", "").strip()
        supplied = self.headers.get("Authorization", "")
        return bool(expected and supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:], expected))

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._send(404, {"error": "Not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "Not authorized"})
            return
        self._send(200, {"ok": True, "service": "shadow-home-agent", "time": int(time.time())})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/action":
            self._send(404, {"error": "Not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "Not authorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 150_000:
                raise HomeAgentError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            result = execute_action(payload.get("action"), payload.get("args"), confirmed=payload.get("confirmed") is True)
            self._send(200, result)
        except (HomeAgentError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception:
            self._send(500, {"error": "Home agent action failed"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[shadow-home-agent] {self.address_string()} {fmt % args}")


def main() -> None:
    bind = os.getenv("SHADOW_HOME_AGENT_BIND", "127.0.0.1").strip()
    port = int(os.getenv("SHADOW_HOME_AGENT_PORT", "8765"))
    token = os.getenv("SHADOW_HOME_AGENT_TOKEN", "").strip()
    if len(token) < 32:
        raise SystemExit("Set SHADOW_HOME_AGENT_TOKEN to a random secret of at least 32 characters")
    if bind in {"0.0.0.0", "::"} and os.getenv("SHADOW_HOME_AGENT_ALLOW_WILDCARD", "").lower() not in {"1", "true", "yes"}:
        raise SystemExit("Refusing wildcard bind. Bind the Tailscale IP or set SHADOW_HOME_AGENT_ALLOW_WILDCARD=true explicitly.")
    print(f"Shadow home agent listening on {bind}:{port}")
    ThreadingHTTPServer((bind, port), Handler).serve_forever()

