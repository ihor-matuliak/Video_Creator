"""OpenRouter API key check (FR-048, docs/04 section 4).

``check_openrouter_key`` sends one ``GET <base>/key`` with the key in the ``Authorization`` header
and turns the answer into ``KeyAccepted`` (200) or ``KeyRejected`` (401). One call is one attempt:
no retry, no redirect, no pause. Every other result is a ``KeyCheckError``. Whether OpenRouter
bills ``GET /key`` is not documented (ADR 036), so nothing here repeats the call: the caller asks
again only on an explicit action of the user.

One deadline (``CHECK_TIMEOUT_S``) covers the whole call: connecting, the TLS handshake, sending
the request, waiting for the headers and reading the body. Every socket operation is cut down to
what is left of it, so a server that answers byte by byte cannot keep the call open. When it
expires the connection is closed and nothing is left running. (Name resolution is one call into
the operating system and cannot be interrupted; the deadline is checked right after it.)

The environment is not trusted (``trust_env=False``): proxy variables and ``SSL_CERT_*`` are
ignored, and the key goes straight to the address that passed ``check_base_url``. The request
asks for ``Accept-Encoding: identity``, and the body is read undecoded (``iter_raw``) whatever the
server answers: a server that ignores ``identity`` and sends a compressed body gets no decoding,
the limit of ``MAX_BODY_BYTES`` counts the bytes as they came over the wire, and the compressed
bytes are not valid JSON (``KeyCheckError`` of kind ``invalid_response``).

The base URL follows the STT adapter (``resolve_base_url``, ``check_base_url``): production sends
the key to OpenRouter only, test mode to a loopback test provider only, and nothing is sent when
the mode refuses the address.

Only ``is_free_tier`` and ``limit_remaining`` of the answer are used. The rest (``label``, which
may carry a part of the key, ``usage``, ``limit`` ...) is dropped. Neither the key nor any text of
the answer goes into a message, ``repr``, a log line or an exception chain: messages are fixed
Ukrainian texts and transport exceptions are never chained (their request carries the headers).
This module does not log.
"""

from __future__ import annotations

import contextlib
import json
import math
import socket
import ssl
import time
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal

import httpcore2
import httpx2

from clipforge.adapters.openrouter_stt import check_base_url
from clipforge.config import Mode

KEY_PATH: Final = "/key"
# One value for connect, read, write and pool, and also the deadline of the whole call.
CHECK_TIMEOUT_S: Final = 15.0
# A real answer is a few hundred bytes; anything bigger is not what we asked for.
MAX_BODY_BYTES: Final = 64 * 1024

KeyCheckErrorKind = Literal["not_sent", "upstream", "invalid_response"]

_MESSAGES: Final = MappingProxyType(
    {
        "not_sent": "Не вдалося з'єднатися з OpenRouter: запит не надіслано, ключ не перевірено.",
        "upstream": (
            "OpenRouter не відповів вчасно або повернув помилку сервера: ключ не перевірено."
        ),
        "invalid_response": (
            "OpenRouter повернув відповідь, яку не можна використати: ключ не перевірено."
        ),
    }
)

# Failures that happen before the request is sent. Anything later (read timeout, a reset, a
# body that cannot be read) is ``upstream``: the request may already have reached OpenRouter.
_NOT_SENT: Final = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


class KeyCheckError(Exception):
    """The key could not be checked. ``kind`` is stable, the message is Ukrainian and fixed.

    ``status`` is the HTTP status of an answer that cannot be used; it is shown in the message.
    """

    def __init__(self, kind: KeyCheckErrorKind, *, status: int | None = None) -> None:
        message = _MESSAGES[kind]
        if status is not None:
            message = f"{message} (HTTP {status})"
        super().__init__(message)
        self.kind: KeyCheckErrorKind = kind
        self.status = status
        self.message = message

    def __repr__(self) -> str:
        return f"KeyCheckError(kind={self.kind!r}, status={self.status!r})"


@dataclass(frozen=True, slots=True)
class KeyAccepted:
    """OpenRouter accepted the key (200). ``limit_remaining_usd`` is a plain decimal string
    (never an exponent) and ``None`` when the key has no spending limit."""

    is_free_tier: bool
    limit_remaining_usd: str | None


@dataclass(frozen=True, slots=True)
class KeyRejected:
    """OpenRouter does not know the key (401)."""


KeyCheckResult = KeyAccepted | KeyRejected


def check_openrouter_key(api_key: str, *, mode: Mode, base_url: str) -> KeyCheckResult:
    """Ask OpenRouter once whether ``api_key`` is accepted. Never retries.

    Raises ``ValueError`` for a key that is not printable ASCII without spaces and ``ConfigError``
    for an address the mode does not allow (nothing is sent in both cases) and ``KeyCheckError``
    for every failure on the way and every answer other than 200 and 401.
    """
    if not api_key or not all(0x21 <= ord(char) <= 0x7E for char in api_key):
        raise ValueError("Ключ API порожній або містить пробіли чи недрукований символ")
    check_base_url(mode, base_url)
    status, content = _fetch(base_url.rstrip("/") + KEY_PATH, api_key)
    if status == 401:
        return KeyRejected()
    if status == 200:
        return _parse_key_info(content)
    raise KeyCheckError("upstream" if status >= 500 else "invalid_response", status=status)


