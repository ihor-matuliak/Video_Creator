---
name: cf-desktop-ux
description: Build the Ukrainian ClipForge desktop workflow, media review controls and accessible states.
---

# cf-desktop-ux

Read docs/03_UX.md. Implement the manual clipping path before AI-dependent UI.
Use native file/folder dialogs through a narrow validated bridge. Keep renderer isolated. Expose projects, sources, categories, candidates, review and export in the documented order.
Implement loading, empty, partial, error, recovery and cancellation states with useful actions. Distinguish selecting clips from approving clips. Preserve manual edits and make autosave errors visible.
Support keyboard focus and shortcuts without hijacking text inputs. Test minimum window size and display scaling. Virtualize long transcripts and clip lists.
Add stable test IDs plus accessible names/roles. Run Electron E2E with isolated data and stubbed native dialogs, then manually review packaged playback on Windows.
Deliver screenshots of implemented screens and test evidence; a screenshot is not proof of backend behavior.

## Project references

Paths below are relative to the repository root.
- docs/03_UX.md
- tests/e2e/review.spec.mjs
