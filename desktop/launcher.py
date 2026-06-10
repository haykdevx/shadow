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
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAMAAADVRocKAAAB+FBMVEUAAAABAQECAgIDAwMEBAQFBQUEAwQUFRULCwsaGx1KTFMCAwMZGRoNDg"
    "4MDQ4kJSgDAgM+Pj5+fn6KioqOjY5paGlBQEEPDw8GBQZaWlqPj5ChoaHg4OHk5OWwsLCpqalFRUVNTU1dXV0IBwheXl/BwcKXl5dVVFUUFBSs"
    "rKxGRkYEAwMsLCxsbGxDQkPf3+C8vLxycnJra2ttbG13d3eQkJCTk5N6entUVFQaGhpHRke6urrr6+zu7u/p6ero6Onj4+PW1tbV1dbk5OTq6u"
    "u9vL1AP0AKCgqrq6uBgYEjIyNOTU5XVldSUlKFhIUtLS1BQUGMjIzn5+i8vL0ZGRkJCQmpqKnd3d5paWlpaWp8fH2xsbKfn6CsrK2rq6x/f4An"
    "JyeHh4g5OTpAQEC7u7zi4uPl5eXe3t7Pz9DCwsPX19jw8PGGhoY3NzcTExM4ODhJSUpsa2xLS0wwMDAcHBwQDxCCgoOvr68mJiZtbW2IiIiDg4"
    "SZmZlJSUkXFxdwcHCWlpdLSkulpaUfHx+3t7jT09TLy8zZ2dnk4+TJycmbm5uenp6Af4CcnJ1UVFUrKysWFhYqKipaWlvCwsLx8fLKysqTk5Q3"
    "NzgQEBBnZ2dHR0c7OztMTExTUlMeHh6UlJQREREICAgiIiInJygbGhsGBgYCAQIFBAX80R9uAAADBUlEQVRo3u1aZ1cUQRCcnd1FzOCZRT0QFS"
    "MmFBMoYhYxZzFnBbOCopgxY8458Dfd9bzHxtuamZ4Pvkd/u7c7Vd3VPeF6lrFu+3/MMDg3/xrnhkEMzk3Lslxk1wyHx/1JRuKgmzwqIBISHgXu"
    "eWpxHCsaPsFHwwlPGt7E3DNtOQqORw964jcrTyhY0XRzS3CAIRaEKaGqSLLlao9bcLSS0wccKI0PDlXAdyx5sBo+wKCI7ziYgK+2diUymMr47u"
    "qe4xlaybIqkODnyKNqgrMWlwYagVyLSSUZfgyUzAoaZ5GVRBgAY3ZEAOAUcAqhR37iWxFygAH07NWb9emb/F4IjoMZ6Ncfq+WQIFRzIGvBuZC0"
    "CIpbwGOzgJqA+zWiViikCblCAUi0hkTMV0d0fyZinNagkD8JAgR4rB5QdBbYhQNSAwcNHgISdPliYDkeOmz4iKKRo0anMQJPYsEiKi4ZY5eOHT"
    "e+DCToKiMOLdVlEyYyNmnylEKwpj2o2F4wtXza9BkzZ1XMnlM5F3nfIzxEMC81f8HCqupFi2uW1C5dhkQgRrB8xcpVq9fUrS2ur1+3fsPGTeQE"
    "m7dsTW3bvmPnrt0Ne/buK9pPTsAOHDxUVX34yNFjx0+cTNecEiOApmdj0+kzZ8+dv5Auv1hyqVGIACrTy80tV662XuP519tu3GwFBnhQkYl26/"
    "adu/fa2+8/ePio8vETxCOP8MhS0dTwtOPZ84oXtS9fvX4DSeoRHlns3r57/+Hjp88ddV9KvyLw/m0YWU2/ff/x85frGAbvB6Xf8wOyUPw3C9pv"
    "b6jaN33txxb9B68C8iQEVNd++NV+fCevo3Dhd9IShCWnnWtRGwBpmqPAsMORfAD6WwmEhRTXMSJr58R6StWQin2ivaXG8gjSkDuV6m3NJBVszY"
    "1Z5XUbaV6r4COnXN3tff0XFEz7FQvTf0mk/5pL2CFTovZ4J0whddXojsMouNJ9rNbr3ox7FjdyPlWC/+djZBzcJLzXz340YGQ+G8h8NEB9Xtb6"
    "2UO36bQ/gtlC3GQe87EAAAAASUVORK5CYII="
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
