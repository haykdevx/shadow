# Remote Desktop — Apache Guacamole (replacing MeshCentral)

Shadow is moving remote desktop from MeshCentral to **Apache Guacamole**, a
mature clientless HTML5 gateway (RDP/VNC/SSH). It embeds in the existing
same-origin `/remote/` iframe, so the front-end and the command page do not
change.

```
Browser ──HTTPS──> Shadow  /remote/ (iframe)
                     └─ guacamole (webapp, :8080)
                           └─ guacd ──VNC/RDP──> target PC
                     per-user connections + tokens in guac-postgres
```

## Status

**Done & verified (this machine):**
- `guacamole` compose profile added (opt-in): `guacd` + `guacamole` + `guac-postgres`.
- PostgreSQL schema generated and committed (`config/guacamole/initdb/001-initdb.sql`, 23 tables).
- Stack boots; web UI returns HTTP 200 and `POST /api/tokens` authenticates — the DB initialized correctly.
- Added **alongside** MeshCentral (not replacing it yet), so the live VPS is untouched until the cutover is proven.

Bring it up locally:
```bash
docker compose --profile guacamole up -d guac-postgres guacd guacamole
# Guacamole UI: http://127.0.0.1:8085  (default guacadmin/guacadmin — change/disable before exposing)
```

## Remaining work (the cutover)

1. **Provisioning backend** — replace the gateway calls in `src/shadow_remote.py`
   with the Guacamole REST API while keeping the same public functions
   (`status`, `ensure_account`, `create_session`, `list_remote_devices`) so
   `routes/shadow_routes.py` and the command page are unchanged:
   - `POST /api/tokens` (admin) → admin session.
   - Create a per-user Guacamole user + a per-target **connection** (VNC/RDP)
     under that user only (account isolation, mirroring the old per-user group).
   - Mint a **short-lived** user token and return a same-origin embed URL:
     `/remote/#/client/<base64 connection id>?token=<token>`.
   - Never expose admin creds or guacd to the browser.
2. **Same-origin proxy** — on the VPS, nginx `location /remote/ { proxy_pass http://guacamole:8080/; }`
   plus the WebSocket upgrade headers for the Guacamole tunnel.
3. **Target agent** — the companion starts a local VNC/RDP server on demand
   (e.g. `x11vnc`/`wayvnc` on Linux, built-in RDP on Windows) and registers its
   host/port/credential with the user's Guacamole connection. Loopback-bound;
   reachable by guacd over the private network/tunnel only.
4. **Harden** — disable the default `guacadmin`, set `GUAC_DB_PASSWORD`, bind
   `guacamole` to loopback (done), keep guacd internal (done).
5. **Cutover** — once the above is verified end-to-end against a real VNC/RDP
   target, remove the `meshcentral*` services, volumes, `SHADOW_MESH_*` env, and
   `scripts/meshcentral-init.sh` / `services/meshcentral-gateway/`.

## Testing checklist (needs a VNC/RDP target)

- [ ] Per-user provisioning creates an isolated connection + token.
- [ ] Embedded `/remote/` console connects to the target and renders the desktop.
- [ ] A second user cannot see/connect to the first user's connection.
- [ ] Token expires; reconnect requires a fresh provision.
- [ ] WebSocket tunnel works through the TLS reverse proxy.

## Env (compose)

| Variable | Default | Purpose |
|---|---|---|
| `GUAC_DB_NAME` / `GUAC_DB_USER` / `GUAC_DB_PASSWORD` | `guacamole_db` / `guacamole` / `guacamole` | Guacamole PostgreSQL. **Change the password before exposing.** |
| `GUAC_BIND` / `GUAC_PORT` | `127.0.0.1` / `8085` | Where the Guacamole web UI binds on the host. |
