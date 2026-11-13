"""Local access control: Host check and session Bearer token.

Pure ASGI middleware so that it runs before routing: an unauthenticated
request gets 401 whether or not the route exists, and no handler or data
access happens before both checks pass.
"""

from __future__ import annotations

import hmac

from starlette.types import ASGIApp, Receive, Scope, Send

from clipforge.api.errors import error_response

LOOPBACK_HOST = "127.0.0.1"


def allowed_hosts(port: int) -> frozenset[str]:
    """Host header values a local client may send (DNS rebinding defence)."""
    return frozenset({f"{LOOPBACK_HOST}:{port}", f"localhost:{port}"})


class LocalAccessMiddleware:
    def __init__(self, app: ASGIApp, *, session_token: str, allowed_hosts: frozenset[str]) -> None:
        self._app = app
        self._token = session_token.encode("ascii")
        self._allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope_type != "http":
            # Default deny: the API has no websocket endpoints.
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return

        headers: list[tuple[bytes, bytes]] = scope["headers"]
        hosts = [value for name, value in headers if name == b"host"]
        if len(hosts) != 1 or hosts[0].decode("latin-1").lower() not in self._allowed_hosts:
            response = error_response(400, "INVALID_HOST", "Недозволений заголовок Host")
            await response(scope, receive, send)
            return

        if not self._is_authorized(headers):
            response = error_response(
                401,
                "UNAUTHORIZED",
                "Потрібен дійсний токен сесії",
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)

    def _is_authorized(self, headers: list[tuple[bytes, bytes]]) -> bool:
        values = [value for name, value in headers if name == b"authorization"]
        if len(values) != 1:
            return False
        scheme, _, credentials = values[0].partition(b" ")
        if scheme.lower() != b"bearer":
            return False
        return hmac.compare_digest(credentials, self._token)
