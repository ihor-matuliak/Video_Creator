"""OpenRouter chat request side for the selection model (ADR 041 §1, §2, §4).

This module builds request bodies and budget reservations (ADR 041 §1, §2, §4) and sends them
through ``OpenRouterChat``: one POST to ``<base>/chat/completions`` per call, one attempt, never
retried. Retries, the spend ledger and the checks of the selection output belong to the service
layer.

Only the models in ``analysis_profiles`` may be used (ADR 041 §1). The production table holds
only models a live probe has confirmed; the test table holds one fake model that only the
loopback test provider serves.

The prompts and the selection schema come from ``clipforge.resources``, which holds byte copies
of ``prompts/`` and ``contracts/``. ``PROMPT_VERSION`` is derived from those bytes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from importlib import resources
from types import MappingProxyType
from typing import Final, Literal

import httpx2

from clipforge.adapters.openrouter_stt import (
    PRODUCTION_BASE_URL,
    RETRY_AFTER_HEADER,
    _cost,
    _parse_retry_after,
    check_base_url,
)
from clipforge.config import Mode

SCHEMA_VERSION: Final = 1
_SCHEMA_NAME: Final = "clipforge_selection"

# Added to the message bytes for the chat template and JSON wrapping (ADR 041 §2).
_TEMPLATE_OVERHEAD_BYTES: Final = 512


def _read_resource(name: str) -> bytes:
    return resources.files("clipforge.resources").joinpath(name).read_bytes()


_SELECT_BYTES: Final = _read_resource("select.system.md")
_REPAIR_BYTES: Final = _read_resource("repair.system.md")
_SCHEMA_BYTES: Final = _read_resource("selection.schema.json")

SELECT_SYSTEM_PROMPT: Final = _SELECT_BYTES.decode("utf-8")
REPAIR_SYSTEM_PROMPT: Final = _REPAIR_BYTES.decode("utf-8")

# Zero bytes separate the files, so moving a byte from one file to the next changes the version.
_PROMPT_DIGEST: Final = hashlib.sha256(
    _SELECT_BYTES + b"\x00" + _REPAIR_BYTES + b"\x00" + _SCHEMA_BYTES
).hexdigest()
PROMPT_VERSION: Final = "sha256:" + _PROMPT_DIGEST[:16]


@dataclass(frozen=True, slots=True)
class AnalysisProfile:
    """A model the selection may use (ADR 041 §1).

    The caps are USD per 1 000 000 tokens. ``max_tokens`` is the completion limit sent in the
    body. ``price_basis`` says where the caps come from (``"test"`` for the fake model, the
    live-probe date for production).
    """

    model: str
    prompt_usd_per_mtok: Decimal
    completion_usd_per_mtok: Decimal
    context_tokens: int
    max_tokens: int
    price_basis: str


_PRODUCTION_PROFILES: Final[Mapping[str, AnalysisProfile]] = MappingProxyType(
    {
        "anthropic/claude-haiku-5.5": AnalysisProfile(
            model="anthropic/claude-haiku-5.5",
            prompt_usd_per_mtok=Decimal("0.10"),
            completion_usd_per_mtok=Decimal("0.50"),
            context_tokens=1_000_000,
            max_tokens=4096,
            price_basis="openrouter-live-probe-2026-11-17",
        ),
    }
)

_TEST_PROFILES: Final[Mapping[str, AnalysisProfile]] = MappingProxyType(
    {
        "test/selector": AnalysisProfile(
            model="test/selector",
            prompt_usd_per_mtok=Decimal("1"),
            completion_usd_per_mtok=Decimal("2"),
            context_tokens=128_000,
            max_tokens=4096,
            price_basis="test",
        ),
    }
)

_PROFILES_BY_MODE: Final[Mapping[Mode, Mapping[str, AnalysisProfile]]] = MappingProxyType(
    {"production": _PRODUCTION_PROFILES, "test": _TEST_PROFILES}
)


def analysis_profiles(mode: Mode) -> Mapping[str, AnalysisProfile]:
    """The models allowed in ``mode``. Production holds only models confirmed by a live probe
    (ADR 041 §1, §24); test mode holds the fake ``test/selector`` only."""
    return _PROFILES_BY_MODE[mode]


def build_chat_body(
    profile: AnalysisProfile,
    *,
    system_prompt: str,
    user_payload: Mapping[str, object],
) -> dict[str, object]:
    """The JSON body of one selection request (ADR 041 §4). Nothing is sent here.

    The user message is the compact JSON of ``user_payload`` with raw UTF-8 text. The body
    holds no file name or path. ``temperature`` is never sent. ``provider.max_price`` carries
    the caps as strings, so OpenRouter cannot route to a dearer provider, and
    ``require_parameters`` makes it refuse a provider that ignores ``response_format``.
    """
    user_content = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "model": profile.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": _SCHEMA_NAME,
                "strict": True,
                "schema": json.loads(_SCHEMA_BYTES),
            },
        },
        "max_tokens": profile.max_tokens,
        "provider": {
            "require_parameters": True,
            "max_price": {
                "prompt": str(profile.prompt_usd_per_mtok),
                "completion": str(profile.completion_usd_per_mtok),
            },
        },
        "stream": False,
    }


def request_bytes(body: Mapping[str, object]) -> int:
    """Bytes of a request as the reservation counts them (ADR 041 §2, §16).

    UTF-8 bytes of all message contents, plus the compact JSON bytes of ``response_format`` (some
    providers bill the schema as input tokens), plus 512 bytes of chat template and wrapping. A
    token count never exceeds its byte count, so these bytes bound the prompt tokens.
    """
    return _message_bytes(body) + _response_format_bytes(body) + _TEMPLATE_OVERHEAD_BYTES


def reservation_micro_usd(body: Mapping[str, object], profile: AnalysisProfile) -> int:
    """The most one request can cost, in micro-USD, rounded up (ADR 041 §2).

    Prompt side: ``request_bytes(body)`` × prompt cap, because a token count never exceeds its
    byte count. Completion side:
    ``max_tokens`` from the body × completion cap.

    ADR 041 §2 writes the formula with ``/ 10**6`` and calls the result micro-USD. That division
    gives USD, not micro-USD. A cap is USD per 10**6 tokens, so one token at cap C costs exactly
    C micro-USD, and the reservation is the plain sum above. Keeping the division would make the
    budget guard 10**6 times too small.
    """
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
        raise ValueError("У тілі запиту немає цілого max_tokens")
    prompt_tokens = Decimal(request_bytes(body))
    total = (
        prompt_tokens * profile.prompt_usd_per_mtok
        + Decimal(max_tokens) * profile.completion_usd_per_mtok
    )
    return int(total.to_integral_value(rounding=ROUND_CEILING))


def _message_bytes(body: Mapping[str, object]) -> int:
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ValueError("У тілі запиту немає списку messages")
    total = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ValueError("Повідомлення запиту не має тексту")
        total += len(content.encode("utf-8"))
    return total


def _response_format_bytes(body: Mapping[str, object]) -> int:
    """UTF-8 bytes of the compact JSON of ``response_format``: the schema is billed as input
    tokens by some providers (ADR 041 §2)."""
    if "response_format" not in body:
        raise ValueError("У тілі запиту немає response_format")
    return len(_compact_json(body["response_format"]).encode("utf-8"))


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------------------------------- HTTP client

CHAT_PATH: Final = "/chat/completions"

# The timeouts of the STT adapter (ADR 029). ADR 041 sets no other values for chat.
CONNECT_TIMEOUT_S: Final = 10.0
WRITE_TIMEOUT_S: Final = 60.0
READ_TIMEOUT_S: Final = 120.0
POOL_TIMEOUT_S: Final = 10.0

# A selection answer is one JSON object with one message. A body above this limit is refused.
MAX_BODY_BYTES: Final = 1024 * 1024
_TOO_LARGE: Final = "Тіло відповіді завелике (понад 1 МіБ)."

ChatErrorKind = Literal[
    "auth",
    "funds",
    "rate_limited",
    "invalid_request",
    "model_unavailable",
    "upstream",
    "not_sent",
    "unknown_outcome",
    "invalid_response",
]

_MESSAGES: Final[Mapping[ChatErrorKind, str]] = MappingProxyType(
    {
        "auth": "OpenRouter не прийняв ключ API. Перевірте ключ у Налаштуваннях.",
        "funds": (
            "На рахунку OpenRouter недостатньо коштів для запиту аналізу. Поповніть баланс "
            "і запустіть аналіз знову."
        ),
        "rate_limited": "OpenRouter обмежив частоту запитів (429). Потрібна пауза перед повтором.",
        "invalid_request": "OpenRouter відхилив запит як некоректний.",
        "model_unavailable": "Модель аналізу недоступна в OpenRouter (404).",
        "upstream": "Тимчасова помилка на боці OpenRouter.",
        "not_sent": "Не вдалося з'єднатися з OpenRouter: запит не було надіслано.",
        "unknown_outcome": (
            "Відповідь OpenRouter не отримано після відправлення запиту. Невідомо, чи його "
            "оброблено й оплачено. Автоматичного повтору немає."
        ),
        "invalid_response": "OpenRouter повернув відповідь, яку не можна використати.",
    }
)

# Failures that happen before the request is sent: the provider cannot have seen it.
# Everything later (write/read timeout, reset, a body that cannot be read) may have been billed.
_NOT_SENT: Final = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


class ChatError(Exception):
    """A chat call failed. ``kind`` is stable, the message is Ukrainian.

    ``status`` is the HTTP status when an answer was received. ``retry_after_s`` is the
    provider's Retry-After in seconds (``rate_limited`` only). No provider text is ever copied
    into the message.
    """

    def __init__(
        self,
        kind: ChatErrorKind,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        detail: str | None = None,
    ) -> None:
        message = _MESSAGES[kind]
        if kind in ("invalid_request", "upstream") and status is not None:
            message = f"{message} (HTTP {status})"
        if detail:
            message = f"{message} {detail}"
        super().__init__(message)
        self.kind: ChatErrorKind = kind
        self.status = status
        self.retry_after_s = retry_after_s
        self.message = message

    def __repr__(self) -> str:
        return (
            f"ChatError(kind={self.kind!r}, status={self.status!r}, "
            f"retry_after_s={self.retry_after_s!r})"
        )


@dataclass(frozen=True, slots=True)
class ChatResult:
    """A 200 answer of one chat call, checked for shape only.

    ``content`` is the model's text, which is not checked here (the selection output is checked
    by ``domain.selection_output``). It is left out of ``repr``: it is user content.
    ``finish_reason`` is what the provider gave, ``None`` when absent. ``cost`` is what the
    provider reported it charged: ``None`` when absent or not a non-negative number, never
    invented. ``generation_id`` is the body's ``id``, ``None`` when absent or blank.
    """

    content: str = field(repr=False)
    finish_reason: str | None
    cost: Decimal | None
    generation_id: str | None


@dataclass(frozen=True, slots=True)
class _Answer:
    status: int
    retry_after: str | None
    body: bytes


class OpenRouterChat:
    """Synchronous OpenRouter chat client; safe to share between job threads.

    Every call opens its own ``httpx2.Client``, so nothing is shared between calls. ``transport``
    and ``now`` exist for tests (``httpx2.MockTransport``, a fixed clock).

    ``api_key`` is required in production. In test mode it is optional (ADR 041 §2): ``None``
    sends no ``Authorization`` header. ``mode`` and ``base_url`` follow the STT adapter:
    production sends to OpenRouter only, test mode only to a loopback test provider. Anything
    else is a ``ConfigError`` before the key is used.
    """

    __slots__ = ("_api_key", "_base_url", "_now", "_transport")

    def __init__(
        self,
        api_key: str | None,
        *,
        mode: Mode = "production",
        base_url: str = PRODUCTION_BASE_URL,
        transport: httpx2.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if api_key is None:
            if mode == "production":
                raise ValueError("Ключ API потрібен для запиту аналізу в production")
        elif not api_key or not all(0x21 <= ord(char) <= 0x7E for char in api_key):
            raise ValueError("Ключ API порожній або містить пробіли чи недрукований символ")
        check_base_url(mode, base_url)
        self._api_key = api_key
        self._base_url = base_url
        self._transport = transport
        self._now = now if now is not None else _utc_now

    def __repr__(self) -> str:
        return f"OpenRouterChat(base_url={self._base_url!r})"

    def complete(self, body: Mapping[str, object]) -> ChatResult:
        """Send ``body`` once. Raises ``ChatError``; never retries."""
        answer = self._send(_compact_json(body).encode("utf-8"))
        if answer.status == 200:
            if len(answer.body) > MAX_BODY_BYTES:
                raise _invalid(_TOO_LARGE)
            return _parse_success(answer.body)
        kind = _kind_for_status(answer.status)
        if kind is None:
            raise ChatError(
                "invalid_response",
                status=answer.status,
                detail=f"Несподіваний статус HTTP {answer.status}.",
            )
        retry_after_s = (
            _parse_retry_after(answer.retry_after, self._now()) if kind == "rate_limited" else None
        )
        raise ChatError(kind, status=answer.status, retry_after_s=retry_after_s)

    def _send(self, payload: bytes) -> _Answer:
        """One POST. The body is read only for a 200, and never past one byte over the limit."""
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        answer: _Answer | None = None
        transport_kind: Literal["not_sent", "unknown_outcome"] = "unknown_outcome"
        try:
            with (
                httpx2.Client(
                    transport=self._transport,
                    timeout=httpx2.Timeout(
                        connect=CONNECT_TIMEOUT_S,
                        read=READ_TIMEOUT_S,
                        write=WRITE_TIMEOUT_S,
                        pool=POOL_TIMEOUT_S,
                    ),
                    follow_redirects=False,
                    # The environment is not trusted: proxy variables must not carry the key.
                    trust_env=False,
                ) as http,
                http.stream(
                    "POST",
                    self._base_url.rstrip("/") + CHAT_PATH,
                    headers=headers,
                    content=payload,
                ) as response,
            ):
                received = bytearray()
                if response.status_code == 200:
                    for chunk in response.iter_raw():
                        received += chunk
                        if len(received) > MAX_BODY_BYTES:
                            break
                answer = _Answer(
                    status=response.status_code,
                    retry_after=response.headers.get(RETRY_AFTER_HEADER),
                    body=bytes(received),
                )
        except httpx2.HTTPError as exc:
            if isinstance(exc, _NOT_SENT):
                transport_kind = "not_sent"
        # Raised outside the handler: the exception holds the request with the key header.
        if answer is None:
            raise ChatError(transport_kind)
        return answer


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _kind_for_status(status: int) -> ChatErrorKind | None:
    """Error kind of a non-200 status; ``None`` for statuses that are not errors (1xx-3xx, 2xx)."""
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "funds"
    if status == 404:
        return "model_unavailable"
    if status == 429:
        return "rate_limited"
    # A timeout answer after the request left: the provider may have processed and billed it
    # (ADR 041 §22). Never repeated without the user's decision.
    if status in (408, 504):
        return "unknown_outcome"
    if 500 <= status <= 599:
        return "upstream"
    if 400 <= status <= 499:
        return "invalid_request"
    return None


def _invalid(detail: str) -> ChatError:
    return ChatError("invalid_response", status=200, detail=detail)


def _parse_success(content: bytes) -> ChatResult:
    """Shape check of a 200 body: a JSON object with ``choices[0].message.content`` as a string.

    Anything else (not JSON, not an object, no choice, no string content) is ``invalid_response``.
    Provider text is never copied into the detail.
    """
    payload: object = None
    parsed = False
    # The decoder error holds a copy of the body: it is swallowed, and the error is raised
    # outside the handler so that it carries no chained context.
    with contextlib.suppress(ValueError, RecursionError):
        payload = json.loads(content)
        parsed = True
    if not parsed:
        raise _invalid("Тіло відповіді не є коректним JSON.")
    if not isinstance(payload, dict):
        raise _invalid("Тіло відповіді не є JSON-об'єктом.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _invalid("У відповіді немає варіантів choices.")
    choice = choices[0]
    message = choice.get("message")
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str):
        raise _invalid("У відповіді немає тексту повідомлення content.")
    finish_reason = choice.get("finish_reason")
    usage = payload.get("usage")
    return ChatResult(
        content=text,
        finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        cost=_cost(usage.get("cost")) if isinstance(usage, dict) else None,
        generation_id=_body_generation_id(payload.get("id")),
    )


def _body_generation_id(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
