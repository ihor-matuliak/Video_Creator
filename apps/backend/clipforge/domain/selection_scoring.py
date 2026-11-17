"""Selection scoring: rank, quality threshold and recommended interval (ADR 041 §6).

Pure functions on integers, standard library only (ADR 017). Times are integer
milliseconds on the source timebase; intervals are half-open ``[start_ms, end_ms)``.
The model's component scores are expected to be validated already (integers 0..4);
the output parser owns that check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

RANK_THRESHOLD: Final = 60
COMPLETENESS_THRESHOLD: Final = 2

OUT_OF_SOURCE: Final = "OUT_OF_SOURCE"
LONGER_THAN_MAX: Final = "LONGER_THAN_MAX"
SHORTER_THAN_MIN: Final = "SHORTER_THAN_MIN"

RejectCode = Literal["OUT_OF_SOURCE", "LONGER_THAN_MAX", "SHORTER_THAN_MIN"]

# Component weights, docs/06 §6. They sum to 100.
_WEIGHTS: Final[tuple[tuple[str, int], ...]] = (
    ("clarity", 20),
    ("hook", 15),
    ("completeness", 25),
    ("emotion", 15),
    ("novelty", 10),
    ("relevance", 15),
)


@dataclass(frozen=True, slots=True)
class Recommended:
    """Recommended interval ``[start_ms, end_ms)`` that contains the evidence."""

    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class Rejected:
    """Candidate that cannot get a recommended interval. ``code`` is stable."""

    code: RejectCode


def compute_rank(scores: Mapping[str, int]) -> int:
    """Return the rank 0..100: the weighted sum divided by 4, halves rounded up.

    The weights are multiples of 5, so the weighted sum is a multiple of 5. Adding 2
    before the floor division turns an exact x.5 into the next integer (docs/06 §6).
    """
    weighted = sum(weight * scores[name] for name, weight in _WEIGHTS)
    return (weighted + 2) // 4


def passes_threshold(rank: int, completeness: int) -> bool:
    """Default candidate threshold: rank >= 60 and completeness >= 2."""
    return rank >= RANK_THRESHOLD and completeness >= COMPLETENESS_THRESHOLD


def recommend_interval(
    evidence_start_ms: int,
    evidence_end_ms: int,
    *,
    duration_ms: int,
    min_ms: int,
    max_ms: int,
    pre_ms: int = 3000,
    post_ms: int = 4000,
) -> Recommended | Rejected:
    """Pad the evidence interval into a recommended interval.

    Checks run in this order: OUT_OF_SOURCE, LONGER_THAN_MAX, padding, SHORTER_THAN_MIN.
    Padding is ``min(pre_ms, evidence_start)`` before and ``min(post_ms, duration - end)``
    after. If the total is longer than ``max_ms``, the excess comes out of the padding only:
    ``cut_pre = excess * pre // (pre + post)`` from the front, the rest from the back.
    The evidence is never cut.
    """
    if not 0 <= evidence_start_ms < evidence_end_ms <= duration_ms:
        return Rejected(OUT_OF_SOURCE)
    evidence_ms = evidence_end_ms - evidence_start_ms
    if evidence_ms > max_ms:
        return Rejected(LONGER_THAN_MAX)

    pre = min(pre_ms, evidence_start_ms)
    post = min(post_ms, duration_ms - evidence_end_ms)
    excess = evidence_ms + pre + post - max_ms
    if excess > 0:
        # excess > 0 with evidence_ms <= max_ms implies pre + post > 0, so the division is safe.
        # excess <= pre + post, so neither cut is larger than its padding.
        cut_pre = excess * pre // (pre + post)
        pre -= cut_pre
        post -= excess - cut_pre

    start_ms = evidence_start_ms - pre
    end_ms = evidence_end_ms + post
    if end_ms - start_ms < min_ms:
        return Rejected(SHORTER_THAN_MIN)
    return Recommended(start_ms=start_ms, end_ms=end_ms)
