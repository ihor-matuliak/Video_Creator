"""Shared query parameters of list routes (docs/04 section 4): ``limit`` and ``cursor``."""

from __future__ import annotations

from typing import Annotated

from fastapi import Query

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=200)]
