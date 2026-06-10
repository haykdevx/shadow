#!/usr/bin/env bash
# Shadow — one command to rule them all.
#
#   ./run.sh
#
# First run: sets up an isolated launcher environment (pywebview + a webview
# backend, no sudo), then installs a "Shadow" entry into your app menu so you
# can PIN IT TO YOUR DOCK and just tap to start from then on.
# Every run after that: opens the window straight away (no setup, no commands).
#
# The app itself runs in Docker for full parity with the server deployment;
# this script only needs Python 3 + Docker installed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESK="$HERE/desktop"
VENV="$DESK/.venv-desktop"
MARKER="$DESK/.installed"
PY="${PYTHON:-python3}"
OS="$(uname -s)"

if [ ! -d "$VENV" ] || [ ! -f "$MARKER" ]; then
  echo "[shadow] first-run setup (one time)…"

  command -v "$PY" >/dev/null 2>&1 || { echo "Python 3 is required (set \$PYTHON to override)." >&2; exit 1; }
  command -v docker >/dev/null 2>&1 || echo "[shadow] WARNING: Docker not found — install Docker Desktop/Engine to run Shadow."

  # On Linux, the GTK webview backend uses the system 'gi' module, so the venv
  # must be able to see system packages.
  VENV_FLAGS=""
  [ "$OS" = "Linux" ] && VENV_FLAGS="--system-site-packages"
  [ -d "$VENV" ] || "$PY" -m venv $VENV_FLAGS "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet pywebview

  # Guarantee a working webview backend without requiring sudo/apt:
  # prefer system GTK if present, otherwise pip-install Qt into the venv.
  if [ "$OS" = "Linux" ]; then
    if "$VENV/bin/python" - <<'PYEOF' >/dev/null 2>&1
import gi
ok = False
for v in ("4.1", "4.0"):
    try:
        gi.require_version("WebKit2", v)
        from gi.repository import WebKit2  # noqa: F401
        ok = True
        break
    except Exception:
        pass
raise SystemExit(0 if ok else 1)
PYEOF
    then
      echo "[shadow] using system GTK webview backend"
    else
      echo "[shadow] installing Qt webview backend (one-time, ~100MB)…"
      "$VENV/bin/pip" install --quiet qtpy PyQt6 PyQt6-WebEngine
    fi
  fi

  # Add Shadow to the app menu / dock (Linux). Safe to re-run.
  if [ "$OS" = "Linux" ] && [ -f "$HERE/packaging/linux/install-local.sh" ]; then
    bash "$HERE/packaging/linux/install-local.sh" || true
  fi

  touch "$MARKER"
  echo "[shadow] setup complete — search 'Shadow' in your app menu and pin it to the dock."
fi

exec "$VENV/bin/python" "$DESK/launcher.py" "$@"
