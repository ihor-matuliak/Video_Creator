"""Clip-local SRT subtitles built from transcript segments (FR-043, T-039).

Segments use global source milliseconds with half-open ``[start_ms, end_ms)``
intervals. A clip covers ``[clip_start_ms, clip_end_ms)``. Every emitted cue is
trimmed to the clip and shifted so the clip starts at zero, so each cue
satisfies ``0 <= start < end <= clip duration``.

Segment timing is segment-level: a segment that crosses a clip edge keeps its
whole text, because word timing is not available to split it. Cue text is plain
text: markup tags and comments are removed, control characters are removed, and
all line breaks collapse into single spaces so a cue can never contain the blank
line that would end its SRT block early.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from clipforge.domain.interval import (
    INTERVAL_END_NOT_AFTER_START,
    INTERVAL_NEGATIVE,
    INTERVAL_NOT_INTEGER,
    IntervalError,
    IntervalField,
)
from clipforge.domain.timecode import format_timecode

# Non-whitespace control characters. Tab, LF, CR, VT and FF are whitespace and are
# normalized by the whitespace collapse in ``_plain_text``.
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")
_MARKUP_RE: Final = re.compile(
    r"<!--.*?-->|</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class SubtitleSegment:
    """A transcript segment in source milliseconds: ``[start_ms, end_ms)``."""

    start_ms: int
    end_ms: int
    text: str


def build_clip_srt(segments: Iterable[SubtitleSegment], *, start_ms: int, end_ms: int) -> str:
    """Return SRT text for the clip ``[start_ms, end_ms)`` with clip-local times.

    Segments fully outside the clip are dropped, segments crossing a clip edge are
    trimmed to it, and segments with empty text after cleaning are skipped. Cues are
    numbered from 1 in time order. Returns an empty string when no cue remains.

    Raises ``IntervalError`` when the clip bounds are not integers, start is negative,
    or end is not after start. Segment times must be integers as well.
    """
    clip_start, clip_end = _validate_clip(start_ms, end_ms)
    cues: list[tuple[int, int, str]] = []
    for segment in segments:
        segment_start = _require_ms(segment.start_ms, "start_ms", "Початок сегмента")
        segment_end = _require_ms(segment.end_ms, "end_ms", "Кінець сегмента")
        text = _plain_text(segment.text)
        if not text:
            continue
        cue_start = max(segment_start, clip_start)
        cue_end = min(segment_end, clip_end)
        if cue_end <= cue_start:
            continue
        cues.append((cue_start - clip_start, cue_end - clip_start, text))
    cues.sort(key=lambda cue: (cue[0], cue[1]))
    return "".join(
        f"{index}\n"
        f"{format_timecode(rel_start, separator=',')} --> "
        f"{format_timecode(rel_end, separator=',')}\n"
        f"{text}\n\n"
        for index, (rel_start, rel_end, text) in enumerate(cues, start=1)
    )


def _validate_clip(start_ms: object, end_ms: object) -> tuple[int, int]:
    start = _require_ms(start_ms, "start_ms", "Початок кліпу")
    end = _require_ms(end_ms, "end_ms", "Кінець кліпу")
    if start < 0:
        raise IntervalError(INTERVAL_NEGATIVE, "Початок кліпу не може бути від'ємним", "start_ms")
    if end <= start:
        raise IntervalError(
            INTERVAL_END_NOT_AFTER_START, "Кінець кліпу має бути пізніше початку", "end_ms"
        )
    return start, end


def _require_ms(value: object, field: IntervalField, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IntervalError(
            INTERVAL_NOT_INTEGER, f"{label}: значення має бути цілим числом мілісекунд", field
        )
    return value


def _plain_text(raw: str) -> str:
    """Return cue text as single-line plain text; the result may be empty."""
    text = _CONTROL_RE.sub("", raw)
    # Removing one tag can join fragments into a new tag (``<<b>b>``), so repeat
    # until nothing changes. Each pass shortens the text, so this terminates.
    while (stripped := _MARKUP_RE.sub("", text)) != text:
        text = stripped
    return " ".join(text.split())
