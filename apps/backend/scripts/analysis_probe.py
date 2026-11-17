"""Dev-only live capability probe of one candidate selection model (ADR 041 §1, §13).

Sends one selection request, once, through ``OpenRouterChat``, with no retry. The request is built
from a built-in synthetic EN/ES transcript with the service's own functions
(``analysis_plan.user_payload``, ``build_chat_body``) and its window limit
(``analysis_plan.window_bytes_limit``). No workspace database is opened.

A recording transport keeps the raw answer, from which the probe reads ``usage``. The answer is
checked with ``parse_selection_output`` and ``validate_candidates``. The redacted summary is written
to test-results/live/ and printed to stdout. It holds no answer text: validation messages quote at
most 64 characters of a model value.

This is a paid call. A timeout after the request was sent is reported as ``unknown_outcome``,
because the provider may still have processed and billed it.

The API key is read only from the OPENROUTER_API_KEY environment variable. It is never accepted on
the command line and never printed or written to disk.

Exit codes as in stt_probe.py: 0 ok; 1 error response, invalid response or selection not accepted;
2 refused before sending; 3 unknown_outcome; 4 not_sent; 5 save_failed (the summary could not be
written; it is printed to stdout).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import httpx2

from clipforge.adapters.openrouter_chat import (
    CHAT_PATH,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    SELECT_SYSTEM_PROMPT,
    AnalysisProfile,
    ChatError,
    ChatResult,
    OpenRouterChat,
    build_chat_body,
    request_bytes,
)
from clipforge.adapters.openrouter_stt import PRODUCTION_BASE_URL
from clipforge.domain.analysis_windows import Unit, plan_windows
from clipforge.domain.search_profile import DEFAULT_PROFILE
from clipforge.domain.selection_output import (
    SelectionOutputError,
    UnitRef,
    parse_selection_output,
    validate_candidates,
)
from clipforge.services.analysis_plan import UNIT_LEVEL_SEGMENT, user_payload, window_bytes_limit
from clipforge.services.errors import ServiceError
from clipforge.storage import TranscriptRevision

API_KEY_ENV = "OPENROUTER_API_KEY"
ENDPOINT = PRODUCTION_BASE_URL.rstrip("/") + CHAT_PATH
MAX_PRICE_FORMS: Final = ("strings", "numbers")
PRICE_BASIS: Final = "owner_probe"
# USD per 1 000 000 tokens as a plain decimal ("0.25", "2"). No exponent, so the value sent is the
# one that was typed.
_CAP_TEXT: Final = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,12})?")
MAX_CAP_USD: Final = Decimal("1000")
ERROR_MESSAGE_LIMIT: Final = 500

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = SCRIPT_DIR.parents[2] / "test-results" / "live"

EXIT_OK = 0
EXIT_ERROR_RESPONSE = 1
EXIT_REFUSED = 2
EXIT_UNKNOWN_OUTCOME = 3
EXIT_NOT_SENT = 4
EXIT_SAVE_FAILED = 5

_EXIT_BY_OUTCOME: Final[Mapping[str, int]] = {
    "ok": EXIT_OK,
    "error_response": EXIT_ERROR_RESPONSE,
    "invalid_response": EXIT_ERROR_RESPONSE,
    "invalid_selection": EXIT_ERROR_RESPONSE,
    "unknown_outcome": EXIT_UNKNOWN_OUTCOME,
    "not_sent": EXIT_NOT_SENT,
}
_FAILURE_OUTCOMES: Final[Mapping[str, str]] = {
    "invalid_response": "invalid_response",
    "not_sent": "not_sent",
    "unknown_outcome": "unknown_outcome",
}

SYNTHETIC_LANGUAGE: Final = "en"
SYNTHETIC_DURATION_MS: Final = 60_000
# (id, start_ms, end_ms, text). English and Spanish are mixed on purpose: the app is used for EN and
# ES videos, so the probe checks the model with both.
SYNTHETIC_SEGMENTS: Final[tuple[tuple[str, int, int, str], ...]] = (
    ("seg-001", 0, 4_200, "Hey everyone, welcome back! Today we test a kitchen gadget."),
    ("seg-002", 4_300, 9_100, "Hoy les traigo una paella sencilla para toda la familia."),
    ("seg-003", 9_300, 13_800, "Okay, the rice is in, and I am adding the saffron now."),
    ("seg-004", 14_000, 19_500, "¡Ay no, se me cayó la sartén encima de la mesa!"),
    ("seg-005", 19_700, 24_600, "Honestly, that was the funniest fail of the whole day."),
    ("seg-006", 24_800, 30_100, "Mi perro se comió la mitad del arroz, ¡pero sabía increíble!"),
    ("seg-007", 30_300, 36_000, "The gadget finally worked, and the sauce thickened fast."),
    ("seg-008", 36_200, 41_000, "¿Lo ven? Ni mi abuela lo podía creer cuando lo probó."),
    ("seg-009", 41_200, 46_500, "Let's move on to the next recipe, a simple lemon dessert."),
    ("seg-010", 46_700, 52_000, "Gracias por ver el video, dejen su comentario y suscríbanse."),
)


class ProbeRefusedError(Exception):
    """A precondition failed; nothing was sent."""


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """The one request of the probe, and what its answer is checked against."""

    body: dict[str, object]
    request_size_bytes: int
    units: tuple[UnitRef, ...]
    core_ordinals: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What the summary says about one call: the outcome name and its fields."""

    name: str
    fields: dict[str, Any]


