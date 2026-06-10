#!/usr/bin/env python3
"""Shadow Desktop launcher.

Runs the *exact same* Docker Compose stack the VPS runs, then opens a native
window pointing at the local app — so the desktop app behaves identically to
the server deployment (chat, agents, MAGI, web research, music, …).

Flow:
  1. Pre-flight: Docker Engine + Compose present and reachable.
  2. Ensure a local ``.env`` exists (copied from the desktop template on a
     fresh checkout; an existing ``.env`` is never overwritten).
  3. ``docker compose up -d`` the parity service set (shadow + bgutil-pot;
     shadow's own ``depends_on`` pulls in searxng + chromadb).
  4. Wait for ``GET /api/health`` to come up (first run also builds the image).
  5. Open a native pywebview window on the app URL.
  6. On window close: ``docker compose stop`` the started services (containers
     and volumes are preserved; nothing is removed).

Modes (no GUI / no pywebview needed):
  --check      validate Docker + compose config, then exit (CI / smoke test)
  --headless   start the stack, wait for health, print the URL, do not open a
               window (useful on servers / for testing)
  --stop       stop the parity services and exit
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration (overridable via environment)
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_FILE = REPO_ROOT / ".env"
ENV_TEMPLATE = Path(__file__).resolve().parent / "shadow-desktop.env.example"

# shadow's depends_on already brings up searxng + chromadb; bgutil-pot powers
# the Music PO-token pipeline. meshcentral / telegram / ntfy are intentionally
# left out of the default desktop set.
SERVICES = os.getenv("SHADOW_DESKTOP_SERVICES", "shadow bgutil-pot").split()
APP_BIND = os.getenv("APP_BIND", "127.0.0.1")
APP_PORT = os.getenv("APP_PORT", "7000")
APP_URL = os.getenv("SHADOW_DESKTOP_URL", f"http://{APP_BIND}:{APP_PORT}")
HEALTH_URL = f"{APP_URL.rstrip('/')}/api/health"
START_TIMEOUT = int(os.getenv("SHADOW_DESKTOP_TIMEOUT", "900"))  # first build is slow
STOP_ON_EXIT = os.getenv("SHADOW_DESKTOP_STOP_ON_EXIT", "1").lower() not in {"0", "false", "no"}
WINDOW_TITLE = os.getenv("SHADOW_DESKTOP_TITLE", "Shadow")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[shadow-desktop] {msg}", flush=True)


def _compose_base() -> list[str] | None:
    """Return the compose command prefix, or None if Docker Compose is absent."""
    if shutil.which("docker"):
        try:
            subprocess.run(
                ["docker", "compose", "version"],
                check=True, capture_output=True, timeout=20,
            )
            return ["docker", "compose", "-f", str(COMPOSE_FILE)]
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass
    if shutil.which("docker-compose"):  # legacy standalone binary
        return ["docker-compose", "-f", str(COMPOSE_FILE)]
    return None


def preflight() -> list[str]:
    """Validate the environment and return the compose command prefix."""
    if not COMPOSE_FILE.is_file():
        raise SystemExit(f"docker-compose.yml not found at {COMPOSE_FILE}")

    if not shutil.which("docker"):
        raise SystemExit(
            "Docker is not installed.\n"
            "Install Docker Desktop (Windows/macOS) or Docker Engine (Linux), "
            "then relaunch Shadow."
        )

    # Engine reachable?
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise SystemExit(
            "Docker is installed but the engine is not running.\n"
            "Start Docker Desktop / the Docker service and relaunch Shadow."
        )

    base = _compose_base()
    if base is None:
        raise SystemExit(
            "Docker Compose v2 is required (the 'docker compose' subcommand). "
            "Update Docker, then relaunch Shadow."
        )
    return base


def ensure_env() -> None:
    """Create a local .env from the desktop template only if one is missing."""
    if ENV_FILE.exists():
        return
    if ENV_TEMPLATE.is_file():
        shutil.copyfile(ENV_TEMPLATE, ENV_FILE)
        log(f"created {ENV_FILE.name} from desktop template (edit it to add API keys)")
    else:
        log("no .env found and no template available; using compose defaults")


def compose_config_ok(base: list[str]) -> bool:
    """Validate the merged compose + env wiring without starting anything."""
    proc = subprocess.run([*base, "config"], capture_output=True, text=True)
    if proc.returncode != 0:
        log("compose config error:\n" + (proc.stderr or proc.stdout))
        return False
    return True


def start_stack(base: list[str]) -> None:
    log(f"starting services: {' '.join(SERVICES)} (first run builds the image — this can take a few minutes)")
    # --build keeps the local image current with the source tree, matching the
    # VPS deploy model. Output streams so the first-run build is visible.
    subprocess.run([*base, "up", "-d", "--build", *SERVICES], check=True)


def stop_stack(base: list[str]) -> None:
    log("stopping services (containers and volumes are preserved)")
    try:
        subprocess.run([*base, "stop", *SERVICES], check=False, timeout=120)
    except subprocess.TimeoutExpired:
        log("stop timed out; leaving containers running")


def wait_for_health(timeout: int, on_status=None) -> bool:
    """Poll the app until it answers. Any HTTP response (even 401/302) means
    uvicorn is up; we only treat connection failures as 'not ready yet'."""
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            req = urllib.request.Request(HEALTH_URL, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status < 500:
                    return True
        except urllib.error.HTTPError as exc:  # server answered -> it's up
            if exc.code < 500:
                return True
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError):
            pass
        if on_status and attempt % 3 == 0:
            on_status(f"Starting Shadow… ({int(deadline - time.time())}s)")
        time.sleep(2)
    return False


# --------------------------------------------------------------------------- #
# Splash + error pages (shown in the native window before the app is ready)
# --------------------------------------------------------------------------- #
# The Shadow serpent emblem (96px PNG), embedded so the splash needs no assets.
EMBLEM_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAMAAADVRocKAAABYlBMVEUHCAoHCAkGBwkGBggEBAUEBQYAAAABAQECAgIDAwMEBAQFBQUICAkVFh"
    "g6PEISEhMaGxwNDg8lJikMDAxYWFhnZmdkZGREREQVFRUTExOCgoKYmJjo6Ojs7O2wsLCBgYJ5eHkgICAqKiuoqKh+fX4dHR0BAAEuLi7X19gs"
    "LCwPDw8KCgohISFlZWUrKysICAgQEBB+fn7x8fL7+/zn5+jt7e7l5ebx8fGGhYaRkZGEhIVaWlqIiIhTU1MtLS05OTmEhIT///+Pj5CAgICQj5"
    "B3d3eBgYG7u7yysrPDw8Svr7AnJyd3d3jc3N18fHzCwsPS0tLT09S9vb2ioqOJiYnp6enBwMHGxsYWFhYtLS5KSUo3NjdpaWqOjo5mZmbw8PHh"
    "4eKHh4hgYGB9fX1/f4C5ubk1NDURERFGRkaOjY7Hx8j09PSmpqdZWVk+Pj5KSkpAQEAeHh5ubW4cHBwLCwsODg4Vs/qwAAAC4klEQVRo3u2Z51"
    "fUQBDAz4Pb2c3ZEbFwWBAbKiLYCyAoxd4VFXvv7f83JBdMQmZms8V7T2/euw/3bjO/adnbnalU2tIWR7KiUnUsocqMVF2bXM1/7eisOZTOjhyh"
    "WqkJp1L7HwEgpVJBKEopCY4BIFUQam2qheirAlcAKLYYQgg4AIR6UDXhb9ISAIGiYx0geE2AYtTja7QAMmCLJfayIE46AB3z0ZUaADR/BSKD0g"
    "Coa4XnT5hyyzkABCW0xw5nCQygvP48gQHUy+sPCfoAvfLMS8ZtEqBK1E9a0rVEAaR2/eclZRkFMEjw8kcJgFkCYoEl53GASYWmgpRYhwM4/Yx/"
    "yeMoAPgM11dSAMkAuAysWi3WrKUcVDSAdWDdemZBMwsYQFmUUMYFDMCXEGsBCdBIMStxEBCAfYQSIxGA1UuWiRHmAfdweDxlt9oAB7hIQTPOxQ"
    "BJWweia0P3xp5NnJcSBXA53rxla2+jbxsDAFMAiO07xM7+XQNC7SbXKRxAei727N23f/DAwUNDzNuGA+gqPTx8ZKQxevTY8d4Tg+TCwAigxMlT"
    "A92nz5w9NzY+cV5ID4DJkakLF6dnZucuXb5C+moGEKLr6rXrN27eun3nbs89wxAx79n9B/MPw9j0P3q8QIaIqCKiOkAMN0afPO1bePb8xUvSDj"
    "ADiKFXr9+8ffd+4sPHT3SZEgB6q/j85ev8NzH2/YegdxRiq2A2u5+/ILKd2U+JzY4uI4g+kv1TIrbrv/CHY3hwzzhKAbz/6buIUawCPXhZx4g5"
    "eNkd3iMH6KOj1fUjFSEcYH5BiyV5S8wvIBxAcADvVyi7S+CSda28xvq/iJdqRaUlbVlrmyGG7ZzMQ61uSHlvqRV0+WhZ1kNsfVtT+G/M+m8tL4"
    "ZJI9cWzXGh0d5Xdu39CEEOKLDfyo1YCmcprkYsTWXR6Ck1JFIOh0QJZFFrPOaSzsdc5eVfBHge93ofWPsfubelLebyG8sftbVFcVtaAAAAAElF"
    "TkSuQmCC"
)


def _page(title_en: str, title_jp: str, detail: str, spinner: bool) -> str:
    spin = (
        '<div class="spin"></div>' if spinner else '<div class="err">!</div>'
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;height:100%;background:#07080a;color:#e8d8b0;
    font-family:ui-monospace,"JetBrains Mono",Menlo,monospace;}}
  .wrap{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;}}
  .emblem{{width:88px;height:88px;image-rendering:auto;
    filter:drop-shadow(0 0 14px rgba(255,176,0,.28));}}
  .mark{{color:#ffb000;letter-spacing:.32em;font-size:12px;}}
  .en{{font-size:22px;font-weight:800;letter-spacing:.18em;color:#ffb000;}}
  .jp{{font-size:13px;opacity:.8;color:#ffb000;}}
  .detail{{font-size:12px;color:#8a7c5f;min-height:16px;text-align:center;padding:0 24px;}}
  .spin{{width:34px;height:34px;border:3px solid #2a2118;border-top-color:#ffb000;border-radius:50%;
    animation:spin 1s linear infinite;}}
  .err{{width:34px;height:34px;border:3px solid #ff3b3b;border-radius:50%;color:#ff3b3b;
    display:flex;align-items:center;justify-content:center;font-weight:800;font-size:20px;}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}
</style></head><body><div class="wrap">
  <img class="emblem" alt="Shadow" src="data:image/png;base64,{EMBLEM_B64}">
  <div class="mark">SHADOW</div>{spin}
  <div class="en">{title_en}</div><div class="jp">{title_jp}</div>
  <div class="detail" id="detail">{detail}</div>
</div></body></html>"""


