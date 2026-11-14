"""Export catalog CSV (FR-042, T-049).

UTF-8 with BOM, ``;`` delimiter, CRLF rows, fixed column order. Every text
cell that a spreadsheet could read as a formula (leading ``=``, ``+``, ``-``,
``@``, tab, CR, LF, their full-width forms, also after leading whitespace)
is prefixed with ``'`` so it stays plain text. Integer columns are written
as digits only.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

CATALOG_COLUMNS: Final = (
    "source_id",
    "clip_id",
    "revision",
    "title",
    "categories",
    "start_ms",
    "end_ms",
    "duration_ms",
    "review_status",
    "path",
    "export_status",
)
CSV_DELIMITER: Final = ";"
CSV_LINE_TERMINATOR: Final = "\r\n"
CATEGORY_SEPARATOR: Final = ", "
UTF8_BOM: Final = "﻿"
FORMULA_PREFIX: Final = "'"
FORMULA_TRIGGERS: Final = frozenset("=+-@\t\r\n＝＋－＠")


def neutralize_formula(cell: str) -> str:
    """Prefix ``'`` when a spreadsheet could interpret ``cell`` as a formula."""
    if cell[:1] in FORMULA_TRIGGERS or cell.lstrip()[:1] in FORMULA_TRIGGERS:
        return f"{FORMULA_PREFIX}{cell}"
    return cell


def _text_cell(value: str) -> str:
    # Lone surrogates cannot be written as UTF-8; they become "?".
    safe = value.encode("utf-8", errors="replace").decode("utf-8")
    return neutralize_formula(safe)


def _require_int(value: object, name: str, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")


@dataclass(frozen=True, slots=True)
class CatalogRow:
    """One exported clip. ``duration_ms`` is derived from the interval."""

    source_id: str
    clip_id: str
    revision: int
    title: str
    categories: tuple[str, ...]
    start_ms: int
    end_ms: int
    review_status: str
    path: str
    export_status: str

    def __post_init__(self) -> None:
        for name in ("source_id", "clip_id", "title", "review_status", "path", "export_status"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a str")
        if not isinstance(self.categories, tuple) or not all(
            isinstance(category, str) for category in self.categories
        ):
            raise TypeError("categories must be a tuple of str")
        _require_int(self.revision, "revision", 1)
        _require_int(self.start_ms, "start_ms", 0)
        _require_int(self.end_ms, "end_ms", self.start_ms + 1)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def cells(self) -> tuple[str, ...]:
        """Cells in ``CATALOG_COLUMNS`` order, text cells neutralized."""
        return (
            _text_cell(self.source_id),
            _text_cell(self.clip_id),
            str(self.revision),
            _text_cell(self.title),
            _text_cell(CATEGORY_SEPARATOR.join(self.categories)),
            str(self.start_ms),
            str(self.end_ms),
            str(self.duration_ms),
            _text_cell(self.review_status),
            _text_cell(self.path),
            _text_cell(self.export_status),
        )


def render_catalog_csv(rows: Iterable[CatalogRow]) -> bytes:
    """Return the full CSV file content: UTF-8 BOM, header, one line per row."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(
        buffer,
        delimiter=CSV_DELIMITER,
        quotechar='"',
        quoting=csv.QUOTE_MINIMAL,
        lineterminator=CSV_LINE_TERMINATOR,
    )
    writer.writerow(CATALOG_COLUMNS)
    for row in rows:
        writer.writerow(row.cells())
    return (UTF8_BOM + buffer.getvalue()).encode("utf-8")
