"""Source identity: quick fingerprint and streaming SHA-256 (docs/01 FR-004).

The quick fingerprint is a cheap change detector, not proof of identity: it hashes the
size and three 1 MiB samples (start, middle, end). Only the full SHA-256 proves that two
files are the same. Both functions open the file read-only and never hold it in memory.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Final

QUICK_FINGERPRINT_VERSION: Final = "qf1"
QUICK_SAMPLE_BYTES: Final = 1 << 20
HASH_CHUNK_BYTES: Final = 8 << 20


class HashCancelledError(Exception):
    """``should_cancel`` returned true; no digest was produced."""


def quick_fingerprint(path: Path) -> str:
    """``qf1:<sha256 hex>`` over the size and the start/middle/end samples."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        digest.update(QUICK_FINGERPRINT_VERSION.encode("ascii"))
        digest.update(size.to_bytes(8, "little"))
        if size <= 3 * QUICK_SAMPLE_BYTES:
            handle.seek(0)
            digest.update(handle.read())
        else:
            for offset in (0, (size - QUICK_SAMPLE_BYTES) // 2, size - QUICK_SAMPLE_BYTES):
                handle.seek(offset)
                digest.update(handle.read(QUICK_SAMPLE_BYTES))
    return f"{QUICK_FINGERPRINT_VERSION}:{digest.hexdigest()}"


def sha256_file(
    path: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
    chunk_bytes: int = HASH_CHUNK_BYTES,
) -> str:
    """Hex SHA-256 of the whole file, read in chunks.

    ``should_cancel`` is checked before every chunk; on true ``HashCancelledError``
    is raised and the file is closed.
    """
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    digest = hashlib.sha256()
    buffer = bytearray(chunk_bytes)
    view = memoryview(buffer)
    with path.open("rb", buffering=0) as handle:
        while True:
            if should_cancel is not None and should_cancel():
                raise HashCancelledError
            read = handle.readinto(view)
            if not read:
                break
            digest.update(view[:read])
    return digest.hexdigest()
