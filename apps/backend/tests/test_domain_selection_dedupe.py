"""T-036/T-037 and ADR 041 §7: selection dedupe, diversity order and the candidate cap."""

from __future__ import annotations

import random

import pytest

from clipforge.domain.categories import CATEGORIES, CategoryError
from clipforge.domain.selection_dedupe import (
    Existing,
    ExistingKind,
    Item,
    Selection,
    Suppressed,
    SuppressionKind,
    dedupe_and_rank,
)


def make_item(
    key: str,
    start_ms: int,
    end_ms: int,
    *,
    ids: tuple[str, ...] = (),
    categories: tuple[str, ...] = ("funny",),
    rank: int = 80,
    visual: bool = False,
) -> Item:
    """A candidate whose evidence entries carry the candidate's own interval."""
    return Item(
        key=key,
        start_ms=start_ms,
        end_ms=end_ms,
        evidence=tuple((evidence_id, start_ms, end_ms) for evidence_id in ids),
        categories=categories,
        rank=rank,
        visual_review_required=visual,
    )


def make_existing(
    clip_id: str,
    start_ms: int,
    end_ms: int,
    *,
    kind: ExistingKind,
    ids: frozenset[str] | None,
) -> Existing:
    return Existing(clip_id=clip_id, start_ms=start_ms, end_ms=end_ms, evidence_ids=ids, kind=kind)


def keys(items: tuple[Item, ...]) -> list[str]:
    return [item.key for item in items]


# T-036: two categories for the same event give one kept item with merged categories.


def test_t036_two_categories_for_one_event_give_one_kept_item() -> None:
    funny = make_item("funny", 10_000, 20_000, ids=("w1", "w2"), categories=("funny",), rank=80)
    action = make_item(
        "action", 10_000, 20_000, ids=("w1", "w2"), categories=("action",), rank=75, visual=True
    )

    selection = dedupe_and_rank([action, funny], [], max_candidates=10)

    assert keys(selection.kept) == ["funny"]
    kept = selection.kept[0]
    assert kept.categories == ("funny", "action")
    assert kept.visual_review_required is True
    assert (kept.start_ms, kept.end_ms, kept.rank) == (10_000, 20_000, 80)
    assert selection.suppressed == (Suppressed("action", "duplicate_in_run", "funny"),)


def test_merged_categories_follow_category_order() -> None:
    late = make_item("late", 10_000, 20_000, ids=("w1",), categories=("game_details",), rank=80)
    early = make_item("early", 10_000, 20_000, ids=("w1",), categories=("fails", "funny"), rank=70)

    kept = dedupe_and_rank([late, early], [], max_candidates=10).kept

    assert kept[0].categories == ("funny", "fails", "game_details")


def test_merge_adds_only_evidence_inside_the_kept_interval() -> None:
    kept_item = Item(
        key="a",
        start_ms=10_000,
        end_ms=20_000,
        evidence=(("w1", 10_000, 12_000),),
        categories=("funny",),
        rank=80,
        visual_review_required=False,
    )
    duplicate = Item(
        key="b",
        start_ms=10_500,
        end_ms=21_000,
        evidence=(("w1", 10_000, 12_000), ("w9", 18_000, 19_500), ("w7", 19_500, 21_000)),
        categories=("unexpected",),
        rank=70,
        visual_review_required=False,
    )

    selection = dedupe_and_rank([duplicate, kept_item], [], max_candidates=10)

    merged = selection.kept[0]
    assert merged.evidence == (("w1", 10_000, 12_000), ("w9", 18_000, 19_500))
    assert merged.categories == ("funny", "unexpected")
    assert (merged.start_ms, merged.end_ms, merged.rank) == (10_000, 20_000, 80)


def test_later_duplicates_match_the_merged_evidence_of_the_kept_item() -> None:
    first = make_item("a", 10_000, 20_000, ids=("w1",), rank=80)
    second = Item(
        key="b",
        start_ms=10_000,
        end_ms=20_000,
        evidence=(("w1", 10_000, 20_000), ("w9", 18_000, 19_500)),
        categories=("funny",),
        rank=70,
        visual_review_required=False,
    )
    third = make_item("c", 15_000, 20_000, ids=("w9",), rank=60)

    selection = dedupe_and_rank([first, second, third], [], max_candidates=10)

    assert keys(selection.kept) == ["a"]
    assert selection.suppressed == (
        Suppressed("b", "duplicate_in_run", "a"),
        Suppressed("c", "duplicate_in_run", "a"),
    )


