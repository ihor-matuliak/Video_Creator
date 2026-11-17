"""Selection output: strict parsing of model JSON and grounding of its candidates (ADR 041 §5).

Pure standard library (ADR 017). ``parse_selection_output`` reproduces
``contracts/selection.schema.json``: the same keys, integers that are not booleans for scores
0..4, length limits, unique items, the category enum and at most 100 candidates. It also rejects
duplicate JSON keys, NaN and Infinity, text above 262144 UTF-8 bytes, control characters in
``title`` and in ``reason`` (a newline is allowed only in ``reason``), unpaired surrogates in both
free-text fields, and a ``title`` or ``reason`` that holds only whitespace. Every error is English
and starts with a JSON path such as ``candidates[0].start_id``; ids and keys in messages are cut to
64 characters; at most 50 errors are kept.

``validate_candidates`` grounds parsed candidates in the units of one request. Hard errors (an id
that is not among the units, a start unit after the end unit, evidence outside the inclusive
ordinal span) go to ``errors``; such a candidate is neither accepted nor listed as dropped. Soft
drops go to ``dropped``, at most one per candidate: ``NOT_OWNED`` when the start unit lies outside
the core (it wins when both apply), else ``CATEGORY_NOT_SELECTED`` when no model category is
selected. Unknown ids are never corrected. Two units with the same id are a caller error and raise
``ValueError``.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from clipforge.domain.categories import CATEGORIES

MAX_OUTPUT_BYTES: Final = 262144
MAX_CANDIDATES: Final = 100
MAX_ERRORS: Final = 50
ID_MAX_LENGTH: Final = 2000
SCORE_KEYS: Final = ("clarity", "hook", "completeness", "emotion", "novelty", "relevance")

CATEGORY_NOT_SELECTED: Final = "CATEGORY_NOT_SELECTED"
NOT_OWNED: Final = "NOT_OWNED"
DropReason = Literal["CATEGORY_NOT_SELECTED", "NOT_OWNED"]

_CANDIDATE_KEYS: Final = (
    "title",
    "categories",
    "start_id",
    "end_id",
    "evidence_ids",
    "reason",
    "scores",
    "visual_review_required",
)
_TITLE_MAX: Final = 160
_REASON_MAX: Final = 1000
_CATEGORIES_MAX: Final = 5
_SCORE_MIN: Final = 0
_SCORE_MAX: Final = 4
_SHOWN_LENGTH: Final = 64
_VISUAL_CATEGORIES: Final = frozenset({"action", "unexpected"})


class SelectionOutputError(ValueError):
    """The model output is not a valid selection; ``errors`` lists each problem in English."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True, slots=True)
class Scores:
    """Editorial ratings 0..4 written by the model. They are not probabilities or predictions."""

    clarity: int
    hook: int
    completeness: int
    emotion: int
    novelty: int
    relevance: int


@dataclass(frozen=True, slots=True)
class RawCandidate:
    """One candidate exactly as the model wrote it (an item of contracts/selection.schema.json)."""

    title: str
    categories: tuple[str, ...]
    start_id: str
    end_id: str
    evidence_ids: tuple[str, ...]
    reason: str
    scores: Scores
    visual_review_required: bool


@dataclass(frozen=True, slots=True)
class UnitRef:
    """A transcript word or segment in source milliseconds: the interval is ``[start, end)``."""

    id: str
    ordinal: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class GroundedCandidate:
    """A candidate whose ids exist in the request units, with the flags the backend derives."""

    start: UnitRef
    end: UnitRef
    evidence_ids: tuple[str, ...]
    categories: tuple[str, ...]
    title: str
    reason: str
    scores: Scores
    evidence_type: Literal["transcript"]
    visual_review_required: bool


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """``accepted`` keeps input order; ``dropped`` holds ``(candidate index, reason)`` pairs."""

    accepted: tuple[GroundedCandidate, ...]
    errors: tuple[str, ...]
    dropped: tuple[tuple[int, DropReason], ...]


