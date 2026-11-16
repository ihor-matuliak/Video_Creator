"""OpenRouter speech-to-text adapter (ADR 029, docs/05 section 5).

``OpenRouterStt.transcribe`` sends one WAV to ``POST <base>/audio/transcriptions`` and
turns the answer into an ``SttResult``. One call is one attempt: retries, Retry-After
waits, backoff, splitting a chunk and the budget belong to the orchestrator. A call that
may already have been processed (a write/read timeout or a broken connection after the
request left) is ``unknown_outcome`` and is never repeated here.

Two models are supported: ``microsoft/mai-transcribe-2`` (provider options go under
``provider.options.azure``) and ``google/gemini-3.5-transcribe`` (no provider options).
The adapter does not invent option values: the MAI options are exactly what the caller passes.

The API key is an argument of the constructor and goes only into the ``Authorization``
header. It is not in the URL, the body, ``repr`` of the client, the error message or the
result. Error messages are fixed Ukrainian texts: no provider text is copied into them,
and transport exceptions are never chained (their request object carries the headers).
The environment is not trusted (``trust_env=False``): proxy variables are ignored.

A 200 answer is validated before it is trusted (ADR 029): see ``_parse_success``.
Times in the result are seconds from the start of the uploaded audio, as the provider
gave them; ``domain.transcript_merge`` turns them into source milliseconds.
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Final, Literal

import httpx2

from clipforge.config import Config, ConfigError, Mode, validate_test_provider_url
from clipforge.domain.transcript_merge import ChunkItem

PRODUCTION_BASE_URL: Final = "https://openrouter.ai/api/v1"
TRANSCRIPTIONS_PATH: Final = "/audio/transcriptions"

MAI_MODEL: Final = "microsoft/mai-transcribe-2"
GEMINI_MODEL: Final = "google/gemini-3.5-transcribe"

AUDIO_FORMAT: Final = "wav"
RESPONSE_FORMAT: Final = "verbose_json"
GRANULARITIES: Final = ("segment", "word")

# OpenRouter cuts the upstream request after 60 s (ADR 029).
CONNECT_TIMEOUT_S: Final = 10.0
WRITE_TIMEOUT_S: Final = 60.0
READ_TIMEOUT_S: Final = 120.0
POOL_TIMEOUT_S: Final = 10.0

GENERATION_ID_HEADER: Final = "X-Generation-Id"
RETRY_AFTER_HEADER: Final = "Retry-After"

SttErrorKind = Literal[
    "auth",
    "funds",
    "rate_limited",
    "payload_too_large",
    "model_unavailable",
    "invalid_request",
    "upstream",
    "not_sent",
    "unknown_outcome",
    "invalid_response",
]

_MESSAGES: Final[Mapping[SttErrorKind, str]] = MappingProxyType(
    {
        "auth": "OpenRouter не прийняв ключ API. Перевірте ключ у Налаштуваннях.",
        "funds": (
            "На рахунку OpenRouter недостатньо коштів: для аудіозапитів потрібен баланс "
            "щонайменше $0,50. Поповніть баланс і запустіть розпізнавання знову."
        ),
        "rate_limited": "OpenRouter обмежив частоту запитів (429). Потрібна пауза перед повтором.",
        "payload_too_large": "Аудіо завелике для одного запиту до OpenRouter (413).",
        "model_unavailable": "Модель розпізнавання недоступна в OpenRouter (404).",
        "invalid_request": "OpenRouter відхилив запит як некоректний.",
        "upstream": "Тимчасова помилка на боці OpenRouter.",
        "not_sent": "Не вдалося з'єднатися з OpenRouter: запит не було надіслано.",
        "unknown_outcome": (
            "Відповідь OpenRouter не отримано після відправлення запиту. Невідомо, чи його "
            "оброблено й оплачено. Автоматичного повтору немає."
        ),
        "invalid_response": "OpenRouter повернув відповідь, яку не можна використати.",
    }
)

_WORD_LABEL: Final = "Слово"
_SEGMENT_LABEL: Final = "Сегмент"


class SttError(Exception):
    """A transcription call failed. ``kind`` is stable, the message is Ukrainian.

    ``status`` is the HTTP status when an answer was received. ``retry_after_s`` is the
    provider's Retry-After in seconds (``rate_limited`` only). ``generation_id`` is the
    provider request id, when the provider sent one. No provider text is ever copied
    into the message.
    """

    def __init__(
        self,
        kind: SttErrorKind,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        generation_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        message = _MESSAGES[kind]
        if kind in ("invalid_request", "upstream") and status is not None:
            message = f"{message} (HTTP {status})"
        if detail:
            message = f"{message} {detail}"
        super().__init__(message)
        self.kind: SttErrorKind = kind
        self.status = status
        self.retry_after_s = retry_after_s
        self.generation_id = generation_id
        self.message = message

    def __repr__(self) -> str:
        return (
            f"SttError(kind={self.kind!r}, status={self.status!r}, "
            f"retry_after_s={self.retry_after_s!r}, generation_id={self.generation_id!r})"
        )


@dataclass(frozen=True, slots=True)
class SttUsage:
    """What the provider reported it charged. A field the provider did not give is ``None``.

    ``cost`` is never invented: it is ``None`` when absent, not a number or negative.
    """

    cost: Decimal | None = None
    seconds: float | None = None
    tokens: int | None = None


@dataclass(frozen=True, slots=True)
class SttResult:
    """A validated transcription of one uploaded audio file.

    ``words`` and ``segments`` are seconds from the start of the uploaded audio, empty
    items removed (counted in ``dropped_words`` / ``dropped_segments``). ``has_word_timing``
    is true when the provider gave at least one word with times. A silent audio is a
    success with empty ``text`` and no items. ``text``, ``words`` and ``segments`` are
    left out of ``repr``: they are user content.
    """

    text: str = field(repr=False)
    words: tuple[ChunkItem, ...] = field(repr=False)
    segments: tuple[ChunkItem, ...] = field(repr=False)
    has_word_timing: bool
    language: str | None
    duration_s: float | None
    usage: SttUsage
    generation_id: str | None
    dropped_words: int = 0
    dropped_segments: int = 0


@dataclass(frozen=True, slots=True)
class SttProfile:
    model: str
    # Key under ``provider.options`` for this model's options; None: no provider options.
    provider_slug: str | None


PROFILES: Final[Mapping[str, SttProfile]] = MappingProxyType(
    {
        MAI_MODEL: SttProfile(MAI_MODEL, "azure"),
        GEMINI_MODEL: SttProfile(GEMINI_MODEL, None),
    }
)

# Failures that happen before the request is sent: the provider cannot have seen it.
# Everything else after a send attempt (write/read timeout, reset, protocol error, a
# body that cannot be read) may already have been billed.
_NOT_SENT: Final = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


def resolve_base_url(config: Config) -> str:
    """Production uses OpenRouter; test mode uses ``Config.test_provider_base_url`` only.

    Test mode without a provider URL is refused instead of falling back to OpenRouter:
    paid AI is disabled in automated tests.
    """
    if config.mode == "test":
        if config.test_provider_base_url is None:
            raise ConfigError(
                "У test mode потрібна адреса тестового провайдера "
                "(CLIPFORGE_TEST_PROVIDER_BASE_URL): платні виклики OpenRouter у тестах заборонені"
            )
        return config.test_provider_base_url
    return PRODUCTION_BASE_URL


def check_base_url(mode: Mode, base_url: str) -> None:
    """The key must not be sent anywhere the mode does not allow (same rule as resolve_base_url)."""
    if mode == "test":
        validate_test_provider_url(base_url)
    elif base_url != PRODUCTION_BASE_URL:
        raise ConfigError(f"У production ключ можна надсилати лише на {PRODUCTION_BASE_URL}")


def build_request_body(
    model: str,
    audio_wav: bytes,
    *,
    language: str | None = None,
    provider_options: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """The JSON body for ``model`` (ADR 029). Raises ``ValueError`` for an unknown model.

    ``language`` is sent only when given. ``provider.options.azure`` is sent only for
    MAI and only from ``provider_options``; Gemini has no provider options, so any
    passed for it are not sent.
    """
    profile = PROFILES.get(model)
    if profile is None:
        raise ValueError(f"Непідтримувана STT-модель: {model!r}")
    if not audio_wav:
        raise ValueError("Аудіо для розпізнавання порожнє")
    body: dict[str, object] = {
        "model": profile.model,
        "input_audio": {
            "data": base64.b64encode(audio_wav).decode("ascii"),
            "format": AUDIO_FORMAT,
        },
        "temperature": 0,
        "response_format": RESPONSE_FORMAT,
        "timestamp_granularities": list(GRANULARITIES),
    }
    if language is not None:
        body["language"] = language
    if profile.provider_slug is not None and provider_options:
        body["provider"] = {"options": {profile.provider_slug: dict(provider_options)}}
    return body


class OpenRouterStt:
    """Synchronous OpenRouter STT client; safe to share between job threads.

    Every call opens its own ``httpx2.Client``, so nothing is shared between calls.
    ``transport`` and ``now`` exist for tests (``httpx2.MockTransport``, a fixed clock).

    ``mode`` follows ``resolve_base_url``: in ``production`` the only allowed ``base_url`` is
    OpenRouter, in ``test`` only a loopback test provider (never OpenRouter: paid AI is
    disabled in automated tests). Anything else is a ``ConfigError`` before the key is used.
    """

    __slots__ = ("_api_key", "_base_url", "_now", "_transport")

    def __init__(
        self,
        api_key: str,
        *,
        mode: Mode = "production",
        base_url: str = PRODUCTION_BASE_URL,
        transport: httpx2.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key or not all(0x21 <= ord(char) <= 0x7E for char in api_key):
            raise ValueError("Ключ API порожній або містить пробіли чи недрукований символ")
        check_base_url(mode, base_url)
        self._api_key = api_key
        self._base_url = base_url
        self._transport = transport
        self._now = now if now is not None else _utc_now

    def __repr__(self) -> str:
        return f"OpenRouterStt(base_url={self._base_url!r})"

    def transcribe(
        self,
        *,
        model: str,
        audio_wav: bytes,
        language: str | None = None,
        provider_options: Mapping[str, object] | None = None,
    ) -> SttResult:
        """Send ``audio_wav`` once. Raises ``SttError``; never retries."""
        body = build_request_body(
            model, audio_wav, language=language, provider_options=provider_options
        )
        response: httpx2.Response | None = None
        transport_kind: Literal["not_sent", "unknown_outcome"] = "unknown_outcome"
        try:
            with httpx2.Client(
                transport=self._transport,
                timeout=httpx2.Timeout(
                    connect=CONNECT_TIMEOUT_S,
                    read=READ_TIMEOUT_S,
                    write=WRITE_TIMEOUT_S,
                    pool=POOL_TIMEOUT_S,
                ),
                follow_redirects=False,
                # The environment is not trusted: HTTP(S)_PROXY, ALL_PROXY and SSL_CERT_* must not
                # redirect the key and the audio, or change who is trusted to receive them.
                trust_env=False,
            ) as http:
                response = http.post(
                    self._base_url.rstrip("/") + TRANSCRIPTIONS_PATH,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Accept": "application/json",
                    },
                    json=body,
                )
        except httpx2.HTTPError as exc:
            if isinstance(exc, _NOT_SENT):
                transport_kind = "not_sent"
        # Raised outside the handler: the exception holds the request with the key header.
        if response is None:
            raise SttError(transport_kind)

        status = response.status_code
        generation_id = _generation_id(response)
        if status == 200:
            return _parse_success(response.content, generation_id)
        kind = _kind_for_status(status)
        if kind is None:
            raise SttError(
                "invalid_response",
                status=status,
                generation_id=generation_id,
                detail=f"Несподіваний статус HTTP {status}.",
            )
        retry_after_s = (
            _parse_retry_after(response.headers.get(RETRY_AFTER_HEADER), self._now())
            if kind == "rate_limited"
            else None
        )
        raise SttError(
            kind, status=status, retry_after_s=retry_after_s, generation_id=generation_id
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _kind_for_status(status: int) -> SttErrorKind | None:
    """Error kind of a non-200 status; ``None`` for statuses that are not errors (1xx-3xx)."""
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "funds"
    if status == 404:
        return "model_unavailable"
    if status == 413:
        return "payload_too_large"
    if status == 429:
        return "rate_limited"
    if status == 408 or 500 <= status <= 599:
        return "upstream"
    if 400 <= status <= 499:
        return "invalid_request"
    return None


def _generation_id(response: httpx2.Response) -> str | None:
    value = response.headers.get(GENERATION_ID_HEADER)
    if value is None:
        return None
    return value.strip() or None


def _parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Retry-After as seconds: delta-seconds or an HTTP-date (never negative); else ``None``."""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        try:
            return float(int(text))
        except (ValueError, OverflowError):
            return None
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - now).total_seconds())


