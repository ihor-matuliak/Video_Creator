"""Validation of free text that comes from a request (project names, titles, paths)."""

from __future__ import annotations

from clipforge.services.errors import ServiceError, invalid


def is_storable(text: str) -> bool:
    """False for NUL and lone surrogates: SQLite and the OS cannot hold them reliably."""
    if "\0" in text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def clean_text(value: str, *, field: str, code: str, label: str, max_length: int) -> str:
    """Trim the text and require ``1..max_length`` characters (code points)."""
    text = value.strip()
    if not is_storable(text):
        raise _error(code, f"{label} містить недопустимі символи", field)
    if not 1 <= len(text) <= max_length:
        raise _error(code, f"{label} має містити від 1 до {max_length} символів", field)
    return text


def _error(code: str, message: str, field: str) -> ServiceError:
    return invalid(code, message, field=field)
