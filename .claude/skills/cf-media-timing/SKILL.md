---
name: cf-media-timing
description: Implement and debug FFmpeg clipping, canonical timestamps, VFR, audio offsets and verified exports.
---

# cf-media-timing

Read docs/05_MEDIA_TRANSCRIPTION.md and the source probe metadata. Establish one canonical video presentation-time origin before editing time calculations.
Use integer domain milliseconds and rational media timebase. Keep audio offsets relative to the same origin. For VFR use actual frame PTS, never average FPS as a clock.
Build subprocess arguments as an array with shell disabled. Render from the original into an owned temporary file. Verify streams, duration and decoding; only then atomically finalize.
Add a non-keyframe content-boundary test, delayed-audio test, VFR test, end-of-file test and original-hash preservation test. A duration-only assertion is insufficient.
Test cancellation on only the owned process tree and paths with spaces/Unicode. Protect external originals and exports from cache cleanup. Record observed first/last frame times and FFmpeg version.
Report what was measured, with precision/tolerance and any unsupported media profiles.

## Project references

Paths below are relative to the repository root.
- docs/05_MEDIA_TRANSCRIPTION.md
- contracts/time-vectors.json
