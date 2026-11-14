"""Clip interval validation: half-open ``[start_ms, end_ms)`` within the source (FR-037, T-002).

Every failure has its own stable ``code`` so callers (API, UI) can explain the
exact problem. The API layer reports them as ``INVALID_INTERVAL`` with the
reason in ``details``. Validation is pure: no subprocess, no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

IntervalField = Literal["start_ms", "end_ms", "duration_ms"]

INTERVAL_NOT_INTEGER: Final = "INTERVAL_NOT_INTEGER"
INTERVAL_NOT_FINITE: Final = "INTERVAL_NOT_FINITE"
INTERVAL_NEGATIVE: Final = "INTERVAL_NEGATIVE"
INTERVAL_INVALID_DURATION: Final = "INTERVAL_INVALID_DURATION"
INTERVAL_END_NOT_AFTER_START: Final = "INTERVAL_END_NOT_AFTER_START"
INTERVAL_END_AFTER_DURATION: Final = "INTERVAL_END_AFTER_DURATION"

_FIELD_NAMES: Final[dict[IntervalField, str]] = {
    "start_ms": "Початок",
    "end_ms": "Кінець",
    "duration_ms": "Тривалість джерела",
}


class IntervalError(ValueError):
    """Interval is invalid. ``code`` is stable, ``message`` is Ukrainian."""

    def __init__(self, code: str, message: str, field: IntervalField) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.field: IntervalField = field


@dataclass(frozen=True, slots=True)
class Interval:
    """A validated half-open interval in source milliseconds."""

    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def _require_int(value: object, field: IntervalField) -> int:
    name = _FIELD_NAMES[field]
    if isinstance(value, float) and not math.isfinite(value):
        raise IntervalError(INTERVAL_NOT_FINITE, f"{name}: значення має бути скінченним", field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise IntervalError(
            INTERVAL_NOT_INTEGER, f"{name}: значення має бути цілим числом мілісекунд", field
        )
    return value


def validate_interval(start_ms: object, end_ms: object, duration_ms: object) -> Interval:
    """Validate ``[start_ms, end_ms)`` against a source of ``duration_ms``.

    Accepts only ``int`` (not ``bool``) values. ``end_ms == duration_ms`` is allowed.
    """
    start = _require_int(start_ms, "start_ms")
    end = _require_int(end_ms, "end_ms")
    duration = _require_int(duration_ms, "duration_ms")
    if duration <= 0:
        raise IntervalError(
            INTERVAL_INVALID_DURATION, "Тривалість джерела має бути додатною", "duration_ms"
        )
    if start < 0:
        raise IntervalError(INTERVAL_NEGATIVE, "Початок не може бути від'ємним", "start_ms")
    if end < 0:
        raise IntervalError(INTERVAL_NEGATIVE, "Кінець не може бути від'ємним", "end_ms")
    if end <= start:
        raise IntervalError(
            INTERVAL_END_NOT_AFTER_START, "Кінець має бути пізніше початку", "end_ms"
        )
    if end > duration:
        raise IntervalError(
            INTERVAL_END_AFTER_DURATION, "Кінець не може бути пізніше кінця відео", "end_ms"
        )
    return Interval(start_ms=start, end_ms=end)
