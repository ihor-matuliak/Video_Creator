"""Offline tests of the OpenRouter chat request side (ADR 041 §1, §2, §4).

Nothing here sends a request: the module only builds bodies and reservations, so no network
and no paid call are involved.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Final

import httpx2
import pytest

from clipforge.adapters import openrouter_chat
from clipforge.adapters.openrouter_chat import (
    MAX_BODY_BYTES,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    AnalysisProfile,
    ChatError,
    ChatResult,
    OpenRouterChat,
    analysis_profiles,
    build_chat_body,
    request_bytes,
    reservation_micro_usd,
)
from clipforge.adapters.openrouter_stt import PRODUCTION_BASE_URL
from clipforge.config import ConfigError, Mode

REPO: Final = Path(__file__).resolve().parents[3]
RESOURCE_SOURCES: Final[Mapping[str, Path]] = MappingProxyType(
    {
        "select.system.md": REPO / "prompts" / "select.system.md",
        "repair.system.md": REPO / "prompts" / "repair.system.md",
        "selection.schema.json": REPO / "contracts" / "selection.schema.json",
    }
)
TEST_PROFILE: Final = analysis_profiles("test")["test/selector"]
USER_PAYLOAD: Final[Mapping[str, object]] = {
    "source_context": {"language": "en", "duration_ms": 60000},
    "profile": {
        "categories": ["funny"],
        "custom_instruction": "",
        "min_duration_ms": 5000,
        "max_duration_ms": 60000,
        "max_candidates": 5,
    },
    "timing_level": "segment",
    "units": [{"id": "s1", "ordinal": 0, "start_ms": 0, "end_ms": 1000, "text": "hi"}],
}


def repo_bytes(name: str) -> bytes:
    return RESOURCE_SOURCES[name].read_bytes()


def version_of(select: bytes, repair: bytes, schema: bytes) -> str:
    """ADR 041 §4 written out independently of the module."""
    digest = hashlib.sha256(select + b"\x00" + repair + b"\x00" + schema).hexdigest()
    return f"sha256:{digest[:16]}"


# ------------------------------------------------------------------------ resources


@pytest.mark.parametrize("name", sorted(RESOURCE_SOURCES))
def test_packaged_resource_bytes_equal_the_repo_file(name: str) -> None:
    packaged = resources.files("clipforge.resources").joinpath(name).read_bytes()

    assert packaged == repo_bytes(name)


def test_the_system_prompts_are_the_resource_text_unchanged() -> None:
    assert openrouter_chat.SELECT_SYSTEM_PROMPT.encode("utf-8") == repo_bytes("select.system.md")
    assert openrouter_chat.REPAIR_SYSTEM_PROMPT.encode("utf-8") == repo_bytes("repair.system.md")


# ------------------------------------------------------------------ prompt version


def test_prompt_version_has_the_documented_format() -> None:
    assert re.fullmatch(r"sha256:[0-9a-f]{16}", PROMPT_VERSION) is not None


def test_prompt_version_is_the_documented_hash_of_the_repo_files() -> None:
    expected = version_of(
        repo_bytes("select.system.md"),
        repo_bytes("repair.system.md"),
        repo_bytes("selection.schema.json"),
    )

    assert expected == PROMPT_VERSION


def test_prompt_version_is_deterministic_and_sensitive_to_file_boundaries() -> None:
    select = repo_bytes("select.system.md")
    repair = repo_bytes("repair.system.md")
    schema = repo_bytes("selection.schema.json")

    first = version_of(select, repair, schema)

    assert first == version_of(select, repair, schema) == PROMPT_VERSION
    # The last byte of select moves into repair: only the NUL separators make this visible.
    assert version_of(select[:-1], select[-1:] + repair, schema) != first


def test_schema_version_is_1() -> None:
    assert SCHEMA_VERSION == 1


# ------------------------------------------------------------------------- profiles


def test_production_has_no_analysis_models() -> None:
    production = analysis_profiles("production")

    assert isinstance(production, MappingProxyType)
    assert len(production) == 0
    assert production.get("test/selector") is None


def test_test_mode_offers_only_the_selector_with_the_documented_caps() -> None:
    assert dict(analysis_profiles("test")) == {
        "test/selector": AnalysisProfile(
            model="test/selector",
            prompt_usd_per_mtok=Decimal("1"),
            completion_usd_per_mtok=Decimal("2"),
            context_tokens=128_000,
            max_tokens=4096,
            price_basis="test",
        )
    }


def test_profiles_and_their_table_cannot_be_changed() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        TEST_PROFILE.max_tokens = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        analysis_profiles("test")["other"] = TEST_PROFILE  # type: ignore[index]


# ------------------------------------------------------------------------ chat body


def test_body_has_the_documented_shape_and_the_repo_schema() -> None:
    body = build_chat_body(
        TEST_PROFILE,
        system_prompt=openrouter_chat.SELECT_SYSTEM_PROMPT,
        user_payload=USER_PAYLOAD,
    )

    assert body == {
        "model": "test/selector",
        "messages": [
            {"role": "system", "content": repo_bytes("select.system.md").decode("utf-8")},
            {
                "role": "user",
                "content": json.dumps(USER_PAYLOAD, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "clipforge_selection",
                "strict": True,
                "schema": json.loads(repo_bytes("selection.schema.json")),
            },
        },
        "max_tokens": 4096,
        "provider": {
            "require_parameters": True,
            "max_price": {"prompt": "1", "completion": "2"},
        },
        "stream": False,
    }


def test_body_keys_are_exactly_the_documented_ones() -> None:
    body = build_chat_body(TEST_PROFILE, system_prompt="s", user_payload=USER_PAYLOAD)

    assert set(body) == {
        "model",
        "messages",
        "response_format",
        "max_tokens",
        "provider",
        "stream",
    }


def test_body_never_sends_temperature() -> None:
    body = build_chat_body(TEST_PROFILE, system_prompt="s", user_payload=USER_PAYLOAD)

    assert "temperature" not in body
    assert "temperature" not in json.dumps(body)


def test_body_carries_no_file_name_or_path_key() -> None:
    text = json.dumps(
        build_chat_body(TEST_PROFILE, system_prompt="s", user_payload=USER_PAYLOAD),
        ensure_ascii=False,
    )

    for key in ('"filename"', '"file_name"', '"file"', '"path"', '"source_path"'):
        assert key not in text


def test_user_message_is_compact_json_with_raw_utf8() -> None:
    body = build_chat_body(
        TEST_PROFILE,
        system_prompt="s",
        user_payload={"назва": "м"},
    )

    messages = body["messages"]
    assert isinstance(messages, list)
    assert messages[1] == {"role": "user", "content": '{"назва":"м"}'}


# ------------------------------------------------------------------------ reservation


def test_reservation_for_a_known_body_is_exact() -> None:
    # 1000 system bytes + 9 bytes of '{"x":"y"}' + 1502 bytes of the compact response_format
    # + 512 = 3023 prompt units at cap 1; max_tokens 4096 at completion cap 2 = 8192.
    # Total micro-USD: 11215.
    body = build_chat_body(TEST_PROFILE, system_prompt="s" * 1000, user_payload={"x": "y"})

    assert reservation_micro_usd(body, TEST_PROFILE) == 11215


def test_reservation_counts_utf8_bytes_not_characters() -> None:
    # The user content '{"назва":"м"}' is 19 UTF-8 bytes (5+1 Cyrillic letters at 2 bytes,
    # 7 ASCII bytes): 1000 + 19 + 1502 + 512 = 3033 prompt units, plus 8192: 11225 micro-USD.
    body = build_chat_body(TEST_PROFILE, system_prompt="s" * 1000, user_payload={"назва": "м"})

    assert reservation_micro_usd(body, TEST_PROFILE) == 11225


def test_reservation_rounds_a_fraction_of_a_micro_dollar_up() -> None:
    profile = AnalysisProfile(
        model="m",
        prompt_usd_per_mtok=Decimal("0.5"),
        completion_usd_per_mtok=Decimal("0.25"),
        context_tokens=1000,
        max_tokens=3,
        price_basis="test",
    )
    # '{}' is 2 bytes and the response_format is 1502 bytes: (2 + 1502 + 512) × 0.5 = 1008.0;
    # 3 × 0.25 = 0.75; 1008.75 rounds up to 1009.
    body = build_chat_body(profile, system_prompt="", user_payload={})

    assert reservation_micro_usd(body, profile) == 1009


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, "max_tokens"),
        ({"messages": "text", "max_tokens": 1}, "messages"),
        ({"messages": [{"role": "user"}], "max_tokens": 1}, "не має тексту"),
        ({"messages": [], "max_tokens": True}, "max_tokens"),
        ({"messages": [{"role": "user", "content": "x"}], "max_tokens": 1}, "response_format"),
    ],
)
def test_reservation_refuses_a_body_it_cannot_read(
    body: Mapping[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        reservation_micro_usd(body, TEST_PROFILE)


def test_response_format_is_counted_as_compact_utf8_json_at_the_prompt_cap() -> None:
    # {"note":"м"} is 13 UTF-8 bytes in compact form (ensure_ascii=False): (13 + 512) × 1 = 525.
    # max_tokens 0 adds nothing on the completion side.
    body = {"messages": [], "max_tokens": 0, "response_format": {"note": "м"}}

    assert reservation_micro_usd(body, TEST_PROFILE) == 525


def test_the_reservation_difference_is_exactly_the_response_format_bytes() -> None:
    body = build_chat_body(TEST_PROFILE, system_prompt="s", user_payload={})
    schema_bytes = len(
        json.dumps(body["response_format"], ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    without_schema = {**body, "response_format": {}}

    assert schema_bytes > 1000
    assert reservation_micro_usd(body, TEST_PROFILE) - reservation_micro_usd(
        without_schema, TEST_PROFILE
    ) == schema_bytes - len(b"{}")


# ------------------------------------------------------------------ request bytes (ADR 041 §16)


def test_request_bytes_are_the_bytes_the_reservation_counts() -> None:
    # 1000 system bytes + 9 bytes of '{"x":"y"}' + 1502 bytes of the compact response_format
    # + 512 = 3023 bytes; the reservation adds max_tokens 4096 at completion cap 2.
    body = build_chat_body(TEST_PROFILE, system_prompt="s" * 1000, user_payload={"x": "y"})

    assert request_bytes(body) == 3_023
    assert reservation_micro_usd(body, TEST_PROFILE) == request_bytes(body) + 2 * 4_096


def test_request_bytes_count_utf8_and_do_not_depend_on_max_tokens() -> None:
    # The user content is 19 UTF-8 bytes: 1000 + 19 + 1502 + 512 = 3033.
    body = build_chat_body(TEST_PROFILE, system_prompt="s" * 1000, user_payload={"назва": "м"})

    assert request_bytes(body) == 3_033
    assert request_bytes({**body, "max_tokens": 1}) == 3_033


@pytest.mark.parametrize(
    ("body", "message"),
    [({"messages": "text"}, "messages"), ({"messages": []}, "response_format")],
)
def test_request_bytes_refuses_a_body_it_cannot_read(
    body: Mapping[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        request_bytes(body)


# ------------------------------------------------------------ HTTP client (ADR 041 §9)
# Every call goes through httpx2.MockTransport: there is no network and no paid call.

KEY: Final = "sk-or-v1-0123456789abcdef0123456789abcdef"
KEY_ECHO: Final = "Hq5Tn8Wd3Pz1"
NOW: Final = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
ENDPOINT: Final = "https://openrouter.ai/api/v1/chat/completions"
TEST_BASE: Final = "http://127.0.0.1:8123/v1"
GENERATION_ID: Final = "gen-chat-1794747983-qXdd7kmxgumWnuQ2t7Ni"
SELECTION_TEXT: Final = '{"candidates":[]}'
ERROR_402: Final = Path(__file__).resolve().parent / "fixtures" / "openrouter" / "error_402.json"

Handler = Callable[[httpx2.Request], httpx2.Response]


def completion(
    *, content: str = SELECTION_TEXT, finish_reason: object = "stop", **extra: object
) -> dict[str, object]:
    """A minimal valid chat answer with one choice; ``extra`` replaces any top-level field."""
    choice = {
        "index": 0,
        "finish_reason": finish_reason,
        "message": {"role": "assistant", "content": content},
    }
    return {
        "id": GENERATION_ID,
        "object": "chat.completion",
        "choices": [choice],
        "usage": {"cost": 0.0042, "total_tokens": 900},
        **extra,
    }


def reply(response: httpx2.Response) -> Handler:
    return lambda _request: response


def raising(exc_type: type[httpx2.TransportError]) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc_type("boom", request=request)

    return handler


def make_chat(
    handler: Handler,
    *,
    key: str | None = KEY,
    mode: Mode = "production",
    base_url: str = PRODUCTION_BASE_URL,
) -> tuple[OpenRouterChat, list[httpx2.Request]]:
    """A client whose transport records every request that reaches it."""
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = OpenRouterChat(
        key,
        mode=mode,
        base_url=base_url,
        transport=httpx2.MockTransport(recording),
        now=lambda: NOW,
    )
    return client, seen


def complete(client: OpenRouterChat) -> ChatResult:
    return client.complete(
        build_chat_body(TEST_PROFILE, system_prompt="s", user_payload=USER_PAYLOAD)
    )


def failure(handler: Handler) -> tuple[ChatError, list[httpx2.Request]]:
    client, seen = make_chat(handler)
    with pytest.raises(ChatError) as caught:
        complete(client)
    return caught.value, seen


class _Pieces(httpx2.SyncByteStream):
    """A body that arrives in pieces, as on the network. httpx2 does not preload it, so the
    adapter's ``iter_raw`` reads it the way it reads a real answer."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self._data), 8192):
            yield self._data[start : start + 8192]


