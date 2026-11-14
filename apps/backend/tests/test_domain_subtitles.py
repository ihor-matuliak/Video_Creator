"""T-039 / FR-043: clip-local SRT subtitles from transcript segments."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.domain.interval import (
    INTERVAL_END_NOT_AFTER_START,
    INTERVAL_NEGATIVE,
    INTERVAL_NOT_INTEGER,
    IntervalError,
)
from clipforge.domain.subtitles import SubtitleSegment, build_clip_srt
from clipforge.domain.timecode import parse_timecode

# Clip [10 000, 20 000) ms: clip-local duration is 10 000 ms.
CLIP_START = 10_000
CLIP_END = 20_000


def _build(segments: list[SubtitleSegment]) -> str:
    return build_clip_srt(segments, start_ms=CLIP_START, end_ms=CLIP_END)


def _blocks(srt: str) -> list[tuple[int, int, int, str]]:
    """Parse SRT text into ``(index, start_ms, end_ms, text)``; structure must be exact."""
    parsed: list[tuple[int, int, int, str]] = []
    for block in srt.split("\n\n"):
        if not block:
            continue
        index_line, timing_line, text_line = block.split("\n")
        start_text, arrow, end_text = timing_line.split(" ")
        assert arrow == "-->"
        parsed.append(
            (
                int(index_line),
                parse_timecode(start_text),
                parse_timecode(end_text),
                text_line,
            )
        )
    return parsed


def test_exact_srt_output_for_mixed_segments() -> None:
    srt = _build(
        [
            SubtitleSegment(9_000, 11_000, "Початок"),
            SubtitleSegment(12_000, 13_500, "Середина"),
            SubtitleSegment(19_000, 25_000, "Кінець"),
        ]
    )
    assert srt == (
        "1\n00:00:00,000 --> 00:00:01,000\nПочаток\n\n"
        "2\n00:00:02,000 --> 00:00:03,500\nСередина\n\n"
        "3\n00:00:09,000 --> 00:00:10,000\nКінець\n\n"
    )


def test_cues_are_shifted_to_clip_zero_with_hour_scale_times() -> None:
    srt = build_clip_srt(
        [SubtitleSegment(3_601_500, 3_603_000, "Привіт")],
        start_ms=3_600_000,
        end_ms=3_700_000,
    )
    assert srt == "1\n00:00:01,500 --> 00:00:03,000\nПривіт\n\n"


def test_segments_crossing_clip_edges_are_trimmed() -> None:
    blocks = _blocks(
        _build(
            [
                SubtitleSegment(9_000, 11_000, "початок"),
                SubtitleSegment(19_000, 25_000, "кінець"),
                SubtitleSegment(5_000, 30_000, "весь"),
            ]
        )
    )
    assert blocks == [
        (1, 0, 1_000, "початок"),
        (2, 0, 10_000, "весь"),
        (3, 9_000, 10_000, "кінець"),
    ]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (0, 5_000),
        (0, CLIP_START),
        (CLIP_END, 21_000),
        (25_000, 26_000),
        (-5_000, CLIP_START),
        (12_000, 12_000),
        (15_000, 14_000),
    ],
)
def test_segments_fully_outside_clip_are_dropped(start: int, end: int) -> None:
    assert _build([SubtitleSegment(start, end, "поза кліпом")]) == ""


def test_segment_ending_exactly_at_clip_end_is_kept() -> None:
    assert _blocks(_build([SubtitleSegment(19_000, CLIP_END, "край")])) == [
        (1, 9_000, 10_000, "край"),
    ]


def test_no_segments_gives_empty_string() -> None:
    assert _build([]) == ""


@pytest.mark.parametrize("text", ["", "   ", "\n\t \r\n", "<b></b>", "<i> </i>", "\x00\x07"])
def test_empty_or_whitespace_text_is_skipped_without_consuming_a_number(text: str) -> None:
    blocks = _blocks(
        _build(
            [
                SubtitleSegment(11_000, 12_000, text),
                SubtitleSegment(13_000, 14_000, "ok"),
            ]
        )
    )
    assert blocks == [(1, 3_000, 4_000, "ok")]


def test_cues_are_numbered_from_one_in_time_order() -> None:
    blocks = _blocks(
        _build(
            [
                SubtitleSegment(18_000, 19_000, "третій"),
                SubtitleSegment(11_000, 12_000, "перший"),
                SubtitleSegment(14_000, 15_000, "другий"),
            ]
        )
    )
    assert [block[0] for block in blocks] == [1, 2, 3]
    assert [block[3] for block in blocks] == ["перший", "другий", "третій"]
    assert [block[1] for block in blocks] == [1_000, 4_000, 8_000]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<i>Привіт</i> світ", "Привіт світ"),
        ('<font color="red">Важливо</font>!', "Важливо!"),
        ("<script>alert(1)</script>текст", "alert(1)текст"),
        ("<<b>b>X", "X"),
        ("<scr\x00ipt>x</script>", "x"),
        ("<!-- коментар -->готово", "готово"),
        ("a < b и c > d", "a < b и c > d"),
        ("Hel\x00lo\x07 wor\x1bld", "Hello world"),
        ("рядок\nдругий\tрядок", "рядок другий рядок"),
    ],
)
def test_markup_and_control_characters_become_plain_text(raw: str, expected: str) -> None:
    blocks = _blocks(_build([SubtitleSegment(11_000, 12_000, raw)]))
    assert [block[3] for block in blocks] == [expected]


def test_cyrillic_text_survives_unchanged() -> None:
    text = "Ґанок, їжак і Щастя — Привіт, світе!"
    srt = _build([SubtitleSegment(11_000, 12_000, text)])
    assert srt == f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n\n"


@pytest.mark.parametrize(
    ("start", "end", "code"),
    [
        (5_000, 5_000, INTERVAL_END_NOT_AFTER_START),
        (5_000, 4_000, INTERVAL_END_NOT_AFTER_START),
        (0, 0, INTERVAL_END_NOT_AFTER_START),
        (-1, 1_000, INTERVAL_NEGATIVE),
        (-1_000, -500, INTERVAL_NEGATIVE),
    ],
)
def test_invalid_clip_interval_raises(start: int, end: int, code: str) -> None:
    with pytest.raises(IntervalError) as caught:
        build_clip_srt([SubtitleSegment(0, 1_000, "x")], start_ms=start, end_ms=end)
    assert caught.value.code == code
    assert caught.value.message


@pytest.mark.parametrize(
    ("start", "end"),
    [(True, 1_000), (0, 1_000.0), ("0", 1_000), (0, None)],
)
def test_non_integer_clip_bounds_raise(start: Any, end: Any) -> None:
    with pytest.raises(IntervalError) as caught:
        build_clip_srt([], start_ms=start, end_ms=end)
    assert caught.value.code == INTERVAL_NOT_INTEGER


def test_non_integer_segment_times_raise() -> None:
    bad: Any = 11.5
    with pytest.raises(IntervalError) as caught:
        _build([SubtitleSegment(bad, 12_000, "x")])
    assert caught.value.code == INTERVAL_NOT_INTEGER
    assert caught.value.field == "start_ms"


@given(
    clip_start=st.integers(min_value=0, max_value=10**7),
    clip_length=st.integers(min_value=1, max_value=10**6),
    raw_segments=st.lists(
        st.tuples(
            st.integers(min_value=-(10**6), max_value=2 * 10**7),
            st.integers(min_value=-(10**6), max_value=2 * 10**7),
            st.text(max_size=30),
        ),
        max_size=25,
    ),
)
def test_cues_stay_inside_clip_and_are_ordered(
    clip_start: int, clip_length: int, raw_segments: list[tuple[int, int, str]]
) -> None:
    srt = build_clip_srt(
        [SubtitleSegment(start, end, text) for start, end, text in raw_segments],
        start_ms=clip_start,
        end_ms=clip_start + clip_length,
    )
    blocks = _blocks(srt)
    assert [block[0] for block in blocks] == list(range(1, len(blocks) + 1))
    starts = [block[1] for block in blocks]
    assert starts == sorted(starts)
    for _, start, end, text in blocks:
        assert 0 <= start < end <= clip_length
        assert text
        assert text == " ".join(text.split())
        assert all(ord(char) >= 32 and not 127 <= ord(char) <= 159 for char in text)
