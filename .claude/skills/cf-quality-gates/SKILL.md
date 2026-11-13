---
name: cf-quality-gates
description: Implement and run ClipForge unit, media, acceptance, UI and release verification with honest reports.
---

# cf-quality-gates

Read docs/07_TEST_PLAN.md and map each completed FR to tests. Keep provided acceptance assertions unless a documented requirement correction proves they are wrong.
Add tests for behavior and failure cases, not implementation-shaped snapshots. Use real FFmpeg for media integration and deterministic local provider stubs for normal CI.
Run syntax, unit, integration and applicable UI gates. Check output content and audio alignment independently. Track skipped and not-run checks explicitly. Never hide failed P0 tests with blanket skip/xfail.
Separate handoff validation, fake-provider product tests, live-provider tests, human quality pilot and packaged Windows acceptance. Do not infer one from another.
Before release run the long-source tests, clean-machine package smoke, credential scan and dependency/license inventory. Save reports and versions.
Deliver exact commands/results and unresolved release gates.

## Project references

Paths below are relative to the repository root.
- docs/07_TEST_PLAN.md
- VALIDATION_REPORT.md
