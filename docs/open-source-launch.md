# Open Source Launch Plan

Shadow should be published as a private-first product, not a generic AI chat UI.
The GitHub promise is:

> Shadow is a self-hosted AI command center for your own computer: chat, MAGI
> multi-model deliberation, memory, research, and approval-gated Linux PC
> control through a private companion agent.

## Product Pillars

1. **Private by default**: localhost/Tailscale-first, auth on, no public home-agent exposure.
2. **Safe control**: every state-changing PC action is server-side confirmation gated.
3. **Instant demo**: `SHADOW_DEMO_MODE=true` gives reviewers a real-feeling dashboard with no setup risk.
4. **Signature AI**: MAGI vote/judge/debate is the feature people remember.
5. **Composable automation**: runbooks and future plugins should be easy to inspect, approve, and share.

## Before First Public Push

Run:

```bash
python scripts/public_readiness_check.py --strict
python -m py_compile app.py routes/*.py src/*.py companion/*.py
node --check static/app.js
find static/js -name '*.js' -print0 | xargs -0 -n1 node --check
```

Confirm:

- `.env`, `data/`, logs, uploaded files, screenshots, databases, and token files are not staged.
- `.env.example` contains placeholders only.
- `AUTH_ENABLED=true` is the documented default.
- Demo mode works without `SHADOW_HOME_AGENT_URL` or `SHADOW_HOME_AGENT_TOKEN`.
- The README includes screenshots/GIFs and the clear product pitch.
- LICENSE and ACKNOWLEDGMENTS preserve upstream Shadow attribution.

## Star-Worthy Next Milestones

- WebRTC live screen stream with confirmation-gated input forwarding.
- Plugin/runbook marketplace format with signed or inspectable manifests.
- One-command local installer for the home agent.
- Vision model screen inspection: "look at my screen and explain the issue."
- Shareable demo recording and polished docs landing page.
