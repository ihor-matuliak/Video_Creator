"""The OpenRouter API key of one running sidecar (FR-048, docs/04 section 4).

The key lives in the memory of this object only. It is never written to SQLite, a file or a log,
never returned by the API, and ``repr`` shows only whether a key is set. The Electron main process
owns the persistent copy (OS encryption) and hands the key over after each start.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final, NoReturn

from pydantic import SecretStr

from clipforge.adapters.openrouter_key import (
    KeyCheckError,
    KeyCheckErrorKind,
    KeyCheckResult,
    check_openrouter_key,
)
from clipforge.adapters.openrouter_stt import resolve_base_url
from clipforge.config import Config, ConfigError
from clipforge.services.errors import ErrorKind, ServiceError, conflict, invalid

# Printable ASCII without a space: 0x21..0x7E.
_API_KEY: Final = re.compile(r"[\x21-\x7e]{20,200}")

_KEY_CHECK_CODES: Final[dict[KeyCheckErrorKind, str]] = {
    "not_sent": "KEY_CHECK_NOT_SENT",
    "upstream": "KEY_CHECK_UPSTREAM",
    "invalid_response": "KEY_CHECK_INVALID_RESPONSE",
}


def invalid_api_key() -> ServiceError:
    # Fixed text: the message must never echo any part of the rejected key.
    return invalid(
        "INVALID_API_KEY",
        "Ключ API має складатися з 20-200 друкованих ASCII-символів без пробілів",
        field="openrouter_api_key",
    )


class CredentialStore:
    def __init__(self) -> None:
        self._openrouter_key: SecretStr | None = None

    def set_openrouter_key(self, key: str) -> None:
        """Replace the key. Raises 422 ``INVALID_API_KEY`` and keeps the old key when ``key``
        is not 20-200 printable ASCII characters without a space."""
        if _API_KEY.fullmatch(key) is None:
            raise invalid_api_key()
        self._openrouter_key = SecretStr(key)

    def clear_openrouter_key(self) -> None:
        """Forget the key; nothing happens when there is none."""
        self._openrouter_key = None

    @property
    def has_openrouter_key(self) -> bool:
        return self._openrouter_key is not None

    def openrouter_key(self) -> str | None:
        """The key for the one caller that sends it to OpenRouter; ``None`` when not set."""
        secret = self._openrouter_key
        return None if secret is None else secret.get_secret_value()

    def __repr__(self) -> str:
        return f"CredentialStore(openrouter_key={'set' if self.has_openrouter_key else 'unset'})"


KeyChecker = Callable[[str], KeyCheckResult]
"""``api_key -> result``: how the key is checked. Production: ``openrouter_key_checker``; tests
point it at a local fake provider. Raises ``KeyCheckError``, ``ConfigError`` or ``ValueError``."""


def openrouter_key_checker(config: Config) -> KeyChecker:
    """The production checker: ``GET <resolve_base_url(config)>/key``.

    The address is resolved when the check runs, not here: test mode without a test provider is
    refused at that moment and nothing is sent (paid AI is never reachable from tests)."""

    def check(api_key: str) -> KeyCheckResult:
        return check_openrouter_key(api_key, mode=config.mode, base_url=resolve_base_url(config))

    return check


def unconfigured_key_checker(_api_key: str) -> NoReturn:
    """The checker of a runtime that was given no provider: every check is refused."""
    raise ConfigError("Постачальника перевірки ключа не налаштовано")


class KeyCheckService:
    """POST /v1/runtime/credentials/check: one request with the key held in memory (FR-048)."""

    def __init__(self, store: CredentialStore, checker: KeyChecker) -> None:
        self._store = store
        self._checker = checker

    def check(self) -> KeyCheckResult:
        """409 ``API_KEY_REQUIRED`` without a key (nothing is sent). A rejected key is a result,
        not an error. Any other failure is 503 with ``retryable``: ``KEY_CHECK_NOT_SENT`` (no
        connection, or this mode has no provider), ``KEY_CHECK_UPSTREAM`` (timeout, 5xx, a broken
        answer stream), ``KEY_CHECK_INVALID_RESPONSE`` (another status, unusable body). The
        message is fixed text: neither the key nor anything the provider said is in it."""
        key = self._store.openrouter_key()
        if key is None:
            raise conflict("API_KEY_REQUIRED", "Спершу задайте ключ API OpenRouter у Налаштуваннях")
        try:
            return self._checker(key)
        except (ConfigError, ValueError):
            failure = KeyCheckError("not_sent")
        except KeyCheckError as exc:
            failure = exc
        # Raised outside the handlers: nothing of the caught exception is chained.
        raise ServiceError(
            ErrorKind.UNAVAILABLE, _KEY_CHECK_CODES[failure.kind], failure.message, retryable=True
        )
