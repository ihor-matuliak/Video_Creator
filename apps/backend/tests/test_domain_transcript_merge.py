"""T-017..T-022: merging per-chunk STT results into one source-time transcript (FR-015)."""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clipforge.domain.chunking import ChunkWindow, plan_chunks
from clipforge.domain.transcript_merge import (
    DIAGNOSTIC_SEGMENT_FALLBACK,
    DIAGNOSTIC_TIMING_CLAMPED,
    DIAGNOSTIC_TIMING_ZERO_LENGTH,
    MAX_SEGMENT_MS,
    MERGE_CORES_NOT_TILING,
    MERGE_INVALID_DURATION,
    MERGE_INVALID_PROVIDER_TEXT,
    MERGE_INVALID_PROVIDER_TIMING,
    MERGE_RESULT_INCONSISTENT,
    MERGE_SEGMENT_TOO_LONG,
    MERGE_WINDOW_INVALID,
    PAUSE_BREAK_MS,
    UNDETERMINED_LANGUAGE,
    ChunkItem,
    ChunkResult,
    ChunkState,
    MergedTranscript,
    MergeError,
    SpeakerKey,
    check_chunk,
    merge_transcript,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SCHEMA: dict[str, Any] = json.loads((CONTRACTS / "transcript.schema.json").read_text("utf-8"))
EXAMPLE: dict[str, Any] = json.loads((CONTRACTS / "transcript.example.json").read_text("utf-8"))
# Live OpenRouter responses (2026-11-15) for the same 47.125 s sample, kept byte for byte.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "openrouter"

# 620 s source: cores 0-300000, 300000-600000, 600000-620000;
# uploads 0-305000, 295000-605000, 595000-620000 (docs/05 section 4).
DURATION = 620_000
A, B, C = plan_chunks(DURATION).active


def _item(
    window: ChunkWindow, start_ms: int, end_ms: int, text: str, speaker: str | None = None
) -> ChunkItem:
    """An item given in source ms, expressed the way a provider would: local seconds."""
    base = window.upload_start_ms
    return ChunkItem((start_ms - base) / 1000, (end_ms - base) / 1000, text, speaker)


def _chunk(
    window: ChunkWindow,
    words: Sequence[ChunkItem] = (),
    segments: Sequence[ChunkItem] = (),
    *,
    state: ChunkState = "succeeded",
    word_timing: bool | None = None,
    language: str | None = None,
) -> ChunkResult:
    return ChunkResult(
        window=window,
        state=state,
        words=tuple(words),
        segments=tuple(segments),
        has_word_timing=bool(words) if word_timing is None else word_timing,
        language=language,
    )


def _run(
    a: Sequence[ChunkItem] = (), b: Sequence[ChunkItem] = (), c: Sequence[ChunkItem] = ()
) -> MergedTranscript:
    return merge_transcript(DURATION, [_chunk(A, a), _chunk(B, b), _chunk(C, c)])


def _spans(items: Sequence[Any]) -> list[tuple[int, int, str]]:
    return [(item.start_ms, item.end_ms, item.text) for item in items]


def _error_code(duration: object, results: Sequence[ChunkResult]) -> str:
    with pytest.raises(MergeError) as caught:
        merge_transcript(duration, results)
    assert caught.value.message
    return caught.value.code


def _validate(value: Any, schema: dict[str, Any]) -> None:
    """Minimal check of the JSON subset used by contracts/transcript.schema.json."""
    if "const" in schema:
        assert value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict)
        assert set(schema["required"]) <= set(value)
        if schema.get("additionalProperties") is False:
            assert set(value) <= set(schema["properties"])
        for key, sub in schema["properties"].items():
            if key in value:
                _validate(value[key], sub)
    elif kind == "array":
        assert isinstance(value, list)
        for element in value:
            _validate(element, schema["items"])
    elif kind == "string":
        assert isinstance(value, str)
        assert schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf)
    elif kind == "integer":
        assert isinstance(value, int)
        assert not isinstance(value, bool)
        assert value >= schema.get("minimum", -math.inf)


def test_contract_example_is_reproduced_and_matches_schema() -> None:
    window = ChunkWindow(0, 12_000, 0, 12_000)
    words = (
        ChunkItem(1.0, 2.0, "Спробую"),
        ChunkItem(2.0, 3.0, "перестрибнути."),
        ChunkItem(5.0, 6.0, "Не"),
        ChunkItem(6.0, 7.0, "вийшло!"),
    )
    segments = (
        ChunkItem(1.0, 3.0, "Спробую перестрибнути."),
        ChunkItem(5.0, 7.0, "Не вийшло!"),
    )
    merged = merge_transcript(12_000, [_chunk(window, words, segments, language="uk")])
    document = merged.to_schema_dict()
    assert document == EXAMPLE
    _validate(document, SCHEMA)
    assert merged.diagnostics == ()


# --- T-017: offset added exactly once -------------------------------------------------


def test_t017_offset_added_once() -> None:
    merged = _run(
        a=[ChunkItem(1.0, 1.5, "раз")],
        b=[ChunkItem(10.0, 10.5, "два")],
        c=[ChunkItem(8.0, 8.4, "три")],
    )
    assert _spans(merged.words) == [
        (1_000, 1_500, "раз"),  # upload_start 0
        (305_000, 305_500, "два"),  # upload_start 295000 (not core_start 300000)
        (603_000, 603_400, "три"),  # upload_start 595000
    ]


@pytest.mark.parametrize(
    ("seconds", "expected_ms"),
    [(1.2344, 1_234), (1.2346, 1_235), (2.0, 2_000), (0.9996, 1_000)],
)
def test_t017_milliseconds_are_rounded_not_truncated(seconds: float, expected_ms: int) -> None:
    merged = _run(b=[ChunkItem(seconds + 10.0, seconds + 11.0, "слово")])
    assert merged.words[0].start_ms == 295_000 + 10_000 + expected_ms
    assert merged.words[0].end_ms == 295_000 + 11_000 + expected_ms


def test_t017_offset_uses_recomputed_context_after_adaptive_split() -> None:
    plan = plan_chunks(DURATION).split(B)
    a, b1, b2, c = plan.active
    assert (b2.core_start_ms, b2.upload_start_ms) == (450_000, 445_000)
    merged = merge_transcript(
        DURATION,
        [
            _chunk(a),
            _chunk(b1),
            _chunk(b2, [ChunkItem(10.0, 10.5, "друга половина")]),
            _chunk(c),
        ],
    )
    assert _spans(merged.words) == [(455_000, 455_500, "друга половина")]


