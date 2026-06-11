#!/usr/bin/env python3
"""Outbound Shadow device agent.

The agent enrolls once, stores its device credential locally, then long-polls
the user's Shadow server for owner-scoped jobs. It never opens a listening port.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from companion.home_agent import ALL_ACTIONS, HomeAgentError, execute_action
except ImportError:  # Standalone installer places both files together.
    from home_agent import ALL_ACTIONS, HomeAgentError, execute_action

try:  # Workspace actions are optional: older installs simply lack them.
    from companion.workspace_agent import WS_ALL_ACTIONS
except ImportError:
    try:
        from workspace_agent import WS_ALL_ACTIONS
    except ImportError:
        WS_ALL_ACTIONS = frozenset()


def _config_path() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.getenv("APPDATA") or Path.home())
        return base / "Shadow" / "device.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Shadow" / "device.json"
    return Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "shadow" / "device.json"


def _post(server: str, path: str, payload: dict[str, Any], token: str = "", timeout: float = 40) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "ShadowDevice/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{server.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"Shadow returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot reach Shadow: {exc}") from exc


def _metadata(name: str = "") -> dict[str, Any]:
    return {
        "name": name.strip() or socket.gethostname() or "Shadow device",
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "capabilities": sorted(ALL_ACTIONS | WS_ALL_ACTIONS),
    }


def enroll(server: str, code: str, name: str = "") -> dict[str, Any]:
    result = _post(server, "/api/shadow/device/enroll", {"code": code, **_metadata(name)})
    config = {
        "server": server.rstrip("/"),
        "device_id": result["device"]["id"],
        "token": result["token"],
        "name": result["device"]["name"],
    }
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return config


def load_config() -> dict[str, Any]:
    path = _config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Device is not enrolled. Missing {path}") from exc
    if not config.get("server") or not config.get("token"):
        raise RuntimeError(f"Device configuration is incomplete: {path}")
    return config


def run(config: dict[str, Any]) -> None:
    server = str(config["server"]).rstrip("/")
    token = str(config["token"])
    delay = 2.0
    print(f"Shadow device online: {config.get('name') or platform.node()} -> {server}", flush=True)
    while True:
        try:
            payload = _post(
                server,
                "/api/shadow/device/poll",
                {"timeout": 25, **_metadata(str(config.get("name") or ""))},
                token,
                timeout=35,
            )
            job = payload.get("job")
            if not job:
                delay = 2.0
                continue
            try:
                result = execute_action(
                    job.get("action"),
                    job.get("args"),
                    confirmed=job.get("confirmed") is True,
                )
                completion = {"job_id": job["id"], "result": result}
            except (HomeAgentError, Exception) as exc:
                completion = {"job_id": job["id"], "error": str(exc)[:1000]}
            _post(server, "/api/shadow/device/result", completion, token, timeout=20)
            delay = 2.0
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[shadow-device] {exc}; retrying in {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 1.7, 30.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect this PC to a Shadow account")
    parser.add_argument("--server", help="Shadow HTTPS origin, e.g. https://shadow.example.com")
    parser.add_argument("--enroll", metavar="CODE", help="Single-use enrollment code from Command")
    parser.add_argument("--name", default="", help="Device name shown in Command")
    parser.add_argument("--once", action="store_true", help="Enroll and exit")
    args = parser.parse_args()

    config = enroll(args.server, args.enroll, args.name) if args.enroll else load_config()
    if args.once:
        print(f"Enrolled {config['name']} ({config['device_id']})")
        return
    run(config)


if __name__ == "__main__":
    main()
