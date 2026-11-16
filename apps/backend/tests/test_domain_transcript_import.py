"""T-023 / T-024 / FR-019: SRT, VTT and normalized JSON transcript import."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clipforge.domain.timecode import format_timecode
from clipforge.domain.transcript_import import (
    DEFAULT_LANGUAGE,
    SUPPORTED_SCHEMA_KEYWORDS,
    TRANSCRIPT_CUE_MALFORMED,
    TRANSCRIPT_DURATION_INVALID,
    TRANSCRIPT_EMPTY,
    TRANSCRIPT_ID_DUPLICATE,
    TRANSCRIPT_INTERVAL_REVERSED,
    TRANSCRIPT_LANGUAGE_INVALID,
    TRANSCRIPT_NO_CUES,
    TRANSCRIPT_OFFSET_INVALID,
    TRANSCRIPT_ORDER_INVALID,
    TRANSCRIPT_OUT_OF_BOUNDS,
    TRANSCRIPT_SCHEMA,
    TRANSCRIPT_SCHEMA_INVALID,
    TRANSCRIPT_TEXT_TOO_LONG,
    TRANSCRIPT_TIMESTAMP_INVALID,
    TRANSCRIPT_TIMING_LEVEL_MISMATCH,
    TRANSCRIPT_VTT_HEADER_INVALID,
    TRANSCRIPT_VTT_HEADER_MISSING,
    CoverageSpan,
    NormalizedTranscript,
    TranscriptImportError,
    parse_srt,
    parse_vtt,
    validate_normalized_json,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SCHEMA_PATH = CONTRACTS / "transcript.schema.json"
EXAMPLE_PATH = CONTRACTS / "transcript.example.json"

DURATION = 60_000
CYRILLIC = re.compile("[А-Яа-яІіЇїЄєҐґ]")

SRT_BASIC = (
    "1\n00:00:01,000 --> 00:00:02,500\nПерша репліка\n\n"
    "2\n00:00:03,000 --> 00:00:04,500\nДруга\nу два рядки\n\n"
)


def _error(call: Callable[[], object]) -> TranscriptImportError:
    with pytest.raises(TranscriptImportError) as caught:
        call()
    error = caught.value
    assert str(error) == error.code
    assert CYRILLIC.search(error.message), error.message
    return error


def _segments(transcript: NormalizedTranscript) -> list[tuple[str, int, int, int, str]]:
    return [(s.id, s.ordinal, s.start_ms, s.end_ms, s.text) for s in transcript.segments]


def _srt(text: str, *, duration: int = DURATION, offset: int = 0) -> NormalizedTranscript:
    return parse_srt(text, duration_ms=duration, offset_ms=offset)


def _vtt(text: str, *, duration: int = DURATION, offset: int = 0) -> NormalizedTranscript:
    return parse_vtt(text, duration_ms=duration, offset_ms=offset)


def _single_cue_srt(text: str) -> NormalizedTranscript:
    return _srt(f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n")


def _example() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    return document


# --- SRT (T-023) ---------------------------------------------------------------------


def test_srt_builds_segment_level_transcript() -> None:
    transcript = _srt(SRT_BASIC)
    assert transcript.timing_level == "segment"
    assert transcript.language == DEFAULT_LANGUAGE == "und"
    assert transcript.duration_ms == DURATION
    assert transcript.words == ()
    assert _segments(transcript) == [
        ("s1", 0, 1000, 2500, "Перша репліка"),
        ("s2", 1, 3000, 4500, "Друга у два рядки"),
    ]
    assert transcript.coverage == (CoverageSpan(0, DURATION, "succeeded"),)


def test_srt_result_round_trips_through_json_contract() -> None:
    transcript = _srt(SRT_BASIC)
    document = transcript.to_json()
    assert document["schema_version"] == 1
    assert document["words"] == []
    assert validate_normalized_json(document, duration_ms=DURATION) == transcript


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_srt_line_endings(newline: str) -> None:
    assert _segments(_srt(SRT_BASIC.replace("\n", newline))) == _segments(_srt(SRT_BASIC))


def test_srt_utf8_bom_is_ignored() -> None:
    transcript = _srt("﻿" + SRT_BASIC.replace("\n", "\r\n"))
    assert _segments(transcript) == _segments(_srt(SRT_BASIC))


def test_srt_multiline_cue_joins_lines_with_single_spaces() -> None:
    transcript = _single_cue_srt("Рядок 1\nРядок 2\n   Рядок 3   ")
    assert transcript.segments[0].text == "Рядок 1 Рядок 2 Рядок 3"


def test_srt_timestamp_formats_and_hours() -> None:
    text = (
        "1\n00:00:00,000 --> 00:00:00,001\nМіліметр\n\n"
        "2\n00:59:59,999 --> 01:00:00,000\nМеж години\n\n"
        "3\n09:00:00,000 --> 09:00:01,500\nДев'ять\n\n"
        "4\n10:00:00,000 --> 10:00:01,000\nДесять\n\n"
        "5\n12:34:56,789 --> 12:34:57,000\nДванадцять\n\n"
        "6\n100:00:00,000 --> 100:00:00,500\nСто годин\n"
    )
    transcript = _srt(text, duration=400_000_000)
    assert [(s.start_ms, s.end_ms) for s in transcript.segments] == [
        (0, 1),
        (3_599_999, 3_600_000),
        (32_400_000, 32_401_500),
        (36_000_000, 36_001_000),
        (45_296_789, 45_297_000),
        (360_000_000, 360_000_500),
    ]


def test_srt_accepts_dot_before_milliseconds() -> None:
    transcript = _srt("1\n00:00:01.250 --> 00:00:02.500\nКрапка\n")
    assert (transcript.segments[0].start_ms, transcript.segments[0].end_ms) == (1250, 2500)


def test_srt_cue_identifiers_are_optional_and_not_trusted() -> None:
    text = (
        "7\n00:00:01,000 --> 00:00:02,000\nЗ номером\n\n"
        "00:00:03,000 --> 00:00:04,000\nБез номера\n\n"
        "intro\n00:00:05,000 --> 00:00:06,000\nЗ назвою\n"
    )
    assert _segments(_srt(text)) == [
        ("s1", 0, 1000, 2000, "З номером"),
        ("s2", 1, 3000, 4000, "Без номера"),
        ("s3", 2, 5000, 6000, "З назвою"),
    ]


def test_srt_extra_blank_lines_and_trailing_whitespace_lines() -> None:
    text = (
        "\n\n1\n00:00:01,000 --> 00:00:02,000\nА\n   \n\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nБ\n\n\n"
    )
    assert [s.text for s in _srt(text).segments] == ["А", "Б"]


def test_srt_overlapping_cues_are_kept() -> None:
    text = (
        "1\n00:00:01,000 --> 00:00:05,000\nПерший\n\n2\n00:00:02,000 --> 00:00:03,000\nПерекриття\n"
    )
    assert [(s.start_ms, s.end_ms) for s in _srt(text).segments] == [(1000, 5000), (2000, 3000)]


def test_srt_unsorted_cues_are_sorted_stably_and_numbered_after_sorting() -> None:
    text = (
        "1\n00:00:05,000 --> 00:00:06,000\nпізніше\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nперше\n\n"
        "3\n00:00:01,000 --> 00:00:03,000\nтой самий початок\n"
    )
    assert _segments(_srt(text)) == [
        ("s1", 0, 1000, 2000, "перше"),
        ("s2", 1, 1000, 3000, "той самий початок"),
        ("s3", 2, 5000, 6000, "пізніше"),
    ]


def test_srt_cues_with_empty_text_are_dropped_and_ids_stay_contiguous() -> None:
    text = (
        "1\n00:00:01,000 --> 00:00:02,000\nА\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\n<i></i>\n\n"
        "4\n00:00:07,000 --> 00:00:08,000\nБ\n"
    )
    assert _segments(_srt(text)) == [
        ("s1", 0, 1000, 2000, "А"),
        ("s2", 1, 7000, 8000, "Б"),
    ]


def test_srt_repeated_speech_at_different_times_survives() -> None:
    text = "1\n00:00:01,000 --> 00:00:02,000\nНі\n\n2\n00:00:10,000 --> 00:00:11,000\nНі\n"
    assert [(s.text, s.start_ms) for s in _srt(text).segments] == [("Ні", 1000), ("Ні", 10_000)]


def test_srt_rejects_settings_after_end_timestamp() -> None:
    error = _error(lambda: _srt("1\n00:00:01,000 --> 00:00:02,000 X1:1 X2:2\nТекст\n"))
    assert error.code == TRANSCRIPT_CUE_MALFORMED
    assert error.line == 2


def test_srt_rejects_vtt_input() -> None:
    error = _error(lambda: _srt("WEBVTT\n\n00:01.000 --> 00:02.000\nТекст\n"))
    assert error.code == TRANSCRIPT_CUE_MALFORMED
    assert error.line == 1


def test_srt_missing_blank_line_between_cues_is_malformed() -> None:
    text = "1\n00:00:01,000 --> 00:00:02,000\nА\n2\n00:00:03,000 --> 00:00:04,000\nБ\n"
    error = _error(lambda: _srt(text))
    assert error.code == TRANSCRIPT_CUE_MALFORMED
    assert error.line == 5


def test_cue_text_containing_an_arrow_is_malformed() -> None:
    error = _error(lambda: _single_cue_srt("до --> після"))
    assert error.code == TRANSCRIPT_CUE_MALFORMED
    assert error.line == 3


def test_srt_timing_line_too_deep_in_block_is_malformed() -> None:
    error = _error(lambda: _srt("1\nсмітник\n00:00:01,000 --> 00:00:02,000\nТекст\n"))
    assert error.code == TRANSCRIPT_CUE_MALFORMED
    assert error.line == 3


@pytest.mark.parametrize(
    "timing",
    [
        "00:00:01 --> 00:00:02,000",
        "0:00:01,000 --> 00:00:02,000",
        "00:60:01,000 --> 00:61:02,000",
        "00:00:60,000 --> 00:01:02,000",
        "00:00:01,5 --> 00:00:02,000",
        "00:00:01,000 --> 00:00:02,0000",
        "aa:bb:cc,ddd --> 00:00:02,000",
        "٠٠:٠٠:٠١,٠٠٠ --> 00:00:02,000",
        "00:00:01,000 --> -00:00:02,000",
        "00:00:01,000 --> " + "9" * 5000,
    ],
)
def test_srt_malformed_timestamp(timing: str) -> None:
    error = _error(lambda: _srt(f"1\n{timing}\nТекст\n"))
    assert error.code == TRANSCRIPT_TIMESTAMP_INVALID
    assert error.line == 2
    assert len(error.message) < 300


@pytest.mark.parametrize(
    "timing",
    [
        "00:00:01,000 - 00:00:02,000",
        "00:00:01,000->00:00:02,000",
        "00:00:01,000-->00:00:02,000",
        "00:00:01,000 -->",
        "--> 00:00:02,000",
    ],
)
def test_srt_malformed_timing_line(timing: str) -> None:
    error = _error(lambda: _srt(f"1\n{timing}\nТекст\n"))
    assert error.code == TRANSCRIPT_CUE_MALFORMED


@pytest.mark.parametrize(
    "timing", ["00:00:02,000 --> 00:00:01,000", "00:00:02,000 --> 00:00:02,000"]
)
def test_srt_reversed_or_empty_interval_is_rejected(timing: str) -> None:
    error = _error(lambda: _srt(f"1\n00:00:00,000 --> 00:00:01,000\nА\n\n2\n{timing}\nБ\n"))
    assert error.code == TRANSCRIPT_INTERVAL_REVERSED
    assert error.line == 6


@pytest.mark.parametrize("text", ["", "   ", "\n\n", " \t\r\n \r\n", "﻿", "﻿\r\n\r\n"])
@pytest.mark.parametrize("parse", [_srt, _vtt], ids=["srt", "vtt"])
def test_empty_file_is_rejected(parse: Callable[[str], NormalizedTranscript], text: str) -> None:
    assert _error(lambda: parse(text)).code == TRANSCRIPT_EMPTY


def test_srt_without_any_text_cue_is_rejected() -> None:
    text = "1\n00:00:01,000 --> 00:00:02,000\n\n2\n00:00:03,000 --> 00:00:04,000\n<i></i>\n"
    assert _error(lambda: _srt(text)).code == TRANSCRIPT_NO_CUES


# --- T-024: cues outside the source --------------------------------------------------


def test_srt_end_beyond_duration_is_rejected() -> None:
    text = "1\n00:00:01,000 --> 00:00:02,000\nА\n\n2\n00:00:09,000 --> 00:00:10,001\nБ\n"
    error = _error(lambda: _srt(text, duration=10_000))
    assert error.code == TRANSCRIPT_OUT_OF_BOUNDS
    assert error.line == 6
    assert "межі" in error.message


def test_srt_end_equal_to_duration_is_allowed() -> None:
    transcript = _srt("1\n00:00:09,000 --> 00:00:10,000\nА\n", duration=10_000)
    assert transcript.segments[0].end_ms == 10_000


def test_srt_start_at_duration_is_rejected() -> None:
    text = "1\n00:00:10,000 --> 00:00:11,000\nА\n"
    assert _error(lambda: _srt(text, duration=10_000)).code == TRANSCRIPT_OUT_OF_BOUNDS


def test_srt_bounds_checked_for_empty_text_cues_too() -> None:
    text = "1\n00:00:09,000 --> 00:00:11,000\n\n2\n00:00:01,000 --> 00:00:02,000\nА\n"
    assert _error(lambda: _srt(text, duration=10_000)).code == TRANSCRIPT_OUT_OF_BOUNDS


# --- offset --------------------------------------------------------------------------

OFFSET_SRT = "1\n00:00:01,000 --> 00:00:02,000\nА\n\n2\n00:00:03,000 --> 00:00:04,500\nБ\n"


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, [(1000, 2000), (3000, 4500)]),
        (500, [(1500, 2500), (3500, 5000)]),
        (-1000, [(0, 1000), (2000, 3500)]),
        (5500, [(6500, 7500), (8500, 10_000)]),
    ],
)
def test_srt_offset_is_applied_once(offset: int, expected: list[tuple[int, int]]) -> None:
    transcript = _srt(OFFSET_SRT, duration=10_000, offset=offset)
    assert [(s.start_ms, s.end_ms) for s in transcript.segments] == expected
    assert transcript.coverage == (CoverageSpan(0, 10_000, "succeeded"),)


def test_srt_offset_pushing_cue_past_the_end_is_rejected() -> None:
    error = _error(lambda: _srt(OFFSET_SRT, duration=10_000, offset=5501))
    assert error.code == TRANSCRIPT_OUT_OF_BOUNDS
    assert error.line == 6
    assert "5501" in error.message


def test_srt_offset_pushing_cue_before_zero_is_rejected() -> None:
    error = _error(lambda: _srt(OFFSET_SRT, duration=10_000, offset=-1001))
    assert error.code == TRANSCRIPT_OUT_OF_BOUNDS
    assert error.line == 2
    assert "-00:00:00.001" in error.message


def test_vtt_offset_is_applied_once() -> None:
    text = "WEBVTT\n\n00:01.000 --> 00:02.000\nА\n"
    assert _vtt(text, duration=10_000, offset=250).segments[0].start_ms == 1250


@pytest.mark.parametrize("offset", [1.5, True, "1", None])
def test_offset_must_be_an_integer(offset: Any) -> None:
    error = _error(lambda: parse_srt(SRT_BASIC, duration_ms=DURATION, offset_ms=offset))
    assert error.code == TRANSCRIPT_OFFSET_INVALID


@pytest.mark.parametrize("duration", [0, -1, True, 1.5, None])
def test_duration_must_be_a_positive_integer(duration: Any) -> None:
    error = _error(lambda: parse_srt(SRT_BASIC, duration_ms=duration))
    assert error.code == TRANSCRIPT_DURATION_INVALID


def test_language_is_passed_through() -> None:
    assert parse_srt(SRT_BASIC, duration_ms=DURATION, language="uk").language == "uk"
    assert parse_vtt(VTT_RICH, duration_ms=40_000_000, language="uk-UA").language == "uk-UA"


@pytest.mark.parametrize("language", ["u", "x" * 33, "", None, 5])
def test_language_is_validated(language: Any) -> None:
    error = _error(lambda: parse_srt(SRT_BASIC, duration_ms=DURATION, language=language))
    assert error.code == TRANSCRIPT_LANGUAGE_INVALID


def test_cue_text_length_limit() -> None:
    assert len(_single_cue_srt("а" * 2000).segments[0].text) == 2000
    error = _error(lambda: _single_cue_srt("а" * 2001))
    assert error.code == TRANSCRIPT_TEXT_TOO_LONG
    assert error.line == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<i>Привіт</i>", "Привіт"),
        ("<b>А</b> <u>Б</u>", "А Б"),
        ("<c.yellow.bg_blue>Жовте</c>", "Жовте"),
        ("<v Esther>Привіт</v>", "Привіт"),
        ("<lang en>Hello</lang>", "Hello"),
        ('<font color="#ff0000">Червоне</font>', "Червоне"),
        ("Ра<00:00:01.500>зом", "Разом"),
        ("<ruby>漢<rt>kan</rt></ruby>", "漢kan"),
        ("<<b>b>текст", "текст"),
        ("&amp; &lt;b&gt; &nbsp;x", "& <b> x"),
        ("{\\an8}Угорі", "Угорі"),
        ("2 < 3 > 1", "2 < 3 > 1"),
        ("a\x07b\x00c", "abc"),
        ("  багато    пробілів\t тут ", "багато пробілів тут"),
    ],
)
def test_cue_text_is_plain(raw: str, expected: str) -> None:
    assert _single_cue_srt(raw).segments[0].text == expected


# --- VTT (T-023) ---------------------------------------------------------------------

VTT_RICH = (
    "WEBVTT - Підсумки\n"
    "Kind: captions\n"
    "Language: uk\n"
    "\n"
    "NOTE Це коментар\n"
    "на два рядки\n"
    "\n"
    "NOTE\n"
    "\n"
    "STYLE\n"
    "::cue {\n"
    "  color: red;\n"
    "}\n"
    "\n"
    "REGION\n"
    "id:fred\n"
    "width:40%\n"
    "\n"
    "intro\n"
    "00:01.000 --> 00:02.500 align:start position:10% line:0\n"
    "Перша <i>репліка</i>\n"
    "\n"
    "00:03.000 --> 01:02:03.456\n"
    "Друга\n"
    "у два рядки\n"
    "\n"
    "cue-3\n"
    "10:00:00.000 --> 10:00:01.000\n"
    "Години\n"
)


def test_vtt_rich_file() -> None:
    transcript = _vtt(VTT_RICH, duration=40_000_000)
    assert transcript.timing_level == "segment"
    assert transcript.words == ()
    assert _segments(transcript) == [
        ("s1", 0, 1000, 2500, "Перша репліка"),
        ("s2", 1, 3000, 3_723_456, "Друга у два рядки"),
        ("s3", 2, 36_000_000, 36_001_000, "Години"),
    ]
    assert transcript.coverage == (CoverageSpan(0, 40_000_000, "succeeded"),)


@pytest.mark.parametrize("newline", ["\r\n", "\r"], ids=["crlf", "cr"])
def test_vtt_bom_and_line_endings(newline: str) -> None:
    text = "﻿" + VTT_RICH.replace("\n", newline)
    assert _segments(_vtt(text, duration=40_000_000)) == _segments(
        _vtt(VTT_RICH, duration=40_000_000)
    )


@pytest.mark.parametrize(
    ("timing", "expected"),
    [
        ("00:00.000 --> 00:00.001", (0, 1)),
        ("00:01.000 --> 59:59.999", (1000, 3_599_999)),
        ("01:00.000 --> 01:00:01.500", (60_000, 3_601_500)),
        ("00:00:01.000 --> 00:00:02.000", (1000, 2000)),
        ("00:00:01.000 --> 00:00:02.000\t align:center", (1000, 2000)),
        ("10:00:00.000 --> 10:00:01.250", (36_000_000, 36_001_250)),
    ],
)
def test_vtt_timestamp_formats(timing: str, expected: tuple[int, int]) -> None:
    transcript = _vtt(f"WEBVTT\n\n{timing}\nТекст\n", duration=40_000_000)
    assert (transcript.segments[0].start_ms, transcript.segments[0].end_ms) == expected


def test_vtt_cue_settings_are_ignored() -> None:
    text = (
        "WEBVTT\n\n"
        "00:01.000 --> 00:02.000 vertical:rl line:-1 position:50% size:80% align:end region:fred\n"
        "Текст\n"
    )
    assert _vtt(text).segments[0].text == "Текст"


def test_vtt_header_only_variants() -> None:
    assert _error(lambda: _vtt("WEBVTT\n")).code == TRANSCRIPT_NO_CUES
    assert _error(lambda: _vtt("WEBVTT\n\nNOTE тільки коментар\n")).code == TRANSCRIPT_NO_CUES


def test_vtt_note_style_region_blocks_are_not_cues() -> None:
    text = (
        "WEBVTT\n\n"
        "NOTE\nНе репліка\n\n"
        "STYLE  \n::cue(b) { color: red }\n\n"
        "00:01.000 --> 00:02.000\nТекст\n"
    )
    assert [s.text for s in _vtt(text).segments] == ["Текст"]


def test_vtt_identifier_that_starts_like_note_is_a_cue() -> None:
    text = "WEBVTT\n\nNOTES-1\n00:01.000 --> 00:02.000\nТекст\n"
    assert [s.text for s in _vtt(text).segments] == ["Текст"]


@pytest.mark.parametrize(
    "text",
    [
        "00:01.000 --> 00:02.000\nТекст\n",
        "WEBVTTX\n\n00:01.000 --> 00:02.000\nТекст\n",
        "webvtt\n\n00:01.000 --> 00:02.000\nТекст\n",
        "NOTE\n\nWEBVTT\n\n00:01.000 --> 00:02.000\nТекст\n",
    ],
)
def test_vtt_requires_header(text: str) -> None:
    assert _error(lambda: _vtt(text)).code == TRANSCRIPT_VTT_HEADER_MISSING


def test_vtt_header_must_be_followed_by_blank_line() -> None:
    error = _error(lambda: _vtt("WEBVTT\n00:01.000 --> 00:02.000\nТекст\n"))
    assert error.code == TRANSCRIPT_VTT_HEADER_INVALID
    assert error.line == 2


@pytest.mark.parametrize(
    ("timing", "code"),
    [
        ("00:01,000 --> 00:02.000", TRANSCRIPT_TIMESTAMP_INVALID),
        ("00:01.000 --> 00:02,000", TRANSCRIPT_TIMESTAMP_INVALID),
        ("01.000 --> 02.000", TRANSCRIPT_TIMESTAMP_INVALID),
        ("1:00:01.000 --> 1:00:02.000", TRANSCRIPT_TIMESTAMP_INVALID),
        ("00:61.000 --> 00:62.000", TRANSCRIPT_TIMESTAMP_INVALID),
        ("00:01.00 --> 00:02.000", TRANSCRIPT_TIMESTAMP_INVALID),
        ("00:01.000 -> 00:02.000", TRANSCRIPT_CUE_MALFORMED),
        ("00:02.000 --> 00:01.000", TRANSCRIPT_INTERVAL_REVERSED),
        ("00:02.000 --> 00:02.000", TRANSCRIPT_INTERVAL_REVERSED),
    ],
)
def test_vtt_malformed_cues(timing: str, code: str) -> None:
    error = _error(lambda: _vtt(f"WEBVTT\n\n{timing}\nТекст\n"))
    assert error.code == code
    assert error.line == 3


def test_vtt_block_without_timing_is_malformed() -> None:
    error = _error(lambda: _vtt("WEBVTT\n\nпросто текст\n"))
    assert error.code == TRANSCRIPT_CUE_MALFORMED
    assert error.line == 3


def test_vtt_end_beyond_duration_is_rejected() -> None:
    text = "WEBVTT\n\n00:09.000 --> 00:10.001\nТекст\n"
    error = _error(lambda: _vtt(text, duration=10_000))
    assert error.code == TRANSCRIPT_OUT_OF_BOUNDS
    assert error.line == 3


def test_vtt_result_round_trips_through_json_contract() -> None:
    transcript = _vtt(VTT_RICH, duration=40_000_000)
    assert validate_normalized_json(transcript.to_json(), duration_ms=40_000_000) == transcript


cue_texts = st.from_regex(r"[A-Za-z]+( [A-Za-z]+){0,3}", fullmatch=True)


@settings(max_examples=60, deadline=None)
@given(
    cues=st.lists(
        st.tuples(st.integers(0, 3_599_000), st.integers(1, 5000), cue_texts),
        min_size=1,
        max_size=15,
    ),
    newline=st.sampled_from(["\n", "\r\n"]),
)
def test_generated_srt_round_trips(cues: list[tuple[int, int, str]], newline: str) -> None:
    text = (newline * 2).join(
        f"{number}{newline}{format_timecode(start, separator=',')} --> "
        f"{format_timecode(start + length, separator=',')}{newline}{words}"
        for number, (start, length, words) in enumerate(cues, start=1)
    )
    transcript = _srt(text, duration=3_700_000)
    expected = sorted(cues, key=lambda cue: cue[0])
    assert _segments(transcript) == [
        (f"s{number}", number - 1, start, start + length, words)
        for number, (start, length, words) in enumerate(expected, start=1)
    ]


# --- normalized JSON -----------------------------------------------------------------


def test_contract_example_is_accepted_with_word_timing() -> None:
    example = _example()
    transcript = validate_normalized_json(example, duration_ms=12_000)
    assert transcript.timing_level == "word"
    assert transcript.language == "uk"
    assert transcript.duration_ms == 12_000
    assert [word.id for word in transcript.words] == ["w1", "w2", "w3", "w4"]
    assert [segment.id for segment in transcript.segments] == ["s1", "s2"]
    assert transcript.coverage == (CoverageSpan(0, 12_000, "succeeded"),)
    assert transcript.to_json() == example


def test_document_is_not_modified() -> None:
    example = _example()
    snapshot = copy.deepcopy(example)
    validate_normalized_json(example, duration_ms=12_000, offset_ms=500)
    assert example == snapshot


def test_result_uses_source_duration_and_keeps_document_coverage() -> None:
    transcript = validate_normalized_json(_example(), duration_ms=60_000)
    assert transcript.duration_ms == 60_000
    assert transcript.coverage == (CoverageSpan(0, 12_000, "succeeded"),)


def test_source_shorter_than_document_content_is_rejected() -> None:
    error = _error(lambda: validate_normalized_json(_example(), duration_ms=5000))
    assert error.code == TRANSCRIPT_OUT_OF_BOUNDS
    assert error.path == "words[2]"


def test_embedded_schema_equals_contract_file() -> None:
    assert json.loads(SCHEMA_PATH.read_text(encoding="utf-8")) == TRANSCRIPT_SCHEMA


def _keywords(schema: dict[str, Any]) -> set[str]:
    found = set(schema)
    for name in ("items", "additionalProperties"):
        sub = schema.get(name)
        if isinstance(sub, dict):
            found |= _keywords(sub)
    for sub in schema.get("properties", {}).values():
        found |= _keywords(sub)
    return found


def test_contract_uses_only_keywords_the_checker_enforces() -> None:
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert _keywords(schema) <= SUPPORTED_SCHEMA_KEYWORDS


Mutation = Callable[[dict[str, Any]], object]

SCHEMA_MUTATIONS: list[tuple[str, Mutation, str]] = [
    ("missing-words", lambda d: d.pop("words"), "words"),
    ("missing-schema-version", lambda d: d.pop("schema_version"), "schema_version"),
    ("extra-top-level", lambda d: d.update(extra=1), "extra"),
    ("version-2", lambda d: d.update(schema_version=2), "schema_version"),
    ("version-true", lambda d: d.update(schema_version=True), "schema_version"),
    ("level-unknown", lambda d: d.update(timing_level="char"), "timing_level"),
    ("language-short", lambda d: d.update(language="u"), "language"),
    ("language-long", lambda d: d.update(language="x" * 33), "language"),
    ("language-number", lambda d: d.update(language=5), "language"),
    ("duration-zero", lambda d: d.update(duration_ms=0), "duration_ms"),
    ("duration-float", lambda d: d.update(duration_ms=12000.0), "duration_ms"),
    ("words-not-list", lambda d: d.update(words={}), "words"),
    ("word-not-object", lambda d: d["words"].append("w"), "words[4]"),
    ("word-extra-key", lambda d: d["words"][0].update(confidence=0.9), "words[0].confidence"),
    ("word-missing-text", lambda d: d["words"][0].pop("text"), "words[0].text"),
    ("word-empty-text", lambda d: d["words"][0].update(text=""), "words[0].text"),
    ("word-text-2001", lambda d: d["words"][0].update(text="а" * 2001), "words[0].text"),
    ("word-empty-id", lambda d: d["words"][0].update(id=""), "words[0].id"),
    ("word-start-negative", lambda d: d["words"][0].update(start_ms=-1), "words[0].start_ms"),
    ("word-start-float", lambda d: d["words"][0].update(start_ms=1000.0), "words[0].start_ms"),
    ("word-start-bool", lambda d: d["words"][0].update(start_ms=True), "words[0].start_ms"),
    ("word-start-string", lambda d: d["words"][0].update(start_ms="1000"), "words[0].start_ms"),
    (
        "segment-ordinal-negative",
        lambda d: d["segments"][1].update(ordinal=-1),
        "segments[1].ordinal",
    ),
    ("coverage-bad-state", lambda d: d["coverage"][0].update(state="done"), "coverage[0].state"),
    ("coverage-missing-end", lambda d: d["coverage"][0].pop("end_ms"), "coverage[0].end_ms"),
]


@pytest.mark.parametrize(
    ("mutate", "path"),
    [(mutate, path) for _, mutate, path in SCHEMA_MUTATIONS],
    ids=[name for name, _, _ in SCHEMA_MUTATIONS],
)
def test_schema_violations_are_rejected_with_path(mutate: Mutation, path: str) -> None:
    document = _example()
    mutate(document)
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_SCHEMA_INVALID
    assert error.path == path


@pytest.mark.parametrize("document", [None, [], "{}", 5, [_example()]])
def test_document_must_be_an_object(document: object) -> None:
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_SCHEMA_INVALID
    assert error.path is None


def test_json_end_beyond_duration_is_rejected() -> None:
    document = _example()
    document["words"][3]["end_ms"] = 12_001
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_OUT_OF_BOUNDS
    assert error.path == "words[3]"


def test_json_segment_end_beyond_duration_is_rejected() -> None:
    document = _example()
    document["segments"][1]["end_ms"] = 12_001
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_OUT_OF_BOUNDS
    assert error.path == "segments[1]"


def test_json_end_equal_to_duration_is_allowed() -> None:
    document = _example()
    document["words"][3]["end_ms"] = 12_000
    document["segments"][1]["end_ms"] = 12_000
    assert validate_normalized_json(document, duration_ms=12_000).words[3].end_ms == 12_000


def test_json_coverage_beyond_duration_is_rejected_without_offset() -> None:
    document = _example()
    document["coverage"][0]["end_ms"] = 12_001
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_OUT_OF_BOUNDS
    assert error.path == "coverage[0]"


@pytest.mark.parametrize(("start", "end"), [(3000, 3000), (3500, 3000)])
def test_json_reversed_or_empty_interval_is_rejected(start: int, end: int) -> None:
    document = _example()
    document["words"][1].update(start_ms=start, end_ms=end)
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_INTERVAL_REVERSED
    assert error.path == "words[1]"


def test_json_reversed_coverage_is_rejected() -> None:
    document = _example()
    document["coverage"] = [{"start_ms": 5000, "end_ms": 5000, "state": "failed"}]
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_INTERVAL_REVERSED
    assert error.path == "coverage[0]"


def test_json_duplicate_ids_are_rejected() -> None:
    document = _example()
    document["words"][1]["id"] = "w1"
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_ID_DUPLICATE
    assert error.path == "words[1]"


def test_json_id_shared_by_a_word_and_a_segment_is_rejected() -> None:
    document = _example()
    document["segments"][0]["id"] = "w1"
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_ID_DUPLICATE
    assert error.path == "segments[0]"


def test_json_ordinals_must_increase() -> None:
    document = _example()
    document["words"][2]["ordinal"] = 1
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_ORDER_INVALID
    assert error.path == "words[2]"


def test_json_ordinal_gaps_are_allowed() -> None:
    document = _example()
    for ordinal, word in zip([0, 2, 5, 9], document["words"], strict=True):
        word["ordinal"] = ordinal
    assert [w.ordinal for w in validate_normalized_json(document, duration_ms=12_000).words] == [
        0,
        2,
        5,
        9,
    ]


def test_json_starts_must_not_go_backwards() -> None:
    document = _example()
    document["words"][1].update(start_ms=500, end_ms=900)
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_ORDER_INVALID
    assert error.path == "words[1]"


def test_json_overlapping_words_with_ordered_starts_are_allowed() -> None:
    document = _example()
    document["words"][1].update(start_ms=1500, end_ms=3500)
    transcript = validate_normalized_json(document, duration_ms=12_000)
    assert (transcript.words[1].start_ms, transcript.words[1].end_ms) == (1500, 3500)


def test_json_coverage_must_be_ordered_and_not_overlap() -> None:
    document = _example()
    document["coverage"] = [
        {"start_ms": 0, "end_ms": 5000, "state": "succeeded"},
        {"start_ms": 4000, "end_ms": 12_000, "state": "failed"},
    ]
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_ORDER_INVALID
    assert error.path == "coverage[1]"


def test_json_adjacent_coverage_with_different_states_is_kept() -> None:
    document = _example()
    document["coverage"] = [
        {"start_ms": 0, "end_ms": 5000, "state": "succeeded"},
        {"start_ms": 5000, "end_ms": 12_000, "state": "unknown_outcome"},
    ]
    transcript = validate_normalized_json(document, duration_ms=12_000)
    assert transcript.coverage == (
        CoverageSpan(0, 5000, "succeeded"),
        CoverageSpan(5000, 12_000, "unknown_outcome"),
    )


def test_json_segment_level_must_not_carry_words() -> None:
    document = _example()
    document["timing_level"] = "segment"
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_TIMING_LEVEL_MISMATCH


def test_json_word_level_with_segments_needs_words() -> None:
    document = _example()
    document["words"] = []
    error = _error(lambda: validate_normalized_json(document, duration_ms=12_000))
    assert error.code == TRANSCRIPT_TIMING_LEVEL_MISMATCH


def test_json_silent_transcript_is_valid_at_both_levels() -> None:
    for level in ("word", "segment"):
        document = _example()
        document.update(timing_level=level, words=[], segments=[])
        transcript = validate_normalized_json(document, duration_ms=12_000)
        assert transcript.timing_level == level
        assert transcript.segments == ()


def test_json_segment_level_document_is_valid() -> None:
    document = _example()
    document.update(timing_level="segment", words=[])
    transcript = validate_normalized_json(document, duration_ms=12_000)
    assert transcript.timing_level == "segment"
    assert len(transcript.segments) == 2


def test_json_offset_is_applied_once_to_every_time() -> None:
    transcript = validate_normalized_json(_example(), duration_ms=12_000, offset_ms=1000)
    assert [(w.start_ms, w.end_ms) for w in transcript.words] == [
        (2000, 3000),
        (3000, 4000),
        (6000, 7000),
        (7000, 8000),
    ]
    assert [(s.start_ms, s.end_ms) for s in transcript.segments] == [(2000, 4000), (6000, 8000)]
    assert [w.ordinal for w in transcript.words] == [0, 1, 2, 3]
    # The part of the coverage that left the source is cut off.
    assert transcript.coverage == (CoverageSpan(1000, 12_000, "succeeded"),)


def test_json_negative_offset_clips_coverage_at_zero() -> None:
    transcript = validate_normalized_json(_example(), duration_ms=12_000, offset_ms=-1000)
    assert (transcript.words[0].start_ms, transcript.words[0].end_ms) == (0, 1000)
    assert transcript.coverage == (CoverageSpan(0, 11_000, "succeeded"),)


def test_json_offset_drops_coverage_that_left_the_source() -> None:
    document = _example()
    document.update(words=[], segments=[])
    document["coverage"] = [
        {"start_ms": 0, "end_ms": 2000, "state": "failed"},
        {"start_ms": 2000, "end_ms": 12_000, "state": "succeeded"},
    ]
    transcript = validate_normalized_json(document, duration_ms=12_000, offset_ms=-2000)
    assert transcript.coverage == (CoverageSpan(0, 10_000, "succeeded"),)


def test_json_offset_pushing_content_out_of_the_source_is_rejected() -> None:
    error = _error(lambda: validate_normalized_json(_example(), duration_ms=12_000, offset_ms=5001))
    assert error.code == TRANSCRIPT_OUT_OF_BOUNDS
    assert error.path == "words[3]"
    error = _error(
        lambda: validate_normalized_json(_example(), duration_ms=12_000, offset_ms=-1001)
    )
    assert error.code == TRANSCRIPT_OUT_OF_BOUNDS
    assert error.path == "words[0]"
    ok = validate_normalized_json(_example(), duration_ms=12_000, offset_ms=5000)
    assert ok.words[3].end_ms == 12_000


@pytest.mark.parametrize("offset", [0.5, False, "0", None])
def test_json_offset_must_be_an_integer(offset: Any) -> None:
    error = _error(
        lambda: validate_normalized_json(_example(), duration_ms=12_000, offset_ms=offset)
    )
    assert error.code == TRANSCRIPT_OFFSET_INVALID


@pytest.mark.parametrize("duration", [0, -5, False, 12.0, None])
def test_json_duration_must_be_a_positive_integer(duration: Any) -> None:
    error = _error(lambda: validate_normalized_json(_example(), duration_ms=duration))
    assert error.code == TRANSCRIPT_DURATION_INVALID
