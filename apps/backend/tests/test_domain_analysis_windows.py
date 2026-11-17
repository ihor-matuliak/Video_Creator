"""ADR 041 §3: analysis units, window planning and owner lookup (pure stdlib)."""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clipforge.domain.analysis_windows import (
    MIN_MAX_WINDOW_BYTES,
    Unit,
    Window,
    owner_window,
    plan_windows,
    unit_json_bytes,
)

TEXT = st.text(alphabet=st.characters(exclude_categories=["Cs"]), max_size=24)


def timeline(parts: Sequence[tuple[int, int]], *, text: str = "t") -> list[Unit]:
    """Units from ``(duration_ms, gap_before_ms)`` pairs; ordinals 1..n, ids u1..un."""
    units: list[Unit] = []
    cursor = 0
    for number, (duration, gap) in enumerate(parts, start=1):
        start = cursor + gap
        end = start + duration
        units.append(Unit(id=f"u{number}", ordinal=number, start_ms=start, end_ms=end, text=text))
        cursor = end
    return units


def core_bounds(windows: Sequence[Window]) -> list[tuple[int, int]]:
    return [(window.core_first, window.core_last) for window in windows]


def reach_of(
    units: Sequence[Unit], sizes: Sequence[int], start: int, *, core_max_ms: int, budget: int
) -> int:
    """Independent statement of the growth rule: last position a core from ``start`` may hold."""
    origin = units[start].start_ms
    total = sizes[start]
    reach = start
    for position in range(start + 1, len(units)):
        if units[position].end_ms - origin > core_max_ms or total + sizes[position] > budget:
            break
        total += sizes[position]
        reach = position
    return reach


def check_plan(
    units: Sequence[Unit],
    windows: Sequence[Window],
    *,
    max_window_bytes: int,
    core_min_ms: int,
    core_max_ms: int,
    context_ms: int,
    context_limit: int,
) -> None:
    count = len(units)
    if count == 0:
        assert windows == ()
        return
    sizes = [unit_json_bytes(unit) for unit in units]
    budget = max_window_bytes - 2 * context_limit
    assert [window.index for window in windows] == list(range(len(windows)))

    # Coverage: cores are non-empty, consecutive, and cover every unit exactly once.
    cursor = 0
    for window in windows:
        assert window.core_first == cursor
        assert window.core_first <= window.core_last
        assert 0 <= window.first <= window.core_first
        assert window.core_last <= window.last < count
        cursor = window.core_last + 1
    assert cursor == count

    for window in windows:
        core_start = units[window.core_first].start_ms
        core_end = units[window.core_last].end_ms
        if window.core_last > window.core_first:
            # Only a single unit may exceed the limits by itself.
            assert core_end - core_start <= core_max_ms
            assert sum(sizes[window.core_first : window.core_last + 1]) <= budget
        left = range(window.first, window.core_first)
        right = range(window.core_last + 1, window.last + 1)
        left_bytes = sum(sizes[p] for p in left)
        right_bytes = sum(sizes[p] for p in right)
        assert left_bytes <= context_limit
        assert right_bytes <= context_limit
        assert all(units[p].start_ms >= core_start - context_ms for p in left)
        assert all(units[p].end_ms <= core_end + context_ms for p in right)
        # Nearest-first and maximal: the first unit outside the context does not fit.
        if window.first > 0:
            outside = window.first - 1
            assert (
                units[outside].start_ms < core_start - context_ms
                or left_bytes + sizes[outside] > context_limit
            )
        if window.last < count - 1:
            outside = window.last + 1
            assert (
                units[outside].end_ms > core_end + context_ms
                or right_bytes + sizes[outside] > context_limit
            )

    # Cuts: a non-final core ends after the largest allowed gap, ties going to the later cut.
    for window in windows[:-1]:
        reach = reach_of(units, sizes, window.core_first, core_max_ms=core_max_ms, budget=budget)
        assert reach < count - 1
        origin = units[window.core_first].start_ms
        allowed = [
            p
            for p in range(window.core_first, reach + 1)
            if units[p].end_ms - origin >= core_min_ms
        ]
        if allowed:
            best = max(allowed, key=lambda p: (units[p + 1].start_ms - units[p].end_ms, p))
            assert window.core_last == best
        else:
            assert window.core_last == reach
    last = windows[-1]
    final_reach = reach_of(units, sizes, last.core_first, core_max_ms=core_max_ms, budget=budget)
    assert final_reach == count - 1

    # Ownership: every unit belongs to exactly the window whose core contains it.
    for position in range(count):
        owners = [w.index for w in windows if w.core_first <= position <= w.core_last]
        assert owners == [owner_window(windows, position)]


