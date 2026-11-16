"""Offline tests of the OpenRouter STT adapter (ADR 029).

Every call goes through ``httpx2.MockTransport``: there is no network and no paid call.
The success fixtures are the real answers recorded by the live probe on 2026-11-15; the
402 fixture is the real answer of 2026-11-13.
"""

from __future__ import annotations

import base64
import contextlib
import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from pathlib import Path

import httpx2
import pytest

from clipforge.adapters import openrouter_key
from clipforge.adapters.openrouter_key import (
    KeyAccepted,
    KeyCheckError,
    KeyCheckResult,
    KeyRejected,
    check_openrouter_key,
)
from clipforge.adapters.openrouter_stt import (
    GEMINI_MODEL,
    MAI_MODEL,
    PRODUCTION_BASE_URL,
    OpenRouterStt,
    SttError,
    SttErrorKind,
    SttResult,
    build_request_body,
    resolve_base_url,
)
from clipforge.config import Config, ConfigError, Mode

from .support import FakeProvider, key_info, refuse_env_proxies

KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
AUDIO = b"RIFF\x24\x00\x00\x00WAVEfmt fake audio bytes"
NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "openrouter"
ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"
GENERATION_ID = "gen-stt-1794747983-qXdd7kmxgumWnuQ2t7Ni"

