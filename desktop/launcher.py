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