class _CutOff(httpx2.SyncByteStream):
    """A body that starts and then times out: the request has already been sent."""

    def __iter__(self) -> Iterator[bytes]:
        yield b'{"id": "gen-cut", '
        raise httpx2.ReadTimeout("boom")


def wire(
    status: int,
    *,
    json: object = None,
    content: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    stream: httpx2.SyncByteStream | None = None,
) -> httpx2.Response:
    """A response whose body is streamed. ``httpx2.Response(json=...)`` would preload the body,
    which a real answer never is, so every fixture goes through this helper."""
    if stream is None:
        if content is None:
            content = b"" if json is None else _json_bytes(json)
        stream = _Pieces(content)
    return httpx2.Response(status, stream=stream, headers=headers)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


# ------------------------------------------------------------------------ request


def test_the_request_is_one_post_with_the_key_in_the_header_only() -> None:
    client, seen = make_chat(reply(wire(200, json=completion())))

    complete(client)

    [request] = seen
    assert (request.method, str(request.url)) == ("POST", ENDPOINT)
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Accept-Encoding"] == "identity"
    assert request.headers["Content-Type"] == "application/json"
    assert KEY not in str(request.url)
    assert KEY.encode("ascii") not in request.content


def test_the_body_is_sent_as_the_compact_utf8_json_the_reservation_counts() -> None:
    body = build_chat_body(TEST_PROFILE, system_prompt="назва", user_payload={"назва": "м"})
    client, seen = make_chat(reply(wire(200, json=completion())))

    client.complete(body)

    assert seen[0].content == json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    assert "назва".encode() in seen[0].content


