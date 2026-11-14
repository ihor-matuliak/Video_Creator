"""Column types."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Dialect, Text
from sqlalchemy.types import TypeDecorator

from .errors import StorageError

JSON_SCHEMA_VERSION = 1


class VersionedJson(TypeDecorator[Any]):
    """JSON stored as ``{"schema_version": 1, "value": ...}`` text (docs/04 section 3).

    SQL NULL is kept for Python ``None``. Values are replaced as a whole: in-place mutation of a
    loaded list or dict is not tracked, so assign a new object instead.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return json.dumps(
            {"schema_version": JSON_SCHEMA_VERSION, "value": value},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    def process_result_value(self, value: str | None, dialect: Dialect) -> Any | None:
        if value is None:
            return None
        try:
            document = json.loads(value)
        except ValueError:
            raise StorageError("Збережений JSON пошкоджений") from None
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != JSON_SCHEMA_VERSION
            or "value" not in document
        ):
            raise StorageError("Збережений JSON має невідому версію схеми")
        return document["value"]
