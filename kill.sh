#!/usr/bin/env bash
# Shadow — complete uninstaller (Linux / macOS).
#
#   ./kill.sh                 # asks for confirmation
#   ./kill.sh --yes           # no prompt
#   ./kill.sh --yes --purge-repo   # also delete the repo directory itself
#
# Removes EVERYTHING Shadow put on this machine — Docker containers/volumes/
# networks/locally-built images, the desktop launcher venv, the app-menu/dock
# entry and icons, webview storage, local app data, and (optionally) the repo
# folder. Scoped strictly to Shadow's own footprint — it never runs a global
# Docker prune or touches unrelated files.
set -uo pipefail   # best-effort: keep going even if a step has nothing to remove

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.haykdevx.shadow"
COMPOSE="$HERE/docker-compose.yml"

FORCE=0; PURGE_REPO=0; RMI="local"
for a in "$@"; do
  case "$a" in
    -y|--yes|--force) FORCE=1 ;;
    --purge-repo)     PURGE_REPO=1 ;;
    --rmi-all)        RMI="all" ;;   # also drop pulled base images used by Shadow
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

echo "This PERMANENTLY removes Shadow from this machine:"
echo "  - Docker containers, volumes, networks, and images (--rmi $RMI) for this project"
echo "  - desktop launcher venv + app-menu/dock entry + icons"
echo "  - webview storage (~/.shadow-desktop) and the first-run marker"
echo "  - local app data + secrets: $HERE/{data,logs,.env}"
[ "$PURGE_REPO" = 1 ] && echo "  - the ENTIRE repo directory: $HERE"
echo
if [ "$FORCE" != 1 ]; then
  printf "Type DELETE to confirm: "
  read -r ans
  [ "$ans" = "DELETE" ] || { echo "Aborted — nothing was removed."; exit 1; }
fi

echo "[kill] stopping & removing Docker stack…"
if command -v docker >/dev/null 2>&1 && [ -f "$COMPOSE" ]; then
  # All profiles so meshcentral/guacamole/etc. are caught too.
  docker compose -f "$COMPOSE" --profile remote --profile guacamole \
    down -v --rmi "$RMI" --remove-orphans 2>/dev/null || true
fi

echo "[kill] removing desktop integration…"
[ -f "$HERE/packaging/linux/install-local.sh" ] && \
  bash "$HERE/packaging/linux/install-local.sh" --uninstall 2>/dev/null || true
# this repo's optional user systemd unit, if it was installed
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now shadow-ui.service 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/shadow-ui.service" 2>/dev/null || true
fi

echo "[kill] removing launcher env, storage, data & secrets…"
rm -rf "$HERE/desktop/.venv-desktop" "$HERE/desktop/.installed" \
       "$HERE/desktop/dist" "$HERE/desktop/build" 2>/dev/null || true
rm -rf "$HOME/.shadow-desktop" 2>/dev/null || true
rm -rf "$HERE/data" "$HERE/logs" "$HERE/.env" 2>/dev/null || true
find "$HERE" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# refresh menus/icon caches so the launcher disappears immediately
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "[kill] Shadow removed."
if [ "$PURGE_REPO" = 1 ]; then
  echo "[kill] deleting repo directory: $HERE"
  cd /tmp 2>/dev/null && rm -rf "$HERE" && echo "[kill] done — no trace left."
else
  echo "Repo files remain at: $HERE"
  echo "Run './kill.sh --yes --purge-repo' to delete the directory too."
fi
