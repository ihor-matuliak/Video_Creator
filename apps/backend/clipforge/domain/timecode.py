"""Timecodes ``HH:MM:SS.mmm`` / ``HH:MM:SS,mmm`` <-> integer milliseconds (FR-031, T-001).

The domain time unit is a non-negative integer number of milliseconds
(docs/05_MEDIA_TRANSCRIPTION.md section 1). Hours have at least two digits and
may exceed 99; minutes and seconds are 00-59; milliseconds always have exactly
three digits. Only ASCII digits are accepted and no surrounding whitespace.
"""

from __future__ import annotations

import re
from typing import Final, Literal

Separator = Literal[".", ","]

MS_PER_SECOND: Final = 1_000
MS_PER_MINUTE: Final = 60 * MS_PER_SECOND
MS_PER_HOUR: Final = 60 * MS_PER_MINUTE

INVALID_TIMECODE: Final = "INVALID_TIMECODE"

_TIMECODE_RE: Final = re.compile(
    r"(?P<h>[0-9]{2,})"
    r":(?P<m>[0-5][0-9])"
    r":(?P<s>[0-5][0-9])"
    r"[.,](?P<ms>[0-9]{3})",
    re.ASCII,
)


class TimecodeError(ValueError):
    """Text is not a valid timecode. ``code`` is stable, ``message`` is Ukrainian."""

    def __init__(self, message: str) -> None:
        super().__init__(INVALID_TIMECODE)
        self.code: str = INVALID_TIMECODE
        self.message = message


def parse_timecode(text: object) -> int:
    """Return milliseconds for ``HH:MM:SS.mmm`` or ``HH:MM:SS,mmm``."""
    if not isinstance(text, str):
        raise TimecodeError("Таймкод має бути рядком у форматі ГГ:ХХ:СС.мс")
    match = _TIMECODE_RE.fullmatch(text)
    if match is None:
        raise TimecodeError("Таймкод має бути у форматі ГГ:ХХ:СС.мс, наприклад 01:02:03.456")
    return (
        int(match["h"]) * MS_PER_HOUR
        + int(match["m"]) * MS_PER_MINUTE
        + int(match["s"]) * MS_PER_SECOND
        + int(match["ms"])
    )


def format_timecode(ms: int, *, separator: Separator = ".") -> str:
    """Format non-negative integer milliseconds as ``HH:MM:SS.mmm`` (or with ``,``)."""
    if isinstance(ms, bool) or not isinstance(ms, int):
        raise TypeError("ms must be an int")
    if ms < 0:
        raise ValueError("ms must be non-negative")
    if separator not in (".", ","):
        raise ValueError("separator must be '.' or ','")
    hours, rest = divmod(ms, MS_PER_HOUR)
    minutes, rest = divmod(rest, MS_PER_MINUTE)
    seconds, millis = divmod(rest, MS_PER_SECOND)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"
