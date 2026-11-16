"""Non-secret application settings (FR-048, FR-049, docs/04 section 4).

Four keys, each with a default; a stored value equal to "unset" has no row:

* ``stt_model``: ``microsoft/mai-transcribe-2`` (default) or any other model of
  ``adapters.openrouter_stt.PROFILES`` (now ``google/gemini-3.5-transcribe``).
* ``stt_language``: ``None`` (default, the model detects it) or two lowercase ASCII letters.
* ``analysis_model``: ``None`` (default, not chosen yet) or ``vendor/model``, at most 100 chars.
* ``run_budget_usd``: ``None`` (default, no ceiling set) or a decimal string 0.01-1000 with at most
  6 digits after the point, kept exactly as the user wrote it.

The API key is not a setting: it lives in ``CredentialStore`` and only ``has_api_key`` is shown.
Every value is checked here, not by the UI. An update is all or nothing. A stored value that
fails its check, or whose text is not versioned JSON (the database was edited by hand), is ignored
and the default is used, so the ``stt_model`` allowlist holds whatever the file says. Only the key
name is logged. Changing or resetting such a key replaces or removes its row.

Messages are fixed text: they never echo the rejected value.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from clipforge.adapters.openrouter_stt import MAI_MODEL, PROFILES
from clipforge.services.credentials import CredentialStore
from clipforge.services.errors import ServiceError, invalid
from clipforge.storage import Database, SettingsRepository

# The allowlist is the set of models the STT adapter has a profile for: a model the adapter cannot
# call is never accepted here, and a new profile is accepted without a second edit.
STT_MODELS: Final = tuple(PROFILES)
DEFAULT_STT_MODEL: Final = MAI_MODEL
MAX_ANALYSIS_MODEL_LENGTH: Final = 100
BUDGET_MIN_USD: Final = Decimal("0.01")
BUDGET_MAX_USD: Final = Decimal("1000")

_LANGUAGE: Final = re.compile(r"[a-z]{2}")
# One slash; an optional leading "~" is OpenRouter's alias form ("~vendor/model-latest").
_MODEL_ID: Final = re.compile(r"~?[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:+-]*")
# No sign, exponent, spaces, superfluous leading zeros or non-ASCII digits; up to 4 integer digits.
_BUDGET: Final = re.compile(r"(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,6})?")

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Settings:
    stt_model: str
    stt_language: str | None
    analysis_model: str | None
    run_budget_usd: str | None
    has_api_key: bool


def check_stt_model(value: object, *, field: str = "stt_model") -> str:
    """The value if it is a supported STT model, else 422 about ``field`` (a STT run request names
    it ``model``)."""
    if isinstance(value, str) and value in STT_MODELS:
        return value
    raise invalid(
        "INVALID_STT_MODEL",
        "Модель розпізнавання має бути однією з підтримуваних",
        field=field,
    )


def check_stt_language(value: object, *, field: str = "stt_language") -> str | None:
    """``None`` or two lowercase ASCII letters, else 422 about ``field``."""
    if value is None:
        return None
    if isinstance(value, str) and _LANGUAGE.fullmatch(value):
        return value
    raise invalid(
        "INVALID_STT_LANGUAGE",
        "Мова розпізнавання має бути двобуквеним малим кодом, наприклад en, або порожньою",
        field=field,
    )


def _analysis_model(value: object) -> str | None:
    if value is None:
        return None
    if (
        isinstance(value, str)
        and len(value) <= MAX_ANALYSIS_MODEL_LENGTH
        and _MODEL_ID.fullmatch(value)
    ):
        return value
    raise invalid(
        "INVALID_ANALYSIS_MODEL",
        "Модель аналізу має мати вигляд «постачальник/модель» і містити не більше 100 символів",
        field="analysis_model",
    )


def check_run_budget(value: object, *, field: str = "run_budget_usd") -> str | None:
    """``None`` or a decimal string 0.01-1000 (the rule of the setting), else 422 about ``field``;
    a STT run request names it ``budget_usd``."""
    if value is None:
        return None
    if (
        isinstance(value, str)
        and _BUDGET.fullmatch(value)
        and BUDGET_MIN_USD <= Decimal(value) <= BUDGET_MAX_USD
    ):
        return value
    raise invalid(
        "INVALID_RUN_BUDGET",
        "Ліміт витрат має бути десятковим рядком від 0.01 до 1000 USD "
        "із не більше ніж 6 цифрами після крапки",
        field=field,
    )


# Each check returns the value to store (``None``: no row, the default applies) or raises 422.
_CHECKS: Final[Mapping[str, Callable[[object], str | None]]] = {
    "stt_model": check_stt_model,
    "stt_language": check_stt_language,
    "analysis_model": _analysis_model,
    "run_budget_usd": check_run_budget,
}
_DEFAULTS: Final[Mapping[str, str | None]] = {
    "stt_model": DEFAULT_STT_MODEL,
    "stt_language": None,
    "analysis_model": None,
    "run_budget_usd": None,
}
SETTING_KEYS: Final = tuple(_CHECKS)


class SettingsService:
    def __init__(self, database: Database, credentials: CredentialStore) -> None:
        self._database = database
        self._credentials = credentials

    def get(self) -> Settings:
        with self._database.session() as session:
            stored = SettingsRepository(session).get_many(SETTING_KEYS)
        return self._settings(stored)

    def update(self, changes: Mapping[str, object]) -> Settings:
        """Apply ``changes`` (setting name -> new value, ``None`` resets to the default where the
        setting allows it). Nothing is stored unless every value passes its check; an unknown
        name or a bad value raises 422."""
        checked: dict[str, str | None] = {}
        for key, value in changes.items():
            check = _CHECKS.get(key)
            if check is None:
                raise invalid("UNKNOWN_SETTING", "Невідоме налаштування", field="settings")
            checked[key] = check(value)
        with self._database.write_session() as session:
            repository = SettingsRepository(session)
            for key, value in checked.items():
                if value is None:
                    repository.delete(key)
                else:
                    repository.set(key, value)
            stored = repository.get_many(SETTING_KEYS)
        return self._settings(stored)

    def _settings(self, stored: Mapping[str, Any]) -> Settings:
        values = {key: _effective(key, stored) for key in SETTING_KEYS}
        return Settings(
            stt_model=values["stt_model"] or DEFAULT_STT_MODEL,
            stt_language=values["stt_language"],
            analysis_model=values["analysis_model"],
            run_budget_usd=values["run_budget_usd"],
            has_api_key=self._credentials.has_openrouter_key,
        )


def _effective(key: str, stored: Mapping[str, Any]) -> str | None:
    if key not in stored:
        return _DEFAULTS[key]
    try:
        return _CHECKS[key](stored[key])
    except ServiceError:
        # The key name is one of ours; the stored value is not logged.
        _LOGGER.warning("The stored value of setting %s is not valid: the default is used", key)
        return _DEFAULTS[key]
