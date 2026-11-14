"""FR-042/T-049: export catalog CSV and formula-injection neutralization."""

from __future__ import annotations

import csv
import io

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.domain.catalog import (
    CATALOG_COLUMNS,
    FORMULA_TRIGGERS,
    CatalogRow,
    neutralize_formula,
    render_catalog_csv,
)

EXPECTED_COLUMNS = (
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


def make_row(**overrides: object) -> CatalogRow:
    values: dict[str, object] = {
        "source_id": "src-1",
        "clip_id": "clip-1",
        "revision": 2,
        "title": "Смішний момент",
        "categories": ("funny", "fails"),
        "start_ms": 4120,
        "end_ms": 7880,
        "review_status": "approved",
        "path": "C:\\Exports\\Гра 1_clip-1_r2.mp4",
        "export_status": "done",
    }
    values.update(overrides)
    return CatalogRow(**values)  # type: ignore[arg-type]


def parse(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=";"))


def test_columns_match_fr042() -> None:
    assert CATALOG_COLUMNS == EXPECTED_COLUMNS


def test_bom_delimiter_header_and_crlf() -> None:
    data = render_catalog_csv([make_row()])
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8")[1:]
    lines = text.split("\r\n")
    assert lines[0] == ";".join(EXPECTED_COLUMNS)
    assert lines[-1] == ""
    assert len(lines) == 3


def test_row_values() -> None:
    rows = parse(render_catalog_csv([make_row()]))
    assert rows[1] == [
        "src-1",
        "clip-1",
        "2",
        "Смішний момент",
        "funny, fails",
        "4120",
        "7880",
        "3760",
        "approved",
        "C:\\Exports\\Гра 1_clip-1_r2.mp4",
        "done",
    ]


def test_empty_catalog_has_header_only() -> None:
    assert parse(render_catalog_csv([])) == [list(EXPECTED_COLUMNS)]


def test_delimiter_quotes_and_newlines_in_text_survive() -> None:
    title = 'Він сказав "так"; потім\nпішов'
    rows = parse(render_catalog_csv([make_row(title=title)]))
    assert rows[1][3] == title
    assert len(rows) == 2


@pytest.mark.parametrize(
    "cell",
    [
        '=HYPERLINK("http://x")',
        "=1+1",
        "+1+1",
        "-1+1",
        "@SUM(A1:A2)",
        "\t=1+1",
        "\r=1+1",
        "\n=1+1",
        "\tplain",
        "\rplain",
        " =1+1",
        "\u00a0=1+1",
        "\uff1d1+1",
        "\uff0b1",
        "\uff0d1",
        "\uff20x",
    ],
)
def test_formula_cells_are_neutralized(cell: str) -> None:
    assert neutralize_formula(cell) == "'" + cell


@pytest.mark.parametrize("cell", ["", "plain", "Матч 1", "1+1", "a=b", "C:\\x", "'quoted"])
def test_plain_cells_are_unchanged(cell: str) -> None:
    assert neutralize_formula(cell) == cell


@pytest.mark.parametrize(
    "field",
    ["source_id", "clip_id", "title", "review_status", "path", "export_status"],
)
@pytest.mark.parametrize("trigger", ["=", "+", "-", "@", "\t", "\r"])
def test_every_text_column_is_neutralized(field: str, trigger: str) -> None:
    payload = f"{trigger}cmd|' /C calc'!A0"
    rows = parse(render_catalog_csv([make_row(**{field: payload})]))
    cell = rows[1][EXPECTED_COLUMNS.index(field)]
    assert cell == "'" + payload


@pytest.mark.parametrize("trigger", ["=", "+", "-", "@", "\t", "\r"])
def test_categories_cell_is_neutralized(trigger: str) -> None:
    rows = parse(render_catalog_csv([make_row(categories=(f"{trigger}x", "funny"))]))
    assert rows[1][EXPECTED_COLUMNS.index("categories")] == f"'{trigger}x, funny"


def test_numeric_cells_are_plain_digits() -> None:
    rows = parse(render_catalog_csv([make_row(start_ms=0, end_ms=1)]))
    numeric = [
        rows[1][EXPECTED_COLUMNS.index(name)]
        for name in ("revision", "start_ms", "end_ms", "duration_ms")
    ]
    assert numeric == ["2", "0", "1", "1"]


def test_lone_surrogate_does_not_break_encoding() -> None:
    rows = parse(render_catalog_csv([make_row(title="a\ud800b")]))
    assert rows[1][3] == "a?b"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"revision": 0}, ValueError),
        ({"revision": True}, TypeError),
        ({"start_ms": -1}, ValueError),
        ({"start_ms": 1.0}, TypeError),
        ({"end_ms": 4120}, ValueError),
        ({"title": None}, TypeError),
        ({"categories": ["funny"]}, TypeError),
        ({"categories": ("funny", 1)}, TypeError),
    ],
)
def test_invalid_rows_are_rejected(overrides: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        make_row(**overrides)


@given(st.text())
def test_no_text_cell_starts_with_a_formula_trigger(title: str) -> None:
    rows = parse(render_catalog_csv([make_row(title=title)]))
    cell = rows[1][3]
    assert cell[:1] not in FORMULA_TRIGGERS
    assert cell.lstrip()[:1] not in FORMULA_TRIGGERS
