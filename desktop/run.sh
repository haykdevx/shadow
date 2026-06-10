#!/usr/bin/env bash
# Backward-compatible alias. The canonical entry point is the repo-root
# ./run.sh, which does one-time setup (webview backend + dock entry) then
# launches. This just forwards to it.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/../run.sh" "$@"