def test_the_request_uses_the_documented_timeouts() -> None:
    client, seen = make_chat(reply(wire(200, json=completion())))

    complete(client)

    assert seen[0].extensions["timeout"] == {
        "connect": 10.0,
        "read": 120.0,
        "write": 60.0,
        "pool": 10.0,
    }


def test_test_mode_sends_no_authorization_header_without_a_key() -> None:
    client, seen = make_chat(
        reply(wire(200, json=completion())), key=None, mode="test", base_url=TEST_BASE
    )

    complete(client)

    assert "Authorization" not in seen[0].headers
    assert str(seen[0].url) == f"{TEST_BASE}/chat/completions"


def test_test_mode_sends_the_key_when_one_is_given() -> None:
    client, seen = make_chat(reply(wire(200, json=completion())), mode="test", base_url=TEST_BASE)

    complete(client)

    assert seen[0].headers["Authorization"] == f"Bearer {KEY}"


def test_a_trailing_slash_of_the_base_url_is_not_doubled() -> None:
    client, seen = make_chat(
        reply(wire(200, json=completion())),
        key=None,
        mode="test",
        base_url=TEST_BASE + "/",
    )

    complete(client)

    assert str(seen[0].url) == f"{TEST_BASE}/chat/completions"


def test_production_requires_a_key() -> None:
    with pytest.raises(ValueError, match="production"):
        OpenRouterChat(None)
    with pytest.raises(ValueError, match="production"):
        OpenRouterChat(None, mode="production")


