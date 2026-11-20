## Agent skills

### Issue tracker

Issues and specs are markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), recorded as a `Status:` line. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Reloading

After changing add-on code, reload the extension in the running Blender with `python3 scripts/reload.py` (or `just reload`). First-time setup: `just link` (`scripts/link.py`) symlinks the repo into Blender's `user_default` extensions dir and enables it. Both talk to the Blender MCP socket.
