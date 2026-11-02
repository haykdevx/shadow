#!/usr/bin/env bash
# One-command Shadow install for a fresh Ubuntu/Debian VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/haykdevx/shadow/main/deploy/vps-install.sh | sudo bash
#
# or, after cloning:   sudo bash deploy/vps-install.sh
#
# Environment overrides (all optional):
#   SHADOW_DOMAIN      public hostname with DNS pointing at this VPS → real
#                      Let's Encrypt certificate. Without it, Caddy serves
#                      https://<server-ip> with a self-signed certificate.
#   SHADOW_BRANCH      git branch to deploy (default: main)
#   SHADOW_REPO        git URL (default: https://github.com/haykdevx/shadow.git)
#   SHADOW_DIR         install directory (default: /opt/shadow)
#   SHADOW_ADMIN_USER  first admin username (default: admin)
#   SHADOW_ADMIN_PASSWORD
#                      first admin password (default: generated and printed once)
#   SHADOW_SKIP_CHECKOUT=1
#                      deploy the checkout already at SHADOW_DIR as-is (no git
#                      clone/fetch) — for local forks or a patched tree.
#
# Idempotent: re-running updates the checkout and restarts the stack; it never
# overwrites an existing .env or data/.
set -euo pipefail

SHADOW_REPO="${SHADOW_REPO:-https://github.com/haykdevx/shadow.git}"
SHADOW_BRANCH="${SHADOW_BRANCH:-main}"
SHADOW_DIR="${SHADOW_DIR:-/opt/shadow}"
SHADOW_DOMAIN="${SHADOW_DOMAIN:-}"
SHADOW_ADMIN_USER="${SHADOW_ADMIN_USER:-admin}"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash deploy/vps-install.sh)"
command -v apt-get >/dev/null || die "this script targets Ubuntu/Debian (apt-get not found)"

export DEBIAN_FRONTEND=noninteractive

log "Base packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl git ufw openssl >/dev/null

if ! command -v docker >/dev/null; then
  log "Installing Docker"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing; install docker-compose-plugin"
systemctl enable --now docker >/dev/null 2>&1 || true

if ! command -v caddy >/dev/null; then
  log "Installing Caddy (HTTPS reverse proxy)"
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg --yes
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

log "Firewall: allow SSH, 80, 443 only (app port 7000 stays loopback)"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

if [ "${SHADOW_SKIP_CHECKOUT:-0}" = 1 ]; then
  log "Using existing checkout at ${SHADOW_DIR} (SHADOW_SKIP_CHECKOUT=1)"
  [ -f "${SHADOW_DIR}/docker-compose.yml" ] || die "${SHADOW_DIR} does not contain a Shadow checkout"
elif [ -d "${SHADOW_DIR}/.git" ]; then
  log "Updating ${SHADOW_DIR} from ${SHADOW_REPO} @ ${SHADOW_BRANCH}"
  git -C "${SHADOW_DIR}" fetch --depth 1 origin "${SHADOW_BRANCH}"
  git -C "${SHADOW_DIR}" checkout -q -B "${SHADOW_BRANCH}" "origin/${SHADOW_BRANCH}"
else
  log "Cloning ${SHADOW_REPO} @ ${SHADOW_BRANCH} → ${SHADOW_DIR}"
  git clone --depth 1 --branch "${SHADOW_BRANCH}" "${SHADOW_REPO}" "${SHADOW_DIR}"
fi
cd "${SHADOW_DIR}"

PUBLIC_IP="$(curl -4fsS --max-time 5 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
if [ -n "${SHADOW_DOMAIN}" ]; then
  SITE="${SHADOW_DOMAIN}"
  PUBLIC_URL="https://${SHADOW_DOMAIN}"
  TLS_LINE=""
else
  SITE="https://${PUBLIC_IP}"
  PUBLIC_URL="https://${PUBLIC_IP}"
  TLS_LINE="    tls internal"
fi

if [ ! -f .env ]; then
  log "Writing .env (first install)"
  ADMIN_PASSWORD="${SHADOW_ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)}"
  cp .env.example .env
  cat >> .env <<ENV

# ---- set by deploy/vps-install.sh on $(date -u +%FT%TZ) ----
AUTH_ENABLED=true
LOCALHOST_BYPASS=false
APP_BIND=127.0.0.1
APP_PORT=7000
SECURE_COOKIES=true
ALLOWED_ORIGINS=${PUBLIC_URL}
SHADOW_ADMIN_USER=${SHADOW_ADMIN_USER}
SHADOW_ADMIN_PASSWORD=${ADMIN_PASSWORD}
PUID=1000
PGID=1000
ENV
  chmod 600 .env
  FIRST_INSTALL=1
else
  log ".env already exists — keeping it"
  FIRST_INSTALL=0
  ADMIN_PASSWORD=""
fi

log "Caddy site: ${SITE}"
cat > /etc/caddy/Caddyfile <<CADDY
${SITE} {
${TLS_LINE}
    encode zstd gzip

    # MeshCentral remote desktop (optional profile) under the same origin.
    handle /remote/* {
        reverse_proxy https://127.0.0.1:8443 {
            transport http {
                tls_insecure_skip_verify
            }
        }
    }

    handle {
        reverse_proxy 127.0.0.1:7000
    }
}
CADDY
systemctl enable --now caddy >/dev/null 2>&1 || true
systemctl reload caddy || systemctl restart caddy

log "Starting the Shadow stack (first build takes several minutes)"
mkdir -p data logs
docker compose pull -q 2>/dev/null || true
docker compose up -d --build

log "Waiting for the app to report healthy"
for _ in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:7000/api/health >/dev/null 2>&1; then HEALTHY=1; break; fi
  sleep 2
done
[ "${HEALTHY:-0}" = 1 ] || { docker compose logs --tail=80 shadow; die "app did not become healthy; see logs above"; }

cat <<DONE

================================================================
  Shadow is running.

  URL:       ${PUBLIC_URL}
DONE
if [ "${FIRST_INSTALL}" = 1 ]; then
cat <<DONE
  Admin:     ${SHADOW_ADMIN_USER}
  Password:  ${ADMIN_PASSWORD}
             (stored in ${SHADOW_DIR}/.env — change it in Settings, then
              enable 2FA. Rotate it if this terminal output was shared.)
DONE
fi
if [ -z "${SHADOW_DOMAIN}" ]; then
cat <<DONE

  No domain was given, so HTTPS uses a self-signed certificate: your
  browser will warn once. For a trusted certificate, point a DNS name
  at ${PUBLIC_IP} and re-run with  SHADOW_DOMAIN=your.host  set.
DONE
fi
cat <<DONE

  Manage:    cd ${SHADOW_DIR}
             docker compose ps
             docker compose logs -f shadow
             sudo bash deploy/vps-install.sh     # update to latest
================================================================
DONE
