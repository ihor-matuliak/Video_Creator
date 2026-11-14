"""T-002/T-004: interval validation (contracts/time-vectors.json)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.domain.interval import (
    INTERVAL_END_AFTER_DURATION,
    INTERVAL_END_NOT_AFTER_START,
    INTERVAL_INVALID_DURATION,
    INTERVAL_NEGATIVE,
    INTERVAL_NOT_FINITE,
    INTERVAL_NOT_INTEGER,
    Interval,
    IntervalError,
    validate_interval,
)

VECTORS_PATH = Path(__file__).resolve().parents[3] / "contracts" / "time-vectors.json"
INTERVALS: list[dict[str, Any]] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["intervals"]
ALL_CODES = {
    INTERVAL_NOT_INTEGER,
    INTERVAL_NOT_FINITE,
    INTERVAL_NEGATIVE,
    INTERVAL_INVALID_DURATION,
    INTERVAL_END_NOT_AFTER_START,
    INTERVAL_END_AFTER_DURATION,
}


def _vector_id(vector: dict[str, Any]) -> str:
    return f"{vector['start_ms']}-{vector['end_ms']}-of-{vector['duration_ms']}"


def test_codes_are_distinct() -> None:
    assert len(ALL_CODES) == 6


def test_vectors_cover_valid_and_invalid() -> None:
    assert {vector["valid"] for vector in INTERVALS} == {True, False}


@pytest.mark.parametrize("vector", INTERVALS, ids=_vector_id)
def test_contract_vector(vector: dict[str, Any]) -> None:
    start, end, duration = vector["start_ms"], vector["end_ms"], vector["duration_ms"]
    if vector["valid"]:
        interval = validate_interval(start, end, duration)
        assert interval == Interval(start_ms=start, end_ms=end)
        assert interval.duration_ms == end - start
    else:
        with pytest.raises(IntervalError) as caught:
            validate_interval(start, end, duration)
        assert caught.value.code in ALL_CODES
        assert caught.value.message


@pytest.mark.parametrize(
    ("start", "end", "duration", "code", "field"),
    [
        (-1, 1000, 12000, INTERVAL_NEGATIVE, "start_ms"),
        (2000, 2000, 12000, INTERVAL_END_NOT_AFTER_START, "end_ms"),
        (5000, 4000, 12000, INTERVAL_END_NOT_AFTER_START, "end_ms"),
        (1000, 12001, 12000, INTERVAL_END_AFTER_DURATION, "end_ms"),
        (12000, 12001, 12000, INTERVAL_END_AFTER_DURATION, "end_ms"),
        (0, -5, 12000, INTERVAL_NEGATIVE, "end_ms"),
        (0, 1000, 0, INTERVAL_INVALID_DURATION, "duration_ms"),
        (0, 1000, -1, INTERVAL_INVALID_DURATION, "duration_ms"),
    ],
)
def test_specific_rejection_codes(
    start: int, end: int, duration: int, code: str, field: str
) -> None:
    with pytest.raises(IntervalError) as caught:
        validate_interval(start, end, duration)
    assert caught.value.code == code
    assert caught.value.field == field


def test_end_equal_to_duration_is_allowed() -> None:
    assert validate_interval(11_000, 12_000, 12_000) == Interval(11_000, 12_000)


def test_contract_invalid_vectors_map_to_expected_codes() -> None:
    expected = {
        (-1, 1000): INTERVAL_NEGATIVE,
        (2000, 2000): INTERVAL_END_NOT_AFTER_START,
        (1000, 12001): INTERVAL_END_AFTER_DURATION,
    }
    seen: dict[tuple[int, int], str] = {}
    for vector in INTERVALS:
        if vector["valid"]:
            continue
        with pytest.raises(IntervalError) as caught:
            validate_interval(vector["start_ms"], vector["end_ms"], vector["duration_ms"])
        seen[(vector["start_ms"], vector["end_ms"])] = caught.value.code
    assert seen == expected


@pytest.mark.parametrize("field_index", [0, 1, 2])
@pytest.mark.parametrize(
    ("bad", "code"),
    [
        (True, INTERVAL_NOT_INTEGER),
        (False, INTERVAL_NOT_INTEGER),
        (1000.0, INTERVAL_NOT_INTEGER),
        (1000.5, INTERVAL_NOT_INTEGER),
        ("1000", INTERVAL_NOT_INTEGER),
        (None, INTERVAL_NOT_INTEGER),
        (float("nan"), INTERVAL_NOT_FINITE),
        (float("inf"), INTERVAL_NOT_FINITE),
        (float("-inf"), INTERVAL_NOT_FINITE),
    ],
)
def test_rejects_non_integer_values(field_index: int, bad: object, code: str) -> None:
    values: list[object] = [1000, 2000, 12000]
    values[field_index] = bad
    with pytest.raises(IntervalError) as caught:
        validate_interval(*values)
    assert caught.value.code == code
    assert caught.value.field == ("start_ms", "end_ms", "duration_ms")[field_index]


@given(
    st.integers(min_value=-(10**7), max_value=10**8),
    st.integers(min_value=-(10**7), max_value=10**8),
    st.integers(min_value=1, max_value=10**8),
)
def test_valid_iff_half_open_inside_source(start: int, end: int, duration: int) -> None:
    if 0 <= start < end <= duration:
        assert validate_interval(start, end, duration).duration_ms == end - start
    else:
        with pytest.raises(IntervalError) as caught:
            validate_interval(start, end, duration)
        assert caught.value.code in ALL_CODES
