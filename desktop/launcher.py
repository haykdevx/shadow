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
