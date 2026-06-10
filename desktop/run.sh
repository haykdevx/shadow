#!/usr/bin/env bash
# Shadow Desktop — Linux / macOS launcher.
# Creates an isolated venv for the pywebview shell and starts the app.
# The app itself runs in Docker; this script only needs Python 3 + Docker.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv-desktop"
PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 is required (set \$PYTHON to override)." >&2
  exit 1
fi

if [ ! -d "$VENV" ]; then
  echo "[shadow-desktop] creating launcher venv…"
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$HERE/requirements-desktop.txt"
fi

# Linux note: pywebview needs a system webview backend. If the window fails to
# open, install one of:
#   Debian/Ubuntu: sudo apt install python3-gi gir1.2-webkit2-4.1 libgtk-3-0
#   (or) pip install 'pywebview[qt]'   with PyQt/PySide present
exec "$VENV/bin/python" "$HERE/launcher.py" "$@"