Handler = Callable[[httpx2.Request], httpx2.Response]


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def ok(payload: object, *, headers: Mapping[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(200, content=json.dumps(payload).encode("utf-8"), headers=headers)


def make_client(
    handler: Handler, *, mode: Mode = "production", base_url: str = PRODUCTION_BASE_URL
) -> tuple[OpenRouterStt, list[httpx2.Request]]:
    """A client whose transport records every request that reaches it."""
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = OpenRouterStt(
        KEY,
        mode=mode,
        base_url=base_url,
        transport=httpx2.MockTransport(recording),
        now=lambda: NOW,
    )
    return client, seen


def spoken(**extra: object) -> dict[str, object]:
    """A minimal valid answer with speech: text plus one timed word."""
    return {"text": "a", "words": [{"word": "a", "start": 0, "end": 1}], **extra}


def reply(response: httpx2.Response) -> Handler:
    return lambda _request: response


def transcribe(
    client: OpenRouterStt,
    model: str = MAI_MODEL,
    *,
    language: str | None = None,
    provider_options: Mapping[str, object] | None = None,
) -> SttResult:
    return client.transcribe(
        model=model, audio_wav=AUDIO, language=language, provider_options=provider_options
    )


def failure(handler: Handler, model: str = MAI_MODEL) -> tuple[SttError, list[httpx2.Request]]:
    client, seen = make_client(handler)
    with pytest.raises(SttError) as caught:
        transcribe(client, model)
    return caught.value, seen


def body_of(request: httpx2.Request) -> dict[str, object]:
    parsed: dict[str, object] = json.loads(request.content)
    return parsed


# ------------------------------------------------------------------------- request


def test_mai_request_is_exact() -> None:
    client, seen = make_client(reply(ok({"text": ""})))
    options = {"test_option": {"value": 1}}

    transcribe(client, MAI_MODEL, language="en", provider_options=options)

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["Content-Type"] == "application/json"
    assert body_of(request) == {
        "model": "microsoft/mai-transcribe-2",
        "input_audio": {"data": base64.b64encode(AUDIO).decode("ascii"), "format": "wav"},
        "temperature": 0,
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment", "word"],
        "language": "en",
        "provider": {"options": {"azure": {"test_option": {"value": 1}}}},
    }
    assert KEY not in request.content.decode("utf-8")
    assert KEY not in str(request.url)


@pytest.mark.parametrize("options", [None, {}])
def test_mai_without_options_and_language_has_neither(options: Mapping[str, object] | None) -> None:
    client, seen = make_client(reply(ok({"text": ""})))

    transcribe(client, MAI_MODEL, language=None, provider_options=options)

    assert body_of(seen[0]) == {
        "model": "microsoft/mai-transcribe-2",
        "input_audio": {"data": base64.b64encode(AUDIO).decode("ascii"), "format": "wav"},
        "temperature": 0,
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment", "word"],
    }


def test_gemini_request_has_no_provider_options() -> None:
    client, seen = make_client(reply(ok({"text": ""})))

    transcribe(client, GEMINI_MODEL, language="uk", provider_options={"test_option": 1})
    transcribe(client, GEMINI_MODEL)

    expected = {
        "model": "google/gemini-3.5-transcribe",
        "input_audio": {"data": base64.b64encode(AUDIO).decode("ascii"), "format": "wav"},
        "temperature": 0,
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment", "word"],
    }
    assert body_of(seen[0]) == {**expected, "language": "uk"}
    assert body_of(seen[1]) == expected
    assert "provider" not in body_of(seen[0])


def test_request_uses_the_documented_timeouts() -> None:
    client, seen = make_client(reply(ok({"text": ""})))

    transcribe(client)

    assert seen[0].extensions["timeout"] == {
        "connect": 10.0,
        "read": 120.0,
        "write": 60.0,
        "pool": 10.0,
    }


def test_unknown_model_and_empty_audio_are_refused_before_sending() -> None:
    client, seen = make_client(reply(ok({"text": ""})))

    with pytest.raises(ValueError, match="STT-модель"):
        client.transcribe(model="openai/whisper-1", audio_wav=AUDIO)
    with pytest.raises(ValueError, match="порожнє"):
        client.transcribe(model=MAI_MODEL, audio_wav=b"")
    with pytest.raises(ValueError, match="STT-модель"):
        build_request_body("nope", AUDIO)

    assert seen == []


def test_test_mode_base_url_is_used_for_the_endpoint() -> None:
    client, seen = make_client(
        reply(ok({"text": ""})), mode="test", base_url="http://127.0.0.1:8123/v1/"
    )

    transcribe(client)

    assert str(seen[0].url) == "http://127.0.0.1:8123/v1/audio/transcriptions"


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8123/v1", "http://localhost:9/v1/"])
def test_production_refuses_a_loopback_base_url(base_url: str) -> None:
    with pytest.raises(ConfigError, match="лише на") as caught:
        OpenRouterStt(KEY, mode="production", base_url=base_url)
    with pytest.raises(ConfigError):
        OpenRouterStt(KEY, base_url=base_url)  # production is the default mode

    assert KEY not in str(caught.value)


def test_production_accepts_the_openrouter_url_only() -> None:
    client = OpenRouterStt(KEY, mode="production", base_url=PRODUCTION_BASE_URL)

    assert repr(client) == f"OpenRouterStt(base_url={PRODUCTION_BASE_URL!r})"
    with pytest.raises(ConfigError):
        OpenRouterStt(KEY, mode="production", base_url=PRODUCTION_BASE_URL + "/")


def test_test_mode_refuses_the_openrouter_url_because_paid_ai_is_off_in_tests() -> None:
    with pytest.raises(ConfigError) as caught:
        OpenRouterStt(KEY, mode="test")  # default base_url is OpenRouter
    with pytest.raises(ConfigError):
        OpenRouterStt(KEY, mode="test", base_url=PRODUCTION_BASE_URL)

    assert KEY not in str(caught.value)


@pytest.mark.parametrize("mode", ["production", "test"])
@pytest.mark.parametrize(
    "base_url",
    [
        "https://evil.example/v1",
        "http://openrouter.ai/api/v1",
        "http://127.0.0.1.evil.test/v1",
        "http://user:pass@127.0.0.1/v1",
        "https://127.0.0.1/v1",
    ],
)
def test_key_is_never_sent_to_an_unapproved_address(mode: Mode, base_url: str) -> None:
    with pytest.raises(ConfigError) as caught:
        OpenRouterStt(KEY, mode=mode, base_url=base_url)

    assert KEY not in str(caught.value)


@pytest.mark.parametrize("bad_key", ["", " ", "sk with space", "sk\nnewline", "ключ"])
def test_invalid_key_is_refused_without_echoing_it(bad_key: str) -> None:
    with pytest.raises(ValueError, match="Ключ API") as caught:
        OpenRouterStt(bad_key)

    assert str(caught.value) == "Ключ API порожній або містить пробіли чи недрукований символ"


def test_resolve_base_url_follows_config_mode(tmp_path: Path) -> None:
    token = "t" * 32
    production = Config(mode="production", data_dir=tmp_path, session_token=token)
    test_with_url = Config(
        mode="test",
        data_dir=tmp_path,
        session_token=token,
        test_provider_base_url="http://127.0.0.1:9/v1",
    )
    test_without_url = Config(mode="test", data_dir=tmp_path, session_token=token)

    assert resolve_base_url(production) == "https://openrouter.ai/api/v1"
    assert resolve_base_url(test_with_url) == "http://127.0.0.1:9/v1"
    with pytest.raises(ConfigError, match="тестового провайдера"):
        resolve_base_url(test_without_url)


# ------------------------------------------------------------------ live fixtures


def test_mai_live_fixture_parses() -> None:
    raw = fixture_bytes("mai_verbose.json")
    expected = json.loads(raw)
    client, _ = make_client(
        reply(httpx2.Response(200, content=raw, headers={"X-Generation-Id": GENERATION_ID}))
    )

    result = transcribe(client, MAI_MODEL)

    assert result.has_word_timing is True
    assert len(result.words) == 104
    assert len(result.segments) == 1
    assert result.text == expected["text"]
    assert result.language == "en"
    assert result.duration_s == 47.12
    assert result.generation_id == GENERATION_ID
    assert result.usage.seconds == 48
    assert result.usage.cost == Decimal("0.0013333333333333333")
    assert result.usage.tokens is None
    assert (result.dropped_words, result.dropped_segments) == (0, 0)
    first, last = result.words[0], result.words[-1]
    assert (first.text, first.start_s, first.end_s) == ("Good", 0.16, 0.299)
    assert (last.text, last.start_s, last.end_s) == ("listening.", 45.902, 46.381)
    assert first.speaker is None
    assert first.confidence is None
    assert result.segments[0].start_s == 0
    assert result.segments[0].end_s == 47.125
    assert [word.text for word in result.words] == [
        entry["word"].strip() for entry in expected["words"]
    ]


def test_gemini_live_fixture_parses_and_keeps_the_zero_length_word() -> None:
    raw = fixture_bytes("gemini_verbose.json")
    expected = json.loads(raw)
    client, _ = make_client(reply(httpx2.Response(200, content=raw)))

    result = transcribe(client, GEMINI_MODEL)

    assert result.has_word_timing is True
    assert len(result.words) == 101
    assert len(result.segments) == 1
    assert result.text == expected["text"]
    assert result.language is None
    assert result.duration_s is None
    assert result.generation_id is None
    assert result.usage.cost == Decimal("0.004148")
    assert result.usage.tokens == 1329
    assert result.usage.seconds is None
    zero_length = [word for word in result.words if word.start_s == word.end_s]
    assert [(word.text, word.start_s, word.end_s) for word in zero_length] == [("in", 40.7, 40.7)]
    assert result.segments[0].end_s == 46.4
    assert (result.dropped_words, result.dropped_segments) == (0, 0)


def test_mai_fixture_has_no_zero_length_word_so_the_gemini_one_is_real() -> None:
    mai = json.loads(fixture_bytes("mai_verbose.json"))
    gemini = json.loads(fixture_bytes("gemini_verbose.json"))

    assert [w for w in mai["words"] if w["start"] == w["end"]] == []
    assert [w["word"] for w in gemini["words"] if w["start"] == w["end"]] == ["in"]


# -------------------------------------------------------------- HTTP error mapping


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "auth"),
        (403, "auth"),
        (402, "funds"),
        (404, "model_unavailable"),
        (413, "payload_too_large"),
        (429, "rate_limited"),
        (400, "invalid_request"),
        (422, "invalid_request"),
        (418, "invalid_request"),
        (451, "invalid_request"),
        (408, "upstream"),
        (500, "upstream"),
        (502, "upstream"),
        (503, "upstream"),
        (504, "upstream"),
        (599, "upstream"),
    ],
)
def test_http_status_maps_to_one_error_kind_after_one_attempt(
    status: int, kind: SttErrorKind
) -> None:
    error, seen = failure(reply(httpx2.Response(status, json={"error": {"message": "x"}})))

    assert error.kind == kind
    assert error.status == status
    assert error.retry_after_s is None
    assert error.generation_id is None
    assert len(seen) == 1


