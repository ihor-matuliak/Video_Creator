"""Error shape from docs/04_ARCHITECTURE_API.md section 4.

{"error": {"code", "message", "details", "retryable"}} with a Ukrainian message.
Responses and logs never carry exception text or echoed request input.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LOGGER = logging.getLogger(__name__)

_HTTP_ERRORS: dict[int, tuple[str, str]] = {
    404: ("NOT_FOUND", "Ресурс не знайдено"),
    405: ("METHOD_NOT_ALLOWED", "Метод не підтримується для цього ресурсу"),
}


class ApiError(Exception):
    """Domain error mapped to the section 4 response shape."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = retryable


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    retryable: bool = False,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": dict(details or {}),
            "retryable": retryable,
        }
    }
    return JSONResponse(body, status_code=status_code, headers=dict(headers or {}))


async def _handle_api_error(_: Request, exc: Exception) -> JSONResponse:
    error = cast(ApiError, exc)
    return error_response(
        error.status_code,
        error.code,
        error.message,
        details=error.details,
        retryable=error.retryable,
    )


async def _handle_http_error(_: Request, exc: Exception) -> JSONResponse:
    error = cast(StarletteHTTPException, exc)
    code, message = _HTTP_ERRORS.get(error.status_code, ("HTTP_ERROR", "Помилка запиту"))
    return error_response(error.status_code, code, message, headers=error.headers)


async def _handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    error = cast(RequestValidationError, exc)
    # Only location and type: pydantic's msg/input/ctx may echo request values.
    fields = [{"loc": list(item["loc"]), "type": item["type"]} for item in error.errors()]
    return error_response(
        422, "VALIDATION_ERROR", "Некоректні дані запиту", details={"fields": fields}
    )


class UnexpectedErrorMiddleware:
    """Turns any unhandled exception into a section 4 500 without leaking its text.

    Starlette's ServerErrorMiddleware always re-raises after its 500 handler, and
    uvicorn then logs the full traceback (exception message included) on
    ``uvicorn.error``. Exception messages may carry secrets or transcript text,
    so the exception is swallowed here, inside ServerErrorMiddleware: only the
    exception class name and a generated error id are logged, and the same id is
    returned in ``details.error_id`` so a failure can be matched to its log line.

    Must be the outermost middleware added with ``add_middleware``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, receive, tracking_send)
        except Exception as exc:
            error_id = secrets.token_hex(8)
            # No exc_info, no str(exc): see the class docstring.
            _LOGGER.error("Unhandled %s (error_id=%s)", type(exc).__name__, error_id)
            if response_started:
                # Headers are already on the wire; returning without completing the
                # response makes uvicorn close the connection (static log line only).
                return
            response = error_response(
                500,
                "INTERNAL_ERROR",
                "Внутрішня помилка сервера",
                details={"error_id": error_id},
            )
            await response(scope, receive, send)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