@pytest.mark.parametrize("bad_key", ["", " ", "sk with space", "sk\nnewline", "ключ"])
def test_a_bad_key_is_refused_without_echoing_it(bad_key: str) -> None:
    with pytest.raises(ValueError, match="Ключ API") as caught:
        OpenRouterChat(bad_key, mode="test", base_url=TEST_BASE)

    assert str(caught.value) == "Ключ API порожній або містить пробіли чи недрукований символ"


@pytest.mark.parametrize(
    ("mode", "base_url"),
    [
        ("production", TEST_BASE),
        ("production", "https://example.com/api/v1"),
        ("test", PRODUCTION_BASE_URL),
        ("test", "https://example.com/api/v1"),
        ("test", "http://192.168.1.5/v1"),
    ],
)
def test_the_key_is_never_sent_to_an_address_the_mode_refuses(mode: Mode, base_url: str) -> None:
    with pytest.raises(ConfigError) as caught:
        OpenRouterChat(KEY, mode=mode, base_url=base_url)

    assert KEY not in str(caught.value)


def test_repr_shows_the_address_and_never_the_key() -> None:
    client = OpenRouterChat(KEY)

    assert repr(client) == f"OpenRouterChat(base_url={PRODUCTION_BASE_URL!r})"
    assert KEY not in repr(client)


