#!/usr/bin/env bash
# Install Shadow into the current user's desktop (app menu + serpent icon),
# wired to launch desktop/run.sh. No root required. Uninstall with --uninstall.
set -euo pipefail

APP_ID="io.github.haykdevx.shadow"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ASSETS="$REPO/desktop/assets"
RUN="$REPO/run.sh"

APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$APPS_DIR/$APP_ID.desktop"
  for s in 16 24 32 48 64 128 256 512; do
    rm -f "$ICONS_DIR/${s}x${s}/apps/$APP_ID.png"
  done
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true
  echo "Shadow removed from the app menu."
  exit 0
fi

mkdir -p "$APPS_DIR"
for s in 16 24 32 48 64 128 256 512; do
  mkdir -p "$ICONS_DIR/${s}x${s}/apps"
  cp "$ASSETS/icon-${s}.png" "$ICONS_DIR/${s}x${s}/apps/$APP_ID.png"
done

sed "s#__EXEC__#$RUN#g" "$HERE/shadow.desktop" > "$APPS_DIR/$APP_ID.desktop"
chmod +x "$RUN" "$APPS_DIR/$APP_ID.desktop" 2>/dev/null || true

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$ICONS_DIR" >/dev/null 2>&1 || true

echo "Installed. Search 'Shadow' in your app menu (you may need to log out/in once)."
