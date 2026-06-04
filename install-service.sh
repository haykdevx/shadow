#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/shadow-ui.service"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: shadow-ui.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing Shadow UI service..."
echo "Make sure you've edited shadow-ui.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable shadow-ui
sudo systemctl start shadow-ui
sudo systemctl status shadow-ui