# --- T-018: one occurrence at a seam --------------------------------------------------


def test_t018_duplicates_in_overlapping_uploads_become_one_occurrence() -> None:
    a = [
        _item(A, 298_000, 298_500, "раз"),
        _item(A, 299_600, 300_200, "привіт"),
        _item(A, 301_000, 301_500, "два"),
    ]
    b = [
        _item(B, 298_050, 298_550, "раз"),
        _item(B, 299_650, 300_250, "привіт"),
        _item(B, 301_000, 301_500, "два"),
    ]
    merged = _run(a=a, b=b)
    assert _spans(merged.words) == [
        (298_000, 298_500, "раз"),
        (299_600, 300_200, "привіт"),
        (301_000, 301_500, "два"),
    ]
    assert [word.id for word in merged.words] == ["w1", "w2", "w3"]


def test_t018_word_claimed_by_both_chunks_by_midpoint_is_kept_once() -> None:
    # A puts the midpoint before the seam (A's core), B after it (B's core).
    merged = _run(
        a=[_item(A, 299_700, 300_100, "слово")],
        b=[_item(B, 299_900, 300_300, "слово")],
    )
    assert _spans(merged.words) == [(299_700, 300_100, "слово")]


def test_t018_word_on_the_seam_is_kept_once_when_chunks_disagree_on_side() -> None:
    # A puts the midpoint after the seam, B before it: neither owns it by midpoint.
    merged = _run(
        a=[_item(A, 299_900, 300_500, "слово")],
        b=[_item(B, 299_500, 300_000, "слово")],
    )
    assert [word.text for word in merged.words] == ["слово"]


def test_t018_midpoint_exactly_on_the_seam_belongs_to_the_next_chunk() -> None:
    merged = _run(
        a=[_item(A, 299_800, 300_200, "тут")],
        b=[_item(B, 299_800, 300_200, "тут")],
    )
    assert _spans(merged.words) == [(299_800, 300_200, "тут")]


def test_t018_case_and_punctuation_do_not_hide_a_duplicate() -> None:
    merged = _run(
        a=[_item(A, 299_000, 299_400, "Привіт,")],
        b=[_item(B, 299_000, 299_400, "привіт")],
    )
    assert [word.text for word in merged.words] == ["Привіт,"]


def test_t018_context_only_words_are_dropped_by_midpoint_ownership() -> None:
    merged = _run(
        a=[_item(A, 302_000, 302_500, "тло")],  # midpoint in B's core
        b=[_item(B, 297_000, 297_500, "ехо")],  # midpoint in A's core
    )
    assert merged.words == ()


def test_t018_duplicate_segment_on_the_seam_is_one_segment() -> None:
    text = "так воно і було"
    merged = merge_transcript(
        DURATION,
        [
            _chunk(A, segments=[_item(A, 298_000, 301_000, text)], word_timing=False),
            _chunk(B, segments=[_item(B, 298_000, 301_000, text)], word_timing=False),
            _chunk(C),
        ],
    )
    assert _spans(merged.segments) == [(298_000, 301_000, text)]
    assert [segment.id for segment in merged.segments] == ["s1"]


# --- T-019: genuine repeats survive ---------------------------------------------------


def test_t019_two_ni_at_1s_and_10s_in_one_chunk_are_both_kept() -> None:
    merged = _run(a=[ChunkItem(1.0, 1.3, "ні"), ChunkItem(10.0, 10.3, "ні")])
    assert _spans(merged.words) == [(1_000, 1_300, "ні"), (10_000, 10_300, "ні")]


def test_t019_identical_words_in_two_chunks_far_from_the_seam_are_both_kept() -> None:
    merged = _run(
        a=[_item(A, 100_000, 100_300, "ні")],
        b=[_item(B, 450_000, 450_300, "ні")],
    )
    assert _spans(merged.words) == [(100_000, 100_300, "ні"), (450_000, 450_300, "ні")]


def test_t019_same_word_six_seconds_apart_inside_the_shared_audio_is_kept_twice() -> None:
    both = (_item(A, 297_000, 297_300, "ні"), _item(A, 303_000, 303_300, "ні"))
    other = (_item(B, 297_000, 297_300, "ні"), _item(B, 303_000, 303_300, "ні"))
    merged = _run(a=both, b=other)
    assert _spans(merged.words) == [(297_000, 297_300, "ні"), (303_000, 303_300, "ні")]


def test_t019_quick_real_repeat_reported_by_both_chunks_stays_two_words() -> None:
    first = (_item(A, 299_000, 299_200, "ні"), _item(A, 299_300, 299_500, "ні"))
    second = (_item(B, 299_000, 299_200, "ні"), _item(B, 299_300, 299_500, "ні"))
    merged = _run(a=first, b=second)
    assert _spans(merged.words) == [(299_000, 299_200, "ні"), (299_300, 299_500, "ні")]


def test_t019_quick_repeat_seen_twice_by_one_chunk_and_once_by_the_other_stays_two() -> None:
    merged = _run(
        a=[_item(A, 299_000, 299_200, "ні"), _item(A, 299_300, 299_500, "ні")],
        b=[_item(B, 299_300, 299_500, "ні")],
    )
    assert _spans(merged.words) == [(299_000, 299_200, "ні"), (299_300, 299_500, "ні")]


def test_t019_touching_words_of_chunks_without_shared_audio_are_not_merged() -> None:
    a, b, c = plan_chunks(DURATION, context_ms=0).active
    merged = merge_transcript(
        DURATION,
        [
            _chunk(a, [_item(a, 299_800, 300_000, "ні")]),
            _chunk(b, [_item(b, 300_000, 300_200, "ні")]),
            _chunk(c),
        ],
    )
    assert _spans(merged.words) == [(299_800, 300_000, "ні"), (300_000, 300_200, "ні")]


# --- T-020: chunk-local speakers ------------------------------------------------------


def test_t020_same_provider_label_in_two_chunks_stays_distinct() -> None:
    merged = _run(
        a=[_item(A, 1_000, 1_500, "раз", "SPEAKER_00")],
        b=[_item(B, 400_000, 400_500, "два", "SPEAKER_00")],
    )
    first, second = merged.words
    assert first.speaker == SpeakerKey(0, "SPEAKER_00")
    assert second.speaker == SpeakerKey(300_000, "SPEAKER_00")
    assert first.speaker != second.speaker