# T-037: the same words at different times, and overlap without shared ids, both survive.


def test_t037_same_word_ids_at_different_times_are_both_kept() -> None:
    early = make_item("early", 10_000, 20_000, ids=("w1", "w2"), rank=80)
    late = make_item("late", 600_000, 610_000, ids=("w1", "w2"), rank=75)

    selection = dedupe_and_rank([early, late], [], max_candidates=10)

    assert keys(selection.kept) == ["early", "late"]
    assert selection.suppressed == ()


def test_t037_overlap_without_shared_evidence_keeps_both() -> None:
    first = make_item("first", 10_000, 20_000, ids=("w1",), rank=80)
    second = make_item("second", 12_000, 22_000, ids=("w2",), rank=75)

    selection = dedupe_and_rank([first, second], [], max_candidates=10)

    assert keys(selection.kept) == ["first", "second"]
    assert selection.suppressed == ()


# Temporal signal: IoU >= 0.65 or overlap / min_len >= 0.8, both with shared evidence.


@pytest.mark.parametrize(
    ("first", "second", "duplicate"),
    [
        ((0, 100), (21, 121), True),  # IoU 79/121 ≈ 0.653; cover 0.79 is not enough alone
        ((0, 100), (22, 122), False),  # IoU 78/122 ≈ 0.639; cover 0.78
        ((0, 1000), (920, 1020), True),  # cover exactly 0.8; IoU ≈ 0.078
        ((0, 1000), (921, 1021), False),  # cover 0.79
        ((0, 100), (100, 200), False),  # touching half-open intervals do not overlap
        ((0, 100), (0, 100), True),
    ],
)
def test_temporal_signal_boundaries_need_a_shared_id(
    first: tuple[int, int], second: tuple[int, int], duplicate: bool
) -> None:
    higher = make_item("a", first[0], first[1], ids=("w1",), rank=80)
    lower = make_item("b", second[0], second[1], ids=("w1",), rank=70)

    selection = dedupe_and_rank([higher, lower], [], max_candidates=10)

    assert (len(selection.kept) == 1) is duplicate


def test_temporal_match_without_shared_id_is_not_a_duplicate() -> None:
    higher = make_item("a", 0, 100, ids=("w1",), rank=80)
    lower = make_item("b", 0, 100, ids=("w2",), rank=70)

    assert keys(dedupe_and_rank([higher, lower], [], max_candidates=10).kept) == ["a", "b"]


# Reviewed and suggested clips.


def test_reviewed_clip_with_shared_evidence_suppresses_the_candidate() -> None:
    clip = make_existing("clip-1", 10_000, 20_000, kind="reviewed", ids=frozenset({"w1"}))
    item = make_item("a", 10_000, 20_000, ids=("w1",))

    selection = dedupe_and_rank([item], [clip], max_candidates=10)

    assert selection == Selection(
        kept=(), suppressed=(Suppressed("a", "already_reviewed", "clip-1"),)
    )


@pytest.mark.parametrize(
    ("kind", "suppression"),
    [("reviewed", "already_reviewed"), ("suggested", "duplicate_of_suggested")],
)
def test_none_evidence_matches_on_the_temporal_signal_alone(
    kind: ExistingKind, suppression: SuppressionKind
) -> None:
    clip = make_existing("clip-1", 10_000, 20_000, kind=kind, ids=None)
    item = make_item("a", 10_500, 20_500, ids=("w9",))

    selection = dedupe_and_rank([item], [clip], max_candidates=10)

    assert selection == Selection(kept=(), suppressed=(Suppressed("a", suppression, "clip-1"),))


def test_existing_clip_with_other_evidence_does_not_suppress_an_overlapping_candidate() -> None:
    clip = make_existing("clip-1", 10_000, 20_000, kind="suggested", ids=frozenset({"w5"}))
    item = make_item("a", 10_000, 20_000, ids=("w1",))

    assert keys(dedupe_and_rank([item], [clip], max_candidates=10).kept) == ["a"]