def synthetic_units() -> tuple[Unit, ...]:
    """The synthetic transcript as segment units; the ordinal is the position."""
    return tuple(
        Unit(id=segment_id, ordinal=ordinal, start_ms=start, end_ms=end, text=text)
        for ordinal, (segment_id, start, end, text) in enumerate(SYNTHETIC_SEGMENTS)
    )


def parse_cap(text: str, option: str) -> Decimal:
    if _CAP_TEXT.fullmatch(text) is None:
        raise ProbeRefusedError(f"{option} must be a plain decimal such as 0.25 or 2")
    value = Decimal(text)
    if not 0 < value <= MAX_CAP_USD:
        raise ProbeRefusedError(f"{option} must be above 0 and at most {MAX_CAP_USD}")
    return value


def build_profile(args: argparse.Namespace) -> AnalysisProfile:
    model: str = args.model
    if not model or model != model.strip():
        raise ProbeRefusedError("--model must be a model id without surrounding spaces")
    if args.context_tokens <= 0:
        raise ProbeRefusedError("--context-tokens must be positive")
    if args.max_tokens <= 0:
        raise ProbeRefusedError("--max-tokens must be positive")
    return AnalysisProfile(
        model=model,
        prompt_usd_per_mtok=parse_cap(args.prompt_cap, "--prompt-cap"),
        completion_usd_per_mtok=parse_cap(args.completion_cap, "--completion-cap"),
        context_tokens=args.context_tokens,
        max_tokens=args.max_tokens,
        price_basis=PRICE_BASIS,
    )


def build_request(profile: AnalysisProfile, max_price_as: str) -> ProbeRequest:
    """The one selection request, built with the service's functions and no database."""
    revision = TranscriptRevision(language=SYNTHETIC_LANGUAGE, duration_ms=SYNTHETIC_DURATION_MS)
    units = synthetic_units()
    try:
        max_window_bytes = window_bytes_limit(revision, DEFAULT_PROFILE, profile)
    except ServiceError as exc:
        raise ProbeRefusedError(f"{exc.code}: {exc.message}") from None
    windows = plan_windows(units, max_window_bytes)
    if len(windows) != 1:
        raise ProbeRefusedError(f"the synthetic transcript gives {len(windows)} windows, not 1")
    window = windows[0]
    body = build_chat_body(
        profile,
        system_prompt=SELECT_SYSTEM_PROMPT,
        user_payload=user_payload(revision, DEFAULT_PROFILE, UNIT_LEVEL_SEGMENT, units, window),
    )
    body = _with_max_price_form(body, profile, max_price_as)
    return ProbeRequest(
        body=body,
        request_size_bytes=request_bytes(body),
        units=tuple(
            UnitRef(id=unit.id, ordinal=unit.ordinal, start_ms=unit.start_ms, end_ms=unit.end_ms)
            for unit in units
        ),
        core_ordinals=(units[window.core_first].ordinal, units[window.core_last].ordinal),
    )


def _with_max_price_form(
    body: dict[str, object], profile: AnalysisProfile, form: str
) -> dict[str, object]:
    """``build_chat_body`` sends the caps as strings; ``numbers`` sends them as JSON numbers."""
    if form == "strings":
        return body
    provider = body["provider"]
    if not isinstance(provider, dict):
        raise ProbeRefusedError("the request body has no provider object")
    provider["max_price"] = {
        "prompt": float(profile.prompt_usd_per_mtok),
        "completion": float(profile.completion_usd_per_mtok),
    }
    return body


