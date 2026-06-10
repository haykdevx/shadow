#!/usr/bin/env python3
"""Freeze the Shadow Desktop launcher into a native binary with PyInstaller.

This packages the *launcher shell* (the pywebview window + Docker orchestration)
into a single executable named per-platform:

    Linux    -> dist/Shadow            (wrap into .AppImage/.deb separately)
    macOS    -> dist/Shadow.app
    Windows  -> dist/Shadow.exe        (build with run.ps1's venv)

Runtime requirements of the produced binary (same as the VPS):
  * Docker Engine / Docker Desktop installed and running.
  * The Shadow source tree + docker-compose.yml present next to the binary
    (the launcher drives `docker compose` against ../docker-compose.yml), OR
    point the launcher at prebuilt images by editing the compose `image:` keys.

Usage:
    pip install pyinstaller pywebview
    python desktop/build.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "launcher.py"


def main() -> int:
    if shutil.which("pyinstaller") is None:
        print("pyinstaller not found. Install with: pip install pyinstaller", file=sys.stderr)
        return 1

    windowed = "--windowed" if sys.platform in {"darwin", "win32"} else "--noconsole"
    cmd = [
        "pyinstaller",
        "--clean",
        "--noconfirm",
        "--name", "Shadow",
        "--onefile",
        windowed,
        # The env template ships inside the bundle so a fresh machine can seed .env.
        "--add-data", f"{HERE / 'shadow-desktop.env.example'}{_sep()}.",
    ]
    # Platform app icon from the serpent emblem.
    icon = {"win32": HERE / "assets" / "shadow.ico",
            "darwin": HERE / "assets" / "shadow.icns"}.get(sys.platform)
    if icon and icon.exists():
        cmd += ["--icon", str(icon)]
    cmd.append(str(LAUNCHER))
    print("[build]", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=HERE)
    if proc.returncode == 0:
        print(f"\nDone. Binary in: {HERE / 'dist'}")
        print("Ship it alongside the Shadow source + docker-compose.yml.")
    return proc.returncode


def _sep() -> str:
    # PyInstaller --add-data uses ';' on Windows, ':' elsewhere.
    return ";" if sys.platform == "win32" else ":"


if __name__ == "__main__":
    raise SystemExit(main())
