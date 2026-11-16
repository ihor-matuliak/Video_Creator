"""Errors raised by the services layer.

``ServiceError`` is the only way a service reports an expected failure. It carries a stable
``code`` and a Ukrainian ``message`` written for the user; the API layer maps ``kind`` to an HTTP
status. Messages are always fixed text: never the text of a caught exception, a path, a stored
value or anything that came from a request.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class ErrorKind(StrEnum):
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    # A limit of this app, not of a remote service: the spending ceiling of a run (docs/04
    # section 4: 429, a local rate/budget condition with its own domain code).
    LIMIT_EXCEEDED = "limit_exceeded"


class ServiceError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.kind = kind
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = retryable


def invalid(code: str, message: str, *, field: str) -> ServiceError:
    """422: the value of ``field`` is not acceptable."""
    return ServiceError(ErrorKind.INVALID, code, message, details={"field": field})


def invalid_cursor() -> ServiceError:
    return invalid("INVALID_CURSOR", "Невідомий курсор сторінки", field="cursor")


def not_found(code: str, message: str) -> ServiceError:
    return ServiceError(ErrorKind.NOT_FOUND, code, message)


def bad_request(code: str, message: str, *, retryable: bool = False) -> ServiceError:
    """400: the request is well formed but the action cannot be done."""
    return ServiceError(ErrorKind.BAD_REQUEST, code, message, retryable=retryable)


def conflict(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    retryable: bool = False,
) -> ServiceError:
    """409: the stored state does not allow the action."""
    return ServiceError(ErrorKind.CONFLICT, code, message, details=details, retryable=retryable)


def unavailable(code: str, message: str) -> ServiceError:
    """503: a dependency the action needs is not available."""
    return ServiceError(ErrorKind.UNAVAILABLE, code, message)


def media_tools_unavailable() -> ServiceError:
    """503 for every route that needs FFmpeg/FFprobe when start-up did not find them."""
    return unavailable(
        "MEDIA_TOOLS_UNAVAILABLE",
        "FFmpeg або FFprobe недоступні. Встановіть FFmpeg і перезапустіть застосунок",
    )