# ------------------------------------------------------------------------ transport


class _RecordingStream(httpx2.SyncByteStream):
    """Passes the answer body through as the client reads it, and keeps a copy of the bytes."""

    def __init__(self, inner: httpx2.SyncByteStream, sink: bytearray) -> None:
        self._inner = inner
        self._sink = sink

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            self._sink.extend(chunk)
            yield chunk

    def close(self) -> None:
        self._inner.close()


class _RecordingTransport(httpx2.BaseTransport):
    """Sends through ``inner`` (the network when None) and records what went out and came back.

    The body is recorded as the client reads it, so nothing is preloaded and the client still
    applies its own size limit.
    """

    def __init__(self, inner: httpx2.BaseTransport | None) -> None:
        self._inner: httpx2.BaseTransport = httpx2.HTTPTransport() if inner is None else inner
        self.sent: bytes | None = None
        self.status: int | None = None
        self.answer = bytearray()

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent = request.content
        response = self._inner.handle_request(request)
        self.status = response.status_code
        stream = response.stream
        if not isinstance(stream, httpx2.SyncByteStream):
            raise TypeError("the transport returned a response without a sync body stream")
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_RecordingStream(stream, self.answer),
            request=request,
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._inner.close()


# ------------------------------------------------------------------------ checks


def check_selection(content: str, request: ProbeRequest) -> dict[str, Any]:
    """Parse and ground the answer with the service's checks (ADR 041 §5)."""
    try:
        candidates = parse_selection_output(content)
    except SelectionOutputError as exc:
        return {
            "accepted": False,
            "parse_errors": list(exc.errors),
            "validate_errors": [],
            "candidate_count": None,
            "accepted_count": None,
            "dropped": [],
        }
    checked = validate_candidates(
        candidates, request.units, request.core_ordinals, DEFAULT_PROFILE.categories
    )
    return {
        "accepted": not checked.errors,
        "parse_errors": [],
        "validate_errors": list(checked.errors),
        "candidate_count": len(candidates),
        "accepted_count": len(checked.accepted),
        "dropped": [{"index": index, "reason": reason} for index, reason in checked.dropped],
    }


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def usage_of(answer: bytes) -> dict[str, int | None] | None:
    """Token counts from the answer's ``usage`` object; ``None`` when the answer has none."""
    try:
        payload: object = json.loads(answer)
    except ValueError:
        return None
    usage: object = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": _token_count(usage.get("prompt_tokens")),
        "completion_tokens": _token_count(usage.get("completion_tokens")),
    }


def sent_max_price(sent: bytes | None) -> object:
    """The ``max_price`` object as it went out in the request body, or ``None``."""
    if sent is None:
        return None
    try:
        payload: object = json.loads(sent)
    except ValueError:
        return None
    provider: object = payload.get("provider") if isinstance(payload, dict) else None
    price: object = provider.get("max_price") if isinstance(provider, dict) else None
    return price


def _answer(result: ChatResult, request: ProbeRequest, answer: bytes) -> _Outcome:
    selection = check_selection(result.content, request)
    return _Outcome(
        name="ok" if selection["accepted"] else "invalid_selection",
        fields={
            "error_kind": None,
            "error_message": None,
            "finish_reason": result.finish_reason,
            "cost_usd": None if result.cost is None else format(result.cost, "f"),
            "generation_id": result.generation_id,
            "usage": usage_of(answer),
            "selection": selection,
        },
    )


def _failure(error: ChatError) -> _Outcome:
    return _Outcome(
        name=_FAILURE_OUTCOMES.get(error.kind, "error_response"),
        fields={
            "error_kind": error.kind,
            "error_message": error.message,
            "finish_reason": None,
            "cost_usd": None,
            "generation_id": None,
            "usage": None,
            "selection": None,
        },
    )


# ------------------------------------------------------------------------- helpers


def model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-.") or "model"


def redact(text: str, key: str) -> str:
    return text.replace(key, "[REDACTED]") if key else text


def _utc_stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _render(summary: Mapping[str, Any], key: str) -> str:
    return redact(json.dumps(summary, indent=2, ensure_ascii=False), key)


def _write_summary(path: Path, text: str) -> None:
    path.write_text(text + "\n", encoding="utf-8")


