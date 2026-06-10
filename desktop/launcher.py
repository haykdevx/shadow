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
# Keep the stack running when the window closes (default) so the app stays
# reachable and you can reopen instantly. Set SHADOW_DESKTOP_STOP_ON_EXIT=1 to
# shut the containers down on close.
STOP_ON_EXIT = os.getenv("SHADOW_DESKTOP_STOP_ON_EXIT", "0").lower() in {"1", "true", "yes"}
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
    "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"
    "SUVORK5CYII="
)


def _splash_html() -> str:
    # Mojang-style intro: pure black, the SHADOW wordmark revealing letter by
    # letter in white, then a quiet tagline and an indeterminate loading bar.
    letters = "".join(f"<span>{c}</span>" for c in "SHADOW")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;height:100%;background:#000;color:#fff;overflow:hidden;
    font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}}
  .wrap{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;}}
  .emblem{{width:60px;height:60px;margin-bottom:28px;opacity:0;
    filter:grayscale(1) brightness(1.7);animation:fade 1s ease .1s forwards;}}
  .word{{display:flex;gap:.34em;font-weight:800;font-size:54px;line-height:1;letter-spacing:.06em;}}
  .word span{{opacity:0;transform:translateY(12px);
    animation:rise .55s cubic-bezier(.2,.7,.2,1) forwards;}}
  .word span:nth-child(1){{animation-delay:.25s}}
  .word span:nth-child(2){{animation-delay:.40s}}
  .word span:nth-child(3){{animation-delay:.55s}}
  .word span:nth-child(4){{animation-delay:.70s}}
  .word span:nth-child(5){{animation-delay:.85s}}
  .word span:nth-child(6){{animation-delay:1.00s}}
  .tag{{margin-top:22px;color:#9a9a9a;font-size:13px;letter-spacing:.12em;
    text-align:center;padding:0 24px;opacity:0;animation:fade 1.2s ease 1.3s forwards;}}
  .bar{{margin-top:30px;width:240px;height:2px;background:#161616;overflow:hidden;
    opacity:0;animation:fade .6s ease 1.5s forwards;}}
  .bar i{{display:block;height:100%;width:38%;background:#fff;
    animation:slide 1.25s ease-in-out infinite;}}
  @keyframes rise{{to{{opacity:1;transform:none}}}}
  @keyframes fade{{to{{opacity:1}}}}
  @keyframes slide{{0%{{transform:translateX(-110%)}}100%{{transform:translateX(360%)}}}}
  @media (prefers-reduced-motion: reduce){{
    .emblem,.word span,.tag,.bar{{animation:none;opacity:1;transform:none;}}
    .bar i{{animation:none;width:100%;}}
  }}
</style></head><body><div class="wrap">
  <img class="emblem" alt="" src="data:image/png;base64,{EMBLEM_B64}">
  <div class="word">{letters}</div>
  <div class="tag" id="detail">i know where you live</div>
  <div class="bar"><i></i></div>
</div></body></html>"""


SPLASH_HTML = _splash_html()


def error_html(message: str) -> str:
    safe = message.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;height:100%;background:#000;color:#fff;
    font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}}
  .wrap{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;
    gap:14px;padding:0 28px;text-align:center;}}
  .x{{width:30px;height:30px;border:2px solid #ff3b3b;border-radius:50%;color:#ff3b3b;
    display:flex;align-items:center;justify-content:center;font-weight:800;}}
  .t{{font-size:20px;font-weight:800;letter-spacing:.2em;}}
  .m{{font-size:12px;color:#9a9a9a;line-height:1.55;max-width:560px;}}
</style></head><body><div class="wrap">
  <div class="x">!</div><div class="t">MALFUNCTION</div>
  <div class="m">{safe}</div>
</div></body></html>"""


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
APP_ID = "io.github.haykdevx.shadow"  # matches the installed .desktop filename


def _brand_process() -> None:
    """Make the OS see this window as 'Shadow' (not 'launcher.py').

    The dock label/icon come from matching the window to the installed Shadow
    .desktop entry. On Wayland the match key is the window's app_id; on X11 it
    is WM_CLASS. GTK derives both from the program name, so we set it to the
    .desktop id (and a human app name). Must run before the GUI loop starts.
    """
    sys.argv[0] = APP_ID
    try:
        from gi.repository import GLib  # GTK backend
        GLib.set_prgname(APP_ID)
        GLib.set_application_name("Shadow")
    except Exception:
        pass


def run_gui(base: list[str]) -> int:
    try:
        import webview  # imported lazily so --check / --headless need no GUI deps
    except ImportError:
        raise SystemExit(
            "pywebview is not installed. Run desktop/run.sh (Linux/macOS) or "
            "desktop/run.ps1 (Windows), or: pip install pywebview"
        )

    _brand_process()
    window = webview.create_window(
        WINDOW_TITLE, html=SPLASH_HTML, width=1280, height=860, min_size=(900, 600),
    )

    def worker() -> None:
        try:
            ensure_env()
            start_stack(base)
            # The splash tagline stays put ("i know where you live"); the
            # animated bar conveys progress through the first-run build.
            if not wait_for_health(START_TIMEOUT):
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

    # private_mode=False + a persistent storage_path are REQUIRED for login to
    # work: pywebview's default private mode does not persist cookies, so the
    # session cookie set on /api/auth/login is dropped and the app bounces back
    # to the login screen (the inputs "clear"). This keeps the session.
    storage_path = str(Path.home() / ".shadow-desktop" / "webview")
    os.makedirs(storage_path, exist_ok=True)
    webview.start(worker, private_mode=False, storage_path=storage_path)  # blocks until closed
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