def run_and_check(
    units: Sequence[Unit],
    max_window_bytes: int,
    *,
    core_min_ms: int,
    core_max_ms: int,
    context_ms: int,
    context_max_bytes: int | None,
) -> None:
    windows = plan_windows(
        units,
        max_window_bytes,
        core_min_ms=core_min_ms,
        core_max_ms=core_max_ms,
        context_ms=context_ms,
        context_max_bytes=context_max_bytes,
    )
    again = plan_windows(
        units,
        max_window_bytes,
        core_min_ms=core_min_ms,
        core_max_ms=core_max_ms,
        context_ms=context_ms,
        context_max_bytes=context_max_bytes,
    )
    assert windows == again
    context_limit = max_window_bytes // 8 if context_max_bytes is None else context_max_bytes
    check_plan(
        units,
        windows,
        max_window_bytes=max_window_bytes,
        core_min_ms=core_min_ms,
        core_max_ms=core_max_ms,
        context_ms=context_ms,
        context_limit=context_limit,
    )


@st.composite
def unit_lists(draw: st.DrawFn, *, monotone: bool) -> list[Unit]:
    ordinals = sorted(draw(st.sets(st.integers(0, 10_000), max_size=40)))
    cursor = draw(st.integers(0, 5_000))
    units: list[Unit] = []
    for ordinal in ordinals:
        if monotone:
            start = cursor + draw(st.one_of(st.just(0), st.integers(1, 120_000)))
            end = start + draw(st.integers(1, 90_000))
            cursor = end
        else:
            start = draw(st.integers(-1_000, 600_000))
            end = draw(st.integers(-1_000, 600_000))
        text = draw(TEXT) + "ж" * draw(st.integers(0, 3_000))
        units.append(Unit(id=f"u{ordinal}", ordinal=ordinal, start_ms=start, end_ms=end, text=text))
    return units


