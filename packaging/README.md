# Shadow — distribution & packaging

The desktop app (`desktop/`) is a native window around the **same Docker stack
the server runs**, giving exact feature parity. That design has one important
consequence for app stores, summarized here honestly.

## What ships where

| Channel | Status | Notes |
|---|---|---|
| **GitHub (open source)** | ✅ Ready | MIT-licensed. Ship AppImage / `.deb` / signed `.exe` / `.dmg` from `desktop/build.py` as release assets. This is the primary channel. |
| **Linux desktop (this machine, now)** | ✅ Ready | `packaging/linux/install-local.sh` adds Shadow to your app menu with the serpent icon, launching `desktop/run.sh`. |
| **Ubuntu Software (Snap Store)** | ⚠️ Conditional | `packaging/snap/snapcraft.yaml` builds a **classic-confinement** snap (it must reach the host Docker daemon). Classic snaps require manual approval to be listed. A `.deb`/AppImage on GitHub is the friction-free Linux route. |
| **Microsoft Store (MSIX)** | ⚠️ Needs native variant | MSIX is sandboxed and can't drive Docker Desktop; Store cert will reject it. Use the signed installer on GitHub, or build the native (no-Docker) variant. See `packaging/windows/README.md`. |

## The store caveat in one line

**Docker-backed = perfect VPS parity, but not a clean fit for the sandboxed
curated stores.** The curated stores (Snap strict-confinement, Microsoft Store
MSIX) expect a self-contained, sandboxable app. To target them first-class,
build the **native variant**: `pywebview` + `uvicorn app:app` in-process with
ChromaDB embedded via pip (web-search/music become optional). The app code is
identical; only the launcher's backend strategy changes.

## Assets

`desktop/assets/` holds the generated icon set from the serpent emblem:
`icon-16..512.png`, `shadow.ico` (Windows), `shadow.icns` (macOS — regenerate a
true multi-resolution `.icns` on macOS with `iconutil`), `icon-flat-512.png`
(transparent, for stores that mask icons themselves).

## Quick recipes

```bash
# Linux: install into your app menu now
./packaging/linux/install-local.sh           # (--uninstall to remove)

# Linux: validate AppStream + desktop metadata
desktop-file-validate packaging/linux/shadow.desktop
appstreamcli validate packaging/linux/io.github.haykdevx.shadow.metainfo.xml

# Snap (classic; requires snapcraft + lxd/multipass)
cd packaging/snap && snapcraft

# Any OS: freeze the launcher binary
pip install pyinstaller pywebview && python desktop/build.py
```
