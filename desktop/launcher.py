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

    class _DesktopApi:
        """Bridge exposed to the page as window.pywebview.api."""
        def open_browser(self, url: str = "") -> bool:
            # Launch the Chromium browser as a separate process so its Qt loop
            # doesn't collide with pywebview's GTK loop. sys.executable is the
            # launcher venv's python (which has PyQt6-WebEngine).
            try:
                browser_py = str(Path(__file__).resolve().parent / "browser.py")
                subprocess.Popen([sys.executable, browser_py, url or ""])
                return True
            except Exception as exc:  # noqa: BLE001
                log(f"failed to open browser: {exc}")
                return False

    window = webview.create_window(
        WINDOW_TITLE, html=SPLASH_HTML, width=1280, height=860, min_size=(900, 600),
        js_api=_DesktopApi(),
    )

    # Performance overlay injected into every loaded page. In the WebKitGTK
    # software-rendering path, backdrop-filter blurs and the animated background
    # patterns repaint the whole window every frame and cause the lag. Killing
    # those (and snapping transitions) makes the UI feel smooth without an image
    # rebuild — it takes effect on relaunch only.
    import json as _json
    _perf_css = (
        "*,*::before,*::after{backdrop-filter:none!important;"
        "-webkit-backdrop-filter:none!important;}"
        "body[class*='bg-pattern']{animation:none!important;background-attachment:scroll!important;}"
        "body[class*='bg-pattern']::before,body[class*='bg-pattern']::after{animation:none!important;}"
        "*{transition-duration:120ms!important;}"
    )

    # Floating "Browser" button that launches the Chromium window via the API.
    _fab_js = (
        "(function(){if(!document.body||document.getElementById('shadow-browser-fab'))return;"
        "var b=document.createElement('button');b.id='shadow-browser-fab';b.title='Open browser';"
        "b.innerHTML='\\u{1F310}';"
        "b.style.cssText='position:fixed;right:18px;bottom:18px;z-index:99999;width:46px;height:46px;"
        "border-radius:50%;border:1px solid rgba(255,255,255,.22);background:rgba(20,24,32,.92);"
        "color:#fff;font-size:20px;line-height:46px;cursor:pointer;padding:0;box-shadow:0 4px 14px rgba(0,0,0,.4)';"
        "b.onclick=function(){try{window.pywebview.api.open_browser('');}catch(e){}};"
        "document.body.appendChild(b);})();"
    )

    def _inject_perf(*_a) -> None:
        js = (
            "(function(){if(document.getElementById('shadow-perf'))return;"
            "var s=document.createElement('style');s.id='shadow-perf';"
            "s.textContent=" + _json.dumps(_perf_css) + ";"
            "(document.head||document.documentElement).appendChild(s);})();"
        )
        try:
            window.evaluate_js(js)
            window.evaluate_js(_fab_js)
        except Exception:
            pass

    try:
        window.events.loaded += _inject_perf
    except Exception:
        pass

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
