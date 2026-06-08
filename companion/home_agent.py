"""Allowlisted home-PC control service for a private Tailscale link."""

from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import os
import platform
import signal
import shlex
import shutil
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn, UnixStreamServer
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # Optional; Linux keeps its /proc fallback.
    psutil = None

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
})
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
    if platform.system() == "Darwin":
        _run(["pbcopy"], input_text=text)
        return {"ok": True, "provider": "pbcopy", "resident": False}
    if platform.system() == "Windows":
        _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"], input_text=text)
        return {"ok": True, "provider": "powershell", "resident": False}
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


_CPU_LAST: tuple[int, int] | None = None


def _mem_info() -> dict[str, int]:
    if psutil is not None:
        values = psutil.virtual_memory()
        return {"total": int(values.total), "used": int(values.used), "available": int(values.available)}
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


def _cpu_percent() -> float | None:
    global _CPU_LAST
    if psutil is not None:
        return round(float(psutil.cpu_percent(interval=0.1)), 1)
    try:
        parts = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
        values = [int(part) for part in parts]
    except (OSError, ValueError, IndexError):
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    previous = _CPU_LAST
    _CPU_LAST = (total, idle)
    if previous is None:
        return None
    total_delta = total - previous[0]
    idle_delta = idle - previous[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1.0 - (idle_delta / total_delta)))), 1)


def _network_info() -> dict[str, Any]:
    if psutil is not None:
        per_nic = psutil.net_io_counters(pernic=True)
        interfaces = [
            {"name": name, "rx_bytes": int(row.bytes_recv), "tx_bytes": int(row.bytes_sent)}
            for name, row in per_nic.items() if name.lower() not in {"lo", "loopback"}
        ]
        totals = psutil.net_io_counters()
        return {"interfaces": interfaces, "rx_bytes": int(totals.bytes_recv), "tx_bytes": int(totals.bytes_sent)}
    interfaces = []
    rx_total = 0
    tx_total = 0
    try:
        lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]
    except OSError:
        return {"interfaces": [], "rx_bytes": 0, "tx_bytes": 0}
    for line in lines:
        name, _, rest = line.partition(":")
        iface = name.strip()
        if not iface or iface == "lo":
            continue
        cols = rest.split()
        if len(cols) < 16:
            continue
        try:
            rx = int(cols[0])
            tx = int(cols[8])
        except ValueError:
            continue
        rx_total += rx
        tx_total += tx
        interfaces.append({"name": iface, "rx_bytes": rx, "tx_bytes": tx})
    return {"interfaces": interfaces, "rx_bytes": rx_total, "tx_bytes": tx_total}


def _gpu_info() -> list[dict[str, Any]]:
    if shutil.which("nvidia-smi") is None:
        return []
    query = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
    try:
        output = _run([
            "nvidia-smi",
            f"--query-gpu={query}",
            "--format=csv,noheader,nounits",
        ], timeout=5)
    except HomeAgentError:
        return []
    gpus = []
    for row in output.splitlines():
        parts = [part.strip() for part in row.split(",")]
        if len(parts) < 5:
            continue
        name, util, mem_used, mem_total, temp = parts[:5]
        def num(value: str) -> float | None:
            try:
                return float(value)
            except ValueError:
                return None
        gpus.append({
            "name": name,
            "util_percent": num(util),
            "memory_used_mib": num(mem_used),
            "memory_total_mib": num(mem_total),
            "temp_c": num(temp),
        })
    return gpus


def _status() -> dict[str, Any]:
    disk = shutil.disk_usage(Path.home().anchor or "/")
    if psutil is not None:
        uptime = max(0, time.time() - float(psutil.boot_time()))
    else:
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
        "os": {"system": platform.system(), "release": platform.release()},
        "uptime_seconds": round(uptime),
        "load": load,
        "cpu": {"percent": _cpu_percent(), "count": os.cpu_count()},
        "memory": _mem_info(),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "network": _network_info(),
        "gpu": _gpu_info(),
    }


