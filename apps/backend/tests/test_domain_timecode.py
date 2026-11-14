"""T-001: timecode parse/format (contracts/time-vectors.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.domain.timecode import (
    INVALID_TIMECODE,
    Separator,
    TimecodeError,
    format_timecode,
    parse_timecode,
)

VECTORS_PATH = Path(__file__).resolve().parents[3] / "contracts" / "time-vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
SEPARATORS: tuple[Separator, ...] = (".", ",")
TIMECODES: list[tuple[str, int]] = [(text, ms) for text, ms in VECTORS["timecodes"]]


def test_vectors_file_has_timecodes() -> None:
    assert VECTORS["schema_version"] == 1
    assert len(TIMECODES) >= 4


@pytest.mark.parametrize(("text", "ms"), TIMECODES)
def test_parse_contract_vector(text: str, ms: int) -> None:
    assert parse_timecode(text) == ms


@pytest.mark.parametrize(("text", "ms"), TIMECODES)
def test_parse_contract_vector_with_comma(text: str, ms: int) -> None:
    assert parse_timecode(text.replace(".", ",")) == ms


@pytest.mark.parametrize(("text", "ms"), TIMECODES)
def test_format_contract_vector(text: str, ms: int) -> None:
    assert format_timecode(ms) == text
    assert format_timecode(ms, separator=",") == text.replace(".", ",")


def test_t001_six_hour_vector() -> None:
    assert parse_timecode("05:59:59.125") == 21_599_125
    assert format_timecode(21_599_125) == "05:59:59.125"


def test_hours_above_99() -> None:
    assert parse_timecode("100:00:00.000") == 360_000_000
    assert format_timecode(360_000_001) == "100:00:00.001"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0:00:00.000",
        "00:00:00",
        "00:00:00.00",
        "00:00:00.0000",
        "00:60:00.000",
        "00:00:60.000",
        "00:0:00.000",
        "-00:00:01.000",
        " 00:00:00.000",
        "00:00:00.000 ",
        "00:00:00.000\n",
        "00:00:00:000",
        "00-00-00.000",
        "00:00:00.abc",
        "００:00:00.000",  # full-width digits
        "٠٠:00:00.000",  # Arabic-Indic digits
        "nan",
    ],
)
def test_parse_rejects_malformed(text: str) -> None:
    with pytest.raises(TimecodeError) as caught:
        parse_timecode(text)
    assert caught.value.code == INVALID_TIMECODE
    assert caught.value.message


@pytest.mark.parametrize("value", [None, 0, 1.5, b"00:00:00.000", ["00:00:00.000"]])
def test_parse_rejects_non_string(value: object) -> None:
    with pytest.raises(TimecodeError):
        parse_timecode(value)


@pytest.mark.parametrize("value", [True, False, 1.0, float("nan"), "1000", None])
def test_format_rejects_non_int(value: object) -> None:
    with pytest.raises(TypeError):
        format_timecode(value)  # type: ignore[arg-type]


def test_format_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        format_timecode(-1)


def test_format_rejects_unknown_separator() -> None:
    with pytest.raises(ValueError, match="separator"):
        format_timecode(0, separator=":")  # type: ignore[arg-type]


@given(st.integers(min_value=0, max_value=10**12), st.sampled_from(SEPARATORS))
def test_format_then_parse_round_trip(ms: int, separator: Separator) -> None:
    assert parse_timecode(format_timecode(ms, separator=separator)) == ms


@given(
    st.integers(min_value=0, max_value=9_999),
    st.integers(min_value=0, max_value=59),
    st.integers(min_value=0, max_value=59),
    st.integers(min_value=0, max_value=999),
    st.sampled_from(SEPARATORS),
)
def test_parse_then_format_round_trip(
    hours: int, minutes: int, seconds: int, millis: int, separator: Separator
) -> None:
    text = f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"
    ms = parse_timecode(text)
    assert ms == ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis
    assert format_timecode(ms, separator=separator) == text