# ------------------------------------------------------------------------ 200 answer


def test_a_200_answer_gives_content_finish_reason_cost_and_the_body_id() -> None:
    client, _ = make_chat(
        reply(wire(200, json=completion(), headers={"X-Generation-Id": "gen-header"}))
    )

    assert complete(client) == ChatResult(
        content=SELECTION_TEXT,
        finish_reason="stop",
        cost=Decimal("0.0042"),
        generation_id=GENERATION_ID,
    )


@pytest.mark.parametrize(
    ("body_id", "expected"),
    [
        (GENERATION_ID, GENERATION_ID),
        ("  gen-padded  ", "gen-padded"),
        ("   ", None),
        (None, None),
        (12, None),
    ],
)
def test_generation_id_comes_from_the_body_and_never_from_a_header(
    body_id: object, expected: str | None
) -> None:
    client, _ = make_chat(
        reply(
            wire(
                200,
                json=completion(id=body_id),
                headers={"X-Generation-Id": "gen-header"},
            )
        )
    )

    assert complete(client).generation_id == expected


def test_a_missing_body_id_gives_no_generation_id() -> None:
    answer = completion()
    del answer["id"]
    client, _ = make_chat(reply(wire(200, json=answer)))

    assert complete(client).generation_id is None


@pytest.mark.parametrize("finish_reason", ["stop", "length", None, 7])
def test_finish_reason_is_kept_as_text_or_none(finish_reason: object) -> None:
    client, _ = make_chat(reply(wire(200, json=completion(finish_reason=finish_reason))))

    expected = finish_reason if isinstance(finish_reason, str) else None
    assert complete(client).finish_reason == expected


@pytest.mark.parametrize(
    "usage",
    [
        None,
        "free",
        [],
        {},
        {"cost": None},
        {"cost": "0.5"},
        {"cost": True},
        {"cost": -0.01},
        {"cost": float("nan")},
        {"total_tokens": 9},
    ],
)
def test_cost_is_none_unless_the_provider_reports_a_valid_number(usage: object) -> None:
    client, _ = make_chat(reply(wire(200, json=completion(usage=usage))))

    assert complete(client).cost is None


def test_cost_is_none_when_the_usage_field_is_absent() -> None:
    answer = completion()
    del answer["usage"]
    client, _ = make_chat(reply(wire(200, json=answer)))

    assert complete(client).cost is None


def test_a_zero_cost_is_kept_as_zero_and_not_as_none() -> None:
    client, _ = make_chat(reply(wire(200, json=completion(usage={"cost": 0}))))

    assert complete(client).cost == Decimal(0)


def test_the_result_repr_leaves_out_the_content() -> None:
    client, _ = make_chat(reply(wire(200, json=completion(content="private words"))))

    result = complete(client)

    assert "private" not in repr(result)
    assert "finish_reason='stop'" in repr(result)


