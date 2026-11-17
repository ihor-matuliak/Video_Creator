"""ADR 041 §6: rank, quality threshold and recommended interval (domain/selection_scoring.py)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.domain.selection_scoring import (
    LONGER_THAN_MAX,
    OUT_OF_SOURCE,
    SHORTER_THAN_MIN,
    Recommended,
    Rejected,
    compute_rank,
    passes_threshold,
    recommend_interval,
)

SCORE_KEYS = ("clarity", "hook", "completeness", "emotion", "novelty", "relevance")
MAX_DURATION_MS = 3_600_000


def _scores(*values: int) -> dict[str, int]:
    return dict(zip(SCORE_KEYS, values, strict=True))


@pytest.mark.parametrize(
    ("values", "rank"),
    [
        ((4, 3, 4, 3, 3, 4), 90),  # the fake provider's candidate
        ((4, 4, 4, 4, 4, 4), 100),
        ((0, 0, 0, 0, 0, 0), 0),
        ((0, 0, 0, 0, 1, 0), 3),  # 10 / 4 = 2.5 rounds up, not to even
        ((0, 0, 0, 0, 3, 0), 8),  # 30 / 4 = 7.5 rounds up
        ((0, 0, 1, 0, 0, 0), 6),  # 25 / 4 = 6.25 rounds down
    ],
)
def test_rank_from_component_scores(values: tuple[int, ...], rank: int) -> None:
    assert compute_rank(_scores(*values)) == rank


@pytest.mark.parametrize(
    ("rank", "completeness", "expected"),
    [
        (60, 2, True),
        (59, 2, False),
        (60, 1, False),
        (59, 1, False),
        (100, 0, False),
        (100, 4, True),
    ],
)
def test_threshold_boundaries(rank: int, completeness: int, expected: bool) -> None:
    assert passes_threshold(rank, completeness) is expected


@pytest.mark.parametrize(
    ("start", "end", "duration", "expected"),
    [
        (20_000, 30_000, 100_000, Recommended(17_000, 34_000)),  # full pre 3000 and post 4000
        (1_000, 5_000, 100_000, Recommended(0, 9_000)),  # pre clamped at the source start
        (0, 5_000, 100_000, Recommended(0, 9_000)),  # evidence starts at zero
        (50_000, 59_500, 60_000, Recommended(47_000, 60_000)),  # post clamped at the end
        (1_000, 10_000, 10_000, Recommended(0, 10_000)),  # evidence ends at duration
    ],
)
def test_padding_at_source_edges(
    start: int, end: int, duration: int, expected: Recommended | Rejected
) -> None:
    result = recommend_interval(start, end, duration_ms=duration, min_ms=1_000, max_ms=60_000)
    assert result == expected


@pytest.mark.parametrize(
    ("start", "end", "duration", "max_ms", "expected"),
    [
        # total 17000, excess 2000: cut_pre = 2000*3000//7000 = 857, cut_post = 1143
        (20_000, 30_000, 100_000, 15_000, Recommended(17_857, 32_857)),
        # total 17000, excess 4000: cut_pre = 4000*3000//7000 = 1714, cut_post = 2286
        (10_000, 20_000, 100_000, 13_000, Recommended(8_714, 21_714)),
        # post clamped to 1000 by the source end; excess 3000: cut_pre = 2250, cut_post = 750
        (20_000, 29_000, 30_000, 10_000, Recommended(19_250, 29_250)),
    ],
)
def test_proportional_cut_exact_values(
    start: int,
    end: int,
    duration: int,
    max_ms: int,
    expected: Recommended | Rejected,
) -> None:
    result = recommend_interval(start, end, duration_ms=duration, min_ms=1_000, max_ms=max_ms)
    assert result == expected
    assert isinstance(result, Recommended)
    assert result.end_ms - result.start_ms == max_ms


def test_evidence_exactly_max_has_no_padding() -> None:
    result = recommend_interval(20_000, 35_000, duration_ms=100_000, min_ms=1_000, max_ms=15_000)
    assert result == Recommended(20_000, 35_000)


def test_evidence_longer_than_max_is_rejected() -> None:
    result = recommend_interval(20_000, 35_001, duration_ms=100_000, min_ms=1_000, max_ms=15_000)
    assert result == Rejected(LONGER_THAN_MAX)


@pytest.mark.parametrize(
    ("start", "end", "duration"),
    [
        (-1, 1_000, 10_000),
        (1_000, 1_000, 10_000),
        (5_000, 4_000, 10_000),
        (1_000, 10_001, 10_000),
        (10_000, 10_000, 10_000),
    ],
)
def test_evidence_outside_source_is_rejected(start: int, end: int, duration: int) -> None:
    result = recommend_interval(start, end, duration_ms=duration, min_ms=1_000, max_ms=60_000)
    assert result == Rejected(OUT_OF_SOURCE)


def test_out_of_source_is_checked_before_longer_than_max() -> None:
    assert recommend_interval(
        -1, 40_000, duration_ms=100_000, min_ms=1_000, max_ms=15_000
    ) == Rejected(OUT_OF_SOURCE)
    assert recommend_interval(
        0, 50_000, duration_ms=10_000, min_ms=1_000, max_ms=1_000
    ) == Rejected(OUT_OF_SOURCE)


def test_recommendation_shorter_than_min_is_rejected() -> None:
    result = recommend_interval(0, 1_000, duration_ms=1_500, min_ms=5_000, max_ms=60_000)
    assert result == Rejected(SHORTER_THAN_MIN)


def test_recommendation_exactly_min_is_accepted() -> None:
    result = recommend_interval(0, 1_000, duration_ms=1_500, min_ms=1_500, max_ms=60_000)
    assert result == Recommended(0, 1_500)


@st.composite
def _cases(draw: st.DrawFn) -> tuple[int, int, int, int, int]:
    duration = draw(st.integers(min_value=1, max_value=MAX_DURATION_MS))
    evidence_start = draw(st.integers(min_value=0, max_value=duration - 1))
    evidence_end = draw(st.integers(min_value=evidence_start + 1, max_value=duration))
    max_ms = draw(st.integers(min_value=1, max_value=600_000))
    min_ms = draw(st.integers(min_value=0, max_value=max_ms))
    return duration, evidence_start, evidence_end, min_ms, max_ms


@given(_cases())
def test_recommendation_contains_evidence_inside_source_within_max(
    case: tuple[int, int, int, int, int],
) -> None:
    duration, evidence_start, evidence_end, min_ms, max_ms = case
    result = recommend_interval(
        evidence_start, evidence_end, duration_ms=duration, min_ms=min_ms, max_ms=max_ms
    )
    if isinstance(result, Recommended):
        assert 0 <= result.start_ms <= evidence_start < evidence_end <= result.end_ms
        assert result.end_ms <= duration
        assert result.end_ms - result.start_ms <= max_ms
        assert result.end_ms - result.start_ms >= min_ms
    else:
        assert result.code in {LONGER_THAN_MAX, SHORTER_THAN_MIN}


@given(_cases())
def test_without_minimum_fitting_evidence_is_always_recommended(
    case: tuple[int, int, int, int, int],
) -> None:
    duration, evidence_start, evidence_end, _, max_ms = case
    result = recommend_interval(
        evidence_start, evidence_end, duration_ms=duration, min_ms=0, max_ms=max_ms
    )
    if evidence_end - evidence_start > max_ms:
        assert result == Rejected(LONGER_THAN_MAX)
    else:
        assert isinstance(result, Recommended)
        assert result.start_ms <= evidence_start
        assert evidence_end <= result.end_ms