SPLASH_HTML = _page("STARTING", "起動中", "Bringing up the Shadow stack…", spinner=True)


def error_html(message: str) -> str:
    safe = message.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    return _page("MALFUNCTION", "停止", safe, spinner=False)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def run_gui(base: list[str]) -> int:
    try:
        import webview  # imported lazily so --check / --headless need no GUI deps
    except ImportError:
        raise SystemExit(
            "pywebview is not installed. Run desktop/run.sh (Linux/macOS) or "
            "desktop/run.ps1 (Windows), or: pip install pywebview"
        )

    window = webview.create_window(
        WINDOW_TITLE, html=SPLASH_HTML, width=1280, height=860, min_size=(900, 600),
    )

    def worker() -> None:
        def set_detail(text: str) -> None:
            esc = text.replace("'", "\\'")
            try:
                window.evaluate_js(f"document.getElementById('detail').innerText='{esc}'")
            except Exception:
                pass
        try:
            ensure_env()
            set_detail("Starting containers…")
            start_stack(base)
            set_detail("Waiting for the app to come up…")
            if not wait_for_health(START_TIMEOUT, on_status=set_detail):
                window.load_html(error_html(
                    f"Shadow did not become healthy within {START_TIMEOUT}s.\n"
                    f"Check container logs:  docker compose logs shadow"
                ))
                return
            window.load_url(APP_URL)
        except SystemExit as exc:
            window.load_html(error_html(str(exc)))
        except subprocess.CalledProcessError as exc:
            window.load_html(error_html(f"Failed to start the stack:\n{exc}"))
        except Exception as exc:  # noqa: BLE001
            window.load_html(error_html(f"Unexpected error:\n{exc}"))

    webview.start(worker)  # blocks until the window is closed
    if STOP_ON_EXIT:
        stop_stack(base)
    return 0


def run_headless(base: list[str]) -> int:
    ensure_env()
    start_stack(base)
    log("waiting for health…")
    if not wait_for_health(START_TIMEOUT, on_status=lambda s: log(s)):
        log(f"app did not become healthy within {START_TIMEOUT}s")
        return 1
    log(f"Shadow is up at {APP_URL}")
    log("(--headless: not opening a window; use --stop to shut the stack down)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow Desktop launcher")
    parser.add_argument("--check", action="store_true",
                        help="validate Docker + compose config, then exit")
    parser.add_argument("--headless", action="store_true",
                        help="start the stack and wait for health without opening a window")
    parser.add_argument("--stop", action="store_true",
                        help="stop the parity services and exit")
    args = parser.parse_args()

    base = preflight()

    if args.check:
        ok = compose_config_ok(base)
        log("compose config OK" if ok else "compose config FAILED")
        log(f"services={SERVICES}  url={APP_URL}  health={HEALTH_URL}")
        return 0 if ok else 1

    if args.stop:
        stop_stack(base)
        return 0

    if args.headless:
        return run_headless(base)

    return run_gui(base)


if __name__ == "__main__":
    sys.exit(main())
