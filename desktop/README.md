# Shadow Desktop

A native desktop app (Linux · Windows · macOS) that runs Shadow **locally with
full parity to the server deployment**. It is a thin pywebview shell around the
*same* `docker-compose.yml` the VPS uses — so every feature (chat, agents,
**MAGI**, memory, web research, music, …) behaves exactly as it does on the
server. Nothing about the app is forked or reimplemented for desktop; the shell
only orchestrates Docker and opens a window.

```
  Shadow.app / Shadow.exe / Shadow.AppImage
   └─ desktop/launcher.py            (native pywebview window)
        ├─ preflight: Docker Engine + Compose present & running
        ├─ ensure ../.env            (from shadow-desktop.env.example, if missing)
        ├─ docker compose up -d --build  shadow bgutil-pot
        │     └─ shadow.depends_on  ->  searxng (healthy) + chromadb (started)
        ├─ wait for  http://127.0.0.1:7000/api/health
        └─ open window  ->  http://127.0.0.1:7000
   on quit: docker compose stop  (containers + volumes preserved)
```

The default service set is `shadow` + `bgutil-pot`; `shadow`'s own `depends_on`
brings up `searxng` and `chromadb`. MeshCentral remote-desktop, Telegram, and
ntfy are intentionally left out of the desktop default (opt in via
`SHADOW_DESKTOP_SERVICES`).

## Requirements

- **Docker** — Docker Desktop (Windows/macOS) or Docker Engine + Compose v2 (Linux).
- **Python 3.10+** on the host (only to run the launcher shell).
- **Linux only:** a pywebview backend — either
  `sudo apt install python3-gi gir1.2-webkit2-4.1 libgtk-3-0`, or
  `pip install 'pywebview[qt]'` with PyQt/PySide present.
  (Windows uses the built-in Edge WebView2; macOS uses WKWebView.)

## Run it — one command, then pin it

```bash
# Linux / macOS — from the repo root
./run.sh

# Windows (PowerShell)
./desktop/run.ps1
```

The **first** `./run.sh` does a one-time setup with no further commands:
- creates an isolated launcher environment + a webview backend (prefers system
  GTK; falls back to a sudo-free Qt backend),
- installs a **Shadow** entry into your app menu,
- builds the Shadow image (a few minutes) and opens the window.

After that, **search "Shadow" in your app menu, pin it to your dock, and just
tap to start** — no terminal, no commands. Create your local account on first
load. (`./desktop/run.sh` still works; it forwards to `./run.sh`.)

### Useful flags

| Command | Effect |
|---|---|
| `./run.sh --check` | Validate Docker + compose wiring, then exit (no containers started). |
| `./run.sh --headless` | Start the stack and wait for health without opening a window (servers / testing). |
| `./run.sh --stop` | Stop the parity services and exit. |

## Configuration (environment)

| Variable | Default | Purpose |
|---|---|---|
| `SHADOW_DESKTOP_SERVICES` | `shadow bgutil-pot` | Compose services to start. |
| `APP_BIND` / `APP_PORT` | `127.0.0.1` / `7000` | Where the app (and the window) connect. |
| `SHADOW_DESKTOP_URL` | `http://127.0.0.1:7000` | Override the full app URL. |
| `SHADOW_DESKTOP_TIMEOUT` | `900` | Seconds to wait for health (first build is slow). |
| `SHADOW_DESKTOP_STOP_ON_EXIT` | `1` | Stop containers when the window closes (`0` to leave running). |

App-level settings (model endpoints, API keys, auth) live in the repo-root
`.env`, seeded from [`shadow-desktop.env.example`](shadow-desktop.env.example).

## Packaging a distributable binary

```bash
pip install pyinstaller pywebview
python desktop/build.py     # -> desktop/dist/Shadow{,.exe,.app}
```

The frozen binary still needs Docker running and the Shadow source +
`docker-compose.yml` available next to it (or compose `image:` keys pointed at
prebuilt images). Wrap the Linux output into `.AppImage`/`.deb` with your tool
of choice; sign/notarize the macOS `.app` and Windows `.exe` as usual.