def test_t020_missing_speaker_stays_none_and_is_not_in_the_schema_document() -> None:
    merged = _run(
        a=[_item(A, 1_000, 1_500, "раз")],
        b=[_item(B, 400_000, 400_500, "два", "SPEAKER_01")],
    )
    assert merged.words[0].speaker is None
    document = merged.to_schema_dict()
    _validate(document, SCHEMA)
    assert all("speaker" not in word for word in document["words"])  # type: ignore[attr-defined]


def test_t020_merged_seam_duplicate_keeps_the_key_of_its_own_chunk() -> None:
    merged = _run(
        a=[_item(A, 299_000, 299_400, "ось", "S")],
        b=[_item(B, 299_000, 299_400, "ось", "S")],
    )
    assert len(merged.words) == 1
    assert merged.words[0].speaker == SpeakerKey(0, "S")


# --- T-021: silence and empty success -------------------------------------------------


def test_t021_silence_is_succeeded_coverage_with_zero_words() -> None:
    merged = _run()
    assert merged.words == ()
    assert merged.segments == ()
    assert merged.diagnostics == ()
    assert [(e.start_ms, e.end_ms, e.state) for e in merged.coverage] == [
        (0, 300_000, "succeeded"),
        (300_000, 600_000, "succeeded"),
        (600_000, 620_000, "succeeded"),
    ]
    _validate(merged.to_schema_dict(), SCHEMA)


def test_t021_silent_chunk_does_not_downgrade_the_timing_level() -> None:
    merged = merge_transcript(
        DURATION,
        [
            _chunk(A, [_item(A, 1_000, 1_500, "раз")]),
            _chunk(B, word_timing=False),
            _chunk(C),
        ],
    )
    assert merged.timing_level == "word"
    assert merged.diagnostics == ()


def test_t021_empty_failed_chunk_is_not_silence() -> None:
    merged = merge_transcript(DURATION, [_chunk(A), _chunk(B, state="failed"), _chunk(C)])
    assert merged.words == ()
    assert [entry.state for entry in merged.coverage] == ["succeeded", "failed", "succeeded"]


# --- T-022: missing word timestamps ---------------------------------------------------


def test_t022_chunk_without_word_timing_gives_flagged_segment_level_transcript() -> None:
    merged = merge_transcript(
        DURATION,
        [
            _chunk(A, segments=[ChunkItem(1.0, 3.0, "Це речення.")]),
            _chunk(B),
            _chunk(C),
        ],
    )
    assert merged.timing_level == "segment"
    assert merged.words == ()  # never synthesised from segments
    assert _spans(merged.segments) == [(1_000, 3_000, "Це речення.")]
    assert [(d.code, d.chunk_core_start_ms) for d in merged.diagnostics] == [
        (DIAGNOSTIC_SEGMENT_FALLBACK, 0)
    ]
    document = merged.to_schema_dict()
    assert document["timing_level"] == "segment"
    _validate(document, SCHEMA)


def test_t022_one_chunk_without_word_timing_turns_the_whole_transcript_to_segment_level() -> None:
    merged = merge_transcript(
        DURATION,
        [
            _chunk(A, [ChunkItem(1.0, 1.4, "слово")], [ChunkItem(1.0, 1.4, "слово")]),
            _chunk(B, segments=[ChunkItem(10.0, 12.0, "інша частина")]),
            _chunk(C),
        ],
    )
    assert merged.timing_level == "segment"
    assert merged.words == ()
    assert [segment.text for segment in merged.segments] == ["слово", "інша частина"]
    assert [d.chunk_core_start_ms for d in merged.diagnostics] == [300_000]


def test_t022_word_level_when_every_speech_chunk_has_word_timing() -> None:
    merged = _run(
        a=[ChunkItem(1.0, 1.4, "раз")],
        b=[ChunkItem(10.0, 10.4, "два")],
    )
    assert merged.timing_level == "word"
    assert [d.code for d in merged.diagnostics] == []


# --- coverage of non-succeeded chunks -------------------------------------------------


@pytest.mark.parametrize("state", ["failed", "pending", "unknown_outcome"])
def test_non_succeeded_chunk_is_visible_in_coverage_and_does_not_hide_others(
    state: ChunkState,
) -> None:
    merged = merge_transcript(
        DURATION,
        [
            _chunk(A, [ChunkItem(1.0, 1.4, "раз")]),
            _chunk(B, state=state),
            _chunk(C, [ChunkItem(8.0, 8.4, "три")]),
        ],
    )
    assert [(e.start_ms, e.end_ms, e.state) for e in merged.coverage] == [
        (0, 300_000, "succeeded"),
        (300_000, 600_000, state),
        (600_000, 620_000, "succeeded"),
    ]
    assert [word.text for word in merged.words] == ["раз", "три"]
    assert merged.to_schema_dict()["coverage"] == [
        {"start_ms": 0, "end_ms": 300_000, "state": "succeeded"},
        {"start_ms": 300_000, "end_ms": 600_000, "state": state},
        {"start_ms": 600_000, "end_ms": 620_000, "state": "succeeded"},
    ]


def test_coverage_counts_cores_only_and_input_order_does_not_matter() -> None:
    forward = [_chunk(A), _chunk(B), _chunk(C)]
    merged = merge_transcript(DURATION, list(reversed(forward)))
    assert merged == merge_transcript(DURATION, forward)
    assert sum(entry.end_ms - entry.start_ms for entry in merged.coverage) == DURATION


def test_adaptive_split_children_cover_the_parent_exactly() -> None:
    plan = plan_chunks(DURATION).split(B)
    merged = merge_transcript(DURATION, [_chunk(window) for window in plan.active])
    assert [(e.start_ms, e.end_ms) for e in merged.coverage] == [
        (0, 300_000),
        (300_000, 450_000),
        (450_000, 600_000),
        (600_000, 620_000),
    ]


@pytest.mark.parametrize("state", ["failed", "pending", "unknown_outcome"])
def test_non_succeeded_chunk_with_words_is_rejected(state: ChunkState) -> None:
    bad = ChunkResult(B, state, words=(ChunkItem(1.0, 2.0, "x"),), has_word_timing=True)
    assert _error_code(DURATION, [_chunk(A), bad, _chunk(C)]) == MERGE_RESULT_INCONSISTENT


# --- bounds: clamp small overflow, reject large ---------------------------------------


