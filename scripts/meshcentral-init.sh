#!/bin/sh
set -eu

CONFIG=/opt/meshcentral/meshcentral-data/config.json
TEMPLATE=/shadow/config.json.template
ADMIN_USER=${SHADOW_MESH_ADMIN_USER:-shadow_mesh_admin}
ADMIN_PASSWORD=${SHADOW_MESH_ADMIN_PASSWORD:-}
PUBLIC_HOST=${SHADOW_MESH_PUBLIC_HOST:-localhost}
PUBLIC_ORIGIN=${SHADOW_MESH_PUBLIC_ORIGIN:-http://localhost:7000}

if [ "${#ADMIN_PASSWORD}" -lt 24 ]; then
  echo "SHADOW_MESH_ADMIN_PASSWORD must contain at least 24 characters." >&2
  exit 1
fi

mkdir -p "$(dirname "$CONFIG")"
if [ ! -s "$CONFIG" ]; then
  SESSION_KEY=$(node -e "process.stdout.write(require('crypto').randomBytes(72).toString('base64url'))")
  jq \
    --arg host "$PUBLIC_HOST" \
    --arg origin "$PUBLIC_ORIGIN" \
    --arg session "$SESSION_KEY" \
    '.settings.cert = $host
     | .settings.sessionKey = $session
     | .domains.remote.allowedFramingOrigins = [$origin]
     | .domains.remote.certUrl = $origin' \
    "$TEMPLATE" > "$CONFIG"
else
  tmp="${CONFIG}.tmp"
  jq \
    --arg host "$PUBLIC_HOST" \
    --arg origin "$PUBLIC_ORIGIN" \
    '.settings.cert = $host
     | .domains.remote.allowedFramingOrigins = [$origin]
     | .domains.remote.certUrl = $origin' \
    "$CONFIG" > "$tmp"
  mv "$tmp" "$CONFIG"
fi

create_output=$(
  node /opt/meshcentral/meshcentral/meshcentral \
    --configfile "$CONFIG" \
    --createaccount "$ADMIN_USER" \
    --pass "$ADMIN_PASSWORD" \
    --domain remote 2>&1
) || {
  echo "$create_output" >&2
  exit 1
}

case "$create_output" in
  *"Done."*|*"User already exists."*) ;;
  *)
    echo "$create_output" >&2
    exit 1
    ;;
esac

node /opt/meshcentral/meshcentral/meshcentral \
  --configfile "$CONFIG" \
  --adminaccount "$ADMIN_USER" \
  --domain remote
