#!/usr/bin/env bash
set -euo pipefail

SERVER=""
CODE=""
NAME=""
ROOTS=""
REPAIR=0
REMOTE=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2 ;;
    --code|--enroll) CODE="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --roots) ROOTS="${2:-}"; shift 2 ;;
    --repair) REPAIR=1; shift ;;
    --no-remote) REMOTE=0; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$SERVER" ]; then
  echo "Usage: install-shadow-device.sh --server https://shadow.example [--code XXXX-XXXX-XXXX] [--roots /allowed/path] [--repair] [--no-remote]" >&2
  exit 2
fi
if [ "$REPAIR" -eq 0 ] && [ -z "$CODE" ]; then
  echo "--code is required unless --repair is used" >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required for the cross-platform Shadow workspace agent." >&2
  exit 1
fi

ROOTS="${ROOTS:-$HOME}"
case "$ROOTS" in
  *$'\n'*|*$'\r'*) echo "Invalid --roots value" >&2; exit 2 ;;
esac

case "$(uname -s)" in
  Darwin)
    BASE="$HOME/Library/Application Support/Shadow/device-agent"
    CONFIG="$HOME/Library/Application Support/Shadow/device.json"
    ;;
  *)
    BASE="${XDG_DATA_HOME:-$HOME/.local/share}/shadow-device"
    CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/shadow/device.json"
    ;;
esac
mkdir -p "$BASE"

download() {
  target="$1"
  source_url="$2"
  temp="$target.tmp.$$"
  trap 'rm -f "$temp"' EXIT
  curl --fail --show-error --silent --location \
    --connect-timeout 15 --max-time 180 --retry 4 --retry-delay 2 \
    "$source_url" -o "$temp"
  test -s "$temp"
  chmod 600 "$temp"
  mv -f "$temp" "$target"
  trap - EXIT
}

download "$BASE/relay_agent.py" "$SERVER/api/shadow/device/source/relay_agent.py"
download "$BASE/home_agent.py" "$SERVER/api/shadow/device/source/home_agent.py"
download "$BASE/workspace_agent.py" "$SERVER/api/shadow/device/source/workspace_agent.py"

# Install optional deps before the sanity check that would otherwise run
# against a bare interpreter (home_agent.py degrades gracefully without
# psutil, but there's no reason to check with less than the real install has).
python3 -m pip install --user -q psutil >/dev/null 2>&1 || true
PYTHONPATH="$BASE" python3 "$BASE/relay_agent.py" --check >/dev/null
if [ "$REPAIR" -eq 0 ]; then
  python3 "$BASE/relay_agent.py" --server "$SERVER" --enroll "$CODE" --name "$NAME" --once
elif [ ! -s "$CONFIG" ]; then
  echo "Shadow enrollment is missing at $CONFIG; generate a new setup code." >&2
  exit 1
fi

if [ "$(uname -s)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/io.shadow.device.plist"
  mkdir -p "$(dirname "$PLIST")"
  # XML-escape before embedding — a --roots value containing & < or >
  # (e.g. a real-world "R&D" or "Documents & Settings" path) would
  # otherwise corrupt the plist and silently break the launchd job.
  ROOTS_XML="${ROOTS//&/&amp;}"
  ROOTS_XML="${ROOTS_XML//</&lt;}"
  ROOTS_XML="${ROOTS_XML//>/&gt;}"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.shadow.device</string>
  <key>ProgramArguments</key><array><string>$(command -v python3)</string><string>$BASE/relay_agent.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONPATH</key><string>$BASE</string>
    <key>SHADOW_ALLOWED_ROOTS</key><string>$ROOTS_XML</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/shadow-device.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/shadow-device.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/io.shadow.device" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "Shadow device installed and started with launchd."
else
  SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/shadow"
  ENV_FILE="$ENV_DIR/device-agent.env"
  mkdir -p "$SERVICE_DIR" "$ENV_DIR"
  escaped_roots="${ROOTS//\\/\\\\}"
  escaped_roots="${escaped_roots//\"/\\\"}"
  printf 'SHADOW_ALLOWED_ROOTS="%s"\nPYTHONPATH="%s"\n' "$escaped_roots" "$BASE" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  cat > "$SERVICE_DIR/shadow-device.service" <<EOF
[Unit]
Description=Shadow account-owned device relay
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$ENV_FILE
ExecStart=$(command -v python3) $BASE/relay_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable shadow-device.service
  systemctl --user restart shadow-device.service
  echo "Shadow device installed and started with systemd."
fi

# ── Remote Desktop (MeshCentral), zero-touch ───────────────────────────────
# The device just earned a token, so it can ask the server for its own
# account's device-group id and join it unattended. Without this the user had
# to open an invite page by hand on every machine before Remote Desktop worked.

# Root is needed to install a system service. Under `curl … | bash` stdin is
# the pipe, so sudo cannot prompt — reconnect it to the terminal when we can,
# and fall back to printing the command rather than hanging or half-failing.
pick_sudo() {
  if [ "$(id -u)" = "0" ]; then SUDO=""; SUDO_TTY=0; return 0; fi
  if ! command -v sudo >/dev/null 2>&1; then return 1; fi
  if sudo -n true 2>/dev/null; then SUDO="sudo"; SUDO_TTY=0; return 0; fi
  if [ -r /dev/tty ]; then SUDO="sudo"; SUDO_TTY=1; return 0; fi
  return 1
}

run_priv() {
  if [ "${SUDO_TTY:-0}" = "1" ]; then
    $SUDO "$@" < /dev/tty
  elif [ -n "${SUDO:-}" ]; then
    $SUDO "$@"
  else
    "$@"
  fi
}

json_field() {
  python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], "") or "")
