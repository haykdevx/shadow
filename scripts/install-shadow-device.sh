#!/usr/bin/env bash
set -euo pipefail

SERVER=""
CODE=""
NAME=""
ROOTS=""
REPAIR=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2 ;;
    --code|--enroll) CODE="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --roots) ROOTS="${2:-}"; shift 2 ;;
    --repair) REPAIR=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$SERVER" ]; then
  echo "Usage: install-shadow-device.sh --server https://shadow.example [--code XXXX-XXXX-XXXX] [--roots /allowed/path] [--repair]" >&2
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

PYTHONPATH="$BASE" python3 "$BASE/relay_agent.py" --check >/dev/null
python3 -m pip install --user -q psutil >/dev/null 2>&1 || true
if [ "$REPAIR" -eq 0 ]; then
  python3 "$BASE/relay_agent.py" --server "$SERVER" --enroll "$CODE" --name "$NAME" --once
elif [ ! -s "$CONFIG" ]; then
  echo "Shadow enrollment is missing at $CONFIG; generate a new setup code." >&2
  exit 1
fi

if [ "$(uname -s)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/io.shadow.device.plist"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.shadow.device</string>
  <key>ProgramArguments</key><array><string>$(command -v python3)</string><string>$BASE/relay_agent.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONPATH</key><string>$BASE</string>
    <key>SHADOW_ALLOWED_ROOTS</key><string>$ROOTS</string>
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
