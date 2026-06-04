# Shadow Changelog

## Unreleased

### Product identity

- Rebranded the visible workspace, login, companion identity, and PWA metadata
  as Shadow while retaining upstream-compatible internal identifiers.
- Added a compact HUD home surface for linked-PC status and quick actions.

### Private PC control

- Added a Tailscale-oriented home-PC companion with an allowlisted action API.
- Added immediate read-only status, process, screenshot, and clipboard access.
- Added short-lived explicit approvals for lock, typing, keypress, clipboard
  writes, media, volume, and application control.
- Added native `pc_control` agent tooling with non-admin blocking.

### Remote access

- Added an optional Telegram bridge that reuses Shadow's existing owner-scoped
  chat API and supports callback approval for PC lock requests.
- Added hardened VPS, Caddy, Tailscale, boot service, backup, and secret
  handling documentation.

### Upstream foundation

Shadow is a fork of [Shadow](https://github.com/haykdevx/dev).
The upstream MIT license and acknowledgments remain in this repository.