class _JsonObject(dict[str, object]):
    """A parsed JSON object; ``duplicates`` lists the keys that appeared more than once."""

    duplicates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _NonFinite:
    """NaN, Infinity or -Infinity, kept as a value so that the validator can report its path."""

    literal: str


def _object_pairs(pairs: list[tuple[str, object]]) -> _JsonObject:
    obj = _JsonObject()
    duplicates: list[str] = []
    for key, value in pairs:
        if key in obj:
            duplicates.append(key)
        obj[key] = value
    obj.duplicates = tuple(duplicates)
    return obj


def _non_finite(literal: str) -> _NonFinite:
    return _NonFinite(literal)


def _where(path: str) -> str:
    return path or "$"


def _at(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _show(value: str) -> str:
    """A key or an id as it appears in a message: repr of at most 64 characters."""
    shown = repr(value[:_SHOWN_LENGTH])
    return f"{shown}..." if len(value) > _SHOWN_LENGTH else shown


def _type_error(path: str, expected: str, value: object, errors: list[str]) -> None:
    if isinstance(value, _NonFinite):
        errors.append(f"{_where(path)}: NaN and Infinity are not allowed")
    else:
        errors.append(f"{_where(path)}: must be {expected}")


def _object(value: object, path: str, errors: list[str]) -> _JsonObject | None:
    if isinstance(value, _JsonObject):
        return value
    _type_error(path, "an object", value, errors)
    return None


def _array(value: object, path: str, errors: list[str]) -> list[object] | None:
    if isinstance(value, list):
        return value
    _type_error(path, "an array", value, errors)
    return None


def _check_keys(obj: _JsonObject, path: str, keys: Sequence[str], errors: list[str]) -> None:
    """Report duplicate, unexpected and missing properties; every name in ``keys`` is required."""
    for key in dict.fromkeys(obj.duplicates):
        errors.append(f"{_where(path)}: duplicate key {_show(key)}")
    for key in obj:
        if key not in keys:
            errors.append(f"{_where(path)}: unexpected property {_show(key)}")
    for key in keys:
        if key not in obj:
            errors.append(f"{_where(path)}: missing required property {_show(key)}")


def _count(size: int, path: str, low: int, high: int | None, errors: list[str]) -> None:
    if size < low:
        errors.append(f"{_where(path)}: must contain at least {low} item(s)")
    if high is not None and size > high:
        errors.append(f"{_where(path)}: must contain at most {high} item(s)")


def _duplicates(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    repeated: dict[str, None] = {}
    for value in values:
        if value in seen:
            repeated[value] = None
        seen.add(value)
    return list(repeated)


def _string(value: object, path: str, low: int, high: int, errors: list[str]) -> str | None:
    if not isinstance(value, str):
        _type_error(path, "a string", value, errors)
        return None
    if not low <= len(value) <= high:
        errors.append(f"{_where(path)}: length must be between {low} and {high} characters")
        return None
    return value


def _free_text(
    value: object, path: str, high: int, *, newline_ok: bool, errors: list[str]
) -> str | None:
    text = _string(value, path, 1, high, errors)
    if text is None:
        return None
    for char in text:
        category = unicodedata.category(char)
        if category == "Cs":
            errors.append(f"{_where(path)}: contains an unpaired surrogate")
            return None
        if category == "Cc" and not (newline_ok and char == "\n"):
            errors.append(f"{_where(path)}: contains a control character")
            return None
    if not text.strip():
        errors.append(f"{_where(path)}: must not be blank")
        return None
    return text


def _score(value: object, path: str, errors: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        _type_error(path, "an integer", value, errors)
        return None
    if not _SCORE_MIN <= value <= _SCORE_MAX:
        errors.append(f"{_where(path)}: must be between {_SCORE_MIN} and {_SCORE_MAX}")
        return None
    return value


def _boolean(value: object, path: str, errors: list[str]) -> bool | None:
    if isinstance(value, bool):
        return value
    _type_error(path, "a boolean", value, errors)
    return None


def _categories(value: object, path: str, errors: list[str]) -> tuple[str, ...] | None:
    items = _array(value, path, errors)
    if items is None:
        return None
    _count(len(items), path, 1, _CATEGORIES_MAX, errors)
    known: list[str] = []
    complete = True
    for number, item in enumerate(items):
        item_path = f"{path}[{number}]"
        if not isinstance(item, str):
            _type_error(item_path, "a string", item, errors)
            complete = False
        elif item not in CATEGORIES:
            errors.append(f"{_where(item_path)}: unknown category {_show(item)}")
            complete = False
        else:
            known.append(item)
    for repeated in _duplicates(known):
        errors.append(f"{_where(path)}: duplicate item {_show(repeated)}")
    return tuple(known) if complete else None


def _evidence(value: object, path: str, errors: list[str]) -> tuple[str, ...] | None:
    items = _array(value, path, errors)
    if items is None:
        return None
    _count(len(items), path, 1, None, errors)
    ids: list[str] = []
    complete = True
    for number, item in enumerate(items):
        ident = _string(item, f"{path}[{number}]", 1, ID_MAX_LENGTH, errors)
        if ident is None:
            complete = False
        else:
            ids.append(ident)
    for repeated in _duplicates(ids):
        errors.append(f"{_where(path)}: duplicate item {_show(repeated)}")
    return tuple(ids) if complete else None


def _scores(value: object, path: str, errors: list[str]) -> Scores | None:
    obj = _object(value, path, errors)
    if obj is None:
        return None
    _check_keys(obj, path, SCORE_KEYS, errors)
    values: dict[str, int] = {}
    for key in SCORE_KEYS:
        if key in obj:
            score = _score(obj[key], _at(path, key), errors)
            if score is not None:
                values[key] = score
    if len(values) != len(SCORE_KEYS):
        return None
    return Scores(**values)


def _candidate(value: object, path: str, errors: list[str]) -> RawCandidate | None:
    obj = _object(value, path, errors)
    if obj is None:
        return None
    _check_keys(obj, path, _CANDIDATE_KEYS, errors)
    if not all(key in obj for key in _CANDIDATE_KEYS):
        return None
    title = _free_text(
        obj["title"], _at(path, "title"), _TITLE_MAX, newline_ok=False, errors=errors
    )
    categories = _categories(obj["categories"], _at(path, "categories"), errors)
    start_id = _string(obj["start_id"], _at(path, "start_id"), 1, ID_MAX_LENGTH, errors)
    end_id = _string(obj["end_id"], _at(path, "end_id"), 1, ID_MAX_LENGTH, errors)
    evidence_ids = _evidence(obj["evidence_ids"], _at(path, "evidence_ids"), errors)
    reason = _free_text(
        obj["reason"], _at(path, "reason"), _REASON_MAX, newline_ok=True, errors=errors
    )
    scores = _scores(obj["scores"], _at(path, "scores"), errors)
    visual = _boolean(obj["visual_review_required"], _at(path, "visual_review_required"), errors)
    if (
        title is None
        or categories is None
        or start_id is None
        or end_id is None
        or evidence_ids is None
        or reason is None
        or scores is None
        or visual is None
    ):
        return None
    return RawCandidate(
        title=title,
        categories=categories,
        start_id=start_id,
        end_id=end_id,
        evidence_ids=evidence_ids,
        reason=reason,
        scores=scores,
        visual_review_required=visual,
    )


def _document(document: object, errors: list[str]) -> tuple[RawCandidate, ...]:
    root = _object(document, "", errors)
    if root is None:
        return ()
    _check_keys(root, "", ("candidates",), errors)
    if "candidates" not in root:
        return ()
    items = _array(root["candidates"], "candidates", errors)
    if items is None:
        return ()
    _count(len(items), "candidates", 0, MAX_CANDIDATES, errors)
    parsed = [
        _candidate(item, f"candidates[{number}]", errors) for number, item in enumerate(items)
    ]
    return tuple(candidate for candidate in parsed if candidate is not None)


def parse_selection_output(text: str) -> tuple[RawCandidate, ...]:
    """Parse model output strictly; raise ``SelectionOutputError`` listing every problem found."""
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise SelectionOutputError(("$: text is not valid Unicode",)) from None
    if size > MAX_OUTPUT_BYTES:
        raise SelectionOutputError((f"$: text is larger than {MAX_OUTPUT_BYTES} UTF-8 bytes",))
    try:
        document: object = json.loads(
            text, object_pairs_hook=_object_pairs, parse_constant=_non_finite
        )
    except json.JSONDecodeError as exc:
        position = f"line {exc.lineno}, column {exc.colno}"
        raise SelectionOutputError((f"$: malformed JSON at {position}",)) from None
    except (ValueError, RecursionError):
        raise SelectionOutputError(("$: malformed JSON",)) from None
    errors: list[str] = []
    candidates = _document(document, errors)
    if errors:
        raise SelectionOutputError(tuple(errors[:MAX_ERRORS]))
    return candidates


def _lookup(
    by_id: Mapping[str, UnitRef], unit_id: str, path: str, errors: list[str]
) -> UnitRef | None:
    unit = by_id.get(unit_id)
    if unit is None:
        errors.append(f"{path}: unknown unit id {_show(unit_id)}")
    return unit


def validate_candidates(
    candidates: Sequence[RawCandidate],
    units: Sequence[UnitRef],
    core_ordinals: tuple[int, int],
    selected: tuple[str, ...],
) -> ValidationResult:
    """Ground ``candidates`` in the ``units`` of one request.

    ``core_ordinals`` is the inclusive ``(first, last)`` ordinal of the window core and
    ``selected`` holds the profile categories. Ids match exactly: an unknown id is reported and
    never replaced by a neighbouring unit. Raises ``ValueError`` when two units share an id.
    """
    by_id = {unit.id: unit for unit in units}
    if len(by_id) != len(units):
        raise ValueError("unit ids must be unique within one request")
    core_first, core_last = core_ordinals
    selected_set = frozenset(selected)
    errors: list[str] = []
    accepted: list[GroundedCandidate] = []
    dropped: list[tuple[int, DropReason]] = []
    for index, candidate in enumerate(candidates):
        where = f"candidates[{index}]"
        before = len(errors)
        start = _lookup(by_id, candidate.start_id, f"{where}.start_id", errors)
        end = _lookup(by_id, candidate.end_id, f"{where}.end_id", errors)
        evidence = [
            _lookup(by_id, item, f"{where}.evidence_ids[{number}]", errors)
            for number, item in enumerate(candidate.evidence_ids)
        ]
        if start is None or end is None:
            continue
        if start.ordinal > end.ordinal:
            errors.append(f"{where}.end_id: must not precede start_id")
            continue
        for number, unit in enumerate(evidence):
            if unit is not None and not start.ordinal <= unit.ordinal <= end.ordinal:
                errors.append(f"{where}.evidence_ids[{number}]: outside start_id..end_id")
        if len(errors) != before:
            continue
        # One drop per candidate. NOT_OWNED wins when both reasons apply.
        if not core_first <= start.ordinal <= core_last:
            dropped.append((index, NOT_OWNED))
            continue
        if selected_set.isdisjoint(candidate.categories):
            dropped.append((index, CATEGORY_NOT_SELECTED))
            continue
        accepted.append(
            GroundedCandidate(
                start=start,
                end=end,
                evidence_ids=candidate.evidence_ids,
                categories=candidate.categories,
                title=candidate.title,
                reason=candidate.reason,
                scores=candidate.scores,
                evidence_type="transcript",
                visual_review_required=candidate.visual_review_required
                or not _VISUAL_CATEGORIES.isdisjoint(candidate.categories),
            )
        )
    return ValidationResult(
        accepted=tuple(accepted),
        errors=tuple(errors[:MAX_ERRORS]),
        dropped=tuple(dropped),
    )
