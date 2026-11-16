"""Transcript import: SRT/VTT text and normalized JSON -> ``NormalizedTranscript`` (FR-019).

Pure validation and conversion: no I/O, standard library only (ADR 017). Times are integer
milliseconds, intervals are half-open ``[start_ms, end_ms)`` and must lie inside the source:
``0 <= start < end <= duration_ms`` (T-024). ``duration_ms`` is always the duration of the
*source*; it replaces the document's own ``duration_ms`` in the result.

SRT/VTT (docs/05 section 7): cues become segments with ids ``s1``, ``s2`` ... and ordinals
``0``, ``1`` ..., ``timing_level`` is ``"segment"``, ``words`` is empty and coverage is one
``succeeded`` span over the whole source. A BOM, CRLF/CR line ends, multiline cues, cue
identifiers, VTT ``NOTE``/``STYLE``/``REGION`` blocks and VTT cue settings are accepted. Cue text
becomes plain text: markup tags, entities, ASS ``{\\an8}`` overrides and control characters are
cleaned; nothing is ever executed. Cues are stably sorted by start (overlaps are allowed);
cues whose cleaned text is empty are dropped.

Normalized JSON is checked against ``contracts/transcript.schema.json`` (embedded as
``TRANSCRIPT_SCHEMA`` because the domain cannot read files; a test pins it to the contract file),
then for ordered, unique ids and ordinals, non-reversed intervals, a ``timing_level`` that
matches the content, and source bounds.

``offset_ms`` is the single explicit user offset: it is added once to every time before the
bounds check. Coverage spans are shifted the same way; with a non-zero offset a span that
leaves the source is clipped to it (the part outside the source is not coverage of the source),
with zero offset it must already fit.

Every failure is a ``TranscriptImportError`` with a stable ``code`` and a Ukrainian ``message``.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from clipforge.domain.timecode import TimecodeError, format_timecode, parse_timecode

TimingLevel = Literal["word", "segment"]
CoverageState = Literal["succeeded", "failed", "pending", "unknown_outcome"]

SCHEMA_VERSION: Final = 1
MAX_TEXT_LENGTH: Final = 2000
DEFAULT_LANGUAGE: Final = "und"

TRANSCRIPT_NOT_TEXT: Final = "TRANSCRIPT_NOT_TEXT"
TRANSCRIPT_EMPTY: Final = "TRANSCRIPT_EMPTY"
TRANSCRIPT_NO_CUES: Final = "TRANSCRIPT_NO_CUES"
TRANSCRIPT_VTT_HEADER_MISSING: Final = "TRANSCRIPT_VTT_HEADER_MISSING"
TRANSCRIPT_VTT_HEADER_INVALID: Final = "TRANSCRIPT_VTT_HEADER_INVALID"
TRANSCRIPT_CUE_MALFORMED: Final = "TRANSCRIPT_CUE_MALFORMED"
TRANSCRIPT_TIMESTAMP_INVALID: Final = "TRANSCRIPT_TIMESTAMP_INVALID"
TRANSCRIPT_INTERVAL_REVERSED: Final = "TRANSCRIPT_INTERVAL_REVERSED"
TRANSCRIPT_OUT_OF_BOUNDS: Final = "TRANSCRIPT_OUT_OF_BOUNDS"
TRANSCRIPT_TEXT_TOO_LONG: Final = "TRANSCRIPT_TEXT_TOO_LONG"
TRANSCRIPT_SCHEMA_INVALID: Final = "TRANSCRIPT_SCHEMA_INVALID"
TRANSCRIPT_TIMING_LEVEL_MISMATCH: Final = "TRANSCRIPT_TIMING_LEVEL_MISMATCH"
TRANSCRIPT_ID_DUPLICATE: Final = "TRANSCRIPT_ID_DUPLICATE"
TRANSCRIPT_ORDER_INVALID: Final = "TRANSCRIPT_ORDER_INVALID"
TRANSCRIPT_OFFSET_INVALID: Final = "TRANSCRIPT_OFFSET_INVALID"
TRANSCRIPT_DURATION_INVALID: Final = "TRANSCRIPT_DURATION_INVALID"
TRANSCRIPT_LANGUAGE_INVALID: Final = "TRANSCRIPT_LANGUAGE_INVALID"


class TranscriptImportError(ValueError):
    """Import failed. ``code`` is stable, ``message`` is Ukrainian.

    ``line`` is the 1-based line of SRT/VTT text, ``path`` is the JSON location such as
    ``words[3].end_ms``; both are ``None`` when they do not apply.
    """

    def __init__(
        self, code: str, message: str, *, line: int | None = None, path: str | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.line = line
        self.path = path


@dataclass(frozen=True, slots=True)
class TranscriptItem:
    """A word or a segment in source milliseconds: ``[start_ms, end_ms)``."""

    id: str
    ordinal: int
    start_ms: int
    end_ms: int
    text: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ordinal": self.ordinal,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class CoverageSpan:
    """Which part of the source has a transcript, and how reliably."""

    start_ms: int
    end_ms: int
    state: CoverageState

    def to_json(self) -> dict[str, Any]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms, "state": self.state}


@dataclass(frozen=True, slots=True)
class NormalizedTranscript:
    """A validated transcript; ``to_json`` gives the document of the schema contract."""

    timing_level: TimingLevel
    language: str
    duration_ms: int
    words: tuple[TranscriptItem, ...]
    segments: tuple[TranscriptItem, ...]
    coverage: tuple[CoverageSpan, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "timing_level": self.timing_level,
            "language": self.language,
            "duration_ms": self.duration_ms,
            "words": [word.to_json() for word in self.words],
            "segments": [segment.to_json() for segment in self.segments],
            "coverage": [span.to_json() for span in self.coverage],
        }


# --------------------------------------------------------------------------------------
# JSON Schema subset (contracts/transcript.schema.json)
# --------------------------------------------------------------------------------------

_ITEM_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 2000},
        "ordinal": {"type": "integer", "minimum": 0},
        "start_ms": {"type": "integer", "minimum": 0},
        "end_ms": {"type": "integer", "minimum": 0},
        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "required": ["id", "ordinal", "start_ms", "end_ms", "text"],
}

# Must equal contracts/transcript.schema.json; tests compare them.
TRANSCRIPT_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "NormalizedTranscript",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": 1},
        "timing_level": {"enum": ["word", "segment"]},
        "language": {"type": "string", "minLength": 2, "maxLength": 32},
        "duration_ms": {"type": "integer", "minimum": 1},
        "words": {"type": "array", "items": _ITEM_SCHEMA},
        "segments": {"type": "array", "items": _ITEM_SCHEMA},
        "coverage": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "start_ms": {"type": "integer", "minimum": 0},
                    "end_ms": {"type": "integer", "minimum": 0},
                    "state": {"enum": ["succeeded", "failed", "pending", "unknown_outcome"]},
                },
                "required": ["start_ms", "end_ms", "state"],
            },
        },
    },
    "required": [
        "schema_version",
        "timing_level",
        "language",
        "duration_ms",
        "words",
        "segments",
        "coverage",
    ],
}

# Keywords the checker below understands; tests assert the contract uses nothing else.
SUPPORTED_SCHEMA_KEYWORDS: Final = frozenset(
    {
        "$schema",
        "title",
        "type",
        "const",
        "enum",
        "minimum",
        "minLength",
        "maxLength",
        "required",
        "properties",
        "additionalProperties",
        "items",
    }
)

_TYPE_NAMES: Final = {
    "object": "об'єкт",
    "array": "масив",
    "string": "рядок",
    "integer": "ціле число",
}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _has_type(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        # Millisecond times are integers: 1.0 is rejected, unlike plain JSON Schema.
        return _is_int(value)
    raise ValueError(f"unsupported schema type: {expected}")


def _same_json_value(value: object, expected: object) -> bool:
    return type(value) is type(expected) and value == expected


def _schema_error(path: str, reason: str) -> TranscriptImportError:
    where = f"Поле «{path}»" if path else "Документ"
    return TranscriptImportError(TRANSCRIPT_SCHEMA_INVALID, f"{where}: {reason}", path=path or None)


def _child(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _check_schema(value: object, schema: Mapping[str, Any], path: str = "") -> None:
    """Raise ``TranscriptImportError`` for the first violation of ``schema`` (fail-fast)."""
    expected_type = schema.get("type")
    if expected_type is not None and not _has_type(value, expected_type):
        raise _schema_error(path, f"очікується {_TYPE_NAMES[expected_type]}")
    if "const" in schema and not _same_json_value(value, schema["const"]):
        raise _schema_error(path, f"значення має дорівнювати {schema['const']}")
    if "enum" in schema and not any(_same_json_value(value, option) for option in schema["enum"]):
        options = ", ".join(str(option) for option in schema["enum"])
        raise _schema_error(path, f"допустимі значення: {options}")
    if "minimum" in schema and _is_int(value) and cast(int, value) < schema["minimum"]:
        raise _schema_error(path, f"значення не може бути меншим за {schema['minimum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise _schema_error(path, f"довжина має бути не меншою за {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise _schema_error(path, f"довжина має бути не більшою за {schema['maxLength']}")
    if isinstance(value, dict):
        _check_object(value, schema, path)
    elif isinstance(value, list) and "items" in schema:
        for index, element in enumerate(value):
            _check_schema(element, schema["items"], f"{path}[{index}]")


def _check_object(value: dict[Any, Any], schema: Mapping[str, Any], path: str) -> None:
    properties: Mapping[str, Any] = schema.get("properties", {})
    for name in schema.get("required", ()):
        if name not in value:
            raise _schema_error(_child(path, name), "обов'язкове поле відсутнє")
    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in properties:
                raise _schema_error(_child(path, str(key)), "невідоме поле")
    for name, sub_schema in properties.items():
        if name in value:
            _check_schema(value[name], sub_schema, _child(path, name))


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


def _stamp(ms: int) -> str:
    return f"-{format_timecode(-ms)}" if ms < 0 else format_timecode(ms)


def _require_duration(duration_ms: object) -> int:
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise TranscriptImportError(
            TRANSCRIPT_DURATION_INVALID,
            "Тривалість джерела має бути додатним цілим числом мілісекунд",
        )
    return duration_ms


def _require_offset(offset_ms: object) -> int:
    if isinstance(offset_ms, bool) or not isinstance(offset_ms, int):
        raise TranscriptImportError(
            TRANSCRIPT_OFFSET_INVALID, "Зсув має бути цілим числом мілісекунд"
        )
    return offset_ms


def _require_language(language: object) -> str:
    if not isinstance(language, str) or not 2 <= len(language) <= 32:
        raise TranscriptImportError(
            TRANSCRIPT_LANGUAGE_INVALID, "Мова має бути рядком довжиною від 2 до 32 символів"
        )
    return language


def _out_of_bounds_message(subject: str, start: int, end: int, duration: int, offset: int) -> str:
    shifted = f" після зсуву на {offset} мс" if offset else ""
    return (
        f"{subject}: інтервал {_stamp(start)}–{_stamp(end)}{shifted} виходить за межі відео "
        f"(від 00:00:00.000 до {_stamp(duration)})"
    )


# --------------------------------------------------------------------------------------
# SRT / VTT
# --------------------------------------------------------------------------------------

_TIMING_RE: Final = re.compile(
    r"(?P<start>\S+)[ \t]+-->[ \t]+(?P<end>\S+)(?:[ \t]+(?P<settings>\S.*))?",
)
_VTT_SHORT_RE: Final = re.compile(r"[0-9]{2}:[0-9]{2}\.[0-9]{3}", re.ASCII)
_VTT_HEADER_RE: Final = re.compile(r"WEBVTT(?:[ \t].*)?")
_VTT_META_RE: Final = re.compile(r"NOTE(?:[ \t].*)?|STYLE[ \t]*|REGION[ \t]*")
# HTML-like tags and VTT timestamp tags. HTML comments need no case: they end with ``-->``,
# which the block check rejects in cue text before it gets here.
_TAG_RE: Final = re.compile(
    r"</?[A-Za-z][A-Za-z0-9._-]*(?:\s[^<>]*)?/?>|<[0-9]+(?::[0-9]{2}){1,2}[.,][0-9]{3}>",
)
_ASS_OVERRIDE_RE: Final = re.compile(r"\{\\[^{}]*\}")
# Non-whitespace control characters and the invisible direction marks of ``&lrm;``/``&rlm;``.
_CONTROL_RE: Final = re.compile("[\x00-\x08\x0e-\x1f\x7f-\x9f‎‏]")
_TOKEN_PREVIEW: Final = 40


@dataclass(frozen=True, slots=True)
class _Cue:
    start_ms: int
    end_ms: int
    text: str


def parse_srt(
    text: str,
    *,
    duration_ms: int,
    offset_ms: int = 0,
    language: str = DEFAULT_LANGUAGE,
) -> NormalizedTranscript:
    """Parse SRT ``text`` into a segment-level transcript for a source of ``duration_ms``."""
    return _import_cues(
        text, vtt=False, duration_ms=duration_ms, offset_ms=offset_ms, language=language
    )


def parse_vtt(
    text: str,
    *,
    duration_ms: int,
    offset_ms: int = 0,
    language: str = DEFAULT_LANGUAGE,
) -> NormalizedTranscript:
    """Parse WebVTT ``text`` into a segment-level transcript for a source of ``duration_ms``."""
    return _import_cues(
        text, vtt=True, duration_ms=duration_ms, offset_ms=offset_ms, language=language
    )


def _import_cues(
    text: str, *, vtt: bool, duration_ms: int, offset_ms: int, language: str
) -> NormalizedTranscript:
    duration = _require_duration(duration_ms)
    offset = _require_offset(offset_ms)
    language = _require_language(language)
    if not isinstance(text, str):
        raise TranscriptImportError(TRANSCRIPT_NOT_TEXT, "Вміст файлу має бути текстом")
    blocks = _split_blocks(text.removeprefix("﻿"))
    if not blocks:
        raise TranscriptImportError(TRANSCRIPT_EMPTY, "Файл порожній: у ньому немає тексту")
    if vtt:
        blocks = _drop_vtt_header(blocks)
    cues = _collect_cues(blocks, vtt=vtt, duration=duration, offset=offset)
    if not cues:
        raise TranscriptImportError(TRANSCRIPT_NO_CUES, "У файлі немає жодної репліки з текстом")
    cues.sort(key=lambda cue: cue.start_ms)  # stable: ties keep the file order
    segments = tuple(
        TranscriptItem(
            id=f"s{number}",
            ordinal=number - 1,
            start_ms=cue.start_ms,
            end_ms=cue.end_ms,
            text=cue.text,
        )
        for number, cue in enumerate(cues, start=1)
    )
    return NormalizedTranscript(
        timing_level="segment",
        language=language,
        duration_ms=duration,
        words=(),
        segments=segments,
        coverage=(CoverageSpan(start_ms=0, end_ms=duration, state="succeeded"),),
    )


def _split_blocks(text: str) -> list[tuple[int, list[str]]]:
    """Blocks of consecutive non-blank lines, each with the 1-based number of its first line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] = []
    first_line = 0
    for number, line in enumerate(lines, start=1):
        if line.strip():
            if not current:
                first_line = number
            current.append(line)
        elif current:
            blocks.append((first_line, current))
            current = []
    if current:
        blocks.append((first_line, current))
    return blocks