def test_funds_error_from_the_live_402_body() -> None:
    error, seen = failure(reply(httpx2.Response(402, content=fixture_bytes("error_402.json"))))

    assert (error.kind, error.status, error.generation_id) == ("funds", 402, None)
    assert "$0,50" in str(error)
    assert "Поповніть" in str(error)
    # Provider text and its remedy hint are never copied into the message.
    assert "openrouter.ai" not in str(error)
    assert "lower max_tokens" not in str(error)
    assert len(seen) == 1


def test_messages_are_ukrainian_and_point_to_the_next_step() -> None:
    auth, _ = failure(reply(httpx2.Response(401)))
    invalid, _ = failure(reply(httpx2.Response(422)))
    upstream, _ = failure(reply(httpx2.Response(503)))

    assert "Налаштуваннях" in str(auth)
    assert "HTTP 422" in str(invalid)
    assert "HTTP 503" in str(upstream)
    assert str(auth) == auth.message


def test_generation_id_is_kept_on_error_answers() -> None:
    error, _ = failure(reply(httpx2.Response(503, headers={"X-Generation-Id": " gen-xyz "})))

    assert error.generation_id == "gen-xyz"


@pytest.mark.parametrize("status", [100, 201, 204, 301, 302, 304])
def test_statuses_that_are_neither_200_nor_errors_are_invalid_responses(status: int) -> None:
    error, seen = failure(
        reply(httpx2.Response(status, content=b'{"text": "ok"}', headers={"Location": "/x"}))
    )

    assert error.kind == "invalid_response"
    assert error.status == status
    assert len(seen) == 1


# ----------------------------------------------------------------------- Retry-After


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7.0),
        (" 120 ", 120.0),
        ("0", 0.0),
        (format_datetime(NOW + timedelta(seconds=90), usegmt=True), 90.0),
        (format_datetime(NOW + timedelta(minutes=5, seconds=1), usegmt=True), 301.0),
        (format_datetime(NOW - timedelta(seconds=30), usegmt=True), 0.0),
        ("Sun, 15 Nov 2026 12:00:45 GMT", 45.0),
        ("Sun, 15 Nov 2026 12:00:45 -0000", 45.0),
        ("-5", None),
        ("1.5", None),
        ("soon", None),
        ("", None),
        ("9" * 5000, None),
    ],
)
def test_rate_limited_reads_retry_after(header: str, expected: float | None) -> None:
    error, seen = failure(reply(httpx2.Response(429, headers={"Retry-After": header})))

    assert error.kind == "rate_limited"
    assert error.retry_after_s == expected
    assert len(seen) == 1