class _Deadline:
    """The end of one call on the monotonic clock."""

    __slots__ = ("_end",)

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def limit(self, timeout: float | None, expired: type[httpcore2.TimeoutException]) -> float:
        """``timeout`` cut down to what is left of the deadline (``None``: all that is left).

        Raises ``expired`` when nothing is left: a socket timeout of 0 means "do not wait", so
        it is never handed on.
        """
        left = self._end - time.monotonic()
        if left <= 0:
            raise expired("The deadline of the key check has passed")
        return left if timeout is None else min(timeout, left)


class _DeadlineStream(httpcore2.NetworkStream):
    """A connection whose every read, write and TLS handshake stops at the deadline."""

    def __init__(self, inner: httpcore2.NetworkStream, deadline: _Deadline) -> None:
        self._inner = inner
        self._deadline = deadline

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        wait = self._deadline.limit(timeout, httpcore2.ReadTimeout)
        return self._inner.read(max_bytes, timeout=wait)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        wait = self._deadline.limit(timeout, httpcore2.WriteTimeout)
        self._inner.write(buffer, timeout=wait)

    def close(self) -> None:
        self._inner.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.NetworkStream:
        wait = self._deadline.limit(timeout, httpcore2.ConnectTimeout)
        secured = self._inner.start_tls(ssl_context, server_hostname, wait)
        return _DeadlineStream(secured, self._deadline)

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _DeadlineBackend(httpcore2.NetworkBackend):
    """The standard TCP backend with every operation cut down to the deadline."""

    def __init__(self, deadline: _Deadline) -> None:
        self._deadline = deadline
        self._inner = httpcore2.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        # One attempt per address of the name, so that a name with several addresses cannot
        # spend the connect timeout once per address: the deadline is shared by all of them.
        failure: httpcore2.ConnectError | httpcore2.ConnectTimeout | None = None
        for address in _addresses(host, port):
            wait = self._deadline.limit(timeout, httpcore2.ConnectTimeout)
            try:
                stream = self._inner.connect_tcp(
                    address,
                    port,
                    timeout=wait,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as exc:
                failure = exc
                continue
            return _DeadlineStream(stream, self._deadline)
        raise failure or httpcore2.ConnectError("The provider has no address to connect to")


def _addresses(host: str, port: int) -> list[str]:
    """The distinct addresses of ``host`` in the order the system prefers them."""
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise httpcore2.ConnectError("The address of the provider cannot be resolved") from None
    return list(dict.fromkeys(str(entry[4][0]) for entry in found))


class _DeadlineTransport(httpx2.HTTPTransport):
    """The default httpx2 transport (environment not trusted) on a deadline-bound backend.

    httpx2 has no option for a network backend. ``network_backend`` of the httpcore2 pool is the
    documented hook, and the pool is the one object this transport builds in its constructor, so
    the backend is set there. A change of that attribute in a new httpx2 would make the tests of
    the deadline fail.
    """

    def __init__(self, deadline: _Deadline) -> None:
        super().__init__(trust_env=False)
        self._pool._network_backend = _DeadlineBackend(deadline)


def _fetch(url: str, api_key: str) -> tuple[int, bytes]:
    """One GET. The body is read (and bounded) only for a 200: no other body is ever used."""
    timeout_s = CHECK_TIMEOUT_S
    deadline = _Deadline(timeout_s)
    failure: KeyCheckErrorKind
    try:
        with (
            httpx2.Client(
                transport=_DeadlineTransport(deadline),
                timeout=httpx2.Timeout(timeout_s),
                follow_redirects=False,
                trust_env=False,
            ) as http,
            http.stream(
                "GET",
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response,
        ):
            status = response.status_code
            if status != 200:
                return status, b""
            body = bytearray()
            # Undecoded: the size limit is on the wire bytes, so a small compressed body that
            # would inflate to a huge one is never inflated here.
            for chunk in response.iter_raw():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    failure = "invalid_response"
                    break
            else:
                return status, bytes(body)
    except httpx2.HTTPError as exc:
        failure = "not_sent" if isinstance(exc, _NOT_SENT) else "upstream"
    # Raised outside the handler: the exception holds the request with the key header.
    raise KeyCheckError(failure)


def _parse_key_info(content: bytes) -> KeyAccepted:
    """Validate a 200 body: ``{"data": {"is_free_tier": bool, "limit_remaining": number | null}}``.

    ``limit_remaining`` may be missing (same as ``null``: no limit). A wrong type, or a missing
    or non-boolean ``is_free_tier``, makes the whole answer unusable.
    """
    payload: object = None
    parsed = False
    # The decoder error holds a copy of the body: it is swallowed, and the error is raised
    # outside the handler so that it carries no chained context.
    with contextlib.suppress(ValueError, RecursionError):
        payload = json.loads(content)
        parsed = True
    data = payload.get("data") if parsed and isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise KeyCheckError("invalid_response")
    is_free_tier = data.get("is_free_tier")
    if not isinstance(is_free_tier, bool):
        raise KeyCheckError("invalid_response")
    remaining = data.get("limit_remaining")
    if remaining is None:
        return KeyAccepted(is_free_tier=is_free_tier, limit_remaining_usd=None)
    text = _decimal_string(remaining)
    if text is None:
        raise KeyCheckError("invalid_response")
    return KeyAccepted(is_free_tier=is_free_tier, limit_remaining_usd=text)


def _decimal_string(value: object) -> str | None:
    """A finite JSON number as a plain decimal string without trailing zeros; else ``None``.

    The decimal comes from the shortest round-trip form of a float (no binary noise) and is
    written without an exponent. A sign is kept: a limit that was overspent is slightly negative.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    amount = Decimal(value) if isinstance(value, int) else Decimal(repr(value))
    if amount == 0:
        return "0"
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
