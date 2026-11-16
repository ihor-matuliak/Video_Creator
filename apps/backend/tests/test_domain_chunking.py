"""T-016: STT chunk core/context planning, adaptive split, payload estimate (FR-015)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.domain.chunking import (
    CHUNK_BELOW_MIN_CORE,
    CHUNK_INVALID_PARAMETER,
    CHUNK_NOT_IN_PLAN,
    CHUNK_NOT_INTEGER,
    CHUNK_SUPERSEDED,
    CHUNK_UPLOAD_TOO_LONG,
    MAX_UPLOAD_MS,
    MIN_CORE_MS,
    PAYLOAD_BINARY_CEILING_BYTES,
    ChunkingError,
    ChunkPlan,
    ChunkWindow,
    estimate_payload,
    plan_chunks,
    split_chunk,
)

VECTORS_PATH = Path(__file__).resolve().parents[3] / "contracts" / "time-vectors.json"
CHUNKS: dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["chunks"]
CONTEXT = 5_000
ALL_CODES = {
    CHUNK_NOT_INTEGER,
    CHUNK_INVALID_PARAMETER,
    CHUNK_UPLOAD_TOO_LONG,
    CHUNK_BELOW_MIN_CORE,
    CHUNK_NOT_IN_PLAN,
    CHUNK_SUPERSEDED,
}


def _tuples(windows: tuple[ChunkWindow, ...]) -> list[tuple[int, int, int, int]]:
    return [(w.core_start_ms, w.core_end_ms, w.upload_start_ms, w.upload_end_ms) for w in windows]


def _assert_exact_cover(plan: ChunkPlan, context_ms: int = CONTEXT) -> None:
    """Active cores tile ``[0, D)``: no gaps, no overlap, context never counted."""
    duration = plan.duration_ms
    active = plan.active
    assert active
    assert active[0].core_start_ms == 0
    assert active[-1].core_end_ms == duration
    for left, right in itertools.pairwise(active):
        assert left.core_end_ms == right.core_start_ms
    assert sum(w.core_duration_ms for w in active) == duration
    for window in active:
        assert window.core_start_ms < window.core_end_ms
        assert window.upload_start_ms == max(0, window.core_start_ms - context_ms)
        assert window.upload_end_ms == min(duration, window.core_end_ms + context_ms)
        assert 0 <= window.upload_start_ms <= window.core_start_ms
        assert window.core_end_ms <= window.upload_end_ms <= duration
        assert window.upload_duration_ms <= MAX_UPLOAD_MS


def test_codes_are_distinct() -> None:
    assert len(ALL_CODES) == 6


def test_contract_vector_matches_expected_windows() -> None:
    plan = plan_chunks(
        CHUNKS["duration_ms"], core_ms=CHUNKS["core_ms"], context_ms=CHUNKS["context_ms"]
    )
    expected = [
        (c["core_start_ms"], c["core_end_ms"], c["upload_start_ms"], c["upload_end_ms"])
        for c in CHUNKS["expected"]
    ]
    assert _tuples(plan.active) == expected


def test_docs_example_620000() -> None:
    plan = plan_chunks(620_000)
    assert _tuples(plan.active) == [
        (0, 300_000, 0, 305_000),
        (300_000, 600_000, 295_000, 605_000),
        (600_000, 620_000, 595_000, 620_000),
    ]
    assert not any(entry.superseded for entry in plan.entries)
    _assert_exact_cover(plan)


def test_full_upload_window_is_310_seconds() -> None:
    plan = plan_chunks(900_000)
    middle = plan.active[1]
    assert middle.upload_duration_ms == MAX_UPLOAD_MS == 310_000
    assert middle.core_duration_ms == 300_000


@pytest.mark.parametrize("duration", [1, 999, 3_000, 4_999, 5_000, 5_001, 10_000])
def test_short_source_is_single_chunk_without_context(duration: int) -> None:
    plan = plan_chunks(duration)
    assert _tuples(plan.active) == [(0, duration, 0, duration)]
    _assert_exact_cover(plan)


@pytest.mark.parametrize(
    ("duration", "cores"),
    [
        (299_999, 1),
        (300_000, 1),
        (300_001, 2),
        (600_000, 2),
        (600_001, 3),
    ],
)
def test_core_boundaries(duration: int, cores: int) -> None:
    plan = plan_chunks(duration)
    assert len(plan.active) == cores
    assert plan.active[-1].core_end_ms == duration
    _assert_exact_cover(plan)


def test_split_recomputes_context_and_supersedes_parent() -> None:
    plan = plan_chunks(620_000)
    parent = plan.active[1]
    split = plan.split(parent)
    assert [(e.window, e.superseded) for e in split.entries if e.window == parent] == [
        (parent, True)
    ]
    assert _tuples(split.active) == [
        (0, 300_000, 0, 305_000),
        (300_000, 450_000, 295_000, 455_000),
        (450_000, 600_000, 445_000, 605_000),
        (600_000, 620_000, 595_000, 620_000),
    ]
    assert len(split.entries) == len(plan.entries) + 2
    assert sum(1 for e in split.entries if e.superseded) == 1
    _assert_exact_cover(split)
    # the original plan is immutable
    assert _tuples(plan.active)[1] == (300_000, 600_000, 295_000, 605_000)


def test_split_at_source_edges_clips_context() -> None:
    plan = plan_chunks(620_000)
    first = plan.split(plan.active[0])
    assert _tuples(first.active)[:2] == [
        (0, 150_000, 0, 155_000),
        (150_000, 300_000, 145_000, 305_000),
    ]
    last = plan.split(plan.active[2])
    assert _tuples(last.active)[2:] == [
        (600_000, 610_000, 595_000, 615_000),
        (610_000, 620_000, 605_000, 620_000),
    ]


def test_split_odd_core_halves_cover_parent_exactly() -> None:
    parent = plan_chunks(30_001).active[0]
    first, second = split_chunk(parent, 30_001)
    assert (first.core_start_ms, first.core_end_ms) == (0, 15_000)
    assert (second.core_start_ms, second.core_end_ms) == (15_000, 30_001)


def test_split_chunk_standalone_matches_plan_split() -> None:
    plan = plan_chunks(620_000)
    parent = plan.active[1]
    assert split_chunk(parent, 620_000) == plan.split(parent).active[1:3]


def test_split_twice_nests_in_place() -> None:
    plan = plan_chunks(620_000)
    once = plan.split(plan.active[1])
    twice = once.split(once.active[1])
    assert _tuples(twice.active)[1:4] == [
        (300_000, 375_000, 295_000, 380_000),
        (375_000, 450_000, 370_000, 455_000),
        (450_000, 600_000, 445_000, 605_000),
    ]
    assert sum(1 for e in twice.entries if e.superseded) == 2
    _assert_exact_cover(twice)


def test_split_down_to_minimum_core_guard() -> None:
    plan = plan_chunks(620_000)
    while True:
        splittable = [w for w in plan.active if w.core_duration_ms // 2 >= MIN_CORE_MS]
        if not splittable:
            break
        plan = plan.split(max(splittable, key=lambda w: w.core_duration_ms))
        _assert_exact_cover(plan)
    assert all(MIN_CORE_MS <= w.core_duration_ms < 2 * MIN_CORE_MS for w in plan.active)
    assert sum(w.core_duration_ms for w in plan.active) == 620_000
    for window in plan.active:
        with pytest.raises(ChunkingError) as caught:
            plan.split(window)
        assert caught.value.code == CHUNK_BELOW_MIN_CORE
        assert caught.value.message


@pytest.mark.parametrize(
    ("core", "minimum", "allowed"),
    [
        (9_999, 5_000, False),
        (10_000, 5_000, True),
        (10_001, 5_000, True),
        (1, 1, False),
        (2, 1, True),
        (3, 1, True),
    ],
)
def test_minimum_core_guard_boundary(core: int, minimum: int, allowed: bool) -> None:
    window = plan_chunks(core).active[0]
    if allowed:
        first, second = split_chunk(window, core, min_core_ms=minimum)
        assert min(first.core_duration_ms, second.core_duration_ms) >= minimum
    else:
        with pytest.raises(ChunkingError) as caught:
            split_chunk(window, core, min_core_ms=minimum)
        assert caught.value.code == CHUNK_BELOW_MIN_CORE


def test_short_source_cannot_be_split() -> None:
    plan = plan_chunks(3_000)
    with pytest.raises(ChunkingError) as caught:
        plan.split(plan.active[0])
    assert caught.value.code == CHUNK_BELOW_MIN_CORE


def test_split_rejects_unknown_and_superseded_window() -> None:
    plan = plan_chunks(620_000)
    parent = plan.active[1]
    unknown = ChunkWindow(1, 2, 0, 3)
    with pytest.raises(ChunkingError) as not_in_plan:
        plan.split(unknown)
    assert not_in_plan.value.code == CHUNK_NOT_IN_PLAN
    with pytest.raises(ChunkingError) as superseded:
        plan.split(parent).split(parent)
    assert superseded.value.code == CHUNK_SUPERSEDED
    assert superseded.value.message


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"duration_ms": 0}, CHUNK_INVALID_PARAMETER),
        ({"duration_ms": -1}, CHUNK_INVALID_PARAMETER),
        ({"duration_ms": 1000.0}, CHUNK_NOT_INTEGER),
        ({"duration_ms": True}, CHUNK_NOT_INTEGER),
        ({"duration_ms": None}, CHUNK_NOT_INTEGER),
        ({"duration_ms": float("nan")}, CHUNK_NOT_INTEGER),
        ({"duration_ms": 1000, "core_ms": 0}, CHUNK_INVALID_PARAMETER),
        ({"duration_ms": 1000, "core_ms": "300"}, CHUNK_NOT_INTEGER),
        ({"duration_ms": 1000, "context_ms": -1}, CHUNK_INVALID_PARAMETER),
        ({"duration_ms": 1000, "context_ms": 2.5}, CHUNK_NOT_INTEGER),
        ({"duration_ms": 1000, "core_ms": 300_001}, CHUNK_UPLOAD_TOO_LONG),
        ({"duration_ms": 1000, "core_ms": 300_000, "context_ms": 5_001}, CHUNK_UPLOAD_TOO_LONG),
    ],
)
def test_plan_chunks_rejects_bad_parameters(kwargs: dict[str, object], code: str) -> None:
    with pytest.raises(ChunkingError) as caught:
        plan_chunks(**kwargs)
    assert caught.value.code == code
    assert caught.value.code in ALL_CODES
    assert caught.value.message


def test_smaller_core_and_context_are_allowed() -> None:
    plan = plan_chunks(25_000, core_ms=10_000, context_ms=2_000)
    assert _tuples(plan.active) == [
        (0, 10_000, 0, 12_000),
        (10_000, 20_000, 8_000, 22_000),
        (20_000, 25_000, 18_000, 25_000),
    ]
    _assert_exact_cover(plan, context_ms=2_000)


@given(st.integers(min_value=1, max_value=10**8))
def test_cores_cover_source_exactly(duration: int) -> None:
    plan = plan_chunks(duration)
    _assert_exact_cover(plan)
    assert len(plan.active) == -(-duration // 300_000)
    assert all(w.core_duration_ms == 300_000 for w in plan.active[:-1])
    assert 0 < plan.active[-1].core_duration_ms <= 300_000


@given(
    st.integers(min_value=1, max_value=2_000_000),
    st.integers(min_value=1, max_value=20_000),
    st.lists(st.integers(min_value=0, max_value=10**6), max_size=40),
)
def test_cores_cover_source_exactly_after_arbitrary_splits(
    duration: int, minimum: int, picks: list[int]
) -> None:
    plan = plan_chunks(duration)
    for pick in picks:
        parent = plan.active[pick % len(plan.active)]
        if parent.core_duration_ms // 2 < minimum:
            with pytest.raises(ChunkingError) as caught:
                plan.split(parent, min_core_ms=minimum)
            assert caught.value.code == CHUNK_BELOW_MIN_CORE
            continue
        after = plan.split(parent, min_core_ms=minimum)
        index = next(i for i, e in enumerate(after.entries) if e.window == parent)
        assert after.entries[index].superseded
        first_entry, second_entry = after.entries[index + 1], after.entries[index + 2]
        assert not first_entry.superseded
        assert not second_entry.superseded
        first, second = first_entry.window, second_entry.window
        assert first.core_start_ms == parent.core_start_ms
        assert first.core_end_ms == second.core_start_ms
        assert second.core_end_ms == parent.core_end_ms
        assert min(first.core_duration_ms, second.core_duration_ms) >= minimum
        assert len(after.entries) == len(plan.entries) + 2
        plan = after
        _assert_exact_cover(plan)
    _assert_exact_cover(plan)


def test_estimate_payload_default_profile_for_full_window() -> None:
    estimate = estimate_payload(MAX_UPLOAD_MS)
    assert estimate.audio_bytes == 44 + 310 * 16_000 * 2
    assert estimate.request_bytes == 4 * ((estimate.audio_bytes + 2) // 3) + 16 * 1024
    assert estimate.binary_ceiling_bytes == PAYLOAD_BINARY_CEILING_BYTES == 20 * 1024 * 1024
    assert estimate.request_ceiling_bytes > estimate.binary_ceiling_bytes
    assert estimate.fits


def test_estimate_payload_rounds_pcm_bytes_up() -> None:
    # 1 ms of mono 16 kHz 16-bit audio is exactly 32 bytes; a rate of 16001 needs 33.
    assert estimate_payload(1).audio_bytes == 44 + 32
    assert estimate_payload(1, sample_rate_hz=16_001).audio_bytes == 44 + 33


def test_estimate_payload_binary_ceiling_boundary() -> None:
    # (20 MiB - 44-byte header) / 32 bytes per ms = 655358.6 ms.
    assert estimate_payload(655_358).fits
    over = estimate_payload(655_359)
    assert over.audio_bytes > over.binary_ceiling_bytes
    assert not over.fits


def test_estimate_payload_request_ceiling_includes_base64_and_json_margin() -> None:
    small_overhead = estimate_payload(655_358)
    assert small_overhead.request_bytes > small_overhead.binary_ceiling_bytes
    assert small_overhead.fits
    huge_overhead = estimate_payload(MAX_UPLOAD_MS, json_overhead_bytes=20 * 1024 * 1024)
    assert huge_overhead.audio_bytes <= huge_overhead.binary_ceiling_bytes
    assert huge_overhead.request_bytes > huge_overhead.request_ceiling_bytes
    assert not huge_overhead.fits


def test_estimate_payload_is_monotonic() -> None:
    sizes = [estimate_payload(ms).audio_bytes for ms in (1, 10, 1_000, 5_000, 310_000)]
    assert sizes == sorted(sizes)
    assert len(set(sizes)) == len(sizes)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"upload_ms": 0}, CHUNK_INVALID_PARAMETER),
        ({"upload_ms": 1.5}, CHUNK_NOT_INTEGER),
        ({"upload_ms": 1000, "binary_ceiling_bytes": 0}, CHUNK_INVALID_PARAMETER),
        ({"upload_ms": 1000, "sample_rate_hz": 0}, CHUNK_INVALID_PARAMETER),
        ({"upload_ms": 1000, "channels": "2"}, CHUNK_NOT_INTEGER),
        ({"upload_ms": 1000, "bytes_per_sample": False}, CHUNK_NOT_INTEGER),
        ({"upload_ms": 1000, "json_overhead_bytes": -1}, CHUNK_INVALID_PARAMETER),
    ],
)
def test_estimate_payload_rejects_bad_parameters(kwargs: dict[str, object], code: str) -> None:
    with pytest.raises(ChunkingError) as caught:
        estimate_payload(**kwargs)
    assert caught.value.code == code
    assert caught.value.message


def test_adaptive_split_until_payload_fits() -> None:
    # A heavier profile (48 kHz stereo) does not fit 310 s; halving the core shrinks the request.
    def fits(window: ChunkWindow) -> bool:
        return estimate_payload(window.upload_duration_ms, sample_rate_hz=48_000, channels=2).fits

    plan = plan_chunks(620_000)
    assert not fits(plan.active[1])
    while True:
        oversized = [w for w in plan.active if not fits(w)]
        if not oversized:
            break
        plan = plan.split(oversized[0])
    assert all(fits(w) for w in plan.active)
    assert sum(1 for e in plan.entries if e.superseded) >= 1
    _assert_exact_cover(plan)