def test_rate_limited_without_retry_after_and_with_generation_id() -> None:
    error, _ = failure(reply(httpx2.Response(429, headers={"X-Generation-Id": GENERATION_ID})))

    assert error.retry_after_s is None
    assert error.generation_id == GENERATION_ID


# ------------------------------------------------------------------ transport errors


def raising(exc_type: type[httpx2.TransportError]) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc_type("boom", request=request)

    return handler


@pytest.mark.parametrize(
    ("exc_type", "kind"),
    [
        (httpx2.ConnectError, "not_sent"),
        (httpx2.ConnectTimeout, "not_sent"),
        (httpx2.PoolTimeout, "not_sent"),
        (httpx2.ReadTimeout, "unknown_outcome"),
        (httpx2.WriteTimeout, "unknown_outcome"),
        (httpx2.ReadError, "unknown_outcome"),
        (httpx2.WriteError, "unknown_outcome"),
        (httpx2.RemoteProtocolError, "unknown_outcome"),
    ],
)
def test_transport_failure_is_classified_by_whether_the_request_was_sent(
    exc_type: type[httpx2.TransportError], kind: SttErrorKind
) -> None:
    error, seen = failure(raising(exc_type))

    assert error.kind == kind
    assert error.status is None
    assert error.retry_after_s is None
    assert error.generation_id is None
    assert len(seen) == 1
    # The transport exception holds the request with the key header: it must not be chained.
    assert error.__cause__ is None
    assert error.__context__ is None


def test_read_timeout_message_says_the_outcome_is_unknown_and_not_retried() -> None:
    error, _ = failure(raising(httpx2.ReadTimeout))

    assert error.kind == "unknown_outcome"
    assert "невідомо" in str(error).lower()
    assert "повтору немає" in str(error)


# ------------------------------------------------------ 200 validation: rejections


def test_a_200_with_no_usable_body_is_invalid() -> None:
    cases: list[bytes] = [
        b"",
        b"not json",
        b"\xff\xfe\x00",
        b"[]",
        b'"text"',
        b"null",
        b"{}",
        b'{"error": {"code": 500, "message": "upstream"}}',
        b'{"text": null}',
        b'{"text": 5}',
        b"[" * 100_000,
    ]
    for content in cases:
        error, seen = failure(
            reply(httpx2.Response(200, content=content, headers={"X-Generation-Id": "gen-1"}))
        )

        assert error.kind == "invalid_response", content[:20]
        assert error.status == 200
        assert error.generation_id == "gen-1"
        assert len(seen) == 1


@pytest.mark.parametrize("field_name", ["words", "segments"])
@pytest.mark.parametrize(
    "bad_items",
    [
        "nope",
        {"word": "a"},
        5,
        ["a"],
        [None],
        [[]],
        [{"start": 0, "end": 1}],
        [{"word": "a", "text": "a", "start": 0}],
        [{"word": "a", "text": "a", "end": 1}],
        [{"word": None, "text": None, "start": 0, "end": 1}],
        [{"word": 7, "text": 7, "start": 0, "end": 1}],
        [{"word": "a", "text": "a", "start": "0", "end": 1}],
        [{"word": "a", "text": "a", "start": 0, "end": "1"}],
        [{"word": "a", "text": "a", "start": True, "end": 1}],
        [{"word": "a", "text": "a", "start": 0, "end": False}],
        [{"word": "a", "text": "a", "start": None, "end": 1}],
        [{"word": "a", "text": "a", "start": 2, "end": 1}],
        [{"word": "a", "text": "a", "start": -3, "end": -2}],
        [{"word": "a", "text": "a", "start": 0, "end": -0.5}],
    ],
)
def test_an_invalid_item_invalidates_the_whole_answer(field_name: str, bad_items: object) -> None:
    good = {"word": "ok", "text": "ok", "start": 0, "end": 1}
    # The good first entry must not rescue the answer when a later one is bad.
    items = [good, *bad_items] if isinstance(bad_items, list) else bad_items
    error, seen = failure(
        reply(ok({"text": "ok a", field_name: items}, headers={"X-Generation-Id": "gen-2"}))
    )

    assert error.kind == "invalid_response"
    assert error.status == 200
    assert error.generation_id == "gen-2"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "literal",
    [b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1e999"],
)
def test_non_finite_times_are_invalid(literal: bytes) -> None:
    for position in ("start", "end"):
        times = {"start": b"0", "end": b"1", position: literal}
        content = (
            b'{"text": "a", "words": [{"word": "a", "start": '
            + times["start"]
            + b', "end": '
            + times["end"]
            + b"}]}"
        )

        error, _ = failure(reply(httpx2.Response(200, content=content)))

        assert error.kind == "invalid_response", (literal, position)


def test_a_blank_item_with_broken_times_is_still_invalid() -> None:
    payload = {"text": "", "words": [{"word": " ", "start": 3, "end": 1}]}

    error, _ = failure(reply(ok(payload)))

    assert error.kind == "invalid_response"


