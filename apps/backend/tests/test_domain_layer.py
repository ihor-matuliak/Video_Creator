"""ADR 017: the domain layer is pure and imports only the standard library."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "clipforge" / "domain"
DOMAIN_FILES = sorted(DOMAIN_DIR.glob("*.py"))


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_domain_modules_exist() -> None:
    names = {path.stem for path in DOMAIN_FILES}
    assert {"timecode", "interval", "naming", "catalog"} <= names


@pytest.mark.parametrize("path", DOMAIN_FILES, ids=lambda path: path.name)
def test_domain_imports_only_stdlib(path: Path) -> None:
    allowed = set(sys.stdlib_module_names) | {"__future__", "clipforge"}
    roots = imported_roots(path)
    assert roots <= allowed, roots - allowed
    assert not roots & {"fastapi", "sqlalchemy", "starlette", "pydantic", "alembic"}
