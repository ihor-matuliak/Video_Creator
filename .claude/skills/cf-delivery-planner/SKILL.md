---
name: cf-delivery-planner
description: Plan and resume ClipForge implementation milestones, dependencies and acceptance gates.
---

# cf-delivery-planner

Read CLAUDE.md, docs/PROGRESS.md and docs/02_IMPLEMENTATION_PLAN.md. Inspect the actual code and available tests before trusting recorded status.
Choose the first incomplete milestone. List its required FR IDs, dependencies, files and observable exit criteria. Implement the smallest complete path and necessary regression tests. Keep app behavior and handoff validation separate.
Record ordinary implementation decisions in docs/DECISIONS.md. Ask only when a real requirement conflict, unavailable credential or irreversible operation blocks progress. Continue independent work on fake providers when live credentials are missing.
After the milestone run its required checks and update PROGRESS with exact commands, passed/failed/not-run and next task. Do not claim a working application from mock screens. Do not silently expand scope into video understanding or publishing.
Deliver the changed requirements, implementation, verification evidence and unresolved blockers.

## Project references

Paths below are relative to the repository root.
- docs/01_SPEC.md
- docs/02_IMPLEMENTATION_PLAN.md
