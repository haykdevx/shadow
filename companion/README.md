# Companion bridge

A thin, additive layer so a LAN client (e.g. a phone) can discover what an
Shadow server offers and pair to it, without duplicating any LLM logic.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/companion/ping` | session or token | cheap, auth-validated health check |
| GET | `/api/companion/info` | session or token | server identity + capability flags |
| GET | `/api/companion/models` | session or token | the **caller's own** model endpoints |
| GET | `/api/companion/pair` | **admin cookie** | pairing page (a form; never mints) |
| POST | `/api/companion/pair` | **admin cookie** | mint a one-time pairing token (`?format=json` for an in-app screen) |

`/models` scopes to the caller's real owner plus legacy null-owner shared rows
(same rule as `owner_filter`) and never returns API-key material.

## Pairing CSRF posture

Minting happens **only on POST**. The session cookie is `SameSite=Lax`
(`routes/auth_routes.py`), so a browser will not send it on a cross-site POST —
the same protection `POST /api/tokens` relies on. A `GET` would be unsafe (Lax
cookies ride top-level GET navigations), so `GET /pair` only renders a form.
Minting invalidates the auth middleware's token cache, so a freshly minted token
works on the next request without a restart.

The pairing/scoping rules live in small, tested units (`token_owner`,
`owner_can_see`, `mint_pairing_token`, `pairing.*`) — see
`tests/test_companion_readonly.py` and `tests/test_companion_pairing.py`.


## Account-owned Command devices

Command uses an outbound device relay. Every Shadow user enrolls their own PC;
there is no request/grant path to another user's machine.

1. Sign in to Shadow and open **Command**.
2. Select **Create setup code**.
3. Copy the generated Linux, macOS, or Windows command and run it on that PC.

The code expires after 10 minutes and works once. The installed agent stores a
hashed-on-server device credential locally, starts at login, and long-polls the
Shadow HTTPS origin. It does not open a home-router port. Device lists, jobs,
approvals, audit events, and Telegram commands are scoped to the Shadow account
that created the enrollment code.

The old `SHADOW_HOME_AGENT_URL` bridge remains only as a migration adapter. It
is visible exclusively to `SHADOW_PC_OWNER`; new accounts never request access
to it and should use Command enrollment.
