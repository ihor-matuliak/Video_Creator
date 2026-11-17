"""ADR 041 §10 / T-031: search profile validation, canonical categories, JSON round-trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipforge.domain.categories import (
    CATEGORIES,
    CATEGORY_DUPLICATE,
    CATEGORY_NOT_STRING,
    CATEGORY_UNKNOWN,
)
from clipforge.domain.search_profile import (
    CATEGORIES_REQUIRED,
    CUSTOM_INSTRUCTION_INVALID,
    CUSTOM_INSTRUCTION_MAX_LENGTH,
    CUSTOM_INSTRUCTION_TOO_LONG,
    DEFAULT_PROFILE,
    DURATION_MAX_MS,
    DURATION_MIN_MS,
    DURATION_RANGE,
    INVALID_DURATION,
    INVALID_MAX_CANDIDATES,
    MAX_CANDIDATES_MAX,
    MAX_CANDIDATES_MIN,
    PROFILE_INVALID,
    ProfileError,
    SearchProfile,
    parse_search_profile,
    profile_to_json,
)

CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "contracts"
PROFILE_FIELDS = (
    "categories",
    "custom_instruction",
    "min_duration_ms",
    "max_duration_ms",
    "max_candidates",
    "allow_partial",
)


def _raw(**changes: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "categories": ["funny", "fails"],
        "custom_instruction": "",
        "min_duration_ms": 5000,
        "max_duration_ms": 90000,
        "max_candidates": 20,
        "allow_partial": False,
    }
    raw.update(changes)
    return raw


def _error(raw: object) -> ProfileError:
    with pytest.raises(ProfileError) as info:
        parse_search_profile(raw)
    return info.value


def test_example_contract_parses() -> None:
    example = json.loads(
        (CONTRACTS_DIR / "search-profile.example.json").read_text(encoding="utf-8")
    )
    profile = parse_search_profile(example)
    assert profile == SearchProfile(
        categories=("funny", "fails"),
        custom_instruction="Завершені невдалі спроби зі зрозумілою реакцією",
        min_duration_ms=5000,
        max_duration_ms=90000,
        max_candidates=20,
        allow_partial=False,
    )
    assert profile_to_json(profile) == example


def test_schema_matches_domain_limits() -> None:
    schema = json.loads((CONTRACTS_DIR / "search-profile.schema.json").read_text(encoding="utf-8"))
    properties = schema["properties"]
    assert tuple(properties) == PROFILE_FIELDS
    assert tuple(schema["required"]) == PROFILE_FIELDS
    assert properties["categories"]["minItems"] == 1
    assert properties["categories"]["items"]["enum"] == list(CATEGORIES)
    assert properties["custom_instruction"]["maxLength"] == CUSTOM_INSTRUCTION_MAX_LENGTH
    for field in ("min_duration_ms", "max_duration_ms"):
        assert properties[field]["minimum"] == DURATION_MIN_MS
        assert properties[field]["maximum"] == DURATION_MAX_MS
    assert properties["max_candidates"]["minimum"] == MAX_CANDIDATES_MIN
    assert properties["max_candidates"]["maximum"] == MAX_CANDIDATES_MAX


def test_default_profile_values_and_round_trip() -> None:
    expected = SearchProfile(
        categories=CATEGORIES,
        custom_instruction="",
        min_duration_ms=15000,
        max_duration_ms=90000,
        max_candidates=20,
        allow_partial=False,
    )
    assert expected == DEFAULT_PROFILE
    assert parse_search_profile(profile_to_json(DEFAULT_PROFILE)) == DEFAULT_PROFILE


def test_t031_empty_categories_rejected() -> None:
    error = _error(_raw(categories=[]))
    assert (error.field, error.code) == ("categories", CATEGORIES_REQUIRED)
    assert error.message_uk == "Оберіть хоча б одну категорію"


@pytest.mark.parametrize("category", CATEGORIES)
def test_t031_single_category_accepted(category: str) -> None:
    assert parse_search_profile(_raw(categories=[category])).categories == (category,)


def test_t031_all_categories_accepted_in_canonical_order() -> None:
    profile = parse_search_profile(_raw(categories=list(reversed(CATEGORIES))))
    assert profile.categories == CATEGORIES


def test_categories_are_returned_in_canonical_order() -> None:
    profile = parse_search_profile(_raw(categories=["game_details", "funny", "fails"]))
    assert profile.categories == ("funny", "fails", "game_details")


@pytest.mark.parametrize(
    ("categories", "code"),
    [
        (["boring"], CATEGORY_UNKNOWN),
        (["Fails"], CATEGORY_UNKNOWN),
        ([""], CATEGORY_UNKNOWN),
        (["fails", "fails"], CATEGORY_DUPLICATE),
        ([1], CATEGORY_NOT_STRING),
        ("funny", PROFILE_INVALID),
        (("funny",), PROFILE_INVALID),
        (None, PROFILE_INVALID),
    ],
    ids=[
        "unknown",
        "wrong-case",
        "empty-string",
        "duplicate",
        "not-string",
        "text-instead-of-list",
        "tuple-instead-of-list",
        "null",
    ],
)
def test_category_errors_name_the_categories_field(categories: object, code: str) -> None:
    error = _error(_raw(categories=categories))
    assert (error.field, error.code) == ("categories", code)


@pytest.mark.parametrize("text", ["a" * 2000, "я" * 2000, "\U0001f600" * 2000])
def test_custom_instruction_at_limit_accepted(text: str) -> None:
    assert parse_search_profile(_raw(custom_instruction=text)).custom_instruction == text


@pytest.mark.parametrize("text", ["a" * 2001, "я" * 2001, "\U0001f600" * 2001])
def test_custom_instruction_over_limit_rejected(text: str) -> None:
    # Each emoji is 2 UTF-16 units; the limit counts code points, so 2001 emoji must still fail.
    error = _error(_raw(custom_instruction=text))
    assert (error.field, error.code) == ("custom_instruction", CUSTOM_INSTRUCTION_TOO_LONG)


@pytest.mark.parametrize("text", ["кінець\x00", "\x00", "a\ud800b", "\udfff", "x\udc80y"])
def test_custom_instruction_rejects_nul_and_surrogates(text: str) -> None:
    error = _error(_raw(custom_instruction=text))
    assert (error.field, error.code) == ("custom_instruction", CUSTOM_INSTRUCTION_INVALID)


def test_custom_instruction_is_kept_exactly_as_typed() -> None:
    text = "  Смішний момент:\nреакція\tта  е́  "
    assert parse_search_profile(_raw(custom_instruction=text)).custom_instruction == text


def test_custom_instruction_empty_string_accepted() -> None:
    assert parse_search_profile(_raw(custom_instruction="")).custom_instruction == ""


@pytest.mark.parametrize("value", [None, 5, ["текст"]])
def test_custom_instruction_must_be_text(value: object) -> None:
    error = _error(_raw(custom_instruction=value))
    assert (error.field, error.code) == ("custom_instruction", PROFILE_INVALID)


@pytest.mark.parametrize("field", ["min_duration_ms", "max_duration_ms"])
@pytest.mark.parametrize("value", [0, 4999, 600001, -1])
def test_duration_out_of_range_rejected(field: str, value: int) -> None:
    error = _error(_raw(**{field: value}))
    assert (error.field, error.code) == (field, INVALID_DURATION)


@pytest.mark.parametrize(("min_ms", "max_ms"), [(5000, 5000), (5000, 600000), (600000, 600000)])
def test_duration_boundaries_accepted(min_ms: int, max_ms: int) -> None:
    profile = parse_search_profile(_raw(min_duration_ms=min_ms, max_duration_ms=max_ms))
    assert (profile.min_duration_ms, profile.max_duration_ms) == (min_ms, max_ms)


@pytest.mark.parametrize(("min_ms", "max_ms"), [(60000, 59999), (600000, 5000)])
def test_min_duration_above_max_rejected(min_ms: int, max_ms: int) -> None:
    error = _error(_raw(min_duration_ms=min_ms, max_duration_ms=max_ms))
    assert (error.field, error.code) == ("max_duration_ms", DURATION_RANGE)


@pytest.mark.parametrize("value", [0, 101, -1])
def test_max_candidates_out_of_range_rejected(value: int) -> None:
    error = _error(_raw(max_candidates=value))
    assert (error.field, error.code) == ("max_candidates", INVALID_MAX_CANDIDATES)


@pytest.mark.parametrize("value", [1, 100])
def test_max_candidates_boundaries_accepted(value: int) -> None:
    assert parse_search_profile(_raw(max_candidates=value)).max_candidates == value


@pytest.mark.parametrize("value", [True, False])
def test_allow_partial_accepts_bool(value: bool) -> None:
    assert parse_search_profile(_raw(allow_partial=value)).allow_partial is value


@pytest.mark.parametrize("value", [1, 0, "false", None])
def test_allow_partial_must_be_bool(value: object) -> None:
    error = _error(_raw(allow_partial=value))
    assert (error.field, error.code) == ("allow_partial", PROFILE_INVALID)


@pytest.mark.parametrize("field", ["min_duration_ms", "max_duration_ms", "max_candidates"])
@pytest.mark.parametrize("value", [True, False, 5000.0, "5000", None])
def test_integer_fields_reject_bool_float_text_and_null(field: str, value: object) -> None:
    error = _error(_raw(**{field: value}))
    assert (error.field, error.code) == (field, PROFILE_INVALID)


def test_true_is_not_an_integer_for_max_candidates() -> None:
    error = _error(_raw(max_candidates=True))
    assert (error.field, error.code) == ("max_candidates", PROFILE_INVALID)


def test_missing_key_rejected_with_no_field() -> None:
    raw = _raw()
    del raw["allow_partial"]
    error = _error(raw)
    assert (error.field, error.code) == (None, PROFILE_INVALID)


def test_extra_key_rejected_with_no_field() -> None:
    raw = _raw()
    raw["model"] = "test/selector"
    error = _error(raw)
    assert (error.field, error.code) == (None, PROFILE_INVALID)


@pytest.mark.parametrize("value", [None, [], "profile", 5])
def test_non_object_rejected_with_no_field(value: object) -> None:
    error = _error(value)
    assert (error.field, error.code) == (None, PROFILE_INVALID)


def test_error_carries_field_code_and_ukrainian_message() -> None:
    error = _error(_raw(max_candidates=101))
    assert error.field == "max_candidates"
    assert error.code == INVALID_MAX_CANDIDATES
    assert error.message_uk == "Кількість моментів має бути від 1 до 100"


@pytest.mark.parametrize(
    "profile",
    [
        DEFAULT_PROFILE,
        SearchProfile(
            categories=("action",),
            custom_instruction="  Текст\nз рядками  ",
            min_duration_ms=5000,
            max_duration_ms=5000,
            max_candidates=1,
            allow_partial=True,
        ),
        SearchProfile(
            categories=CATEGORIES,
            custom_instruction="\U0001f600" * 2000,
            min_duration_ms=600000,
            max_duration_ms=600000,
            max_candidates=100,
            allow_partial=False,
        ),
    ],
)
def test_profile_json_round_trip(profile: SearchProfile) -> None:
    payload = profile_to_json(profile)
    assert tuple(payload) == PROFILE_FIELDS
    assert payload["categories"] == list(profile.categories)
    restored = parse_search_profile(json.loads(json.dumps(payload, ensure_ascii=False)))
    assert restored == profile