def _drop_vtt_header(blocks: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
    first_line, header = blocks[0]
    if _VTT_HEADER_RE.fullmatch(header[0].rstrip()) is None:
        raise TranscriptImportError(
            TRANSCRIPT_VTT_HEADER_MISSING,
            f"Рядок {first_line}: файл WebVTT має починатися з рядка WEBVTT",
            line=first_line,
        )
    for offset, line in enumerate(header):
        if "-->" in line:
            number = first_line + offset
            raise TranscriptImportError(
                TRANSCRIPT_VTT_HEADER_INVALID,
                f"Рядок {number}: після заголовка WEBVTT має бути порожній рядок",
                line=number,
            )
    return blocks[1:]


def _collect_cues(
    blocks: Sequence[tuple[int, list[str]]], *, vtt: bool, duration: int, offset: int
) -> list[_Cue]:
    cues: list[_Cue] = []
    for first_line, lines in blocks:
        arrow = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if vtt and _VTT_META_RE.fullmatch(lines[0].rstrip()) and (arrow is None or arrow > 1):
            continue  # NOTE / STYLE / REGION block
        if arrow is None:
            raise _malformed(first_line, "очікувалась репліка з рядком «початок --> кінець»")
        timing_line = first_line + arrow
        if arrow > 1:
            raise _malformed(
                timing_line,
                "таймкод має бути першим або другим рядком репліки; "
                "можливо, між репліками немає порожнього рядка",
            )
        for extra, text_line in enumerate(lines[arrow + 1 :], start=arrow + 1):
            if "-->" in text_line:
                raise _malformed(
                    first_line + extra,
                    "текст репліки не може містити «-->»; "
                    "можливо, між репліками немає порожнього рядка",
                )
        cue = _parse_cue(lines[arrow], lines[arrow + 1 :], timing_line, vtt, duration, offset)
        if cue is not None:
            cues.append(cue)
    return cues


def _malformed(line: int, reason: str) -> TranscriptImportError:
    return TranscriptImportError(TRANSCRIPT_CUE_MALFORMED, f"Рядок {line}: {reason}", line=line)


def _parse_cue(
    timing: str, text_lines: list[str], line: int, vtt: bool, duration: int, offset: int
) -> _Cue | None:
    match = _TIMING_RE.fullmatch(timing.strip())
    if match is None:
        raise _malformed(line, "очікувався рядок виду «00:00:01,000 --> 00:00:02,000»")
    if match["settings"] is not None and not vtt:
        raise _malformed(line, "після кінцевого таймкоду в SRT не може бути інших даних")
    start = _parse_stamp(match["start"], vtt=vtt, line=line)
    end = _parse_stamp(match["end"], vtt=vtt, line=line)
    if end <= start:
        raise TranscriptImportError(
            TRANSCRIPT_INTERVAL_REVERSED,
            f"Рядок {line}: кінець репліки ({_stamp(end)}) має бути пізніше початку "
            f"({_stamp(start)})",
            line=line,
        )
    start += offset
    end += offset
    if start < 0 or end > duration:
        raise TranscriptImportError(
            TRANSCRIPT_OUT_OF_BOUNDS,
            _out_of_bounds_message(f"Рядок {line}", start, end, duration, offset),
            line=line,
        )
    text = _plain_text("\n".join(text_lines))
    if not text:
        return None
    if len(text) > MAX_TEXT_LENGTH:
        raise TranscriptImportError(
            TRANSCRIPT_TEXT_TOO_LONG,
            f"Рядок {line}: текст репліки довший за {MAX_TEXT_LENGTH} символів",
            line=line,
        )
    return _Cue(start_ms=start, end_ms=end, text=text)


def _parse_stamp(token: str, *, vtt: bool, line: int) -> int:
    candidate = token
    if vtt:
        if "," in candidate:
            candidate = ""  # WebVTT uses only a dot before the milliseconds
        elif _VTT_SHORT_RE.fullmatch(candidate):
            candidate = f"00:{candidate}"
    try:
        return parse_timecode(candidate)
    except TimecodeError:
        shown = token if len(token) <= _TOKEN_PREVIEW else f"{token[:_TOKEN_PREVIEW]}…"
        expected = "ГГ:ХХ:СС.мс або ХХ:СС.мс" if vtt else "ГГ:ХХ:СС,мс"
        raise TranscriptImportError(
            TRANSCRIPT_TIMESTAMP_INVALID,
            f"Рядок {line}: некоректний таймкод «{shown}», очікується {expected}",
            line=line,
        ) from None


def _plain_text(raw: str) -> str:
    """Return cue text as single-line plain text; the result may be empty."""
    text = _ASS_OVERRIDE_RE.sub("", raw)
    # Removing one tag can join fragments into a new tag (``<<b>b>``), so repeat until
    # nothing changes. Each pass shortens the text, so this terminates.
    while (stripped := _TAG_RE.sub("", text)) != text:
        text = stripped
    # Entities are decoded after the tags are gone: ``&lt;b&gt;`` is literal text.
    text = _CONTROL_RE.sub("", html.unescape(text))
    return " ".join(text.split())


# --------------------------------------------------------------------------------------
# Normalized JSON
# --------------------------------------------------------------------------------------


def validate_normalized_json(
    document: object, *, duration_ms: int, offset_ms: int = 0
) -> NormalizedTranscript:
    """Validate a ``normalized_json`` document and return it shifted by ``offset_ms``.

    The result's ``duration_ms`` is the source duration passed in, not the document's own.
    """
    duration = _require_duration(duration_ms)
    offset = _require_offset(offset_ms)
    _check_schema(document, TRANSCRIPT_SCHEMA)
    doc = cast(dict[str, Any], document)
    timing_level = cast(TimingLevel, doc["timing_level"])
    words = [_item(raw) for raw in doc["words"]]
    segments = [_item(raw) for raw in doc["segments"]]
    coverage = [
        CoverageSpan(raw["start_ms"], raw["end_ms"], cast(CoverageState, raw["state"]))
        for raw in doc["coverage"]
    ]
    _check_timing_level(timing_level, words, segments)
    seen_ids: set[str] = set()
    _check_items("words", words, seen_ids)
    _check_items("segments", segments, seen_ids)
    _check_coverage(coverage)
    return NormalizedTranscript(
        timing_level=timing_level,
        language=doc["language"],
        duration_ms=duration,
        words=_shift_items("words", words, duration, offset),
        segments=_shift_items("segments", segments, duration, offset),
        coverage=_shift_coverage(coverage, duration, offset),
    )


def _item(raw: Mapping[str, Any]) -> TranscriptItem:
    return TranscriptItem(
        id=raw["id"],
        ordinal=raw["ordinal"],
        start_ms=raw["start_ms"],
        end_ms=raw["end_ms"],
        text=raw["text"],
    )


def _check_timing_level(
    timing_level: TimingLevel, words: Sequence[TranscriptItem], segments: Sequence[TranscriptItem]
) -> None:
    if timing_level == "segment" and words:
        raise TranscriptImportError(
            TRANSCRIPT_TIMING_LEVEL_MISMATCH,
            "Рівень часу «segment» не допускає слів: приберіть words або вкажіть «word»",
            path="words",
        )
    if timing_level == "word" and segments and not words:
        raise TranscriptImportError(
            TRANSCRIPT_TIMING_LEVEL_MISMATCH,
            "Рівень часу «word» вимагає слів із часом: words порожній, а segments ні",
            path="words",
        )


def _check_items(label: str, items: Sequence[TranscriptItem], seen_ids: set[str]) -> None:
    previous: TranscriptItem | None = None
    for index, item in enumerate(items):
        path = f"{label}[{index}]"
        if item.id in seen_ids:
            raise TranscriptImportError(
                TRANSCRIPT_ID_DUPLICATE,
                f"Елемент «{path}»: ідентифікатор «{item.id}» вже використано",
                path=path,
            )
        seen_ids.add(item.id)
        if item.end_ms <= item.start_ms:
            raise TranscriptImportError(
                TRANSCRIPT_INTERVAL_REVERSED,
                f"Елемент «{path}»: кінець ({_stamp(item.end_ms)}) має бути пізніше початку "
                f"({_stamp(item.start_ms)})",
                path=path,
            )
        if previous is not None and (
            item.ordinal <= previous.ordinal or item.start_ms < previous.start_ms
        ):
            raise TranscriptImportError(
                TRANSCRIPT_ORDER_INVALID,
                f"Елемент «{path}»: порядковий номер і початок мають зростати разом із "
                "положенням у масиві",
                path=path,
            )
        previous = item


def _check_coverage(coverage: Sequence[CoverageSpan]) -> None:
    previous_end = 0
    for index, span in enumerate(coverage):
        path = f"coverage[{index}]"
        if span.end_ms <= span.start_ms:
            raise TranscriptImportError(
                TRANSCRIPT_INTERVAL_REVERSED,
                f"Покриття «{path}»: кінець ({_stamp(span.end_ms)}) має бути пізніше початку "
                f"({_stamp(span.start_ms)})",
                path=path,
            )
        if span.start_ms < previous_end:
            raise TranscriptImportError(
                TRANSCRIPT_ORDER_INVALID,
                f"Покриття «{path}»: інтервали покриття мають іти за зростанням і не перекриватися",
                path=path,
            )
        previous_end = span.end_ms


def _shift_items(
    label: str, items: Sequence[TranscriptItem], duration: int, offset: int
) -> tuple[TranscriptItem, ...]:
    shifted: list[TranscriptItem] = []
    for index, item in enumerate(items):
        start = item.start_ms + offset
        end = item.end_ms + offset
        if start < 0 or end > duration:
            path = f"{label}[{index}]"
            raise TranscriptImportError(
                TRANSCRIPT_OUT_OF_BOUNDS,
                _out_of_bounds_message(f"Елемент «{path}»", start, end, duration, offset),
                path=path,
            )
        shifted.append(
            TranscriptItem(
                id=item.id, ordinal=item.ordinal, start_ms=start, end_ms=end, text=item.text
            )
        )
    return tuple(shifted)


def _shift_coverage(
    coverage: Sequence[CoverageSpan], duration: int, offset: int
) -> tuple[CoverageSpan, ...]:
    shifted: list[CoverageSpan] = []
    for index, span in enumerate(coverage):
        start = span.start_ms + offset
        end = span.end_ms + offset
        if offset == 0:
            if end > duration:
                path = f"coverage[{index}]"
                raise TranscriptImportError(
                    TRANSCRIPT_OUT_OF_BOUNDS,
                    _out_of_bounds_message(f"Покриття «{path}»", start, end, duration, offset),
                    path=path,
                )
        else:
            # The shifted part outside the source is not coverage of the source.
            start = max(start, 0)
            end = min(end, duration)
            if end <= start:
                continue
        shifted.append(CoverageSpan(start_ms=start, end_ms=end, state=span.state))
    return tuple(shifted)