def test_invalid_item_detail_names_the_item_but_not_the_provider_text() -> None:
    payload = {
        "text": "secret transcript",
        "words": [
            {"word": "fine", "start": 0, "end": 1},
            {"word": "secret", "start": 5, "end": 2},
        ],
    }

    error, _ = failure(reply(ok(payload)))

    assert "Слово №2" in str(error)
    assert "secret" not in str(error)


# ------------------------------------------------ 200 validation: accepted shapes


def test_blank_items_are_dropped_and_counted() -> None:
    payload = {
        "text": "hello world",
        "words": [
            {"word": "hello", "start": 0, "end": 0.5},
            {"word": "", "start": 0.5, "end": 0.6},
            {"word": "  ", "start": 0.6, "end": 0.6},
            {"word": " world ", "start": 0.7, "end": 1.2},
        ],
        "segments": [
            {"text": "", "start": 0, "end": 0.1},
            {"text": "hello world", "start": 0, "end": 1.2},
            {"text": "\n", "start": 1.2, "end": 1.3},
        ],
    }
    client, _ = make_client(reply(ok(payload)))

    result = transcribe(client)

    assert [word.text for word in result.words] == ["hello", "world"]
    assert [segment.text for segment in result.segments] == ["hello world"]
    assert (result.dropped_words, result.dropped_segments) == (2, 2)
    assert result.has_word_timing is True


def test_a_slightly_negative_start_and_equal_times_are_kept_for_the_merge_to_judge() -> None:
    payload = {
        "text": "a b",
        "words": [
            {"word": "a", "start": -0.04, "end": 0.2},
            {"word": "b", "start": 0.5, "end": 0.5},
        ],
    }
    client, _ = make_client(reply(ok(payload)))

    result = transcribe(client)

    assert [(w.text, w.start_s, w.end_s) for w in result.words] == [
        ("a", -0.04, 0.2),
        ("b", 0.5, 0.5),
    ]


def test_integer_times_become_floats() -> None:
    client, _ = make_client(
        reply(ok({"text": "a", "words": [{"word": "a", "start": 0, "end": 2}]}))
    )

    word = transcribe(client).words[0]

    assert isinstance(word.start_s, float)
    assert (word.start_s, word.end_s) == (0.0, 2.0)


@pytest.mark.parametrize("extra", [{}, {"words": None}, {"words": []}])
def test_text_without_words_means_segment_timing_only(extra: dict[str, object]) -> None:
    payload = {
        "text": "hello world",
        "segments": [{"text": "hello world", "start": 0, "end": 2}],
        **extra,
    }
    client, _ = make_client(reply(ok(payload)))

    result = transcribe(client)

    assert result.has_word_timing is False
    assert result.words == ()
    assert len(result.segments) == 1
    assert result.text == "hello world"


def test_empty_text_and_no_items_is_silence_not_an_error() -> None:
    for payload in (
        {"text": ""},
        {"text": "", "words": [], "segments": []},
        {"text": "", "words": None, "segments": None, "usage": {"cost": 0}},
    ):
        client, seen = make_client(reply(ok(payload)))

        result = transcribe(client)

        assert (result.text, result.words, result.segments) == ("", (), ())
        assert result.has_word_timing is False
        assert (result.dropped_words, result.dropped_segments) == (0, 0)
        assert len(seen) == 1


def test_blank_text_and_no_items_is_silence_too() -> None:
    client, _ = make_client(reply(ok({"text": " \n\t ", "words": [], "segments": []})))

    result = transcribe(client)

    assert (result.words, result.segments) == ((), ())
    assert result.has_word_timing is False


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"words": None, "segments": None},
        {"words": [], "segments": []},
        # Every entry is blank and dropped: nothing usable is left.
        {"words": [{"word": " ", "start": 0, "end": 1}], "segments": []},
        {"words": [], "segments": [{"text": "", "start": 0, "end": 1}]},
        {
            "words": [{"word": "", "start": 0, "end": 1}],
            "segments": [{"text": "\n", "start": 0, "end": 1}],
        },
    ],
)
def test_text_without_any_usable_item_is_invalid_not_silence(extra: dict[str, object]) -> None:
    error, seen = failure(
        reply(
            ok({"text": "secret speech", **extra}, headers={"X-Generation-Id": GENERATION_ID}),
        )
    )

    assert error.kind == "invalid_response"
    assert error.status == 200
    assert error.generation_id == GENERATION_ID
    assert "secret" not in str(error)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"words": [{"word": "a", "start": 0, "end": 1}]},
        {"segments": [{"text": "a", "start": 0, "end": 1}]},
    ],
)
def test_text_with_one_usable_word_or_segment_is_a_success(extra: dict[str, object]) -> None:
    client, _ = make_client(reply(ok({"text": "a", **extra})))

    result = transcribe(client)

    assert result.text == "a"
    assert len(result.words) + len(result.segments) == 1


def test_optional_fields_default_to_none_when_absent_null_or_mistyped() -> None:
    for extra in (
        {},
        {"language": None, "duration": None, "usage": None},
        {"language": 5, "duration": "47", "usage": "free"},
        {"language": "  ", "duration": -1, "usage": []},
        {"language": True, "duration": True},
        {"duration": float("nan")},
    ):
        client, _ = make_client(
            reply(httpx2.Response(200, content=json.dumps(spoken(**extra)).encode()))
        )

        result = transcribe(client)

        assert result.language is None, extra
        assert result.duration_s is None, extra
        assert result.usage.cost is None, extra
        assert result.usage.seconds is None, extra
        assert result.usage.tokens is None, extra
        assert result.generation_id is None