def _processes(args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit", 15)), 50))
    if psutil is not None:
        rows = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                info = proc.info
                rows.append((float(info.get("cpu_percent") or 0), f"{int(info.get('pid') or 0):>7} {str(info.get('name') or '')[:28]:<28} {float(info.get('cpu_percent') or 0):>6.1f} {float(info.get('memory_percent') or 0):>6.1f}"))
            except Exception:
                continue
        rows.sort(key=lambda item: item[0], reverse=True)
        return {"ok": True, "processes": ["PID COMMAND %CPU %MEM", *[row[1] for row in rows[:limit]]]}
    if platform.system() == "Windows":
        raise HomeAgentError("Install psutil for process monitoring on Windows")
    output = _run(["ps", "-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"], timeout=5)
    return {"ok": True, "processes": output.splitlines()[: limit + 1]}


def _windows() -> dict[str, Any]:
    if platform.system() != "Linux":
        raise HomeAgentError("Window listing currently requires Linux with wmctrl")
    output = _run(["wmctrl", "-lpx"], timeout=5)
    windows = []
    for line in output.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        wid, desktop, pid, wm_class, host, title = parts
        windows.append({
            "id": wid,
            "desktop": desktop,
            "pid": pid,
            "class": wm_class,
            "host": host,
            "title": title,
        })
    return {"ok": True, "windows": windows}


def _screenshot() -> dict[str, Any]:
    fd, name = tempfile.mkstemp(prefix="shadow-screen-", suffix=".png")
    os.close(fd)
    path = Path(name)
    if platform.system() == "Darwin":
        candidates = [["screencapture", "-x", str(path)]]
    elif platform.system() == "Windows":
        escaped = str(path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;Add-Type -AssemblyName System.Drawing;"
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
            "$i=New-Object Drawing.Bitmap $b.Width,$b.Height;"
            "$g=[Drawing.Graphics]::FromImage($i);$g.CopyFromScreen($b.Location,[Drawing.Point]::Empty,$b.Size);"
            f"$i.Save('{escaped}',[Drawing.Imaging.ImageFormat]::Png);$g.Dispose();$i.Dispose()"
        )
        candidates = [["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]]
    else:
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
                    width, height = _png_size(path)
                    return {
                        "ok": True,
                        "mime": mime,
                        "width": width,
                        "height": height,
                        "image_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
                    }
            except HomeAgentError as exc:
                errors.append(str(exc))
        raise HomeAgentError("; ".join(errors) or "Install grim, gnome-screenshot, spectacle, or ImageMagick import")
    finally:
        path.unlink(missing_ok=True)


def _png_size(path: Path) -> tuple[int | None, int | None]:
    try:
        header = path.read_bytes()[:24]
    except OSError:
        return None, None
    if len(header) >= 24 and header.startswith(b"\x89PNG\r\n\x1a\n"):
        return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    return None, None


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


def _allowed_roots() -> list[Path]:
    raw = os.getenv("SHADOW_ALLOWED_ROOTS", "").strip()
    items = [item.strip() for item in raw.split(os.pathsep) if item.strip()] if raw else [str(Path.home())]
    roots: list[Path] = []
    for item in items:
        try:
            root = Path(os.path.expanduser(item)).resolve(strict=False)
        except OSError:
            continue
        if root.exists() and root.is_dir():
            roots.append(root)
    return roots or [Path.home().resolve(strict=False)]


def _resolve_allowed(value: str | None = None, *, must_exist: bool = True) -> Path:
    roots = _allowed_roots()
    raw = str(value or roots[0])
    try:
        path = Path(os.path.expanduser(raw)).resolve(strict=must_exist)
    except FileNotFoundError:
        path = Path(os.path.expanduser(raw)).resolve(strict=False)
    except OSError as exc:
        raise HomeAgentError(f"Invalid path: {exc}") from exc
    for root in roots:
        if path == root or root in path.parents:
            return path
    raise HomeAgentError("Path is outside SHADOW_ALLOWED_ROOTS")


def _file_entry(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        return {"name": path.name, "path": str(path), "error": "unreadable"}
    return {
        "name": path.name or str(path),
        "path": str(path),
        "type": "dir" if path.is_dir() else "file",
        "size": st.st_size,
        "modified": int(st.st_mtime),
    }


def _file_list(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_allowed(str(args.get("path") or ""), must_exist=True)
    if not path.is_dir():
        raise HomeAgentError("Path is not a directory")
    limit = max(1, min(int(args.get("limit", 160)), 400))
    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:limit]
    except OSError as exc:
        raise HomeAgentError(f"Cannot list directory: {exc}") from exc
    return {"ok": True, "path": str(path), "roots": [str(root) for root in _allowed_roots()], "entries": [_file_entry(item) for item in entries]}


def _file_read(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_allowed(_need_text(args, "path", limit=4096), must_exist=True)
    if not path.is_file():
        raise HomeAgentError("Path is not a file")
    max_bytes = max(1_000, min(int(args.get("max_bytes", 200_000)), 1_000_000))
    data = path.read_bytes()[:max_bytes]
    binary = b"\x00" in data
    text = "" if binary else data.decode("utf-8", errors="replace")
    return {
        "ok": True,
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size,
        "truncated": path.stat().st_size > len(data),
        "binary": binary,
        "text": text,
        "mime": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
    }


def _file_search(args: dict[str, Any]) -> dict[str, Any]:
    base = _resolve_allowed(str(args.get("path") or ""), must_exist=True)
    if not base.is_dir():
        raise HomeAgentError("Search path is not a directory")
    query = _need_text(args, "query", limit=120).lower()
    limit = max(1, min(int(args.get("limit", 50)), 100))
    visited = 0
    results = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")][:80]
        for name in dirs + files:
            visited += 1
            if visited > 6000:
                return {"ok": True, "path": str(base), "query": query, "truncated": True, "results": results}
            if query in name.lower():
                results.append(_file_entry(Path(root) / name))
                if len(results) >= limit:
                    return {"ok": True, "path": str(base), "query": query, "truncated": False, "results": results}
    return {"ok": True, "path": str(base), "query": query, "truncated": False, "results": results}


def _file_write(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_allowed(_need_text(args, "path", limit=4096), must_exist=False)
    text = str(args.get("text") or "")
    if len(text.encode("utf-8")) > 1_000_000:
        raise HomeAgentError("File write is limited to 1 MB")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(path), "bytes": len(text.encode("utf-8"))}


def _shell(args: dict[str, Any]) -> dict[str, Any]:
    command = _need_text(args, "command", limit=4000)
    cwd = _resolve_allowed(str(args.get("cwd") or ""), must_exist=True)
    if not cwd.is_dir():
        raise HomeAgentError("Shell cwd is not a directory")
    timeout = max(1, min(int(args.get("timeout", 20)), 60))
    try:
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command] if platform.system() == "Windows" else ["/bin/bash", "-lc", command]
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HomeAgentError(f"Command timed out after {timeout}s") from exc
    output = (proc.stdout or "")[-12000:]
    error = (proc.stderr or "")[-12000:]
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": output, "stderr": error, "cwd": str(cwd)}


def _kill_process(args: dict[str, Any]) -> dict[str, Any]:
    try:
        pid = int(args.get("pid"))
    except (TypeError, ValueError) as exc:
        raise HomeAgentError("pid must be an integer") from exc
    if pid <= 1 or pid == os.getpid():
        raise HomeAgentError("Refusing to kill a protected process")
    sig_name = str(args.get("signal") or "TERM").upper()
    if psutil is not None:
        proc = psutil.Process(pid)
        proc.kill() if sig_name == "KILL" else proc.terminate()
        return {"ok": True, "pid": pid, "signal": sig_name}
    sig = signal.SIGKILL if sig_name == "KILL" else signal.SIGTERM
    os.kill(pid, sig)
    return {"ok": True, "pid": pid, "signal": sig.name}


def _mouse_coordinates(args: dict[str, Any]) -> tuple[int, int]:
    try:
        x = int(float(args.get("x")))
        y = int(float(args.get("y")))
    except (TypeError, ValueError) as exc:
        raise HomeAgentError("x and y must be numbers") from exc
    if not (0 <= x <= 10000 and 0 <= y <= 10000):
        raise HomeAgentError("mouse coordinates are outside the safety bounds")
    return x, y


def _mouse_move(args: dict[str, Any]) -> dict[str, Any]:
    x, y = _mouse_coordinates(args)
    _run(["xdotool", "mousemove", str(x), str(y)])
    return {"ok": True, "x": x, "y": y}


def _mouse_click(args: dict[str, Any]) -> dict[str, Any]:
    button = max(1, min(int(args.get("button", 1)), 5))
    if args.get("x") is not None and args.get("y") is not None:
        x, y = _mouse_coordinates(args)
        _run(["xdotool", "mousemove", str(x), str(y)])
    else:
        x = y = None
    _run(["xdotool", "click", str(button)])
    return {"ok": True, "button": button, "x": x, "y": y}


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
    if action == "windows":
        return _windows()
    if action == "file_list":
        return _file_list(args)
    if action == "file_read":
        return _file_read(args)
    if action == "file_search":
        return _file_search(args)
    if action == "screenshot":
        return _screenshot()
    if action == "clipboard_get":
        if platform.system() == "Darwin":
            text = _run(["pbpaste"])
        elif platform.system() == "Windows":
            text = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"])
        else:
            text = _run_first([["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]])
        return {"ok": True, "text": text[:100_000]}
    if action == "clipboard_set":
        return _clipboard_set(str(args.get("text") or "")[:100_000])
    if action == "kill_process":
        return _kill_process(args)
    if action == "shell":
        return _shell(args)
    if action == "file_write":
        return _file_write(args)
    if action == "lock":
        if platform.system() == "Windows":
            _run(["rundll32.exe", "user32.dll,LockWorkStation"])
        elif platform.system() == "Darwin":
            _run_first([["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession", "-suspend"], ["pmset", "displaysleepnow"]])
        else:
            _run_first([["loginctl", "lock-session"], ["gnome-screensaver-command", "-l"], ["xdg-screensaver", "lock"]])
        return {"ok": True}
    if action == "sleep":
        if platform.system() == "Windows":
            _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        elif platform.system() == "Darwin":
            _run(["pmset", "sleepnow"])
        else:
            _run_first([["systemctl", "suspend"], ["loginctl", "suspend"]])
        return {"ok": True}
    if action == "shutdown":
        if platform.system() == "Windows":
            _run(["shutdown.exe", "/s", "/t", "0"])
        elif platform.system() == "Darwin":
            _run(["osascript", "-e", 'tell application "System Events" to shut down'])
        else:
            _run_first([["systemctl", "poweroff"], ["loginctl", "poweroff"]])
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
    if action == "mouse_move":
        return _mouse_move(args)
    if action == "mouse_click":
        return _mouse_click(args)
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
        wid = str(args.get("id") or "").strip()
        if wid.startswith("0x") and all(ch in "0123456789abcdefABCDEFx" for ch in wid):
            _run(["wmctrl", "-ia" if action == "app_focus" else "-ic", wid])
            return {"ok": True, "id": wid}
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
            if length <= 0 or length > 1_200_000:
                raise HomeAgentError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            result = execute_action(payload.get("action"), payload.get("args"), confirmed=payload.get("confirmed") is True)
            self._send(200, result)
        except (HomeAgentError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception:
            self._send(500, {"error": "Home agent action failed"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[shadow-home-agent] {self.client_address or 'local-socket'} {fmt % args}")


class ThreadingUnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True


def main() -> None:
    token = os.getenv("SHADOW_HOME_AGENT_TOKEN", "").strip()
    if len(token) < 32:
        raise SystemExit("Set SHADOW_HOME_AGENT_TOKEN to a random secret of at least 32 characters")
    socket_path = os.getenv("SHADOW_HOME_AGENT_SOCKET", "").strip()
    if socket_path:
        path = Path(socket_path)
        if not path.is_absolute():
            raise SystemExit("SHADOW_HOME_AGENT_SOCKET must be an absolute path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        server = ThreadingUnixHTTPServer(str(path), Handler)
        os.chmod(path, 0o600)
        print(f"Shadow home agent listening on unix:{path}")
        try:
            server.serve_forever()
        finally:
            server.server_close()
            path.unlink(missing_ok=True)
        return
    bind = os.getenv("SHADOW_HOME_AGENT_BIND", "127.0.0.1").strip()
    port = int(os.getenv("SHADOW_HOME_AGENT_PORT", "8765"))
    if bind in {"0.0.0.0", "::"} and os.getenv("SHADOW_HOME_AGENT_ALLOW_WILDCARD", "").lower() not in {"1", "true", "yes"}:
        raise SystemExit("Refusing wildcard bind. Bind the Tailscale IP or set SHADOW_HOME_AGENT_ALLOW_WILDCARD=true explicitly.")
    print(f"Shadow home agent listening on {bind}:{port}")
    ThreadingHTTPServer((bind, port), Handler).serve_forever()

