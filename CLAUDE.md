# ClipForge Local

Build a single-user Windows desktop video clipper. User-facing language: Ukrainian.
This repository starts as an implementation brief, not a finished application.

## Read first

- Product authority: docs/01_SPEC.md.
- Milestones: docs/02_IMPLEMENTATION_PLAN.md.
- API and domain authority: docs/04_ARCHITECTURE_API.md and contracts/.
- Timing/transcription: docs/05_MEDIA_TRANSCRIPTION.md.
- Selection: docs/06_SELECTION.md and prompts/.
- UX: docs/03_UX.md; tests: docs/07_TEST_PLAN.md.
- Resume state: docs/PROGRESS.md; decisions: docs/DECISIONS.md.

If requirements conflict, record the conflict before changing a public contract.
Explicit current owner instructions take precedence over this package.


## How You Work

- You are an orchestrator. You plan, write tasks, and accept completed work.
Do not write code yourself: delegate it to the builder agent. Exception: a one- or two-line edit.
- A task for builder must be no longer than 10 lines, following this template:
Goal: what to do, in one sentence.
Read: which files.
Do not touch: which files.
Done when: a runnable command and what its output should show.
- Independent tasks that do not touch the same files can be assigned to multiple builder agents simultaneously.
- Before accepting work, run the command from Done when yourself. Do not take builder's report at face value.
If it fails: return the task to builder with the error output. After three rejections, stop and ask me.
- Once the entire task is accepted, call reviewer: pass them the tasks and ask them to verify the changes.
- Start of session: read docs/progress.md.
- End of task: append a line to docs/progress.md. Record any new decision in docs/decisions.md, and any new pitfall in docs/known-issues.md.

## Scope and architecture

Windows 11 x64 first. Electron + React + TypeScript renderer; Python FastAPI
sidecar; SQLite; FFmpeg/FFprobe. No remote web hosting, accounts or Redis.
STT uses OpenRouter microsoft/mai-transcribe-2 or google/gemini-3.5-transcribe.
The text selection model is separately configurable and must be capability-tested.
Manual clipping and imported transcripts work without an API key.
Use current compatible dependency versions, pin them and commit lock files.

## Invariants

- Preserve source bytes. User originals are never cache or cleanup targets.
- Store source-relative times as integer milliseconds, intervals [start,end).
- One canonical source timebase; preserve inter-stream offsets and VFR timing.
- LLM selects existing word/segment IDs; it does not invent final timestamps.
- Validate external model output and evidence IDs before saving a candidate.
- Text-only candidates cannot claim a visually verified event.
- Overlap deduplication is time-aware; repeated speech at different times survives.
- Rejected/reviewed clips and manual edits survive reanalysis.
- Final output uses source media and explicit frame-accurate transcoding policy.
- Export success requires output validation; existence alone is insufficient.
- Never invoke a shell with paths, model output, filenames or transcript text.
- Secrets stay out of renderer persistence, logs, Git and exported manifests.
- Paid AI is disabled in automated tests; no fake results in production.
- Resume from valid checkpoints. Do not silently retry an ambiguous paid timeout.
- Cancellation ends owned child processes only; no global process killing.

## Workflow

Implement milestones M0–M8 with tests alongside behavior. Use the relevant
.claude/skills/cf-* procedure. Avoid unrelated features and speculative abstractions.
Keep acceptance tests intact; add regression tests for any proven test correction.
Never mark a phase complete with failing required tests or unimplemented behavior.
Record commands, outcomes and limitations in docs/PROGRESS.md after each milestone.
Do not treat model self-reported confidence as a calibrated probability.

## Commands

Available immediately:
- npm run qa:pack
- npm run fixtures
- npm run qa:syntax

After backend implementation:
- npm run qa:acceptance

After desktop implementation and installation of pinned @playwright/test:
- npm run qa:e2e

Implement documented developer scripts during M0; do not claim they exist sooner.
Product Python gates: pytest, ruff and mypy. Frontend gates: typecheck,
lint, Vitest and Playwright. See docs/07_TEST_PLAN.md for required coverage.

## Completion

Deliver a working Windows package, reproducible build instructions, test reports,
dependency/license inventory and user guide. Mark live-provider, long-source and
clean-machine checks explicitly as run or not run. No placeholders in shipped code.
