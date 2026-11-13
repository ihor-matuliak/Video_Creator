---
name: cf-transcription
description: Implement OpenRouter STT adapters, chunk overlap, word timing normalization and bounded recovery.
---

# cf-transcription

Read docs/05_MEDIA_TRANSCRIPTION.md, contracts/transcript.schema.json and provider documentation listed in docs/09_SOURCES.md.
Capability-test the chosen provider options on short audio when live access is available. Keep test mode isolated and never label fake responses as live.
Separate nonoverlapping core windows from overlapping upload context. Translate local timestamps using upload_start exactly once. Preserve silence and the canonical source clock.
Deduplicate only matching temporal occurrences near chunk overlaps. Keep repeated phrases at distinct times. Do not link speaker IDs across chunks without evidence.
Validate missing timing, silence, malformed response, partial coverage and payload limits. Mark segment fallback explicitly. Use bounded retries, respect Retry-After and preserve unknown paid outcomes.
Test cache invalidation, interrupted resume and parallel spend reservations. Store actual usage or unknown, never fabricated cost. Deliver adapter contracts and exact test results.

## Project references

Paths below are relative to the repository root.
- docs/05_MEDIA_TRANSCRIPTION.md
- contracts/transcript.schema.json
