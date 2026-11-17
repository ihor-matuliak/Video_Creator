"""Search profile (ADR 041 §10, contracts/search-profile.schema.json): the input of AI selection.

Pure validation: no I/O. ``parse_search_profile`` accepts exactly the six documented keys and
returns a frozen profile whose categories are in canonical order. ``custom_instruction`` is kept
exactly as typed; it is only ever passed to the model as data.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Final

from clipforge.domain.categories import CATEGORIES, CategoryError, validate_categories

PROFILE_INVALID: Final = "PROFILE_INVALID"
CATEGORIES_REQUIRED: Final = "CATEGORIES_REQUIRED"
CUSTOM_INSTRUCTION_TOO_LONG: Final = "CUSTOM_INSTRUCTION_TOO_LONG"
CUSTOM_INSTRUCTION_INVALID: Final = "CUSTOM_INSTRUCTION_INVALID"
INVALID_DURATION: Final = "INVALID_DURATION"
DURATION_RANGE: Final = "DURATION_RANGE"
INVALID_MAX_CANDIDATES: Final = "INVALID_MAX_CANDIDATES"

CUSTOM_INSTRUCTION_MAX_LENGTH: Final = 2000
DURATION_MIN_MS: Final = 5000
DURATION_MAX_MS: Final = 600000
MAX_CANDIDATES_MIN: Final = 1
MAX_CANDIDATES_MAX: Final = 100

_PROFILE_KEYS: Final = frozenset(
    {
        "categories",
        "custom_instruction",
        "min_duration_ms",
        "max_duration_ms",
        "max_candidates",
        "allow_partial",
    }
)


class ProfileError(ValueError):
    """The search profile is invalid. ``code`` is stable, ``message_uk`` is Ukrainian.

    ``field`` names the offending profile key, or is None when the whole object has the wrong shape.
    """

    def __init__(self, field: str | None, code: str, message_uk: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code
        self.message_uk = message_uk


@dataclass(frozen=True)
class SearchProfile:
    categories: tuple[str, ...]
    custom_instruction: str
    min_duration_ms: int
    max_duration_ms: int
    max_candidates: int
    allow_partial: bool


DEFAULT_PROFILE: Final = SearchProfile(
    categories=CATEGORIES,
    custom_instruction="",
    min_duration_ms=15000,
    max_duration_ms=90000,
    max_candidates=20,
    allow_partial=False,
)


def parse_search_profile(value: object) -> SearchProfile:
    """Return ``value`` as a SearchProfile or raise ProfileError for the first violated rule."""
    if not isinstance(value, dict) or frozenset(value) != _PROFILE_KEYS:
        raise ProfileError(
            None,
            PROFILE_INVALID,
            "Профіль пошуку має містити рівно поля: categories, custom_instruction, "
            "min_duration_ms, max_duration_ms, max_candidates, allow_partial",
        )
    categories = _categories(value["categories"])
    custom_instruction = _custom_instruction(value["custom_instruction"])
    min_duration_ms = _duration(value["min_duration_ms"], "min_duration_ms")
    max_duration_ms = _duration(value["max_duration_ms"], "max_duration_ms")
    if min_duration_ms > max_duration_ms:
        raise ProfileError(
            "max_duration_ms",
            DURATION_RANGE,
            "Мінімальна тривалість не може перевищувати максимальну",
        )
    max_candidates = _max_candidates(value["max_candidates"])
    allow_partial = value["allow_partial"]
    if not isinstance(allow_partial, bool):
        raise ProfileError("allow_partial", PROFILE_INVALID, "Значення має бути true або false")
    return SearchProfile(
        categories=categories,
        custom_instruction=custom_instruction,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
        max_candidates=max_candidates,
        allow_partial=allow_partial,
    )


def profile_to_json(profile: SearchProfile) -> dict[str, object]:
    """Return the JSON object of ``profile`` with the six documented keys."""
    return {
        "categories": list(profile.categories),
        "custom_instruction": profile.custom_instruction,
        "min_duration_ms": profile.min_duration_ms,
        "max_duration_ms": profile.max_duration_ms,
        "max_candidates": profile.max_candidates,
        "allow_partial": profile.allow_partial,
    }


def _categories(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProfileError("categories", PROFILE_INVALID, "Категорії мають бути списком")
    if not value:
        raise ProfileError("categories", CATEGORIES_REQUIRED, "Оберіть хоча б одну категорію")
    try:
        checked = validate_categories(value)
    except CategoryError as error:
        raise ProfileError("categories", error.code, error.message) from error
    return tuple(category for category in CATEGORIES if category in checked)


def _custom_instruction(value: object) -> str:
    if not isinstance(value, str):
        raise ProfileError(
            "custom_instruction", PROFILE_INVALID, "Власна інструкція має бути текстом"
        )
    # len() counts code points, not UTF-16 units or bytes.
    if len(value) > CUSTOM_INSTRUCTION_MAX_LENGTH:
        raise ProfileError(
            "custom_instruction",
            CUSTOM_INSTRUCTION_TOO_LONG,
            f"Власна інструкція має бути не довшою за {CUSTOM_INSTRUCTION_MAX_LENGTH} символів",
        )
    for char in value:
        if char == "\x00" or unicodedata.category(char) == "Cs":
            raise ProfileError(
                "custom_instruction",
                CUSTOM_INSTRUCTION_INVALID,
                "Власна інструкція містить недопустимі символи",
            )
    return value


def _duration(value: object, field: str) -> int:
    duration = _int(value, field)
    if not DURATION_MIN_MS <= duration <= DURATION_MAX_MS:
        raise ProfileError(
            field,
            INVALID_DURATION,
            f"Тривалість має бути від {DURATION_MIN_MS} до {DURATION_MAX_MS} мс",
        )
    return duration


def _max_candidates(value: object) -> int:
    candidates = _int(value, "max_candidates")
    if not MAX_CANDIDATES_MIN <= candidates <= MAX_CANDIDATES_MAX:
        raise ProfileError(
            "max_candidates",
            INVALID_MAX_CANDIDATES,
            f"Кількість моментів має бути від {MAX_CANDIDATES_MIN} до {MAX_CANDIDATES_MAX}",
        )
    return candidates


def _int(value: object, field: str) -> int:
    # bool is a subclass of int in Python, but true/false are not integers in the contract.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(field, PROFILE_INVALID, "Значення має бути цілим числом")
    return value