def test_language_and_duration_are_passed_through_unchanged() -> None:
    client, _ = make_client(reply(ok(spoken(language="english", duration=12.5))))

    result = transcribe(client)

    assert (result.language, result.duration_s) == ("english", 12.5)


def test_speaker_and_confidence_are_kept_when_valid_and_none_otherwise() -> None:
    payload = {
        "text": "a b c",
        "words": [
            {"word": "a", "start": 0, "end": 1, "speaker": "S1", "confidence": 0.75},
            {"word": "b", "start": 1, "end": 2},
            {"word": "c", "start": 2, "end": 3, "speaker": 4, "confidence": "high"},
        ],
        "segments": [{"text": "a b c", "start": 0, "end": 3, "speaker": "S1", "confidence": 1}],
    }
    client, _ = make_client(reply(ok(payload)))

    result = transcribe(client)

    assert [(w.speaker, w.confidence) for w in result.words] == [
        ("S1", 0.75),
        (None, None),
        (None, None),
    ]
    assert (result.segments[0].speaker, result.segments[0].confidence) == ("S1", 1.0)


@pytest.mark.parametrize(
    ("usage", "cost", "seconds", "tokens"),
    [
        ({"cost": 0.004148}, Decimal("0.004148"), None, None),
        (
            {"cost": 0.0013333333333333333, "seconds": 48},
            Decimal("0.0013333333333333333"),
            48,
            None,
        ),
        ({"cost": 1}, Decimal(1), None, None),
        ({"cost": 0}, Decimal(0), None, None),
        ({"cost": 0.0}, Decimal("0.0"), None, None),
        ({"cost": "0.5"}, None, None, None),
        ({"cost": True}, None, None, None),
        ({"cost": None}, None, None, None),
        ({"cost": -0.01}, None, None, None),
        ({"cost": -1}, None, None, None),
        ({"cost": [0.1]}, None, None, None),
        ({"total_tokens": 1329, "input_tokens": 1180, "output_tokens": 149}, None, None, 1329),
        ({"total_tokens": "9"}, None, None, None),
        ({"total_tokens": True}, None, None, None),
        ({"total_tokens": -1}, None, None, None),
        ({"total_tokens": 1.5}, None, None, None),
        ({"seconds": 2.5}, None, 2.5, None),
        ({"seconds": -2}, None, None, None),
        ({}, None, None, None),
    ],
)
def test_usage_is_read_only_when_it_is_a_valid_number(
    usage: dict[str, object], cost: Decimal | None, seconds: float | None, tokens: int | None
) -> None:
    client, _ = make_client(reply(ok(spoken(usage=usage))))

    result = transcribe(client).usage

    assert result.cost == cost
    assert (result.cost is None) == (cost is None)
    assert result.seconds == seconds
    assert result.tokens == tokens


def test_cost_is_exact_decimal_text_of_the_json_number() -> None:
    client, _ = make_client(
        reply(
            httpx2.Response(
                200,
                content=(
                    b'{"text": "a", "words": [{"word": "a", "start": 0, "end": 1}],'
                    b' "usage": {"cost": 0.1}}'
                ),
            )
        )
    )

    cost = transcribe(client).usage.cost

    assert cost == Decimal("0.1")
    assert str(cost) == "0.1"


def test_generation_id_comes_only_from_the_header() -> None:
    body = spoken(id="gen-from-body", generation_id="gen-from-body")
    for headers, expected in (
        ({"X-Generation-Id": GENERATION_ID}, GENERATION_ID),
        ({"x-generation-id": "  gen-lower  "}, "gen-lower"),
        ({"X-Generation-Id": "   "}, None),
        ({}, None),
    ):
        client, _ = make_client(reply(ok(body, headers=headers)))

        assert transcribe(client).generation_id == expected


def test_result_is_immutable() -> None:
    client, _ = make_client(reply(ok(spoken())))
    result = transcribe(client)

    with pytest.raises(AttributeError):
        result.text = "b"  # type: ignore[misc]


# --------------------------------------------------------------------- one attempt


def test_every_failure_is_a_single_attempt_and_success_too() -> None:
    attempts: dict[str, int] = {}
    for name, handler in {
        "429": reply(httpx2.Response(429, headers={"Retry-After": "1"})),
        "503": reply(httpx2.Response(503)),
        "500": reply(httpx2.Response(500)),
        "timeout": raising(httpx2.ReadTimeout),
        "refused": raising(httpx2.ConnectError),
        "invalid": reply(httpx2.Response(200, content=b"nope")),
        "ok": reply(ok(spoken())),
    }.items():
        client, seen = make_client(handler)
        with contextlib.suppress(SttError):
            transcribe(client)
        attempts[name] = len(seen)

    assert set(attempts.values()) == {1}


