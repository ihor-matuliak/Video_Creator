"""State of a registered source's file on disk (FR-004).

Kept apart from ``services.sources`` because ``services.jobs`` needs it at start-up and
``services.sources`` already imports ``services.jobs``. Never logs a path or an exception text.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Final

from clipforge.storage import Source

AVAILABLE: Final = "available"
MISSING: Final = "missing"
CHANGED: Final = "changed"


def current_availability(source: Source) -> str:
    """``available`` when the file at the stored path is a regular file with the stored size and
    mtime_ns, ``changed`` when it exists with other facts, ``missing`` otherwise."""
    try:
        current = Path(source.path).stat()
    except OSError:
        return MISSING
    if not stat.S_ISREG(current.st_mode):
        return MISSING
    if current.st_size != source.size or current.st_mtime_ns != source.mtime:
        return CHANGED
    return AVAILABLE