def test_empty_evidence_set_never_shares_an_id() -> None:
    clip = make_existing("clip-1", 10_000, 20_000, kind="reviewed", ids=frozenset())
    item = make_item("a", 10_000, 20_000, ids=("w1",))

    assert keys(dedupe_and_rank([item], [clip], max_candidates=10).kept) == ["a"]


def test_same_ids_at_another_time_are_not_suppressed_by_a_suggested_clip() -> None:
    clip = make_existing("clip-1", 500_000, 510_000, kind="suggested", ids=frozenset({"w1"}))
    item = make_item("a", 10_000, 20_000, ids=("w1",))

    assert keys(dedupe_and_rank([item], [clip], max_candidates=10).kept) == ["a"]


def test_reviewed_match_takes_precedence_over_suggested_match() -> None:
    reviewed = make_existing("reviewed-clip", 10_000, 20_000, kind="reviewed", ids=None)
    suggested = make_existing("suggested-clip", 10_000, 20_000, kind="suggested", ids=None)
    item = make_item("a", 10_000, 20_000, ids=("w1",))

    selection = dedupe_and_rank([item], [suggested, reviewed], max_candidates=10)

    assert selection.suppressed == (Suppressed("a", "already_reviewed", "reviewed-clip"),)


# Diversity order.


def test_diversity_lifts_a_least_used_category_within_the_rank_window() -> None:
    first = make_item("a", 0, 10_000, categories=("funny",), rank=90)
    second = make_item("b", 100_000, 110_000, categories=("funny",), rank=88)
    third = make_item("c", 200_000, 210_000, categories=("action",), rank=86)

    kept = dedupe_and_rank([first, second, third], [], max_candidates=10).kept

    assert keys(kept) == ["a", "c", "b"]


@pytest.mark.parametrize(
    ("weak_rank", "expected"),
    [(84, ["z", "y", "x"]), (85, ["z", "x", "y"])],
)
def test_diversity_cannot_lift_a_rank_below_best_minus_five(
    weak_rank: int, expected: list[str]
) -> None:
    strong = make_item("z", 0, 10_000, categories=("funny",), rank=91)
    close = make_item("y", 100_000, 110_000, categories=("funny",), rank=90)
    weak = make_item("x", 200_000, 210_000, categories=("action",), rank=weak_rank)

    kept = dedupe_and_rank([weak, close, strong], [], max_candidates=10).kept

    assert keys(kept) == expected