def _eprint(message: str) -> None:
    print(message, file=sys.stderr)


def _prepare_out_dir(out_dir: Path) -> None:
    """Create the output directory and prove that a file can be written into it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=out_dir, prefix=".write-test-") as handle:
        handle.write(b"write test\n")
        handle.flush()


# ---------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analysis_probe",
        description=(
            "Send one synthetic selection request to one candidate model and record the answer. "
            f"The key is read from {API_KEY_ENV} only."
        ),
    )
    parser.add_argument("--model", required=True, help="OpenRouter model id to test")
    parser.add_argument(
        "--prompt-cap",
        required=True,
        help="prompt cap in USD per 1 000 000 tokens, a plain decimal such as 0.25",
    )
    parser.add_argument(
        "--completion-cap",
        required=True,
        help="completion cap in USD per 1 000 000 tokens, a plain decimal such as 2",
    )
    parser.add_argument(
        "--context-tokens", required=True, type=int, help="the model's context window in tokens"
    )
    parser.add_argument(
        "--max-tokens", required=True, type=int, help="completion limit sent as max_tokens"
    )
    parser.add_argument(
        "--max-price-as",
        choices=MAX_PRICE_FORMS,
        default="strings",
        help="send the provider.max_price caps as strings (default) or as JSON numbers",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx2.BaseTransport | None = None,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> int:
    args = build_parser().parse_args(argv)
    environment = os.environ if env is None else env

    try:
        profile = build_profile(args)
        request = build_request(profile, args.max_price_as)
    except ProbeRefusedError as exc:
        _eprint(f"refused: {exc}")
        return EXIT_REFUSED
    key = environment.get(API_KEY_ENV, "").strip()
    if not key:
        _eprint(f"refused: {API_KEY_ENV} is not set")
        return EXIT_REFUSED
    recorder = _RecordingTransport(transport)
    try:
        chat = OpenRouterChat(key, mode="production", transport=recorder)
    except ValueError:
        _eprint(f"refused: {API_KEY_ENV} is not a usable API key")
        return EXIT_REFUSED
    # Prove the output directory before spending a paid call.
    try:
        _prepare_out_dir(out_dir)
    except OSError as exc:
        _eprint(f"refused: output directory {out_dir} cannot be created or written: {exc}")
        return EXIT_REFUSED

    started_at = datetime.now(UTC)
    began = time.perf_counter()
    try:
        result = chat.complete(request.body)
    except ChatError as exc:
        outcome = _failure(exc)
    else:
        outcome = _answer(result, request, bytes(recorder.answer))
    latency_s = time.perf_counter() - began

    if outcome.name == "unknown_outcome":
        _eprint(
            "unknown_outcome: the request was sent but no answer arrived. The provider may have "
            "processed and billed it. Not retried; check the OpenRouter activity log."
        )
    elif outcome.name == "not_sent":
        _eprint("not_sent: the connection failed before the request was sent.")

    summary: dict[str, Any] = {
        "model": profile.model,
        "started_utc": started_at.isoformat(timespec="seconds"),
        "endpoint": ENDPOINT,
        "outcome": outcome.name,
        "status": recorder.status,
        "latency_s": round(latency_s, 3),
        "request": {
            "transcript": "built-in synthetic EN/ES",
            "unit_level": UNIT_LEVEL_SEGMENT,
            "unit_count": len(request.units),
            "request_bytes": request.request_size_bytes,
            "context_tokens": profile.context_tokens,
            "max_tokens": profile.max_tokens,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "max_price_as": args.max_price_as,
            "max_price": sent_max_price(recorder.sent),
        },
        **outcome.fields,
    }
    text = _render(summary, key)
    summary_path = out_dir / f"{model_slug(profile.model)}-{_utc_stamp(started_at)}.summary.json"
    try:
        _write_summary(summary_path, text)
    except OSError as exc:
        summary["save_error"] = {
            "type": type(exc).__name__,
            "message": redact(str(exc), key)[:ERROR_MESSAGE_LIMIT],
        }
        _eprint(
            "save_failed: the redacted summary could not be written and is printed to stdout. "
            "A request may already have been billed: check the OpenRouter activity log before "
            "rerunning."
        )
        print(_render(summary, key))
        return EXIT_SAVE_FAILED
    print(text)
    print(f"summary:  {summary_path}")
    return _EXIT_BY_OUTCOME[outcome.name]


if __name__ == "__main__":
    sys.exit(main())
