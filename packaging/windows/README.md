# Shadow — Windows packaging

## Recommended: signed installer (GitHub release)

The Docker-backed desktop app distributes cleanly on Windows as a standalone
installer, **not** through the Microsoft Store (see the caveat below).

1. Build the launcher binary on Windows (needs Python + Docker Desktop):
   ```powershell
   ./desktop/run.ps1            # creates the venv
   ./desktop/.venv-desktop/Scripts/pip install pyinstaller
   ./desktop/.venv-desktop/Scripts/python desktop/build.py   # -> desktop/dist/Shadow.exe
   ```
   `desktop/build.py` already embeds `desktop/assets/shadow.ico` as the exe icon.

2. Wrap `Shadow.exe` + the source tree (or compose pointed at prebuilt images)
   into an installer with **Inno Setup** or **WiX**, then **code-sign** it
   (Authenticode / EV cert) so SmartScreen trusts it.

## Microsoft Store (MSIX) — read this first

The Store ships MSIX packages that run **sandboxed**. A sandboxed app cannot
reliably drive the host's Docker Desktop daemon, and Store certification will
reject an app that requires an external engine it can't control.

To actually ship on the Microsoft Store you need the **native (no-Docker)
variant** of Shadow — uvicorn bundled in-process with embedded ChromaDB — which
can be packaged as a self-contained MSIX. That variant is a separate build
target; see `packaging/README.md`. The Docker-backed app here is intended for
GitHub releases and self-hosters.

If/when the native variant exists, package it with:
```powershell
# MSVC + Windows SDK provide makeappx/signtool
makeappx pack /d <staging> /p Shadow.msix
signtool sign /fd SHA256 /a Shadow.msix
```
A starter `AppxManifest.xml` should declare the `runFullTrust` capability only
if bundling a full-trust backend.
