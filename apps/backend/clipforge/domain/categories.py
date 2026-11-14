"""Clip categories (FR-007): the closed set shared by clips, profiles and selection contracts.

Pure validation: no I/O. A clip may have no category (manual clips), but never an unknown or
repeated one.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

CATEGORIES: Final = ("funny", "action", "fails", "unexpected", "game_details")

CATEGORY_NOT_STRING: Final = "CATEGORY_NOT_STRING"
CATEGORY_UNKNOWN: Final = "CATEGORY_UNKNOWN"
CATEGORY_DUPLICATE: Final = "CATEGORY_DUPLICATE"


class CategoryError(ValueError):
    """Categories are invalid. ``code`` is stable, ``message`` is Ukrainian."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def validate_categories(values: Sequence[object]) -> tuple[str, ...]:
    """Return ``values`` as a tuple of known, unique categories; an empty sequence is valid."""
    checked: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise CategoryError(CATEGORY_NOT_STRING, "Категорія має бути текстом")
        if value not in CATEGORIES:
            raise CategoryError(CATEGORY_UNKNOWN, "Невідома категорія")
        if value in checked:
            raise CategoryError(CATEGORY_DUPLICATE, "Категорії не мають повторюватися")
        checked.append(value)
    return tuple(checked)
