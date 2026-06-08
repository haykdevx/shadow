# Shadow Deployment

Shadow is a privileged personal workspace. Keep the web app authenticated and
keep the home-PC bridge private. The recommended topology is:

```text
phone -> Tailscale HTTPS -> VPS Shadow UI -> Tailscale -> home PC companion
                                  |
                                  +-> optional Telegram Bot API
```

Do not expose the home-PC companion or the application port directly to the
public internet.

## 1. VPS Bootstrap

Use a fresh Ubuntu VPS, SSH keys, and an unprivileged deployment user.

```bash
sudo apt update
sudo apt install -y ca-certificates curl git ufw
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
```

Log out and back in after adding the Docker group.

```bash
git clone https://github.com/YOUR_USER/YOUR_SHADOW_FORK.git shadow
cd shadow
cp .env.example .env
chmod 600 .env
```

Set at least:

```dotenv
AUTH_ENABLED=true
LOCALHOST_BYPASS=false
APP_BIND=127.0.0.1
APP_PORT=7000
SECURE_COOKIES=true
ALLOWED_ORIGINS=https://shadow.YOUR_DOMAIN
SHADOW_ADMIN_USER=YOUR_ADMIN_NAME
SHADOW_ADMIN_PASSWORD=USE_A_RANDOM_FIRST_BOOT_PASSWORD
```

`SHADOW_*` is the environment-variable prefix. Start the stack:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=150 shadow
```

The container has `restart: unless-stopped`, so Docker brings it back after a
VPS reboot. Log in, change the bootstrap password, and enable TOTP 2FA.

## 2. Private Access With Tailscale

This is the preferred single-user deployment. Install Tailscale on the VPS and
your phone, then publish loopback Shadow only inside your tailnet:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg https / http://127.0.0.1:7000
tailscale serve status
```

Open the HTTPS tailnet URL reported by `tailscale serve status`. Keep
`APP_BIND=127.0.0.1`; Tailscale Serve terminates HTTPS and proxies to loopback.

## 3. Public Domain With Caddy

Use this only when you need public reachability. Point your DNS record at the
VPS, install Caddy, and copy `deploy/Caddyfile.example` to `/etc/caddy/Caddyfile`
after replacing the hostname.

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo systemctl restart caddy
```

Caddy terminates HTTPS and proxies to `127.0.0.1:7000`. Never open port `7000`
in UFW. Keep signup disabled, use a strong admin password, and enable TOTP 2FA.

## 4. Linked Home PC

Install Tailscale on your Linux PC and bind the companion to that PC's exact
Tailscale IP. Do not use a wildcard bind.

```bash
mkdir -p ~/.config/shadow
cp deploy/shadow-home-agent.env.example ~/.config/shadow/home-agent.env
chmod 600 ~/.config/shadow/home-agent.env
mkdir -p ~/shadow ~/.config/systemd/user
# Clone or copy this repository to ~/shadow first.
cp deploy/shadow-home-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now shadow-home-agent
loginctl enable-linger "$USER"
```

Desktop capture and input require your graphical-session environment. After the
first desktop login, import it and restart the service if the session changes:

```bash
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
systemctl --user restart shadow-home-agent
```

Generate the shared token with:

```bash
openssl rand -hex 32
```

Put the same token and the linked PC's Tailscale URL in the VPS `.env`:

```dotenv
SHADOW_HOME_AGENT_URL=http://100.x.y.z:8765
SHADOW_HOME_AGENT_TOKEN=YOUR_RANDOM_64_HEX_TOKEN
SHADOW_HOME_AGENT_ALLOW_PUBLIC=false
SHADOW_PC_CONFIRM_TTL_SECONDS=300
SHADOW_PC_OWNER=YOUR_SHADOW_USERNAME
```

Restart Shadow:

```bash
docker compose up -d --build
```

For a same-host Docker install, prefer a Unix socket instead of TCP. Set the
host companion env file to an absolute host path inside the checkout data
directory, and set the app `.env` to the mounted in-container path:

```dotenv
# ~/.config/shadow/home-agent.env on the host
SHADOW_HOME_AGENT_SOCKET=/home/YOUR_USER/shadow/data/shadow-home-agent.sock

# .env consumed by Docker Compose
SHADOW_HOME_AGENT_SOCKET=/app/data/shadow-home-agent.sock
SHADOW_HOME_AGENT_URL=
```

The socket is created as mode `600`; no home-companion TCP port is exposed.

Read-only status, process, screenshot, and clipboard reads execute directly.
Lock, typing, keypresses, clipboard writes, media, volume, and application
control wait for explicit approval. App launching is label-based and restricted
by `SHADOW_ALLOWED_APPS` on the home PC.

## 5. Optional Telegram Remote

Create a bot with BotFather. The bridge is PC-control only and does not need a Shadow chat API token.
Add the token to VPS `.env`; the numeric allowlist is optional defense-in-depth:

```dotenv
SHADOW_TELEGRAM_BOT_TOKEN=YOUR_BOTFATHER_TOKEN
SHADOW_TELEGRAM_ALLOWED_USER_IDS=
```

Start the opt-in Compose profile:

```bash
docker compose --profile telegram up -d --build
docker compose logs -f shadow-telegram
```

Pair an account from **Command > Device Access**:

1. Sign in to Shadow with an account that already has linked-PC `view` permission.
2. Select **Generate Telegram pairing code**.
3. Send `/pair CODE` to the bot within ten minutes. The code is single-use.
4. Telegram inherits that Shadow account's current `view`, `control`, and `approve` grants. Revoking the web grant also revokes its Telegram link.

Unauthorized Telegram accounts receive only `Not authorized user.` Pending actions,
approvals, and audit events are isolated to the paired Shadow account.

## 6. Backups And Operations

Persist and back up `data/`, the Docker named volumes, and `.env` separately.
Treat every backup as secret material.

```bash
docker compose ps
docker compose logs --tail=200 shadow
tar -czf "shadow-data-$(date +%F).tgz" data
```

Before publishing a fork:

```bash
git status --short
git check-ignore -v .env data/auth.json data/app.db logs/compound.log
git grep -n -I -E "(sk-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9_-]{20,}|github_pat_|ghp_)"
```

Rotate any secret that has appeared in a terminal transcript, screenshot,
shared chat, issue, or commit.