def test_small_overflow_is_clamped_with_a_diagnostic() -> None:
    merged = _run(c=[ChunkItem(24.0, 25.05, "кінець")])  # 50 ms past the end of the source
    assert _spans(merged.words) == [(619_000, 620_000, "кінець")]
    assert [(d.code, d.chunk_core_start_ms) for d in merged.diagnostics] == [
        (DIAGNOSTIC_TIMING_CLAMPED, 600_000)
    ]


def test_negative_start_within_tolerance_is_clamped_to_zero() -> None:
    merged = _run(a=[ChunkItem(-0.1, 0.5, "початок")])  # exactly 100 ms before 0
    assert _spans(merged.words) == [(0, 500, "початок")]
    assert [d.code for d in merged.diagnostics] == [DIAGNOSTIC_TIMING_CLAMPED]


# --- zero-length provider items ---------------------------------------------------------


def test_zero_length_word_gets_one_millisecond_and_a_diagnostic() -> None:
    merged = _run(b=[ChunkItem(10.0, 10.0, "in")])
    assert _spans(merged.words) == [(305_000, 305_001, "in")]
    assert [(d.code, d.chunk_core_start_ms) for d in merged.diagnostics] == [
        (DIAGNOSTIC_TIMING_ZERO_LENGTH, 300_000)
    ]


def test_zero_length_word_at_the_very_end_of_the_audio_stays_inside_it() -> None:
    merged = _run(c=[ChunkItem(25.0, 25.0, "кінець")])  # exactly at the end of the source
    assert _spans(merged.words) == [(619_999, 620_000, "кінець")]
    assert [d.code for d in merged.diagnostics] == [DIAGNOSTIC_TIMING_ZERO_LENGTH]


def test_zero_length_word_slightly_past_the_end_is_clamped_and_flagged_twice() -> None:
    merged = _run(c=[ChunkItem(25.05, 25.05, "кінець")])
    assert _spans(merged.words) == [(619_999, 620_000, "кінець")]
    assert [d.code for d in merged.diagnostics] == [
        DIAGNOSTIC_TIMING_CLAMPED,
        DIAGNOSTIC_TIMING_ZERO_LENGTH,
    ]


@pytest.mark.parametrize("seconds", [0.0, -0.05])
def test_zero_length_word_at_the_start_of_the_audio(seconds: float) -> None:
    merged = _run(a=[ChunkItem(seconds, seconds, "початок")])
    assert _spans(merged.words) == [(0, 1, "початок")]
    assert DIAGNOSTIC_TIMING_ZERO_LENGTH in [d.code for d in merged.diagnostics]


def test_zero_length_segment_is_handled_like_a_word() -> None:
    merged = merge_transcript(
        DURATION,
        [
            _chunk(A, segments=[ChunkItem(2.0, 2.0, "пауза")], word_timing=False),
            _chunk(B),
            _chunk(C),
        ],
    )
    assert _spans(merged.segments) == [(2_000, 2_001, "пауза")]
    assert DIAGNOSTIC_TIMING_ZERO_LENGTH in [d.code for d in merged.diagnostics]


@pytest.mark.parametrize(
    ("start_s", "end_s"),
    [(1.0001, 1.0004), (1.0002, 1.0003), (0.9996, 1.0004), (1.0004, 1.0004)],
)
def test_item_that_rounds_to_one_millisecond_is_a_zero_length_word(
    start_s: float, end_s: float
) -> None:
    # Zero length is decided after rounding to ms, not by comparing the seconds.
    merged = _run(a=[ChunkItem(start_s, end_s, "ні")])
    assert _spans(merged.words) == [(1_000, 1_001, "ні")]
    assert [(d.code, d.chunk_core_start_ms) for d in merged.diagnostics] == [
        (DIAGNOSTIC_TIMING_ZERO_LENGTH, 0)
    ]


def test_item_that_rounds_to_one_millisecond_is_a_zero_length_segment() -> None:
    merged = merge_transcript(
        DURATION,
        [_chunk(A, segments=[ChunkItem(1.0001, 1.0004, "пауза")]), _chunk(B), _chunk(C)],
    )
    assert _spans(merged.segments) == [(1_000, 1_001, "пауза")]
    assert DIAGNOSTIC_TIMING_ZERO_LENGTH in [d.code for d in merged.diagnostics]


def test_item_of_one_rounded_millisecond_is_not_a_zero_length_item() -> None:
    merged = _run(a=[ChunkItem(1.0004, 1.0006, "ні")])  # 1000 ms and 1001 ms after rounding
    assert _spans(merged.words) == [(1_000, 1_001, "ні")]
    assert merged.diagnostics == ()


@pytest.mark.parametrize(
    ("window_name", "item"),
    [
        ("A", ChunkItem(-0.101, 0.5, "x")),
        ("C", ChunkItem(24.0, 25.101, "x")),
        ("C", ChunkItem(24.0, 700.0, "x")),
        ("B", ChunkItem(-50.0, -49.0, "x")),  # inside the source, far outside this upload
        ("A", ChunkItem(2.0, 1.0, "x")),
        ("A", ChunkItem(1.0004, 1.0001, "x")),  # start > end, even if both round to one ms
        ("B", ChunkItem(-50.0, -50.0, "x")),  # a point, but far outside the uploaded audio
        ("C", ChunkItem(700.0, 700.0, "x")),
        ("A", ChunkItem(math.nan, 1.0, "x")),
        ("A", ChunkItem(1.0, math.inf, "x")),
        ("A", ChunkItem(1.0, 1e308, "x")),
    ],
)
def test_large_or_malformed_provider_timing_is_invalid(window_name: str, item: ChunkItem) -> None:
    windows = {"A": A, "B": B, "C": C}
    chunks = [_chunk(w, [item] if name == window_name else []) for name, w in windows.items()]
    with pytest.raises(MergeError) as caught:
        merge_transcript(DURATION, chunks)
    assert caught.value.code == MERGE_INVALID_PROVIDER_TIMING
    assert caught.value.chunk_core_start_ms == windows[window_name].core_start_ms


@pytest.mark.parametrize("text", ["", "   ", "x" * 2_001])
def test_empty_or_oversized_text_is_invalid(text: str) -> None:
    assert (
        _error_code(DURATION, [_chunk(A, [ChunkItem(1.0, 2.0, text)]), _chunk(B), _chunk(C)])
        == MERGE_INVALID_PROVIDER_TEXT
    )


def test_text_is_stripped_but_otherwise_preserved() -> None:
    merged = _run(a=[ChunkItem(1.0, 2.0, "  Вийшло! ")])
    assert merged.words[0].text == "Вийшло!"