# ------------------------------------------------------------------ key never leaks


def test_key_never_appears_in_errors_results_or_the_client() -> None:
    echo_key = httpx2.Response(401, json={"error": {"message": f"bad key {KEY}"}})
    texts: list[str] = []

    def collect_error(error: SttError) -> None:
        texts.extend([str(error), repr(error), error.message, repr(error.args), repr(vars(error))])
        assert error.__cause__ is None
        assert error.__context__ is None

    handlers: list[Handler] = [
        reply(echo_key),
        reply(httpx2.Response(402, content=fixture_bytes("error_402.json"))),
        reply(httpx2.Response(429, headers={"Retry-After": "3"})),
        reply(httpx2.Response(500, headers={"X-Generation-Id": GENERATION_ID})),
        reply(httpx2.Response(200, content=b"not json")),
        reply(ok({"text": "x", "words": [{"word": "x", "start": 2, "end": 1}]})),
        raising(httpx2.ConnectError),
        raising(httpx2.ReadTimeout),
    ]
    for handler in handlers:
        error, _ = failure(handler)
        collect_error(error)

    for model, fixture in ((MAI_MODEL, "mai_verbose.json"), (GEMINI_MODEL, "gemini_verbose.json")):
        client, _ = make_client(reply(httpx2.Response(200, content=fixture_bytes(fixture))))
        result = transcribe(client, model)
        texts.extend([str(result), repr(result), repr(result.usage), repr(client), str(client)])
        assert not hasattr(client, "__dict__")

    assert len(texts) > 30
    assert all(KEY not in text for text in texts)
    assert all("0123456789abcdef" not in text for text in texts)


def test_result_repr_leaves_out_user_content() -> None:
    client, _ = make_client(
        reply(ok({"text": "private words", "words": [{"word": "private", "start": 0, "end": 1}]}))
    )

    result = transcribe(client)

    assert "private" not in repr(result)
    assert "has_word_timing=True" in repr(result)


# ------------------------------------------------------------------ key check (GET /key)
# Against the loopback fake provider, with the real httpx2 transport: no network, no paid call.


def key_check(provider: FakeProvider, *, key: str = KEY) -> KeyCheckResult:
    return check_openrouter_key(key, mode="test", base_url=provider.base_url)


def key_check_failure(provider: FakeProvider) -> KeyCheckError:
    with pytest.raises(KeyCheckError) as caught:
        key_check(provider)
    return caught.value


def assert_key_check_error_is_clean(error: KeyCheckError) -> None:
    """Nothing of the key or of the provider's text in the error, and no chained exception (a
    transport error carries the request, and the request carries the key header)."""
    for text in (str(error), repr(error), error.message):
        assert KEY not in text
        assert "0123456789abcdef" not in text
        assert KEY_ECHO not in text
    assert error.__cause__ is None
    assert error.__context__ is None


KEY_ECHO = "Hq5Tn8Wd3Pz1"


