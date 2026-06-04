# Shadow Demo Mode

Demo mode lets contributors and GitHub visitors open the Command dashboard
without pairing a real home PC. It is intentionally fake but exercises the same
UI, API shape, pending approvals, timeline, runbooks, and safety model.

## Enable

Add this to `.env` before starting Shadow:

```bash
SHADOW_DEMO_MODE=true
```

Then run:

```bash
docker compose up -d --build
```

Open `http://localhost:7000`, log in, and open **Command**.

## What Is Simulated

- Host vitals: CPU, RAM, GPU, disk, network, uptime
- Screenshot: generated SVG screen image
- Processes and windows
- File list/read/search under `/demo`
- Clipboard read
- All write actions, including lock, shell, mouse click, and runbooks

Write actions still create server-side pending approvals. Confirming them returns
a simulated result instead of touching a real machine.

## What Demo Mode Is Not

Demo mode is not a security bypass and it is not remote desktop. It is a safe
showcase for screenshots, demos, docs, reviews, and CI smoke checks. For real PC
control, run `scripts/shadow_home_agent.py` on the private Linux machine and
connect over loopback or Tailscale as documented in [DEPLOY.md](../DEPLOY.md).