# --- structure ------------------------------------------------------------------------


@pytest.mark.parametrize("duration", [0, -1, 1.5, "620000", True, None])
def test_invalid_duration(duration: object) -> None:
    assert _error_code(duration, [_chunk(A)]) == MERGE_INVALID_DURATION


def test_cores_must_tile_the_source() -> None:
    assert _error_code(DURATION, []) == MERGE_CORES_NOT_TILING
    assert _error_code(DURATION, [_chunk(A), _chunk(C)]) == MERGE_CORES_NOT_TILING  # gap
    assert _error_code(DURATION, [_chunk(A), _chunk(B)]) == MERGE_CORES_NOT_TILING  # short end
    assert _error_code(DURATION, [_chunk(B), _chunk(C)]) == MERGE_CORES_NOT_TILING  # no start
    assert _error_code(DURATION, [_chunk(A), _chunk(A), _chunk(B), _chunk(C)]) == (
        MERGE_CORES_NOT_TILING
    )


def test_superseded_parent_together_with_children_is_rejected() -> None:
    first, second = plan_chunks(DURATION).split(B).active[1:3]
    assert (
        _error_code(DURATION, [_chunk(A), _chunk(B), _chunk(first), _chunk(second), _chunk(C)])
        == MERGE_CORES_NOT_TILING
    )


@pytest.mark.parametrize(
    "window",
    [
        ChunkWindow(0, 300_000, 0, 700_000),  # upload past the source
        ChunkWindow(0, 300_000, 100, 305_000),  # core starts before its own upload
        ChunkWindow(0, 0, 0, 5_000),  # empty core
        ChunkWindow(0, 300_000, 0, 299_999),  # core ends after its own upload
    ],
)
def test_malformed_window_is_rejected(window: ChunkWindow) -> None:
    assert _error_code(DURATION, [_chunk(window), _chunk(B), _chunk(C)]) == MERGE_WINDOW_INVALID


def test_words_without_word_timing_flag_are_rejected() -> None:
    bad = ChunkResult(A, "succeeded", words=(ChunkItem(1.0, 2.0, "x"),), has_word_timing=False)
    assert _error_code(DURATION, [bad, _chunk(B), _chunk(C)]) == MERGE_RESULT_INCONSISTENT


def test_declared_word_timing_without_words_but_with_segments_is_rejected() -> None:
    bad = ChunkResult(A, "succeeded", segments=(ChunkItem(1.0, 2.0, "x"),), has_word_timing=True)
    assert _error_code(DURATION, [bad, _chunk(B), _chunk(C)]) == MERGE_RESULT_INCONSISTENT


def test_unknown_state_is_rejected() -> None:
    bad = ChunkResult(A, "done")  # type: ignore[arg-type]
    assert _error_code(DURATION, [bad, _chunk(B), _chunk(C)]) == MERGE_RESULT_INCONSISTENT


# --- language, ids, determinism -------------------------------------------------------


def test_language_is_the_most_common_reported_one() -> None:
    merged = merge_transcript(
        DURATION,
        [_chunk(A, language="uk"), _chunk(B, language="en"), _chunk(C, language="uk")],
    )
    assert merged.language == "uk"
    tie = merge_transcript(
        DURATION, [_chunk(A, language="en"), _chunk(B, language="uk"), _chunk(C)]
    )
    assert tie.language == "en"


def test_language_defaults_to_undetermined_and_ignores_failed_chunks() -> None:
    assert _run().language == UNDETERMINED_LANGUAGE
    merged = merge_transcript(
        DURATION, [_chunk(A), _chunk(B, state="failed", language="uk"), _chunk(C, language="x")]
    )
    assert merged.language == UNDETERMINED_LANGUAGE


def test_ids_and_ordinals_follow_start_order_across_chunks() -> None:
    merged = _run(
        a=[ChunkItem(5.0, 5.5, "б"), ChunkItem(1.0, 1.5, "а")],  # provider order is not trusted
        b=[ChunkItem(10.0, 10.5, "в")],
        c=[ChunkItem(8.0, 8.5, "г")],
    )
    assert [(w.id, w.ordinal, w.text) for w in merged.words] == [
        ("w1", 0, "а"),
        ("w2", 1, "б"),
        ("w3", 2, "в"),
        ("w4", 3, "г"),
    ]


def test_confidence_is_carried_and_never_invented() -> None:
    merged = _run(
        a=[ChunkItem(1.0, 1.5, "а", confidence=0.7), ChunkItem(2.0, 2.5, "б")],
    )
    assert [word.confidence for word in merged.words] == [0.7, None]


# --- word level: segments are built from the merged words -------------------------------


def _seq(
    window: ChunkWindow,
    texts: Sequence[str],
    start_ms: int = 1_000,
    *,
    step_ms: int = 300,
    length_ms: int = 200,
    speaker: str | None = None,
) -> list[ChunkItem]:
    return [
        _item(
            window,
            start_ms + n * step_ms,
            start_ms + n * step_ms + length_ms,
            text,
            speaker,
        )
        for n, text in enumerate(texts)
    ]


@pytest.mark.parametrize(
    "ending", [".", "!", "?", "…", "...", '."', ".»", "?)", "!”", ".”)", "!?", ".]"]
)
def test_segment_ends_after_sentence_punctuation_even_with_closing_quotes(ending: str) -> None:
    merged = _run(a=_seq(A, [f"раз{ending}", "два"]))
    assert [segment.text for segment in merged.segments] == [f"раз{ending}", "два"]


@pytest.mark.parametrize(
    "text", ["раз", "раз,", "раз;", "раз:", "3:00", "п'ять", "«раз»", '"', "—"]
)
def test_segment_continues_after_a_word_that_does_not_end_a_sentence(text: str) -> None:
    merged = _run(a=_seq(A, [text, "два"]))
    assert [segment.text for segment in merged.segments] == [f"{text} два"]


@pytest.mark.parametrize(
    ("gap_ms", "segments"),
    [(PAUSE_BREAK_MS - 1, 1), (PAUSE_BREAK_MS, 2), (PAUSE_BREAK_MS + 1, 2)],
)
def test_pause_of_1500_ms_or_more_ends_a_segment(gap_ms: int, segments: int) -> None:
    merged = _run(
        a=[_item(A, 1_000, 1_200, "раз"), _item(A, 1_200 + gap_ms, 1_400 + gap_ms, "два")]
    )
    assert PAUSE_BREAK_MS == 1_500
    assert len(merged.segments) == segments
    assert len(merged.words) == 2


