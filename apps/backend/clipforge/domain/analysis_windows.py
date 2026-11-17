"""Analysis units and window planning for candidate selection (ADR 041 §3).

A transcript is a sequence of units (segments or words). The planner covers the sequence with
consecutive *cores*. Each core is sent to the model together with neighbouring units on both
sides (*context*); only a unit inside a core may own a candidate that starts there.

Pure functions and immutable values only: standard library, no I/O.
"""

from __future__ import annotations

import bisect
import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

MIN_MAX_WINDOW_BYTES: Final = 32_768


@dataclass(frozen=True, slots=True)
class Unit:
    """A transcript segment or word. Times are source-relative integer milliseconds."""

    id: str
    ordinal: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class Window:
    """One model request. Positions are inclusive indexes into the unit sequence.

    The core is ``[core_first, core_last]`` and ``first <= core_first <= core_last <= last``.
    """

    index: int
    first: int
    core_first: int
    core_last: int
    last: int


def unit_json_bytes(unit: Unit) -> int:
    """Size of the unit in a request: its compact UTF-8 JSON plus one byte."""
    payload = {
        "id": unit.id,
        "ordinal": unit.ordinal,
        "start_ms": unit.start_ms,
        "end_ms": unit.end_ms,
        "text": unit.text,
    }
    compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return len(compact.encode("utf-8")) + 1


def plan_windows(
    units: Sequence[Unit],
    max_window_bytes: int,
    *,
    core_min_ms: int = 300_000,
    core_max_ms: int = 480_000,
    context_ms: int = 60_000,
    context_max_bytes: int | None = None,
) -> tuple[Window, ...]:
    """Cover ``units`` with consecutive cores and add context to each core.

    A core grows while its span stays within ``core_max_ms`` and its bytes stay within
    ``max_window_bytes - 2 * context_max_bytes``. A single unit always forms a core, even when
    it exceeds these limits by itself. Once a core spans at least ``core_min_ms``, it is cut
    after the unit whose following gap is largest among the allowed cut points; on equal gaps
    the later cut wins. A core that can reach the last unit is not cut.

    Context on each side holds the nearest units whose times lie within ``context_ms`` of the
    core and whose bytes fit ``context_max_bytes`` (default ``max_window_bytes // 8``).

    Raises ``ValueError`` when ``context_max_bytes`` is negative or ``2 * context_max_bytes`` is
    not below ``max_window_bytes``, because no core could then hold anything.
    """
    if max_window_bytes < MIN_MAX_WINDOW_BYTES:
        raise ValueError(f"max_window_bytes must be at least {MIN_MAX_WINDOW_BYTES}")
    context_limit = max_window_bytes // 8 if context_max_bytes is None else context_max_bytes
    if context_limit < 0 or 2 * context_limit >= max_window_bytes:
        raise ValueError(
            "context_max_bytes must be >= 0 and 2 * context_max_bytes < max_window_bytes"
        )
    if any(later.ordinal <= earlier.ordinal for earlier, later in itertools.pairwise(units)):
        raise ValueError("unit ordinals must be strictly increasing")
    if not units:
        return ()
    core_budget = max_window_bytes - 2 * context_limit
    sizes = tuple(unit_json_bytes(unit) for unit in units)
    cores: list[tuple[int, int]] = []
    start = 0
    while start < len(units):
        end = _core_end(
            units,
            sizes,
            start,
            core_min_ms=core_min_ms,
            core_max_ms=core_max_ms,
            core_budget=core_budget,
        )
        cores.append((start, end))
        start = end + 1
    return tuple(
        _window(index, core_first, core_last, units, sizes, context_ms, context_limit)
        for index, (core_first, core_last) in enumerate(cores)
    )


def owner_window(windows: Sequence[Window], position: int) -> int:
    """Index of the window whose core contains ``position``.

    ``windows`` must be a plan from :func:`plan_windows`: cores are sorted and contiguous.
    Raises ``ValueError`` when no core contains ``position``.
    """
    slot = bisect.bisect_right(windows, position, key=_core_first) - 1
    if slot < 0 or position > windows[slot].core_last:
        raise ValueError(f"position {position} is not inside any window core")
    return windows[slot].index


def _core_first(window: Window) -> int:
    return window.core_first


def _reach(
    units: Sequence[Unit], sizes: Sequence[int], start: int, *, core_max_ms: int, core_budget: int
) -> int:
    """Last position a core starting at ``start`` can include without breaking its limits."""
    origin = units[start].start_ms
    total = sizes[start]
    reach = start
    for position in range(start + 1, len(units)):
        total += sizes[position]
        if units[position].end_ms - origin > core_max_ms or total > core_budget:
            break
        reach = position
    return reach


def _core_end(
    units: Sequence[Unit],
    sizes: Sequence[int],
    start: int,
    *,
    core_min_ms: int,
    core_max_ms: int,
    core_budget: int,
) -> int:
    """Last position of the core that starts at ``start``."""
    reach = _reach(units, sizes, start, core_max_ms=core_max_ms, core_budget=core_budget)
    if reach == len(units) - 1:
        return reach
    origin = units[start].start_ms
    best: int | None = None
    best_gap = 0
    for position in range(start, reach + 1):
        if units[position].end_ms - origin < core_min_ms:
            continue
        gap = units[position + 1].start_ms - units[position].end_ms
        if best is None or gap >= best_gap:
            best = position
            best_gap = gap
    return reach if best is None else best


def _window(
    index: int,
    core_first: int,
    core_last: int,
    units: Sequence[Unit],
    sizes: Sequence[int],
    context_ms: int,
    context_limit: int,
) -> Window:
    core_start = units[core_first].start_ms
    core_end = units[core_last].end_ms
    first = core_first
    used = 0
    while first > 0:
        candidate = first - 1
        if (
            units[candidate].start_ms < core_start - context_ms
            or used + sizes[candidate] > context_limit
        ):
            break
        used += sizes[candidate]
        first = candidate
    last = core_last
    used = 0
    while last < len(units) - 1:
        candidate = last + 1
        if (
            units[candidate].end_ms > core_end + context_ms
            or used + sizes[candidate] > context_limit
        ):
            break
        used += sizes[candidate]
        last = candidate
    return Window(index=index, first=first, core_first=core_first, core_last=core_last, last=last)
