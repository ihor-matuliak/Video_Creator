"""Read-only byte-range streaming of a registered source file (docs/04 section 4).

Single ``Range`` only (``bytes=a-b``, ``bytes=a-``, ``bytes=-n``): 200 without a Range header, 206
with a satisfiable one, 416 for an unsatisfiable, multi-range or malformed header. Responses never
carry a path or an exception text.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO, Final

from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from clipforge.api.errors import error_response
from clipforge.services.sources import OpenedMedia

CHUNK_SIZE: Final = 1024 * 1024

_CONTENT_TYPES: Final = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".ts": "video/mp2t",
}
_DEFAULT_CONTENT_TYPE: Final = "application/octet-stream"
# ASCII digits only: ``\d`` would also accept other Unicode digits.
_RANGE: Final = re.compile(r"bytes=([0-9]+)?-([0-9]+)?", re.IGNORECASE)
# Anything longer than this is far beyond any file size; clamping keeps int() cheap and safe.
_MAX_DIGITS: Final = 18


@dataclass(frozen=True, slots=True)
class ByteRange:
    """Inclusive ``first``..``last`` byte positions."""

    first: int
    last: int

    @property
    def length(self) -> int:
        return self.last - self.first + 1


class RangeNotSatisfiableError(Exception):
    """The Range header cannot be served for this file (also for multi-range or malformed)."""


def content_type_for(extension: str) -> str:
    return _CONTENT_TYPES.get(extension.lower(), _DEFAULT_CONTENT_TYPE)


def parse_byte_range(header: str, size: int) -> ByteRange:
    match = _RANGE.fullmatch(header.strip())
    if match is None:  # also a comma (multi-range), another unit, or an empty spec
        raise RangeNotSatisfiableError
    first_text, last_text = match.groups()
    if first_text is None:
        if last_text is None:
            raise RangeNotSatisfiableError
        suffix = _to_int(last_text)
        if suffix == 0 or size == 0:
            raise RangeNotSatisfiableError
        return ByteRange(max(size - suffix, 0), size - 1)

    first = _to_int(first_text)
    if first >= size:
        raise RangeNotSatisfiableError
    if last_text is None:
        return ByteRange(first, size - 1)
    last = _to_int(last_text)
    if last < first:
        raise RangeNotSatisfiableError
    return ByteRange(first, min(last, size - 1))


def _to_int(digits: str) -> int:
    return int(digits) if len(digits) <= _MAX_DIGITS else 10**_MAX_DIGITS


def media_response(media: OpenedMedia, range_header: str | None) -> Response:
    """The response for ``media``; it owns ``media.file`` and closes it (also on errors)."""
    base_headers = {"Accept-Ranges": "bytes"}
    try:
        if range_header is None:
            selected, status = ByteRange(0, media.size - 1), 200
        else:
            selected, status = parse_byte_range(range_header, media.size), 206
    except RangeNotSatisfiableError:
        media.file.close()
        return _not_satisfiable(media.size, base_headers)
    except BaseException:
        media.file.close()
        raise

    headers = {**base_headers, "Content-Length": str(selected.length)}
    if status == 206:
        headers["Content-Range"] = f"bytes {selected.first}-{selected.last}/{media.size}"
    try:
        media.file.seek(selected.first)
    except BaseException:
        media.file.close()
        raise
    return _ClosingStreamingResponse(
        media.file,
        _read_chunks(media.file, selected.length),
        status_code=status,
        headers=headers,
        media_type=content_type_for(media.extension),
    )


def _not_satisfiable(size: int, headers: dict[str, str]) -> JSONResponse:
    return error_response(
        416,
        "RANGE_NOT_SATISFIABLE",
        "Запитаний діапазон байтів недоступний",
        headers={**headers, "Content-Range": f"bytes */{size}"},
    )


def _read_chunks(handle: BinaryIO, length: int) -> Iterator[bytes]:
    remaining = length
    while remaining > 0:
        chunk = handle.read(min(CHUNK_SIZE, remaining))
        if not chunk:
            # The file shrank after the check. The declared Content-Length cannot be met:
            # abort the response instead of sending a short body.
            raise EOFError
        remaining -= len(chunk)
        yield chunk


class _ClosingStreamingResponse(StreamingResponse):
    """Closes the file when the response ends for any reason, even if streaming never started."""

    def __init__(
        self,
        file: BinaryIO,
        content: Iterator[bytes],
        *,
        status_code: int,
        headers: dict[str, str],
        media_type: str,
    ) -> None:
        super().__init__(content, status_code=status_code, headers=headers, media_type=media_type)
        self._file = file

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._file.close()