def test_speaker_change_ends_a_segment() -> None:
    merged = _run(
        a=[
            _item(A, 1_000, 1_200, "раз", "S0"),
            _item(A, 1_300, 1_500, "два", "S0"),
            _item(A, 1_600, 1_800, "три", "S1"),
            _item(A, 1_900, 2_100, "чотири", None),
        ]
    )
    assert [(s.text, s.speaker) for s in merged.segments] == [
        ("раз два", SpeakerKey(0, "S0")),
        ("три", SpeakerKey(0, "S1")),
        ("чотири", None),
    ]


def test_same_provider_label_in_the_next_chunk_is_a_speaker_change() -> None:
    merged = _run(
        a=[_item(A, 299_700, 299_900, "раз", "S0")],
        b=[_item(B, 300_000, 300_200, "два", "S0")],
    )
    assert [(s.text, s.speaker) for s in merged.segments] == [
        ("раз", SpeakerKey(0, "S0")),
        ("два", SpeakerKey(300_000, "S0")),
    ]


def test_segment_is_never_longer_than_20_seconds() -> None:
    words = [_item(A, n * 1_000, (n + 1) * 1_000, f"с{n}") for n in range(50)]  # no punctuation
    merged = _run(a=words)
    assert MAX_SEGMENT_MS == 20_000
    assert [(s.start_ms, s.end_ms) for s in merged.segments] == [
        (0, 20_000),  # exactly 20 s is allowed
        (20_000, 40_000),
        (40_000, 50_000),
    ]
    assert merged.segments[0].text == " ".join(f"с{n}" for n in range(20))


def test_segment_text_stays_within_the_schema_limit() -> None:
    words = [_item(A, n * 1_000, (n + 1) * 1_000, "а" * 900) for n in range(3)]
    merged = _run(a=words)
    assert [len(segment.text) for segment in merged.segments] == [1_801, 900]
    _validate(merged.to_schema_dict(), SCHEMA)


def test_segment_interval_text_ids_and_ordinals_come_from_its_words() -> None:
    merged = _run(
        a=[
            ChunkItem(1.0, 1.4, "Добрий", confidence=0.9),
            ChunkItem(1.5, 2.0, "ранок."),
            ChunkItem(5.0, 5.5, "Дякую!"),
        ]
    )
    assert [(s.id, s.ordinal, s.start_ms, s.end_ms, s.text) for s in merged.segments] == [
        ("s1", 0, 1_000, 2_000, "Добрий ранок."),
        ("s2", 1, 5_000, 5_500, "Дякую!"),
    ]
    assert all(s.speaker is None and s.confidence is None for s in merged.segments)


def test_provider_segments_are_ignored_at_word_level() -> None:
    words = _seq(A, ["Раз.", "Два.", "Три."], 1_000, step_ms=1_000, length_ms=800)
    one_for_everything = ChunkItem(0.0, 10.0, "Раз. Два. Три.")
    out_of_range = ChunkItem(0.0, 1e6, "зламаний")
    for provider_segments in ([one_for_everything], [out_of_range]):
        merged = merge_transcript(
            DURATION, [_chunk(A, words, provider_segments), _chunk(B), _chunk(C)]
        )
        assert merged.timing_level == "word"
        assert [s.text for s in merged.segments] == ["Раз.", "Два.", "Три."]
        assert [d.code for d in merged.diagnostics] == []


def test_word_level_segments_do_not_need_provider_segments_at_all() -> None:
    merged = _run(a=_seq(A, ["Раз.", "Два."]))
    assert [s.id for s in merged.segments] == ["s1", "s2"]


def test_segments_follow_the_merged_words_after_seam_deduplication() -> None:
    texts = ["раз", "привіт.", "два"]
    a = _seq(A, texts, 299_000, step_ms=500, length_ms=300)
    b = _seq(B, texts, 299_000, step_ms=500, length_ms=300)
    merged = _run(a=a, b=b)
    assert [w.text for w in merged.words] == texts
    assert [s.text for s in merged.segments] == ["раз привіт.", "два"]


# --- segment level: a chunk with word timing builds its own segments ----------------------


def _oversized_provider_segment() -> ChunkItem:
    return ChunkItem(0.0, 10.0, "я" * 4_000)


@pytest.mark.parametrize(
    "provider_segment", [_oversized_provider_segment(), ChunkItem(0.0, 1e6, "зламаний")]
)
def test_segment_level_chunk_with_word_timing_ignores_its_provider_segments(
    provider_segment: ChunkItem,
) -> None:
    words = _seq(A, ["Раз.", "Два.", "Три"], 1_000, step_ms=1_000, length_ms=800)
    merged = merge_transcript(
        DURATION,
        [
            _chunk(A, words, [provider_segment]),
            _chunk(B, segments=[_item(B, 400_000, 402_000, "Інша частина")]),
            _chunk(C),
        ],
    )
    assert merged.timing_level == "segment"
    assert merged.words == ()
    assert _spans(merged.segments) == [
        (1_000, 1_800, "Раз."),
        (2_000, 2_800, "Два."),
        (3_000, 3_800, "Три"),
        (400_000, 402_000, "Інша частина"),
    ]
    assert [(s.id, s.ordinal) for s in merged.segments] == [(f"s{n}", n - 1) for n in range(1, 5)]
    assert [(d.code, d.chunk_core_start_ms) for d in merged.diagnostics] == [
        (DIAGNOSTIC_SEGMENT_FALLBACK, 300_000)
    ]
    _validate(merged.to_schema_dict(), SCHEMA)


def test_segment_level_chunk_with_word_timing_uses_the_word_level_segment_rules() -> None:
    words = [
        _item(A, 1_000, 1_200, "раз", "S0"),
        _item(A, 1_300, 1_500, "два", "S0"),
        _item(A, 1_500 + PAUSE_BREAK_MS, 1_700 + PAUSE_BREAK_MS, "три", "S0"),  # after a pause
        _item(A, 3_300, 3_500, "чотири", "S1"),  # another speaker
        *(_item(A, 10_000 + n * 1_000, 11_000 + n * 1_000, f"с{n}") for n in range(25)),
    ]
    merged = merge_transcript(
        DURATION,
        [
            _chunk(A, words),
            _chunk(B, segments=[_item(B, 400_000, 402_000, "Інша частина")]),
            _chunk(C),
        ],
    )
    assert merged.timing_level == "segment"
    assert [s.text for s in merged.segments[:3]] == ["раз два", "три", "чотири"]
    assert [s.speaker for s in merged.segments[:3]] == [
        SpeakerKey(0, "S0"),
        SpeakerKey(0, "S0"),
        SpeakerKey(0, "S1"),
    ]
    assert [(s.start_ms, s.end_ms) for s in merged.segments[3:5]] == [
        (10_000, 30_000),  # exactly 20 s is allowed
        (30_000, 35_000),
    ]
    assert merged.segments[3].text.startswith("с0 с1 ")
    assert merged.segments[-1].text == "Інша частина"