# ------------------------------------------------------------------ HTTP error mapping


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "auth"),
        (403, "auth"),
        (402, "funds"),
        (404, "model_unavailable"),
        (429, "rate_limited"),
        (400, "invalid_request"),
        (413, "invalid_request"),
        (422, "invalid_request"),
        (451, "invalid_request"),
        (408, "unknown_outcome"),
        (504, "unknown_outcome"),
        (500, "upstream"),
        (502, "upstream"),
        (503, "upstream"),
        (599, "upstream"),
    ],
)
def test_http_status_maps_to_one_kind_after_one_attempt(status: int, kind: str) -> None:
    error, seen = failure(reply(wire(status, json={"error": {"message": "x"}})))

    assert (error.kind, error.status) == (kind, status)
    assert len(seen) == 1


@pytest.mark.parametrize("status", [408, 504])
def test_a_timeout_answer_is_unknown_outcome_and_the_request_is_never_sent_again(
    status: int,
) -> None:
    # The provider may have processed and billed the request (ADR 041 §22): one POST, no retry.
    error, seen = failure(reply(wire(status, json={"error": {"message": "x"}})))

    assert (error.kind, error.status, error.retry_after_s) == ("unknown_outcome", status, None)
    assert "Автоматичного повтору немає." in str(error)
    assert len(seen) == 1


def test_funds_error_from_the_live_402_body_copies_no_provider_text() -> None:
    error, seen = failure(reply(wire(402, content=ERROR_402.read_bytes())))

    assert (error.kind, error.status) == ("funds", 402)
    assert "lower max_tokens" not in str(error)
    assert "openrouter.ai" not in str(error)
    assert len(seen) == 1


def test_messages_are_ukrainian_and_name_the_status_where_it_helps() -> None:
    auth, _ = failure(reply(wire(401)))
    invalid, _ = failure(reply(wire(422)))
    upstream, _ = failure(reply(wire(503)))

    assert "Налаштуваннях" in str(auth)
    assert "HTTP 422" in str(invalid)
    assert "HTTP 503" in str(upstream)
    assert str(auth) == auth.message


@pytest.mark.parametrize("status", [100, 201, 204, 301, 302, 304])
def test_statuses_that_are_neither_200_nor_errors_are_invalid_responses(status: int) -> None:
    error, seen = failure(
        reply(wire(status, content=b'{"choices": []}', headers={"Location": "/x"}))
    )

    assert (error.kind, error.status) == ("invalid_response", status)
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7.0),
        (" 120 ", 120.0),
        ("0", 0.0),
        (format_datetime(NOW + timedelta(seconds=90), usegmt=True), 90.0),
        (format_datetime(NOW - timedelta(seconds=30), usegmt=True), 0.0),
        ("-5", None),
        ("soon", None),
        ("", None),
    ],
)
def test_rate_limited_reads_retry_after(header: str, expected: float | None) -> None:
    error, seen = failure(reply(wire(429, headers={"Retry-After": header})))

    assert error.kind == "rate_limited"
    assert error.retry_after_s == expected
    assert len(seen) == 1


def test_retry_after_is_read_only_for_429() -> None:
    error, _ = failure(reply(wire(503, headers={"Retry-After": "5"})))

    assert (error.kind, error.retry_after_s) == ("upstream", None)


# ------------------------------------------------------------------ transport errors


@pytest.mark.parametrize(
    ("exc_type", "kind"),
    [
        (httpx2.ConnectError, "not_sent"),
        (httpx2.ConnectTimeout, "not_sent"),
        (httpx2.PoolTimeout, "not_sent"),
        (httpx2.ReadTimeout, "unknown_outcome"),
        (httpx2.WriteTimeout, "unknown_outcome"),
        (httpx2.ReadError, "unknown_outcome"),
        (httpx2.RemoteProtocolError, "unknown_outcome"),
    ],
)
def test_a_transport_failure_is_classified_by_whether_the_request_was_sent(
    exc_type: type[httpx2.TransportError], kind: str
) -> None:
    error, seen = failure(raising(exc_type))

    assert (error.kind, error.status, error.retry_after_s) == (kind, None, None)
    assert len(seen) == 1
    # The transport exception holds the request with the key header: it must not be chained.
    assert error.__cause__ is None
    assert error.__context__ is None


