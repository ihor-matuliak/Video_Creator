---
name: cf-local-security
description: Review and implement ClipForge IPC, API tokens, filesystem boundaries, secrets and subprocess safety.
---

# cf-local-security

Read docs/04_ARCHITECTURE_API.md and security requirements in docs/01_SPEC.md.
Trace untrusted input from renderer, transcript and model output into filesystem, process and network operations. Prevent command/path/HTML injection at each boundary.
Use context isolation and sandboxing, allowlisted typed IPC, validated sender, loopback authenticated backend and registered media IDs. Never return API keys to settings read endpoints.
Store secrets using OS protection or session-only fallback. Keep secrets out of argv, logs, SQLite and export manifests. Restrict fake provider URLs to loopback test mode.
Resolve paths and reject cleanup outside owned cache, including symlink/reparse escapes. Use atomic output and collision-safe names. Do not overwrite source originals.
Write negative tests for every discovered boundary issue and rerun integration flows. Report concrete findings and evidence, not a generic security checklist.

## Project references

Paths below are relative to the repository root.
- docs/04_ARCHITECTURE_API.md
- docs/05_MEDIA_TRANSCRIPTION.md