def test_segment_level_seam_duplicates_of_two_chunks_with_word_timing_are_one_segment() -> None:
    texts = ["раз", "привіт."]
    merged = merge_transcript(
        DURATION,
        [
            _chunk(A, _seq(A, texts, 299_000, step_ms=500, length_ms=300)),
            _chunk(B, _seq(B, texts, 299_000, step_ms=500, length_ms=300)),
            _chunk(C, segments=[_item(C, 610_000, 611_000, "кінець")]),
        ],
    )
    assert merged.timing_level == "segment"
    assert _spans(merged.segments) == [
        (299_000, 299_800, "раз привіт."),
        (610_000, 611_000, "кінець"),
    ]


def test_segment_level_provider_segment_of_a_chunk_without_word_timing_is_used_as_given() -> None:
    merged = merge_transcript(
        DURATION,
        [
            _chunk(A),
            _chunk(B, segments=[_item(B, 400_000, 405_000, "я" * 2_000)]),
            _chunk(C),
        ],
    )
    assert [len(s.text) for s in merged.segments] == [2_000]


# --- a provider segment too long for a chunk without word timing (check_chunk) ---------------


def _too_long_segment() -> ChunkItem:
    return ChunkItem(1.0, 5.0, "а" * 2_001)


def test_segment_only_chunk_with_oversized_segment_names_the_chunk() -> None:
    chunks = [_chunk(A, [ChunkItem(1.0, 1.4, "раз")]), _chunk(B, segments=[_too_long_segment()])]
    with pytest.raises(MergeError) as caught:
        merge_transcript(DURATION, [*chunks, _chunk(C)])
    assert caught.value.code == MERGE_SEGMENT_TOO_LONG
    assert caught.value.chunk_core_start_ms == B.core_start_ms == 300_000
    assert caught.value.message


def test_oversized_provider_segment_of_a_chunk_with_word_timing_is_not_an_error() -> None:
    words = _seq(A, ["Раз.", "Два."])
    chunks = [_chunk(A, words, [_oversized_provider_segment()]), _chunk(B), _chunk(C)]
    assert [s.text for s in merge_transcript(DURATION, chunks).segments] == ["Раз.", "Два."]
    check_chunk(chunks[0])


def test_check_chunk_gives_the_same_oversized_segment_error() -> None:
    with pytest.raises(MergeError) as caught:
        check_chunk(_chunk(B, segments=[_too_long_segment()]))
    assert caught.value.code == MERGE_SEGMENT_TOO_LONG
    assert caught.value.chunk_core_start_ms == 300_000
    assert caught.value.message


def test_check_chunk_accepts_valid_chunks() -> None:
    check_chunk(_chunk(A, [ChunkItem(1.0, 1.4, "раз")]))
    check_chunk(_chunk(B, segments=[ChunkItem(1.0, 5.0, "а" * 2_000)]))
    check_chunk(_chunk(B))  # silence
    check_chunk(_chunk(B, state="failed"))
    check_chunk(_chunk(C, [ChunkItem(25.0, 25.0, "кінець")]))  # a zero-length point is fine


_BAD_CHUNKS: dict[str, tuple[ChunkResult, str]] = {
    "window": (
        _chunk(ChunkWindow(300_000, 600_000, 300_100, 605_000)),
        MERGE_WINDOW_INVALID,
    ),
    "state": (
        ChunkResult(B, "failed", words=(ChunkItem(1.0, 2.0, "x"),), has_word_timing=True),
        MERGE_RESULT_INCONSISTENT,
    ),
    "words_without_flag": (
        ChunkResult(B, "succeeded", words=(ChunkItem(1.0, 2.0, "x"),), has_word_timing=False),
        MERGE_RESULT_INCONSISTENT,
    ),
    "empty_word": (_chunk(B, [ChunkItem(1.0, 2.0, "  ")]), MERGE_INVALID_PROVIDER_TEXT),
    "long_word": (_chunk(B, [ChunkItem(1.0, 2.0, "x" * 2_001)]), MERGE_INVALID_PROVIDER_TEXT),
    "empty_segment": (_chunk(B, segments=[ChunkItem(1.0, 2.0, "")]), MERGE_INVALID_PROVIDER_TEXT),
    "word_timing": (_chunk(B, [ChunkItem(-50.0, -49.0, "x")]), MERGE_INVALID_PROVIDER_TIMING),
    "segment_timing": (
        _chunk(B, segments=[ChunkItem(2.0, 1.0, "x")]),
        MERGE_INVALID_PROVIDER_TIMING,
    ),
    "too_long_segment": (_chunk(B, segments=[_too_long_segment()]), MERGE_SEGMENT_TOO_LONG),
}


@pytest.mark.parametrize("name", sorted(_BAD_CHUNKS))
def test_check_chunk_rejects_what_merge_rejects_with_the_same_code(name: str) -> None:
    bad, code = _BAD_CHUNKS[name]
    with pytest.raises(MergeError) as alone:
        check_chunk(bad)
    with pytest.raises(MergeError) as merged:
        merge_transcript(DURATION, [_chunk(A), bad, _chunk(C)])
    assert alone.value.code == merged.value.code == code
    assert alone.value.chunk_core_start_ms == merged.value.chunk_core_start_ms == 300_000
    assert alone.value.message


# --- live OpenRouter responses as one chunk (47.125 s) ----------------------------------

LIVE_WINDOW = ChunkWindow(0, 47_125, 0, 47_125)


def _live_chunk(body: dict[str, Any]) -> ChunkResult:
    return ChunkResult(
        window=LIVE_WINDOW,
        state="succeeded",
        words=tuple(ChunkItem(w["start"], w["end"], w["word"]) for w in body["words"]),
        segments=tuple(ChunkItem(s["start"], s["end"], s["text"]) for s in body["segments"]),
        has_word_timing=True,
        language=body.get("language"),
    )