@st.composite
def budgets(draw: st.DrawFn) -> tuple[int, int | None]:
    """A window budget and an optional context cap that the planner accepts (2 * cap < budget)."""
    max_window_bytes = draw(st.integers(MIN_MAX_WINDOW_BYTES, 200_000))
    context_max_bytes = draw(st.none() | st.integers(0, (max_window_bytes - 1) // 2))
    return max_window_bytes, context_max_bytes


@settings(max_examples=200, deadline=None)
@given(
    units=unit_lists(monotone=True),
    budget=budgets(),
    core_min_ms=st.integers(0, 120_000),
    core_extra_ms=st.integers(0, 360_000),
    context_ms=st.integers(0, 120_000),
)
def test_random_monotone_transcripts_hold_invariants(
    units: list[Unit],
    budget: tuple[int, int | None],
    core_min_ms: int,
    core_extra_ms: int,
    context_ms: int,
) -> None:
    max_window_bytes, context_max_bytes = budget
    run_and_check(
        units,
        max_window_bytes,
        core_min_ms=core_min_ms,
        core_max_ms=core_min_ms + core_extra_ms,
        context_ms=context_ms,
        context_max_bytes=context_max_bytes,
    )


@settings(max_examples=200, deadline=None)
@given(
    units=unit_lists(monotone=False),
    budget=budgets(),
    core_min_ms=st.integers(0, 480_000),
    core_max_ms=st.integers(0, 480_000),
    context_ms=st.integers(0, 120_000),
)
def test_arbitrary_times_still_cover_and_respect_limits(
    units: list[Unit],
    budget: tuple[int, int | None],
    core_min_ms: int,
    core_max_ms: int,
    context_ms: int,
) -> None:
    max_window_bytes, context_max_bytes = budget
    run_and_check(
        units,
        max_window_bytes,
        core_min_ms=core_min_ms,
        core_max_ms=core_max_ms,
        context_ms=context_ms,
        context_max_bytes=context_max_bytes,
    )


def test_unit_json_bytes_is_compact_utf8_json_plus_one() -> None:
    unit = Unit(id="a", ordinal=1, start_ms=0, end_ms=5, text="пр")
    expected = '{"id":"a","ordinal":1,"start_ms":0,"end_ms":5,"text":"пр"}'
    assert unit_json_bytes(unit) == len(expected.encode("utf-8")) + 1


def test_units_and_windows_are_frozen() -> None:
    unit = Unit(id="u", ordinal=1, start_ms=0, end_ms=1, text="a")
    window = Window(index=0, first=0, core_first=0, core_last=0, last=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        unit.text = "b"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        window.last = 1  # type: ignore[misc]


def test_empty_units_have_no_windows() -> None:
    assert plan_windows([], MIN_MAX_WINDOW_BYTES) == ()


def test_budget_below_minimum_is_rejected() -> None:
    units = timeline([(1_000, 0)])
    with pytest.raises(ValueError, match="max_window_bytes"):
        plan_windows(units, MIN_MAX_WINDOW_BYTES - 1)
    with pytest.raises(ValueError, match="max_window_bytes"):
        plan_windows([], MIN_MAX_WINDOW_BYTES - 1)
    assert len(plan_windows(units, MIN_MAX_WINDOW_BYTES)) == 1


@pytest.mark.parametrize("context_max_bytes", [-1, MIN_MAX_WINDOW_BYTES // 2, MIN_MAX_WINDOW_BYTES])
def test_context_cap_that_leaves_no_core_room_is_rejected(context_max_bytes: int) -> None:
    units = timeline([(1_000, 0)])
    with pytest.raises(ValueError, match="context_max_bytes"):
        plan_windows(units, MIN_MAX_WINDOW_BYTES, context_max_bytes=context_max_bytes)
    with pytest.raises(ValueError, match="context_max_bytes"):
        plan_windows([], MIN_MAX_WINDOW_BYTES, context_max_bytes=context_max_bytes)


def test_largest_valid_context_cap_still_gives_each_unit_its_own_core() -> None:
    units = timeline([(1_000, 0)] * 3)
    cap = (MIN_MAX_WINDOW_BYTES - 1) // 2
    windows = plan_windows(units, MIN_MAX_WINDOW_BYTES, core_min_ms=0, context_max_bytes=cap)
    assert core_bounds(windows) == [(0, 0), (1, 1), (2, 2)]


@pytest.mark.parametrize("ordinals", [[1, 1], [2, 1], [1, 3, 2]])
def test_non_increasing_ordinals_are_rejected(ordinals: list[int]) -> None:
    units = [Unit(id=f"u{n}", ordinal=n, start_ms=0, end_ms=1, text="t") for n in ordinals]
    with pytest.raises(ValueError, match="strictly increasing"):
        plan_windows(units, MIN_MAX_WINDOW_BYTES)


def test_remainder_that_fits_is_one_core() -> None:
    windows = plan_windows(timeline([(1_000, 0)] * 10), MIN_MAX_WINDOW_BYTES)
    assert core_bounds(windows) == [(0, 9)]


def test_cut_is_placed_at_the_largest_allowed_gap() -> None:
    gaps = [0, 0, 0, 0, 2_000, 0, 0, 5_000, 0, 0, 0, 0]
    units = timeline([(4_000, gap) for gap in gaps])
    windows = plan_windows(
        units,
        1_000_000,
        core_min_ms=10_000,
        core_max_ms=40_000,
        context_ms=0,
        context_max_bytes=0,
    )
    assert core_bounds(windows) == [(0, 6), (7, 11)]


def test_equal_gaps_cut_at_the_later_position() -> None:
    gaps = [0, 0, 0, 0, 5_000, 0, 0, 5_000, 0, 0, 0, 0]
    units = timeline([(4_000, gap) for gap in gaps])
    windows = plan_windows(
        units,
        1_000_000,
        core_min_ms=10_000,
        core_max_ms=40_000,
        context_ms=0,
        context_max_bytes=0,
    )
    assert core_bounds(windows) == [(0, 6), (7, 11)]


def test_gap_before_core_min_is_not_a_cut_point() -> None:
    gaps = [0, 0, 9_000, 0, 0, 0, 0, 0]
    units = timeline([(1_000, gap) for gap in gaps])
    windows = plan_windows(
        units,
        1_000_000,
        core_min_ms=5_000,
        core_max_ms=13_500,
        context_ms=0,
        context_max_bytes=0,
    )
    assert core_bounds(windows) == [(0, 3), (4, 7)]


def test_core_bytes_stay_within_budget() -> None:
    units = timeline([(1_000, 0)] * 6, text="a" * 8_300)
    size = unit_json_bytes(units[0])
    budget = MIN_MAX_WINDOW_BYTES - 2 * (MIN_MAX_WINDOW_BYTES // 8)
    assert 2 * size <= budget < 3 * size
    windows = plan_windows(
        units,
        MIN_MAX_WINDOW_BYTES,
        core_min_ms=0,
        core_max_ms=10**9,
        context_ms=0,
    )
    assert core_bounds(windows) == [(0, 1), (2, 3), (4, 5)]


def test_oversized_unit_forms_its_own_core() -> None:
    units = [
        Unit(id="a", ordinal=1, start_ms=0, end_ms=1_000, text="x"),
        Unit(id="b", ordinal=2, start_ms=1_000, end_ms=2_000, text="y" * 40_000),
        Unit(id="c", ordinal=3, start_ms=2_000, end_ms=3_000, text="z"),
    ]
    assert unit_json_bytes(units[1]) > MIN_MAX_WINDOW_BYTES
    windows = plan_windows(units, MIN_MAX_WINDOW_BYTES)
    assert core_bounds(windows) == [(0, 0), (1, 1), (2, 2)]


def test_context_follows_time_and_nearest_first() -> None:
    units = timeline([(1_000, 0)] * 10)
    windows = plan_windows(
        units,
        1_000_000,
        core_min_ms=0,
        core_max_ms=4_000,
        context_ms=2_000,
        context_max_bytes=100_000,
    )
    assert [(w.first, w.core_first, w.core_last, w.last) for w in windows] == [
        (0, 0, 3, 5),
        (2, 4, 7, 9),
        (6, 8, 9, 9),
    ]


def test_context_is_capped_per_side_in_bytes() -> None:
    units = timeline([(1_000, 0)] * 10)
    # Units 1-8 have equal JSON size (same digit counts); unit 0 is shorter ("start_ms":0),
    # and unit 9 is longer. A cap of one such unit admits exactly one neighbour per side.
    cap = unit_json_bytes(units[1])
    assert unit_json_bytes(units[0]) < cap
    windows = plan_windows(
        units,
        1_000_000,
        core_min_ms=0,
        core_max_ms=4_000,
        context_ms=2_000,
        context_max_bytes=cap,
    )
    assert [(w.first, w.core_first, w.core_last, w.last) for w in windows] == [
        (0, 0, 3, 4),
        (3, 4, 7, 8),
        (7, 8, 9, 9),
    ]


def test_owner_window_follows_cores_not_context() -> None:
    units = timeline([(1_000, 0)] * 10)
    windows = plan_windows(
        units,
        1_000_000,
        core_min_ms=0,
        core_max_ms=4_000,
        context_ms=2_000,
        context_max_bytes=100_000,
    )
    owners = [owner_window(windows, position) for position in range(10)]
    assert owners == [0, 0, 0, 0, 1, 1, 1, 1, 2, 2]
    assert [window.index for window in windows] == [0, 1, 2]


def test_owner_window_rejects_uncovered_positions() -> None:
    units = timeline([(1_000, 0)] * 3)
    windows = plan_windows(units, MIN_MAX_WINDOW_BYTES)
    with pytest.raises(ValueError, match="not inside any window core"):
        owner_window(windows, 3)
    with pytest.raises(ValueError, match="not inside any window core"):
        owner_window(windows, -1)
    with pytest.raises(ValueError, match="not inside any window core"):
        owner_window((), 0)


def test_planning_is_deterministic() -> None:
    units = timeline([(4_000, 0), (7_000, 3_000), (2_000, 0), (9_000, 40_000)] * 5)
    first = plan_windows(units, MIN_MAX_WINDOW_BYTES, core_min_ms=10_000, core_max_ms=30_000)
    second = plan_windows(units, MIN_MAX_WINDOW_BYTES, core_min_ms=10_000, core_max_ms=30_000)
    assert first == second
    assert all(left.core_last + 1 == right.core_first for left, right in itertools.pairwise(first))