# ------------------------------------------------------------------ 200 validation


def _invalid(generation_id: str | None, detail: str) -> SttError:
    return SttError("invalid_response", status=200, generation_id=generation_id, detail=detail)


def _parse_success(content: bytes, generation_id: str | None) -> SttResult:
    """Validate a 200 body (ADR 029) and build the result.

    * the body is a JSON object and ``text`` is a string;
    * ``words`` / ``segments`` are lists (or absent / null); every entry is an object with a
      string text (``word`` for words, ``text`` for segments) and finite ``start <= end``,
      ``end >= 0``: otherwise the whole answer is invalid;
    * entries whose text is blank are dropped and counted;
    * ``language``, ``duration``, ``speaker``, ``confidence`` and ``usage`` fields are
      optional: absent, ``null`` or of the wrong type means ``None``;
    * no usable word but a segment: ``has_word_timing`` is false (segment timing only);
    * text that is not blank after ``strip`` but no usable word and no usable segment is
      ``invalid_response``: it would look like silence while speech was claimed;
    * blank text and no entries: silence, a success.
    """
    payload: object = None
    parsed = False
    # The decoder error holds a copy of the body: it is swallowed, and the error is raised
    # outside the handler so that it carries no chained context.
    with contextlib.suppress(ValueError, RecursionError):
        payload = json.loads(content)
        parsed = True
    if not parsed:
        raise _invalid(generation_id, "Тіло відповіді не є коректним JSON.")
    if not isinstance(payload, dict):
        raise _invalid(generation_id, "Тіло відповіді не є JSON-об'єктом.")
    text = payload.get("text")
    if not isinstance(text, str):
        raise _invalid(generation_id, "У відповіді немає текстового поля text.")

    words, dropped_words = _parse_items(payload.get("words"), "word", _WORD_LABEL, generation_id)
    segments, dropped_segments = _parse_items(
        payload.get("segments"), "text", _SEGMENT_LABEL, generation_id
    )
    if text.strip() and not words and not segments:
        raise _invalid(generation_id, "Текст є, але немає жодного слова чи сегмента з часом.")
    return SttResult(
        text=text,
        words=words,
        segments=segments,
        has_word_timing=bool(words),
        language=_optional_text(payload.get("language")),
        duration_s=_non_negative(payload.get("duration")),
        usage=_parse_usage(payload.get("usage")),
        generation_id=generation_id,
        dropped_words=dropped_words,
        dropped_segments=dropped_segments,
    )