@pytest.mark.parametrize("seed", range(20))
def test_every_pick_is_within_the_rank_window_of_the_best_remaining(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - seeded test data, not security
    items = [
        make_item(
            f"k{number:02d}",
            1_000 * number,
            1_000 * number + 500,
            categories=(rng.choice(CATEGORIES),),
            rank=rng.randint(40, 100),
        )
        for number in range(20)
    ]

    kept = dedupe_and_rank(items, [], max_candidates=100).kept

    assert len(kept) == 20
    for position, item in enumerate(kept):
        assert item.rank >= max(later.rank for later in kept[position:]) - 5


# Cap.


def test_cap_applies_after_dedupe_and_the_rest_are_over_cap() -> None:
    top = make_item("a", 0, 10_000, ids=("w1",), rank=90)
    duplicate = make_item("dup", 0, 10_000, ids=("w1",), rank=85)
    second = make_item("b", 100_000, 110_000, ids=("w2",), rank=80)
    third = make_item("c", 200_000, 210_000, ids=("w3",), rank=70)

    selection = dedupe_and_rank([top, duplicate, second, third], [], max_candidates=2)

    assert keys(selection.kept) == ["a", "b"]
    assert selection.suppressed == (
        Suppressed("dup", "duplicate_in_run", "a"),
        Suppressed("c", "over_cap", None),
    )


def test_zero_cap_suppresses_every_candidate_as_over_cap() -> None:
    items = [
        make_item("a", 0, 10_000, rank=90),
        make_item("b", 100_000, 110_000, rank=80),
    ]

    selection = dedupe_and_rank(items, [], max_candidates=0)

    assert selection == Selection(
        kept=(),
        suppressed=(Suppressed("a", "over_cap", None), Suppressed("b", "over_cap", None)),
    )


# Determinism under input shuffling.


def _scenario() -> tuple[list[Item], list[Existing]]:
    items = [
        make_item("a", 10_000, 20_000, ids=("w1", "w2"), categories=("funny",), rank=90),
        make_item("b", 10_500, 19_500, ids=("w2",), categories=("action",), rank=90, visual=True),
        make_item("c", 100_000, 110_000, ids=("w5",), categories=("fails",), rank=88),
        make_item("d", 200_000, 210_000, ids=("w6",), categories=("funny",), rank=88),
        make_item("e", 200_000, 210_000, ids=("w7",), categories=("unexpected",), rank=88),
        make_item("f", 300_000, 310_000, ids=("w8",), categories=("game_details",), rank=70),
        make_item("g", 400_000, 410_000, ids=("w9",), categories=("funny",), rank=60),
        make_item("h", 500_000, 510_000, ids=("w10",), categories=("action",), rank=84),
        make_item("i", 600_000, 610_000, ids=("w11",), categories=("funny",), rank=45),
    ]
    existing = [
        make_existing("r1", 600_000, 610_000, kind="reviewed", ids=frozenset({"w11"})),
        make_existing("s1", 300_000, 310_000, kind="suggested", ids=None),
    ]
    return items, existing


def test_scenario_result_matches_the_hand_computed_order() -> None:
    items, existing = _scenario()

    selection = dedupe_and_rank(items, existing, max_candidates=5)

    # Kept before diversity: a (funny+action, merged b), c, d, e, h, g.
    # Diversity: a, then c (unused category), then e, then d, then h; g is over the cap.
    assert keys(selection.kept) == ["a", "c", "e", "d", "h"]
    assert selection.kept[0].categories == ("funny", "action")
    assert selection.kept[0].visual_review_required is True
    assert selection.suppressed == (
        Suppressed("b", "duplicate_in_run", "a"),
        Suppressed("f", "duplicate_of_suggested", "s1"),
        Suppressed("i", "already_reviewed", "r1"),
        Suppressed("g", "over_cap", None),
    )


def test_result_does_not_depend_on_input_order() -> None:
    items, existing = _scenario()
    baseline = dedupe_and_rank(items, existing, max_candidates=5)

    for seed in range(30):
        rng = random.Random(seed)  # noqa: S311 - seeded test data, not security
        shuffled_items = list(items)
        shuffled_existing = list(existing)
        rng.shuffle(shuffled_items)
        rng.shuffle(shuffled_existing)
        assert dedupe_and_rank(shuffled_items, shuffled_existing, max_candidates=5) == baseline


# Input validation.


def test_rejects_an_empty_or_reversed_interval() -> None:
    with pytest.raises(ValueError, match=r"0 <= start_ms < end_ms"):
        dedupe_and_rank([make_item("a", 5_000, 5_000)], [], max_candidates=1)


def test_rejects_duplicate_item_keys() -> None:
    items = [make_item("a", 0, 10_000), make_item("a", 100_000, 110_000)]

    with pytest.raises(ValueError, match="item keys must be unique"):
        dedupe_and_rank(items, [], max_candidates=10)


def test_rejects_an_item_without_categories() -> None:
    with pytest.raises(ValueError, match="has no categories"):
        dedupe_and_rank([make_item("a", 0, 10_000, categories=())], [], max_candidates=10)


def test_rejects_an_unknown_category() -> None:
    with pytest.raises(CategoryError, match="CATEGORY_UNKNOWN"):
        dedupe_and_rank([make_item("a", 0, 10_000, categories=("boring",))], [], max_candidates=10)


def test_rejects_duplicate_existing_clip_ids() -> None:
    clips = [
        make_existing("clip", 0, 10_000, kind="reviewed", ids=None),
        make_existing("clip", 100_000, 110_000, kind="suggested", ids=None),
    ]

    with pytest.raises(ValueError, match="clip ids must be unique"):
        dedupe_and_rank([], clips, max_candidates=10)


def test_rejects_a_negative_cap() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        dedupe_and_rank([], [], max_candidates=-1)