def test_the_key_check_sends_one_get_with_the_key_in_the_header_only(
    fake_provider: FakeProvider,
) -> None:
    result = key_check(fake_provider)

    assert result == KeyAccepted(is_free_tier=False, limit_remaining_usd="87.5")
    [request] = fake_provider.requests
    assert (request.method, request.path, request.body) == ("GET", "/api/v1/key", b"")
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["accept"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"
    assert KEY not in request.path


def test_the_key_check_ignores_the_proxy_variables_of_the_environment(
    fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    refuse_env_proxies(monkeypatch)

    assert key_check(fake_provider) == KeyAccepted(is_free_tier=False, limit_remaining_usd="87.5")
    assert len(fake_provider.requests) == 1


def test_a_transcription_ignores_the_proxy_variables_of_the_environment(
    fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key and the audio go to the address the mode allows, never to a proxy of the
    environment (``trust_env=False``): the proxy here is a port nobody listens on."""
    refuse_env_proxies(monkeypatch)
    fake_provider.reply(body=json.dumps(spoken()).encode())
    client = OpenRouterStt(KEY, mode="test", base_url=fake_provider.base_url)

    result = transcribe(client)

    assert result.text == "a"
    [request] = fake_provider.requests
    assert (request.method, request.path) == ("POST", "/api/v1/audio/transcriptions")
    assert request.headers["authorization"] == f"Bearer {KEY}"


def test_the_key_check_ignores_a_trailing_slash_of_the_base_url(
    fake_provider: FakeProvider,
) -> None:
    check_openrouter_key(KEY, mode="test", base_url=fake_provider.base_url + "/")

    assert [request.path for request in fake_provider.requests] == ["/api/v1/key"]


def test_the_key_check_reports_a_401_as_a_rejected_key(fake_provider: FakeProvider) -> None:
    fake_provider.reply(401, f'{{"error":"{KEY} {KEY_ECHO}"}}'.encode())

    assert key_check(fake_provider) == KeyRejected()
    assert len(fake_provider.requests) == 1


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (500, "upstream"),
        (502, "upstream"),
        (599, "upstream"),
        (201, "invalid_response"),
        (302, "invalid_response"),
        (400, "invalid_response"),
        (402, "invalid_response"),
        (403, "invalid_response"),
        (404, "invalid_response"),
        (408, "invalid_response"),
        (429, "invalid_response"),
    ],
)
def test_the_key_check_turns_any_other_status_into_an_error_after_one_attempt(
    fake_provider: FakeProvider, status: int, kind: str
) -> None:
    fake_provider.reply(
        status, f'{{"error":"{KEY} {KEY_ECHO}"}}'.encode(), headers={"Location": "/api/v1/other"}
    )

    error = key_check_failure(fake_provider)

    assert (error.kind, error.status) == (kind, status)
    assert error.message.endswith(f"(HTTP {status})")
    assert_key_check_error_is_clean(error)
    # One attempt: no retry on a 5xx and no redirect that would carry the key elsewhere.
    assert [request.path for request in fake_provider.requests] == ["/api/v1/key"]


def test_the_key_check_waits_at_most_the_timeout(
    fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert openrouter_key.CHECK_TIMEOUT_S == 15.0
    monkeypatch.setattr(openrouter_key, "CHECK_TIMEOUT_S", 0.3)
    fake_provider.reply(delay_s=10)

    started = time.monotonic()
    error = key_check_failure(fake_provider)

    assert time.monotonic() - started < 5
    assert (error.kind, error.status) == ("upstream", None)
    assert_key_check_error_is_clean(error)
    assert len(fake_provider.requests) == 1


def test_the_key_check_has_a_deadline_for_the_whole_call(
    fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openrouter_key, "CHECK_TIMEOUT_S", 0.3)
    fake_provider.reply(drip_s=0.05)  # a byte every 50 ms: no single read ever times out

    started = time.monotonic()
    error = key_check_failure(fake_provider)

    assert time.monotonic() - started < 5
    assert error.kind == "upstream"
    assert_key_check_error_is_clean(error)


def test_the_key_check_has_a_deadline_for_the_headers_too(
    fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openrouter_key, "CHECK_TIMEOUT_S", 1.0)
    fake_provider.reply(header_drip_s=0.1)  # the head alone takes about 8 s, a byte at a time

    started = time.monotonic()
    error = key_check_failure(fake_provider)

    assert time.monotonic() - started < 3
    assert (error.kind, error.status) == ("upstream", None)
    assert_key_check_error_is_clean(error)
    assert fake_provider.wait_idle(2)  # the connection was closed at the deadline


def test_the_key_check_reports_an_answer_cut_short_as_upstream(fake_provider: FakeProvider) -> None:
    body = key_info()
    fake_provider.reply(body=body[:20], claimed_length=len(body))

    error = key_check_failure(fake_provider)

    assert error.kind == "upstream"
    assert_key_check_error_is_clean(error)


def test_the_key_check_reports_a_refused_connection_as_not_sent(
    fake_provider: FakeProvider,
) -> None:
    fake_provider.close()

    error = key_check_failure(fake_provider)

    assert (error.kind, error.status) == ("not_sent", None)
    assert_key_check_error_is_clean(error)


@pytest.mark.parametrize(
    "body",
    [
        f"not json {KEY} {KEY_ECHO}".encode(),
        b"",
        b"[]",
        b'{"data": {}}',
        key_info(is_free_tier="false"),
        key_info(limit_remaining="5"),
        b'{"data": "' + b"x" * (openrouter_key.MAX_BODY_BYTES + 1) + b'"}',
    ],
    ids=[
        "not json",
        "empty",
        "a list",
        "no is_free_tier",
        "is_free_tier is a string",
        "limit_remaining is a string",
        "over the size limit",
    ],
)
def test_the_key_check_turns_an_unusable_200_into_an_invalid_response(
    fake_provider: FakeProvider, body: bytes
) -> None:
    fake_provider.reply(body=body)

    error = key_check_failure(fake_provider)

    assert (error.kind, error.status) == ("invalid_response", None)
    assert_key_check_error_is_clean(error)
    assert len(fake_provider.requests) == 1


@pytest.mark.parametrize(
    ("mode", "base_url"),
    [
        ("production", "http://127.0.0.1:9/api/v1"),  # a loopback address in production
        ("production", "https://example.com/api/v1"),
        ("production", "http://openrouter.ai/api/v1"),
        ("test", PRODUCTION_BASE_URL),  # OpenRouter itself in test mode
        ("test", "https://example.com/api/v1"),
        ("test", "http://192.168.1.5/api/v1"),
    ],
)
def test_the_key_is_never_checked_against_an_address_the_mode_refuses(
    mode: Mode, base_url: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        check_openrouter_key(KEY, mode=mode, base_url=base_url)

    assert KEY not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("bad_key", ["", " ", "sk with space", "sk\nnewline", "ключ"])
def test_the_key_check_refuses_a_bad_key_before_sending(
    fake_provider: FakeProvider, bad_key: str
) -> None:
    with pytest.raises(ValueError, match="Ключ API") as caught:
        key_check(fake_provider, key=bad_key)

    assert str(caught.value) == "Ключ API порожній або містить пробіли чи недрукований символ"
    assert fake_provider.requests == []
