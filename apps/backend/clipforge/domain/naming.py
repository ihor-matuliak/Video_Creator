"""Safe export file names (FR-040, T-005).

Basename = sanitized source stem + clip ID + revision + start/end, for example
``Гра 1_3f2a…_r2_00h01m02s345-00h01m10s000.mp4``. Windows-forbidden characters,
control/format characters and reserved device names are neutralized, and the
whole file name, including the extension and the longest collision suffix,
fits into ``max_length`` UTF-16 code units (the unit NTFS limits count in).

Collisions get `` (2)``, `` (3)``... suffixes. These functions only propose
names: the export service must still create the final file without
overwriting (exclusive create / no-replace rename) and move to the next
candidate when the name is taken.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator
from typing import Final

DEFAULT_EXTENSION: Final = ".mp4"
MAX_BASENAME_LENGTH: Final = 150
MAX_COLLISION_SUFFIX: Final = 999
MAX_CLIP_ID_LENGTH: Final = 64
FALLBACK_STEM: Final = "відео"
FALLBACK_CLIP_ID: Final = "clip"
MIN_STEM_BUDGET: Final = 8

FORBIDDEN_CHARS: Final = frozenset('<>:"/\\|?*')
RESERVED_DEVICE_NAMES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{device}{digit}" for device in ("COM", "LPT") for digit in "0123456789¹²³"}
)

_EXTENSION_RE: Final = re.compile(r"\.[a-z0-9]{1,8}", re.ASCII)
_SPACES_RE: Final = re.compile(r" {2,}")
_STRIP_CHARS: Final = " ."


class NameCollisionError(Exception):
    """Every collision candidate is already taken."""


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units (characters outside the BMP count twice)."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def is_reserved_name(name: str) -> bool:
    """True for Windows device names, also with an extension or trailing spaces (``nul.txt``)."""
    head = name.split(".", 1)[0].rstrip(" ")
    return head.upper() in RESERVED_DEVICE_NAMES


def _map_char(char: str) -> str:
    if char in FORBIDDEN_CHARS:
        return "_"
    category = unicodedata.category(char)
    # C*: control, format (bidi, zero width), surrogate, private use, unassigned.
    if category.startswith("C"):
        return "_"
    # Z*: any Unicode space or line/paragraph separator.
    if category.startswith("Z"):
        return " "
    return char


def _clean(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    return _SPACES_RE.sub(" ", "".join(_map_char(char) for char in normalized))


def _finalize(text: str, fallback: str) -> str:
    result = text.strip(_STRIP_CHARS) or fallback
    if is_reserved_name(result):
        result = f"_{result}"
    return result


def _truncate_utf16(text: str, limit: int) -> str:
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            return text[:index]
    return text


def sanitize_component(text: str, *, fallback: str = FALLBACK_STEM) -> str:
    """Make one path component safe for Windows without length limiting."""
    return _finalize(_clean(text), fallback)


def _time_token(ms: int) -> str:
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return f"{hours:02d}h{minutes:02d}m{seconds:02d}s{millis:03d}"


def _require_int(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _require_extension(extension: str) -> str:
    if not isinstance(extension, str) or _EXTENSION_RE.fullmatch(extension) is None:
        raise ValueError("extension must look like '.mp4'")
    return extension


def _suffix(number: int) -> str:
    return f" ({number})"


def export_basename(
    *,
    source_stem: str,
    clip_id: str,
    revision: int,
    start_ms: int,
    end_ms: int,
    extension: str = DEFAULT_EXTENSION,
    max_length: int = MAX_BASENAME_LENGTH,
) -> str:
    """Return the export basename without extension (FR-040).

    ``basename + " (999)" + extension`` is guaranteed to fit into ``max_length``
    UTF-16 code units; only the source stem is shortened.
    """
    if not isinstance(source_stem, str):
        raise TypeError("source_stem must be a str")
    if not isinstance(clip_id, str) or not clip_id:
        raise ValueError("clip_id must be a non-empty str")
    revision = _require_int(revision, "revision", 1)
    start_ms = _require_int(start_ms, "start_ms", 0)
    end_ms = _require_int(end_ms, "end_ms", start_ms + 1)
    extension = _require_extension(extension)

    clip = _truncate_utf16(
        sanitize_component(clip_id, fallback=FALLBACK_CLIP_ID), MAX_CLIP_ID_LENGTH
    )
    tail = f"_{clip}_r{revision}_{_time_token(start_ms)}-{_time_token(end_ms)}"
    reserved = utf16_length(tail) + utf16_length(_suffix(MAX_COLLISION_SUFFIX)) + len(extension)
    budget = max_length - reserved
    if budget < MIN_STEM_BUDGET:
        raise ValueError("max_length is too small for the clip ID, revision and times")
    # One unit stays free for the "_" prefix of a reserved device name.
    stem = _finalize(_truncate_utf16(_clean(source_stem), budget - 1), FALLBACK_STEM)
    return f"{stem}{tail}"


def collision_candidates(
    basename: str,
    extension: str = DEFAULT_EXTENSION,
    *,
    limit: int = MAX_COLLISION_SUFFIX,
) -> Iterator[str]:
    """Yield ``name.mp4``, ``name (2).mp4``, ... ``name (limit).mp4``."""
    extension = _require_extension(extension)
    yield f"{basename}{extension}"
    for number in range(2, limit + 1):
        yield f"{basename}{_suffix(number)}{extension}"


def first_free_name(
    basename: str,
    existing: Iterable[str],
    extension: str = DEFAULT_EXTENSION,
    *,
    limit: int = MAX_COLLISION_SUFFIX,
) -> str:
    """First candidate not in ``existing``; comparison is case-insensitive like NTFS."""
    taken = {name.casefold() for name in existing}
    for candidate in collision_candidates(basename, extension, limit=limit):
        if candidate.casefold() not in taken:
            return candidate
    raise NameCollisionError("no free export file name")
