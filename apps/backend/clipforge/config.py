"""Sidecar runtime configuration (ADR 013, docs/04_ARCHITECTURE_API.md section 6).

Two sources exist:

* production and E2E: one JSON line on stdin sent by Electron main;
* automated tests: environment variables, allowed only with CLIPFORGE_MODE=test.

Error messages never contain configuration values, so a secret cannot leak
through a rejected configuration.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

Mode = Literal["production", "test"]

ENV_MODE: Final = "CLIPFORGE_MODE"
ENV_DATA_DIR: Final = "CLIPFORGE_DATA_DIR"
ENV_SESSION_TOKEN: Final = "CLIPFORGE_SESSION_TOKEN"  # noqa: S105 - variable name, not a secret
ENV_TEST_PROVIDER_BASE_URL: Final = "CLIPFORGE_TEST_PROVIDER_BASE_URL"
TEST_ENV_PREFIX: Final = "CLIPFORGE_TEST_"

MIN_TOKEN_LENGTH: Final = 32
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
_STDIN_KEYS: Final = frozenset({"mode", "data_dir", "session_token"})


class ConfigError(Exception):
    """Configuration is invalid. The message is Ukrainian and free of secrets."""


@dataclass(frozen=True, slots=True)
class Config:
    mode: Mode
    data_dir: Path
    session_token: str = field(repr=False)
    test_provider_base_url: str | None = None


def load_config(*, stdin_line: str | None, env: Mapping[str, str]) -> Config:
    """Build the configuration from the stdin line (``--config-stdin``) or from env."""
    raw_data_dir: object
    raw_token: object
    if stdin_line is None:
        if env.get(ENV_MODE) != "test":
            raise ConfigError(
                "Без --config-stdin конфігурація з середовища дозволена "
                "лише при CLIPFORGE_MODE=test"
            )
        mode: Mode = "test"
        raw_data_dir = env.get(ENV_DATA_DIR)
        raw_token = env.get(ENV_SESSION_TOKEN)
    else:
        mode, raw_data_dir, raw_token = _parse_stdin(stdin_line)

    _check_environment(mode, env)
    data_dir = _validate_data_dir(raw_data_dir)
    token = _validate_token(raw_token)

    provider_url: str | None = None
    if mode == "test":
        raw_url = env.get(ENV_TEST_PROVIDER_BASE_URL)
        if raw_url is not None:
            provider_url = validate_test_provider_url(raw_url)

    return Config(
        mode=mode, data_dir=data_dir, session_token=token, test_provider_base_url=provider_url
    )


def validate_test_provider_url(url: str) -> str:
    """Accept only http://127.0.0.1 or http://localhost (any port, any path)."""
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        raise ConfigError("Адреса тестового провайдера містить недопустимі символи")
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError:
        raise ConfigError("Адреса тестового провайдера некоректна") from None
    if (
        parts.scheme != "http"
        or parts.hostname not in LOOPBACK_HOSTS
        or parts.username is not None
        or parts.password is not None
    ):
        raise ConfigError(
            "Тестовий провайдер дозволений лише за адресою http://127.0.0.1 або http://localhost"
        )
    return url


def _parse_stdin(line: str) -> tuple[Mode, object, object]:
    try:
        document = json.loads(line)
    except ValueError:
        raise ConfigError("Конфігурація в stdin не є коректним JSON") from None
    if not isinstance(document, dict) or set(document) != _STDIN_KEYS:
        raise ConfigError(
            "Конфігурація в stdin має бути JSON-об'єктом з ключами mode, data_dir, session_token"
        )
    mode = document["mode"]
    if mode == "production":
        return "production", document["data_dir"], document["session_token"]
    if mode == "test":
        return "test", document["data_dir"], document["session_token"]
    raise ConfigError('Поле mode має бути "production" або "test"')


def _check_environment(mode: Mode, env: Mapping[str, str]) -> None:
    declared = env.get(ENV_MODE)
    if declared is not None and declared != mode:
        raise ConfigError(f"Змінна {ENV_MODE} суперечить режиму з конфігурації")
    if mode == "production" and any(name.upper().startswith(TEST_ENV_PREFIX) for name in env):
        raise ConfigError(f"Production-режим не приймає тестових змінних {TEST_ENV_PREFIX}*")


def _validate_data_dir(value: object) -> Path:
    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
        raise ConfigError("data_dir має бути абсолютним шляхом")
    return Path(value)


def _validate_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) < MIN_TOKEN_LENGTH
        or not all(0x21 <= ord(ch) <= 0x7E for ch in value)
    ):
        raise ConfigError(
            f"session_token має містити щонайменше {MIN_TOKEN_LENGTH} "
            "друкованих ASCII-символів без пробілів"
        )
    return value