except Exception:
    print("")' "$1" 2>/dev/null || echo ""
}

install_remote_desktop() {
  [ "$REMOTE" -eq 1 ] || return 0
  [ -s "$CONFIG" ] || return 0

  TOKEN="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("token","") or "")
except Exception:
    print("")' "$CONFIG" 2>/dev/null || echo "")"
  [ -n "$TOKEN" ] || return 0

  RC="$(curl --fail --show-error --silent --location --connect-timeout 15 --max-time 60 \
        -H "Authorization: Bearer $TOKEN" \
        "$SERVER/api/shadow/device/remote-config" 2>/dev/null || echo "")"
  [ -n "$RC" ] || { echo "Remote Desktop: server did not answer; skipping (device agent is fine)."; return 0; }

  CONFIGURED="$(printf '%s' "$RC" | json_field configured)"
  if [ "$CONFIGURED" != "True" ] && [ "$CONFIGURED" != "true" ]; then
    echo "Remote Desktop is not enabled on this Shadow server — skipping."
    return 0
  fi

  MESHID="$(printf '%s' "$RC" | json_field group_id)"
  MPATH="$(printf '%s' "$RC" | json_field public_path)"
  [ -n "$MESHID" ] || { echo "Remote Desktop: no device group returned; skipping."; return 0; }
  MPATH="${MPATH:-/remote/}"
  MESH_BASE="$SERVER${MPATH%/}"

  if ! pick_sudo; then
    echo ""
    echo "Remote Desktop needs administrator rights to install its service."
    echo "The Shadow device agent is installed and running. To finish Remote Desktop, run:"
    echo "  sudo bash -c \"curl -fsSL '$MESH_BASE/meshagents?script=1' -o /tmp/meshinstall.sh && chmod 755 /tmp/meshinstall.sh && /tmp/meshinstall.sh '$MESH_BASE' '$MESHID'\""
    return 0
  fi

  WORK="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$WORK'" RETURN

  if [ "$(uname -s)" = "Darwin" ]; then
    # MeshCentral's meshinstall.sh is Linux-only; on macOS install the
    # universal (Intel + Apple Silicon) agent binary beside its .msh settings
    # and let the agent install its own LaunchDaemon.
    curl --fail --show-error --silent --location --connect-timeout 15 --max-time 300 \
      "$MESH_BASE/meshagents?id=10005" -o "$WORK/meshagent" || {
        echo "Remote Desktop: could not download the macOS agent; skipping."; return 0; }
    curl --fail --show-error --silent --location --connect-timeout 15 --max-time 120 \
      -G --data-urlencode "id=$MESHID" "$MESH_BASE/meshsettings" -o "$WORK/meshagent.msh" || {
        echo "Remote Desktop: could not download agent settings; skipping."; return 0; }
    test -s "$WORK/meshagent" && test -s "$WORK/meshagent.msh" || {
        echo "Remote Desktop: agent download was empty; skipping."; return 0; }
    chmod 755 "$WORK/meshagent"
    ( cd "$WORK" && run_priv ./meshagent -fullinstall ) || {
        echo "Remote Desktop: the macOS agent installer failed; skipping."; return 0; }
    echo "Remote Desktop installed (macOS)."
    echo "  macOS gates screen access: grant Screen Recording and Accessibility to"
    echo "  'meshagent' in System Settings ▸ Privacy & Security, or the remote screen stays black."
  else
    curl --fail --show-error --silent --location --connect-timeout 15 --max-time 120 \
      "$MESH_BASE/meshagents?script=1" -o "$WORK/meshinstall.sh" || {
        echo "Remote Desktop: could not download the installer; skipping."; return 0; }
    test -s "$WORK/meshinstall.sh" || { echo "Remote Desktop: empty installer; skipping."; return 0; }
    chmod 755 "$WORK/meshinstall.sh"
    # The mesh id contains $ and @ — always pass it quoted.
    run_priv "$WORK/meshinstall.sh" "$MESH_BASE" "$MESHID" || {
        echo "Remote Desktop: the agent installer failed; skipping."; return 0; }
    echo "Remote Desktop installed (Linux)."
  fi
  echo "This machine will appear under Command ▸ Remote within about a minute."
}

install_remote_desktop || true
