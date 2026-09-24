# Global command palette

Open with Cmd+K / Ctrl+K anywhere the Console module is loaded, or the Console header button. The desktop shell's shortcut also returns you from a browser tab to Shadow.

Choose a device, type a supported phrase, inspect the exact action/arguments and target, then press Enter. Examples: `take a screenshot`, `find files report`, `read file /path/to/file`, `launch app firefox`, `volume 30`, `open tasks`, and `open missions`. Explicit action syntax accepts JSON, for example `mouse_move {"x":40,"y":80}`. Shell execution requires `shell: COMMAND`, `run shell COMMAND`, or explicit `shell` JSON. Unknown phrases are rejected; this first increment uses a deterministic intent parser, not an LLM.

The palette uses the existing authenticated PC action API, device agent, allowlists, and risk checks. Critical operations stay pending for approval in Overview. Tasks and Missions open their existing pages. No new agent protocol or permissions are required. Updating the desktop shell adds its native shortcut; existing enrolled agents already support the routed actions.

The dialog lives directly under document.body and survives Console polling. A browser-local kill switch is available: set localStorage key `shadow.commandPalette.enabled` to `false`; remove it to restore the feature. No commands or results are persisted by the palette.

Regression suite: `node --test tests/js/commandConsole.test.cjs`. Python companion permission regressions run with the normal pytest suite.