def test_a_body_that_times_out_after_the_request_was_sent_is_unknown_outcome() -> None:
    error, seen = failure(reply(wire(200, stream=_CutOff())))

    assert error.kind == "unknown_outcome"
    assert error.status is None
    assert len(seen) == 1
    assert error.__cause__ is None
    assert error.__context__ is None


def test_unknown_outcome_says_the_outcome_is_unknown_and_not_retried() -> None:
    error, _ = failure(raising(httpx2.ReadTimeout))

    assert "Невідомо" in str(error)
    assert "Автоматичного повтору немає." in str(error)


# ---------------------------------------------------------------- 200 body validation


def test_a_body_over_one_mebibyte_is_an_invalid_response() -> None:
    body = json.dumps(completion(content="a" * MAX_BODY_BYTES), ensure_ascii=False)
    assert len(body.encode("utf-8")) > MAX_BODY_BYTES

    error, seen = failure(reply(wire(200, content=body.encode("utf-8"))))

    assert (error.kind, error.status) == ("invalid_response", 200)
    assert len(seen) == 1


def test_a_body_of_exactly_one_mebibyte_is_read() -> None:
    prefix = b'{"choices":[{"message":{"content":"'
    suffix = b'"}}]}'
    padding = MAX_BODY_BYTES - len(prefix) - len(suffix)
    content = prefix + b"a" * padding + suffix
    assert len(content) == MAX_BODY_BYTES
    client, _ = make_chat(reply(wire(200, content=content)))

    assert len(complete(client).content) == padding


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"\xff\xfe\x00",
        b"[]",
        b'"text"',
        b"null",
        b"[" * 100_000,
        b'{"error": {"code": 500, "message": "upstream"}}',
    ],
)
def test_a_200_that_is_not_a_json_object_is_an_invalid_response(content: bytes) -> None:
    error, seen = failure(reply(wire(200, content=content)))

    assert (error.kind, error.status) == ("invalid_response", 200)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": None},
        {"choices": []},
        {"choices": "text"},
        {"choices": [None]},
        {"choices": [{}]},
        {"choices": [{"message": "text"}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": 5}}]},
        {"choices": [{"message": {"content": ["x"]}}]},
        {"error": {"message": "upstream"}},
    ],
)
def test_a_200_without_a_text_choice_is_an_invalid_response(payload: dict[str, object]) -> None:
    error, seen = failure(reply(wire(200, json=payload)))

    assert (error.kind, error.status) == ("invalid_response", 200)
    assert len(seen) == 1


# ------------------------------------------------------------------------ key never leaks


def test_the_key_never_appears_in_errors_results_or_the_client() -> None:
    echo = wire(401, json={"error": {"message": f"bad key {KEY} {KEY_ECHO}"}})
    handlers: list[Handler] = [
        reply(echo),
        reply(wire(402, content=ERROR_402.read_bytes())),
        reply(wire(429, headers={"Retry-After": "3"})),
        reply(wire(500, json={"error": {"message": KEY}})),
        reply(wire(200, content=f"not json {KEY} {KEY_ECHO}".encode())),
        reply(wire(200, json={"choices": [{"message": {"content": None}}]})),
        raising(httpx2.ConnectError),
        raising(httpx2.ReadTimeout),
    ]
    texts: list[str] = []
    for handler in handlers:
        error, _ = failure(handler)
        texts.extend([str(error), repr(error), error.message, repr(error.args), repr(vars(error))])
        assert error.__cause__ is None
        assert error.__context__ is None

    client, _ = make_chat(reply(wire(200, json=completion(content=f"text {KEY}"))))
    result = complete(client)
    texts.extend([repr(client), str(client), repr(result)])

    assert len(texts) > 30
    assert all(KEY not in text for text in texts)
    assert all(KEY_ECHO not in text for text in texts)
    assert all("0123456789abcdef" not in text for text in texts)


@pytest.mark.parametrize(
    "handler",
    [
        reply(wire(401)),
        reply(wire(429, headers={"Retry-After": "1"})),
        reply(wire(503)),
        reply(wire(400)),
        reply(wire(200, content=b"nope")),
        raising(httpx2.ReadTimeout),
        raising(httpx2.ConnectError),
    ],
)
def test_every_failure_is_a_single_attempt(handler: Handler) -> None:
    client, seen = make_chat(handler)

    with pytest.raises(ChatError):
        complete(client)

    assert len(seen) == 1
