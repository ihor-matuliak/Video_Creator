"""Small helpers shared by the storage modules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def new_id() -> str:
    """ADR 017: identifiers are uuid4 strings."""
    return str(uuid.uuid4())


def utc_now() -> str:
    """ISO 8601 UTC with fixed width, so lexicographic order equals chronological order."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utc_stamp() -> str:
    """Same instant as ``utc_now`` but safe inside a Windows file name."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
