#!/usr/bin/env bash
set -euo pipefail

SERVER=""
CODE=""
NAME=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2 ;;
    --code|--enroll) CODE="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$SERVER" ] || [ -z "$CODE" ]; then
  echo "Usage: install-shadow-device.sh --server https://shadow.example --code XXXX-XXXX-XXXX" >&2
  exit 2
fi

case "$(uname -s)" in
  Darwin) BASE="$HOME/Library/Application Support/Shadow/device-agent" ;;
  *) BASE="${XDG_DATA_HOME:-$HOME/.local/share}/shadow-device" ;;
esac
mkdir -p "$BASE"
curl -fsSL "$SERVER/api/shadow/device/source/relay_agent.py" -o "$BASE/relay_agent.py"
curl -fsSL "$SERVER/api/shadow/device/source/home_agent.py" -o "$BASE/home_agent.py"
curl -fsSL "$SERVER/api/shadow/device/source/workspace_agent.py" -o "$BASE/workspace_agent.py" || true
python3 -m pip install --user -q psutil >/dev/null 2>&1 || true
python3 "$BASE/relay_agent.py" --server "$SERVER" --enroll "$CODE" --name "$NAME" --once

if [ "$(uname -s)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/io.shadow.device.plist"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.shadow.device</string>
  <key>ProgramArguments</key><array><string>$(command -v python3)</string><string>$BASE/relay_agent.py</string></array>
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
  mkdir -p "$SERVICE_DIR"
  cat > "$SERVICE_DIR/shadow-device.service" <<EOF
[Unit]
Description=Shadow account-owned device relay
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$(command -v python3) $BASE/relay_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now shadow-device.service
  echo "Shadow device installed and started with systemd."
fi