def _parse_items(
    raw: object, text_key: str, label: str, generation_id: str | None
) -> tuple[tuple[ChunkItem, ...], int]:
    if raw is None:
        return (), 0
    if not isinstance(raw, list):
        raise _invalid(generation_id, f"{label}: поле відповіді не є списком.")
    items: list[ChunkItem] = []
    dropped = 0
    for number, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise _invalid(generation_id, f"{label} №{number} не є JSON-об'єктом.")
        text = entry.get(text_key)
        if not isinstance(text, str):
            raise _invalid(generation_id, f"{label} №{number} без тексту.")
        start = _finite(entry.get("start"))
        end = _finite(entry.get("end"))
        if start is None or end is None or start > end or end < 0:
            raise _invalid(generation_id, f"{label} №{number} має некоректний час.")
        cleaned = text.strip()
        if not cleaned:
            dropped += 1
            continue
        items.append(
            ChunkItem(
                start_s=start,
                end_s=end,
                text=cleaned,
                speaker=_optional_text(entry.get("speaker")),
                confidence=_finite(entry.get("confidence")),
            )
        )
    return tuple(items), dropped


def _finite(value: object) -> float | None:
    """A finite JSON number as float; booleans, strings, NaN and infinities give ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _non_negative(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _parse_usage(raw: object) -> SttUsage:
    if not isinstance(raw, dict):
        return SttUsage()
    tokens = raw.get("total_tokens")
    return SttUsage(
        cost=_cost(raw.get("cost")),
        seconds=_non_negative(raw.get("seconds")),
        tokens=tokens
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0
        else None,
    )


def _cost(value: object) -> Decimal | None:
    """The provider's cost as an exact decimal; ``None`` unless it is a non-negative number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, int):
        return Decimal(value) if value >= 0 else None
    if not math.isfinite(value) or value < 0:
        return None
    # repr of a float is its shortest round-trip form: no binary noise in the decimal.
    return Decimal(repr(value))