@pytest.mark.parametrize(
    ("fixture", "zero_length_words", "language"),
    [("mai_verbose.json", 0, "en"), ("gemini_verbose.json", 1, UNDETERMINED_LANGUAGE)],
)
def test_live_response_merges_at_word_level_into_sentence_segments(
    fixture: str, zero_length_words: int, language: str
) -> None:
    body: dict[str, Any] = json.loads((FIXTURES / fixture).read_text("utf-8"))
    assert len(body["segments"]) == 1  # the provider returns one segment for the whole audio

    merged = merge_transcript(47_125, [_live_chunk(body)])

    assert merged.timing_level == "word"
    assert merged.language == language
    assert [w.text for w in merged.words] == [w["word"] for w in body["words"]]
    assert [d.code for d in merged.diagnostics] == [
        DIAGNOSTIC_TIMING_ZERO_LENGTH
    ] * zero_length_words
    assert len(merged.segments) > 1
    assert all(s.end_ms - s.start_ms <= MAX_SEGMENT_MS for s in merged.segments)
    assert len(merged.segments) == 9  # nine words in the sample end with a full stop
    assert " ".join(s.text for s in merged.segments) == " ".join(w.text for w in merged.words)
    assert merged.segments[0].text.endswith("March 14th.")
    assert merged.segments[-1].text.endswith("Thank you for listening.")
    assert merged.segments[0].start_ms == merged.words[0].start_ms
    assert merged.segments[-1].end_ms == merged.words[-1].end_ms
    for word in merged.words:
        assert 0 <= word.start_ms < word.end_ms <= 47_125
    _validate(merged.to_schema_dict(), SCHEMA)


def test_live_gemini_zero_length_word_becomes_one_millisecond() -> None:
    body: dict[str, Any] = json.loads((FIXTURES / "gemini_verbose.json").read_text("utf-8"))
    merged = merge_transcript(47_125, [_live_chunk(body)])
    (flagged,) = (w for w in merged.words if w.text == "in" and w.start_ms == 40_700)
    assert flagged.end_ms == 40_701


# --- Hypothesis -----------------------------------------------------------------------

_STATES: tuple[ChunkState, ...] = (
    "succeeded",
    "succeeded",
    "succeeded",
    "failed",
    "pending",
    "unknown_outcome",
)
_TEXTS = ("ні", "так", "Ні,", "а")
_SPEAKERS = (None, "S0", "S1")


@st.composite
def _items(draw: st.DrawFn, window: ChunkWindow, limit: int) -> tuple[ChunkItem, ...]:
    span = window.upload_duration_ms
    produced: list[ChunkItem] = []
    for _ in range(draw(st.integers(min_value=0, max_value=limit))):
        start = draw(st.integers(min_value=0, max_value=span))
        end = draw(st.integers(min_value=start, max_value=span))  # start == end: a point
        point = start == end
        before = draw(st.integers(min_value=0, max_value=100)) if start == 0 and not point else 0
        after = draw(st.integers(min_value=0, max_value=100)) if end == span and not point else 0
        produced.append(
            ChunkItem(
                (start - before) / 1000,
                (end + after) / 1000,
                draw(st.sampled_from(_TEXTS)),
                draw(st.sampled_from(_SPEAKERS)),
            )
        )
    return tuple(produced)


@st.composite
def _scenarios(draw: st.DrawFn) -> tuple[int, list[ChunkResult], list[ChunkResult]]:
    duration = draw(st.integers(min_value=1, max_value=60_000))
    core = draw(st.integers(min_value=1_000, max_value=30_000))
    context = draw(st.integers(min_value=0, max_value=5_000))
    results: list[ChunkResult] = []
    for window in plan_chunks(duration, core_ms=core, context_ms=context).active:
        state = draw(st.sampled_from(_STATES))
        if state != "succeeded":
            results.append(ChunkResult(window, state))
            continue
        words = draw(_items(window, 12))
        segments = draw(_items(window, 4))
        word_timing = bool(words) or (not segments and draw(st.booleans()))
        results.append(
            ChunkResult(
                window,
                state,
                words=words,
                segments=segments,
                has_word_timing=word_timing,
                language=draw(st.sampled_from((None, "uk", "en"))),
            )
        )
    return duration, results, draw(st.permutations(results))


@settings(max_examples=200, deadline=None)
@given(_scenarios())
def test_property_ordinals_monotonic_and_times_within_the_source(
    scenario: tuple[int, list[ChunkResult], list[ChunkResult]],
) -> None:
    duration, results, shuffled = scenario
    merged = merge_transcript(duration, results)
    assert merge_transcript(duration, shuffled) == merged

    word_level = merged.timing_level == "word"
    # Segments are built from words, one per word at most, wherever words have times:
    # at word level everywhere, at segment level in the chunks that have word timing.
    # Only a chunk without word timing contributes provider segments.
    supplied_segments = (
        len(merged.words)
        if word_level
        else sum(len(r.words) if r.has_word_timing else len(r.segments) for r in results)
    )
    for items, prefix, supplied in (
        (merged.words, "w", sum(len(r.words) for r in results)),
        (merged.segments, "s", supplied_segments),
    ):
        assert [item.ordinal for item in items] == list(range(len(items)))
        assert [item.id for item in items] == [f"{prefix}{n + 1}" for n in range(len(items))]
        assert len(items) <= supplied
        for item in items:
            assert 0 <= item.start_ms < item.end_ms <= duration
            assert item.text
        for left, right in itertools.pairwise(items):
            assert left.start_ms <= right.start_ms
    for left, right in itertools.pairwise(merged.words):
        assert (left.start_ms, left.end_ms) <= (right.start_ms, right.end_ms)

    if word_level:
        assert " ".join(s.text for s in merged.segments) == " ".join(w.text for w in merged.words)
        for segment in merged.segments:
            # Only a single word may be longer than the limit; nothing is ever split.
            assert segment.end_ms - segment.start_ms <= MAX_SEGMENT_MS or any(
                (w.start_ms, w.end_ms) == (segment.start_ms, segment.end_ms) for w in merged.words
            )
    else:
        assert merged.words == ()
    coverage = merged.coverage
    assert coverage[0].start_ms == 0
    assert coverage[-1].end_ms == duration
    for earlier, later in itertools.pairwise(coverage):
        assert earlier.end_ms == later.start_ms
    assert [entry.state for entry in coverage] == [
        r.state for r in sorted(results, key=lambda r: r.window.core_start_ms)
    ]
    _validate(merged.to_schema_dict(), SCHEMA)
