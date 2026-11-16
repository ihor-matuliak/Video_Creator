"""GET and PATCH /v1/settings, POST and DELETE /v1/runtime/credentials, POST
/v1/runtime/credentials/check (FR-048, FR-049): the four non-secret settings, their bounds, the
write-only in-memory API key, the one-request key check against a local fake provider, and 429 for
a local limit."""

from __future__ import annotations

import gzip
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import text

from clipforge.adapters import openrouter_key
from clipforge.adapters.openrouter_stt import PROFILES
from clipforge.services.credentials import KeyChecker, openrouter_key_checker
from clipforge.services.runtime import Runtime
from clipforge.services.settings import DEFAULT_STT_MODEL, STT_MODELS
from clipforge.services.spend import SpendLedger
from clipforge.storage import SettingsRepository
from clipforge.storage.database import DB_FILENAME, LOCK_FILENAME

from .support import LABEL, FakeProvider, RuntimeFactory, client_for, key_info, make_config

# Written out, not read from the code under test: the default and the allowlist are product
# decisions, so a change to either must show up here.
MAI = "microsoft/mai-transcribe-2"
GEMINI = "google/gemini-3.5-transcribe"
# Part of every key and every rejected value the tests send: if any response, log line, file or
# repr contains it, something echoed or stored the secret.
MARKER = "Zq7Lk2Vx9Rm4"
KEY = f"sk-or-v1-{MARKER}0123456789abcdef"
DEFAULTS: dict[str, Any] = {
    "stt_model": MAI,
    "stt_language": None,
    "analysis_model": None,
    "run_budget_usd": None,
    "has_api_key": False,
}
CHOSEN: dict[str, Any] = {
    "stt_model": GEMINI,
    "stt_language": "en",
    "analysis_model": "anthropic/claude-sonnet-4.5",
    "run_budget_usd": "12.50",
}


def checker_for(provider: FakeProvider, data_dir: Path) -> KeyChecker:
    """The production checker in test mode, pointed at the fake provider (and nowhere else)."""
    return openrouter_key_checker(make_config(data_dir, "test", provider.base_url))


@pytest.fixture
def runtime(make_runtime: RuntimeFactory, fake_provider: FakeProvider, tmp_path: Path) -> Runtime:
    """These tests need no FFmpeg. The key check goes to ``fake_provider``."""
    return make_runtime(with_tools=False, key_checker=checker_for(fake_provider, tmp_path))


def settings_of(client: TestClient) -> dict[str, Any]:
    response = client.get("/v1/settings")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def patched(client: TestClient, **changes: Any) -> dict[str, Any]:
    response = client.patch("/v1/settings", json=changes)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def error_of(response: Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


def set_key(client: TestClient, key: str = KEY) -> Response:
    return client.post("/v1/runtime/credentials", json={"openrouter_api_key": key})


# --- GET and PATCH /v1/settings ------------------------------------------------------------------


def test_the_defaults(client: TestClient) -> None:
    assert DEFAULT_STT_MODEL == "microsoft/mai-transcribe-2"
    assert settings_of(client)["stt_model"] == "microsoft/mai-transcribe-2"
    assert settings_of(client) == DEFAULTS


def test_the_stt_model_allowlist_is_the_set_of_adapter_profiles(client: TestClient) -> None:
    """The models the settings accept are the ones the STT adapter can call: nothing more, and a
    profile that is added or removed there changes the allowlist without a second edit here."""
    assert set(STT_MODELS) == set(PROFILES)
    assert set(PROFILES) == {MAI, GEMINI}  # the two supported models, as written in the spec
    assert DEFAULT_STT_MODEL in PROFILES
    for model in PROFILES:
        assert patched(client, stt_model=model)["stt_model"] == model
    response = client.patch("/v1/settings", json={"stt_model": "openai/whisper-1"})
    assert response.status_code == 422
    assert error_of(response)["code"] == "INVALID_STT_MODEL"


def test_patch_changes_only_the_given_keys_and_returns_the_settings(client: TestClient) -> None:
    assert patched(client, stt_language="uk") == {**DEFAULTS, "stt_language": "uk"}
    assert patched(client, run_budget_usd="5") == {
        **DEFAULTS,
        "stt_language": "uk",
        "run_budget_usd": "5",
    }
    assert patched(client, **CHOSEN) == {**CHOSEN, "has_api_key": False}
    assert settings_of(client) == {**CHOSEN, "has_api_key": False}


def test_an_empty_patch_changes_nothing(client: TestClient) -> None:
    patched(client, **CHOSEN)
    assert patched(client) == {**CHOSEN, "has_api_key": False}


@pytest.mark.parametrize("key", ["stt_language", "analysis_model", "run_budget_usd"])
def test_null_resets_a_setting_to_its_default(client: TestClient, key: str) -> None:
    patched(client, **CHOSEN)
    after = patched(client, **{key: None})
    assert after == {**CHOSEN, "has_api_key": False, key: None}
    assert settings_of(client) == after


def test_settings_survive_a_restart_of_the_runtime(
    runtime: Runtime, client: TestClient, make_runtime: RuntimeFactory
) -> None:
    patched(client, **CHOSEN)
    runtime.close()

    reopened = make_runtime(with_tools=False, data_dir=runtime.database.data_dir)

    assert settings_of(client_for(reopened)) == {**CHOSEN, "has_api_key": False}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("stt_model", MAI),
        ("stt_model", GEMINI),
        ("stt_language", "en"),
        ("stt_language", "uk"),
        ("analysis_model", "a/b"),
        ("analysis_model", "google/gemini-2.5-pro:free"),
        ("analysis_model", "~anthropic/claude-opus-latest"),
        ("analysis_model", "v/" + "m" * 98),  # exactly 100 characters
        ("run_budget_usd", "0.01"),
        ("run_budget_usd", "0.010000"),
        ("run_budget_usd", "0.123456"),
        ("run_budget_usd", "1"),
        ("run_budget_usd", "999.999999"),
        ("run_budget_usd", "1000"),
        ("run_budget_usd", "1000.000000"),
    ],
)
def test_values_inside_the_bounds_are_stored_exactly_as_given(
    client: TestClient, key: str, value: str
) -> None:
    assert patched(client, **{key: value})[key] == value
    assert settings_of(client)[key] == value


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("stt_model", None),
        ("stt_model", ""),
        ("stt_model", "openai/whisper-1"),
        ("stt_model", "Microsoft/mai-transcribe-2"),
        ("stt_model", MAI + " "),
        ("stt_model", MAI + "\n"),
        ("stt_language", ""),
        ("stt_language", "e"),
        ("stt_language", "eng"),
        ("stt_language", "EN"),
        ("stt_language", "En"),
        ("stt_language", "e1"),
        ("stt_language", "єн"),
        ("stt_language", "en\n"),
        ("stt_language", " en"),
        ("analysis_model", ""),
        ("analysis_model", "gpt"),
        ("analysis_model", "/model"),
        ("analysis_model", "vendor/"),
        ("analysis_model", "a/b/c"),
        ("analysis_model", " a/b"),
        ("analysis_model", "a/b "),
        ("analysis_model", "a/b\n"),
        ("analysis_model", "a /b"),
        ("analysis_model", "вендор/модель"),
        ("analysis_model", "a/b;c"),
        ("analysis_model", "v/" + "m" * 99),  # 101 characters
        ("run_budget_usd", ""),
        ("run_budget_usd", "0"),
        ("run_budget_usd", "0.00"),
        ("run_budget_usd", "0.009999"),
        ("run_budget_usd", "1000.000001"),
        ("run_budget_usd", "1000.01"),
        ("run_budget_usd", "1001"),
        ("run_budget_usd", "0.0000001"),  # 7 digits after the point
        ("run_budget_usd", "1.2345678"),
        ("run_budget_usd", "-1"),
        ("run_budget_usd", "+1"),
        ("run_budget_usd", "1e2"),
        ("run_budget_usd", "1,5"),
        ("run_budget_usd", " 1"),
        ("run_budget_usd", "1 "),
        ("run_budget_usd", "1\n"),
        ("run_budget_usd", "1."),
        ("run_budget_usd", ".5"),
        ("run_budget_usd", "007"),
        ("run_budget_usd", "10000"),
        ("run_budget_usd", "NaN"),
        ("run_budget_usd", "Infinity"),
        ("run_budget_usd", "١"),  # an Arabic-Indic digit
    ],
)
def test_values_outside_the_bounds_are_422_and_change_nothing(
    client: TestClient, key: str, value: Any
) -> None:
    before = patched(client, **CHOSEN)

    response = client.patch("/v1/settings", json={key: value})

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"].startswith("INVALID_")
    assert error["details"] == {"field": key}
    assert error["retryable"] is False
    assert settings_of(client) == before


@pytest.mark.parametrize(
    "body",
    [
        {"stt_model": 1},
        {"stt_language": 5},
        {"analysis_model": ["a/b"]},
        {"run_budget_usd": 5},
        {"run_budget_usd": 5.5},
        {"run_budget_usd": True},
    ],
)
def test_values_of_a_wrong_json_type_are_422(client: TestClient, body: dict[str, Any]) -> None:
    response = client.patch("/v1/settings", json=body)
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"
    assert settings_of(client) == DEFAULTS


@pytest.mark.parametrize(
    "body",
    [
        {"openrouter_api_key": KEY},
        {"has_api_key": True},
        {"foo": 1},
        {"stt_model": GEMINI, "foo": None},
        {"stt_language": "en", "run_budget_usd": "2", "extra": "x"},
    ],
)
def test_a_key_outside_the_four_allowed_ones_is_422_and_applies_nothing(
    client: TestClient, body: dict[str, Any]
) -> None:
    response = client.patch("/v1/settings", json=body)

    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"
    assert settings_of(client) == DEFAULTS
    assert MARKER not in response.text


def test_one_bad_value_applies_none_of_the_other_values(client: TestClient) -> None:
    before = patched(client, stt_language="uk")

    response = client.patch(
        "/v1/settings", json={"stt_model": GEMINI, "stt_language": "en", "run_budget_usd": "0"}
    )

    assert response.status_code == 422
    assert error_of(response)["details"] == {"field": "run_budget_usd"}
    assert settings_of(client) == before


@pytest.mark.parametrize("body", [[], "stt_language", 5, None])
def test_a_body_that_is_not_an_object_is_422(client: TestClient, body: Any) -> None:
    assert client.patch("/v1/settings", json=body).status_code == 422


def test_a_stored_value_that_is_not_allowed_is_ignored_and_the_default_is_used(
    runtime: Runtime, client: TestClient
) -> None:
    """Someone edited the database file: the stt_model allowlist must still hold."""
    with runtime.database.write_session() as session:
        repository = SettingsRepository(session)
        repository.set("stt_model", "evil/model")
        repository.set("stt_language", "english")
        repository.set("run_budget_usd", "-5")
        repository.set("analysis_model", "vendor/model")
        repository.set("unrelated_future_key", "x")

    assert settings_of(client) == {**DEFAULTS, "analysis_model": "vendor/model"}


# A row whose text is not versioned JSON (the file was edited by hand or damaged). Each text holds
# the marker, so a log line or response that echoes the stored text is caught.
DAMAGED_TEXTS = {
    "not json": f"not json {MARKER}",
    "empty": "",
    "unfinished": f'{{"schema_version":1,"value":"{MARKER}',
    "a bare JSON string": f'"{MARKER}"',
    "a list": f'["{MARKER}"]',
    "no schema_version": f'{{"value":"{MARKER}"}}',
    "an unknown schema_version": f'{{"schema_version":2,"value":"{MARKER}"}}',
    "no value": '{"schema_version":1}',
}
SETTING_NAMES = ["stt_model", "stt_language", "analysis_model", "run_budget_usd"]
RESETTABLE = ["stt_language", "analysis_model", "run_budget_usd"]  # `null` is allowed for these
VALID: dict[str, str] = {
    "stt_model": GEMINI,
    "stt_language": "en",
    "analysis_model": "vendor/model",
    "run_budget_usd": "5",
}


def put_text(runtime: Runtime, key: str, stored: str) -> None:
    """The row as it would be after someone edited the file: any text, no versioned JSON."""
    with runtime.database.write_session() as session:
        session.execute(
            text("INSERT OR REPLACE INTO app_settings (key, value_json) VALUES (:key, :stored)"),
            {"key": key, "stored": stored},
        )


def stored_texts(runtime: Runtime) -> dict[str, str]:
    with runtime.database.session() as session:
        rows = session.execute(text("SELECT key, value_json FROM app_settings"))
        return {key: stored for key, stored in rows}


@pytest.mark.parametrize("stored", DAMAGED_TEXTS.values(), ids=DAMAGED_TEXTS.keys())
@pytest.mark.parametrize("key", SETTING_NAMES)
def test_a_row_that_is_not_versioned_json_is_ignored_and_only_its_key_is_logged(
    runtime: Runtime,
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    key: str,
    stored: str,
) -> None:
    put_text(runtime, key, stored)
    caplog.set_level(logging.DEBUG)

    response = client.get("/v1/settings")

    assert response.status_code == 200, response.text
    assert response.json() == DEFAULTS  # the default, as if the row were not there
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert key in warnings[0].getMessage()
    assert MARKER not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert stored_texts(runtime) == {key: stored}  # reading did not touch the row


def test_a_damaged_row_does_not_hide_the_valid_rows_next_to_it(
    runtime: Runtime, client: TestClient
) -> None:
    put_text(runtime, "stt_model", DAMAGED_TEXTS["not json"])
    put_text(runtime, "run_budget_usd", DAMAGED_TEXTS["an unknown schema_version"])
    patched(client, stt_language="uk", analysis_model="vendor/model")

    assert settings_of(client) == {
        **DEFAULTS,
        "stt_language": "uk",
        "analysis_model": "vendor/model",
    }


@pytest.mark.parametrize("key", SETTING_NAMES)
def test_a_patch_of_a_key_with_a_damaged_row_replaces_the_row(
    runtime: Runtime, client: TestClient, key: str
) -> None:
    put_text(runtime, key, DAMAGED_TEXTS["not json"])

    after = patched(client, **{key: VALID[key]})

    assert after == {**DEFAULTS, key: VALID[key]}
    assert settings_of(client) == after
    assert stored_texts(runtime) == {key: f'{{"schema_version":1,"value":"{VALID[key]}"}}'}


@pytest.mark.parametrize("key", RESETTABLE)
def test_a_reset_of_a_key_with_a_damaged_row_removes_the_row(
    runtime: Runtime, client: TestClient, key: str
) -> None:
    put_text(runtime, key, DAMAGED_TEXTS["a bare JSON string"])

    assert patched(client, **{key: None}) == DEFAULTS

    assert stored_texts(runtime) == {}


@pytest.mark.parametrize("damaged", SETTING_NAMES)
def test_a_patch_of_another_key_works_while_one_row_is_damaged(
    runtime: Runtime, client: TestClient, damaged: str
) -> None:
    put_text(runtime, damaged, DAMAGED_TEXTS["unfinished"])
    other = next(key for key in SETTING_NAMES if key != damaged)

    after = patched(client, **{other: VALID[other]})

    assert after == {**DEFAULTS, other: VALID[other]}
    texts = stored_texts(runtime)
    assert texts[damaged] == DAMAGED_TEXTS["unfinished"]  # not this key's change: left as it was
    assert set(texts) == {damaged, other}


def test_a_refused_patch_leaves_a_damaged_row_as_it_was(
    runtime: Runtime, client: TestClient
) -> None:
    put_text(runtime, "stt_language", DAMAGED_TEXTS["not json"])

    response = client.patch("/v1/settings", json={"stt_language": "en", "run_budget_usd": "0"})

    assert response.status_code == 422
    assert stored_texts(runtime) == {"stt_language": DAMAGED_TEXTS["not json"]}


# --- credentials ---------------------------------------------------------------------------------


def test_a_key_is_accepted_replaced_and_deleted(runtime: Runtime, client: TestClient) -> None:
    posted = set_key(client)
    assert posted.status_code == 204
    assert posted.content == b""
    assert settings_of(client)["has_api_key"] is True
    assert runtime.credentials.openrouter_key() == KEY

    other = KEY[:-4] + "wxyz"
    assert set_key(client, other).status_code == 204
    assert runtime.credentials.openrouter_key() == other

    deleted = client.delete("/v1/runtime/credentials")
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert settings_of(client)["has_api_key"] is False
    assert runtime.credentials.openrouter_key() is None
    assert client.delete("/v1/runtime/credentials").status_code == 204  # idempotent


@pytest.mark.parametrize("length", [20, 21, 200])
def test_a_key_of_20_to_200_printable_ascii_characters_is_accepted(
    client: TestClient, length: int
) -> None:
    assert set_key(client, "k" * length).status_code == 204
    assert settings_of(client)["has_api_key"] is True


def test_saving_replacing_and_deleting_a_key_sends_nothing_to_the_provider(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The key is checked only on an explicit request (ADR 036): not when it is saved, and not
    when it is replaced or deleted. Not even a connection is opened."""
    assert set_key(client).status_code == 204
    assert set_key(client, KEY[:-4] + "wxyz").status_code == 204
    assert set_key(client, "k" * 19).status_code == 422  # a refused key is not sent either
    assert client.delete("/v1/runtime/credentials").status_code == 204

    assert fake_provider.requests == []
    assert fake_provider.connections == 0


@pytest.mark.parametrize(
    "key",
    [
        "",
        "k" * 19,
        "k" * 201,
        "k" * 10 + " " + "k" * 20,
        " " + "k" * 20,
        "k" * 20 + " ",
        "k" * 20 + "\n",
        "k" * 10 + "\t" + "k" * 20,
        "k" * 10 + "\x00" + "k" * 20,
        "k" * 10 + "\x07" + "k" * 20,
        "k" * 10 + "\x7f" + "k" * 20,
        "k" * 10 + "ключ" + "k" * 20,
        "k" * 10 + "\u00a0" + "k" * 20,  # a no-break space looks like a space but is not ASCII
        "k" * 10 + "\u2028" + "k" * 20,  # a line separator
        f"{MARKER}-{MARKER}" + "é",
        MARKER,
    ],
)
def test_a_bad_key_is_422_and_keeps_the_old_one(
    runtime: Runtime, client: TestClient, key: str
) -> None:
    assert set_key(client).status_code == 204

    response = set_key(client, key)

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "INVALID_API_KEY"
    assert error["details"] == {"field": "openrouter_api_key"}
    assert MARKER not in response.text
    assert runtime.credentials.openrouter_key() == KEY


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"openrouter_api_key": None},
        {"openrouter_api_key": 12345678901234567890},
        {"openrouter_api_key": [KEY]},
        {"openrouter_api_key": KEY, "extra": 1},
        {"api_key": KEY},
        KEY,
        [KEY],
    ],
)
def test_a_credentials_body_of_the_wrong_shape_is_422(
    runtime: Runtime, client: TestClient, body: Any
) -> None:
    response = client.post("/v1/runtime/credentials", json=body)

    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"
    assert runtime.credentials.openrouter_key() is None
    assert MARKER not in response.text


def test_the_key_is_kept_in_memory_only(
    runtime: Runtime, client: TestClient, make_runtime: RuntimeFactory
) -> None:
    set_key(client)
    runtime.close()
    assert runtime.credentials.openrouter_key() is None  # close forgets it

    reopened = make_runtime(with_tools=False, data_dir=runtime.database.data_dir)

    assert reopened.credentials.openrouter_key() is None
    assert settings_of(client_for(reopened))["has_api_key"] is False


# --- POST /v1/runtime/credentials/check ----------------------------------------------------------

# Text a provider could put into an answer (an echo of the key, a message of its own): it must not
# reach a response or a log line of ours.
ECHO = "Hq5Tn8Wd3Pz1"
UPSTREAM_TEXT = "OpenRouter не відповів вчасно або повернув помилку сервера: ключ не перевірено."
INVALID_RESPONSE_TEXT = (
    "OpenRouter повернув відповідь, яку не можна використати: ключ не перевірено."
)
NOT_SENT_TEXT = "Не вдалося з'єднатися з OpenRouter: запит не надіслано, ключ не перевірено."


def check(client: TestClient) -> Response:
    return client.post("/v1/runtime/credentials/check")


def checked(client: TestClient, key: str = KEY) -> Response:
    """Set the key and check it."""
    assert set_key(client, key).status_code == 204
    return check(client)


def assert_unavailable(response: Response, code: str, message: str) -> None:
    """503, retryable, the fixed text, and nothing of the key or of what the provider said."""
    assert response.status_code == 503, response.text
    assert response.json() == {
        "error": {"code": code, "message": message, "details": {}, "retryable": True}
    }
    for secret in (MARKER, ECHO, LABEL):
        assert secret not in response.text


def test_a_valid_key_is_valid_with_the_free_tier_flag_and_the_remaining_limit(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    response = checked(client)

    assert response.status_code == 200
    # Exactly these three keys: no label, no usage, nothing else of the provider's answer.
    assert response.json() == {
        "state": "valid",
        "is_free_tier": False,
        "limit_remaining_usd": "87.5",
    }
    assert LABEL not in response.text
    assert MARKER not in response.text
    [request] = fake_provider.requests  # exactly one request
    assert (request.method, request.path, request.body) == ("GET", "/api/v1/key", b"")
    assert request.headers["authorization"] == f"Bearer {KEY}"
    # Asked for plain; a server may still compress (see the gzip tests: nothing is decoded).
    assert request.headers["accept-encoding"] == "identity"
    assert KEY not in request.path  # the key is in the header only


def test_a_free_tier_key_is_reported(client: TestClient, fake_provider: FakeProvider) -> None:
    fake_provider.reply(body=key_info(is_free_tier=True, limit_remaining=0.25))

    assert checked(client).json() == {
        "state": "valid",
        "is_free_tier": True,
        "limit_remaining_usd": "0.25",
    }


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        (87.5, "87.5"),
        (5, "5"),
        (10, "10"),
        (0, "0"),
        (0.0, "0"),
        (-0.0, "0"),
        (3.0, "3"),
        (100.10, "100.1"),
        (0.000001, "0.000001"),
        (1e-07, "0.0000001"),
        (12.3456789, "12.3456789"),
        (1e22, "10000000000000000000000"),
        (-0.25, "-0.25"),  # an overspent limit stays negative: nothing is invented
        (None, None),  # no spending limit
    ],
)
def test_the_remaining_limit_is_a_plain_decimal_string_or_null(
    client: TestClient, fake_provider: FakeProvider, sent: float | None, expected: str | None
) -> None:
    fake_provider.reply(body=key_info(limit_remaining=sent))

    response = checked(client)

    assert response.status_code == 200
    assert response.json()["limit_remaining_usd"] == expected


def test_a_missing_limit_is_the_same_as_no_limit(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    fake_provider.reply(body=json.dumps({"data": {"is_free_tier": False}}).encode())

    assert checked(client).json() == {
        "state": "valid",
        "is_free_tier": False,
        "limit_remaining_usd": None,
    }


def test_a_key_that_openrouter_rejects_is_200_invalid(
    runtime: Runtime, client: TestClient, fake_provider: FakeProvider
) -> None:
    fake_provider.reply(
        401, f'{{"error":{{"code":401,"message":"No auth for {KEY} {ECHO}"}}}}'.encode()
    )

    response = checked(client)

    assert response.status_code == 200
    assert response.json() == {"state": "invalid"}  # no other field
    assert len(fake_provider.requests) == 1
    assert MARKER not in response.text
    assert ECHO not in response.text
    # The check only reports: the key stays where it is, for the user to replace or delete.
    assert runtime.credentials.openrouter_key() == KEY
    assert settings_of(client)["has_api_key"] is True


def test_without_a_key_it_is_409_and_nothing_is_sent(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    response = check(client)

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "API_KEY_REQUIRED",
            "message": "Спершу задайте ключ API OpenRouter у Налаштуваннях",
            "details": {},
            "retryable": False,
        }
    }
    assert fake_provider.requests == []


def test_a_deleted_key_is_not_checked_and_a_replaced_key_is_the_one_sent(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    other = KEY[:-4] + "wxyz"
    assert checked(client).status_code == 200
    assert checked(client, other).status_code == 200
    assert client.delete("/v1/runtime/credentials").status_code == 204
    assert check(client).status_code == 409

    assert [r.headers["authorization"] for r in fake_provider.requests] == [
        f"Bearer {KEY}",
        f"Bearer {other}",
    ]


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        pytest.param(
            json.dumps({"openrouter_api_key": "o" * 30}).encode(), "application/json", id="a key"
        ),
        pytest.param(b'{"openrouter_api_key": ', "application/json", id="unfinished JSON"),
        pytest.param(b"not json at all", "application/json", id="not JSON"),
        pytest.param(b"openrouter_api_key=" + b"o" * 30, "text/plain", id="plain text"),
        pytest.param(b"\xff\xfe\x00", "application/octet-stream", id="binary"),
    ],
)
def test_the_body_of_a_check_is_ignored(
    runtime: Runtime,
    client: TestClient,
    fake_provider: FakeProvider,
    content: bytes,
    content_type: str,
) -> None:
    """The check has no input: whatever the body is, the key in memory is what is checked."""
    set_key(client)

    response = client.post(
        "/v1/runtime/credentials/check", content=content, headers={"Content-Type": content_type}
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "valid"
    [request] = fake_provider.requests
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.body == b""  # nothing of it is forwarded
    assert "o" * 30 not in repr(request)
    assert runtime.credentials.openrouter_key() == KEY  # and it does not replace the key


def test_every_check_is_one_request_and_nothing_is_cached(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    set_key(client)
    assert check(client).status_code == 200
    assert check(client).status_code == 200

    assert len(fake_provider.requests) == 2


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_a_server_error_is_503_upstream_and_is_not_repeated(
    client: TestClient, fake_provider: FakeProvider, status: int
) -> None:
    fake_provider.reply(status, f'{{"error":"{KEY} {ECHO}"}}'.encode())

    response = checked(client)

    assert_unavailable(response, "KEY_CHECK_UPSTREAM", f"{UPSTREAM_TEXT} (HTTP {status})")
    assert len(fake_provider.requests) == 1  # one attempt, no retry


@pytest.mark.parametrize("status", [201, 301, 302, 400, 402, 403, 404, 408, 429])
def test_any_other_status_is_503_invalid_response_and_is_not_repeated(
    client: TestClient, fake_provider: FakeProvider, status: int
) -> None:
    # A redirect is not followed either: the key would go to the address it points at.
    fake_provider.reply(
        status, f'{{"error":"{KEY} {ECHO}"}}'.encode(), headers={"Location": "/api/v1/elsewhere"}
    )

    response = checked(client)

    assert_unavailable(
        response, "KEY_CHECK_INVALID_RESPONSE", f"{INVALID_RESPONSE_TEXT} (HTTP {status})"
    )
    assert [request.path for request in fake_provider.requests] == ["/api/v1/key"]


def test_the_timeout_is_15_seconds_and_a_slow_answer_is_503_upstream(
    client: TestClient, fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert openrouter_key.CHECK_TIMEOUT_S == 15.0  # written out: the product decision
    # The same code path with a short limit, so that the test does not wait 15 seconds.
    monkeypatch.setattr(openrouter_key, "CHECK_TIMEOUT_S", 0.3)
    fake_provider.reply(delay_s=10)
    set_key(client)

    started = time.monotonic()
    response = check(client)
    elapsed = time.monotonic() - started

    assert_unavailable(response, "KEY_CHECK_UPSTREAM", UPSTREAM_TEXT)
    assert elapsed < 5  # gave up at the limit, long before the provider answered
    assert len(fake_provider.requests) == 1


def test_an_answer_that_trickles_in_is_cut_at_the_deadline_of_the_whole_call(
    client: TestClient, fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each read is quick, so no read timeout fires: only the deadline of the call can end it."""
    monkeypatch.setattr(openrouter_key, "CHECK_TIMEOUT_S", 0.3)
    fake_provider.reply(drip_s=0.05)
    set_key(client)

    started = time.monotonic()
    response = check(client)
    elapsed = time.monotonic() - started

    assert_unavailable(response, "KEY_CHECK_UPSTREAM", UPSTREAM_TEXT)
    assert elapsed < 5
    assert len(fake_provider.requests) == 1


def test_slow_headers_are_cut_at_the_deadline_of_the_whole_call(
    client: TestClient, fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The head of the answer comes one byte every 0.1 s (about 8 s in all). No single read waits
    anywhere near the limit, so only a deadline of the call itself can end this one."""
    monkeypatch.setattr(openrouter_key, "CHECK_TIMEOUT_S", 1.0)
    fake_provider.reply(header_drip_s=0.1)
    set_key(client)

    started = time.monotonic()
    response = check(client)
    elapsed = time.monotonic() - started

    assert_unavailable(response, "KEY_CHECK_UPSTREAM", UPSTREAM_TEXT)
    assert elapsed < 3
    assert len(fake_provider.requests) == 1
    # Nothing is left running: the connection was closed at the deadline, so the fake sees the
    # client gone long before it would have finished the head.
    assert fake_provider.wait_idle(2)


def test_an_answer_that_is_cut_short_is_503_upstream(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    body = key_info()
    fake_provider.reply(body=body[:20], claimed_length=len(body))

    assert_unavailable(checked(client), "KEY_CHECK_UPSTREAM", UPSTREAM_TEXT)
    assert len(fake_provider.requests) == 1


def test_a_network_error_is_503_not_sent(client: TestClient, fake_provider: FakeProvider) -> None:
    fake_provider.close()  # nobody listens on the address any more: the connection is refused

    assert_unavailable(checked(client), "KEY_CHECK_NOT_SENT", NOT_SENT_TEXT)


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        # Before the request is sent: the provider cannot have seen it.
        (httpx2.ConnectTimeout, "KEY_CHECK_NOT_SENT", NOT_SENT_TEXT),
        (httpx2.PoolTimeout, "KEY_CHECK_NOT_SENT", NOT_SENT_TEXT),
        (httpx2.ConnectError, "KEY_CHECK_NOT_SENT", NOT_SENT_TEXT),
        # After it: the request may have arrived.
        (httpx2.WriteTimeout, "KEY_CHECK_UPSTREAM", UPSTREAM_TEXT),
        (httpx2.ReadTimeout, "KEY_CHECK_UPSTREAM", UPSTREAM_TEXT),
        (httpx2.RemoteProtocolError, "KEY_CHECK_UPSTREAM", UPSTREAM_TEXT),
    ],
)
def test_a_transport_failure_is_503_not_sent_only_before_the_request_is_sent(
    client: TestClient,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    error: type[httpx2.HTTPError],
    code: str,
    message: str,
) -> None:
    """The kind of failure decides the code. A connect or pool timeout is raised by the transport
    itself, so the test makes it raise the same exception instead of waiting for a real one."""

    def fail(self: httpx2.Client, *args: object, **kwargs: object) -> NoReturn:
        raise error(f"{KEY} {ECHO}")

    monkeypatch.setattr(httpx2.Client, "stream", fail)

    assert_unavailable(checked(client), code, message)
    assert fake_provider.requests == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(f"not json {KEY} {ECHO}".encode(), id="not json"),
        pytest.param(b"", id="empty"),
        pytest.param(f'{{"data": {{"is_free_tier": false, "x": "{KEY} {ECHO}'.encode(), id="cut"),
        pytest.param(b"\xff\xfe\x00", id="not utf-8"),
        pytest.param(b"[]", id="a list"),
        pytest.param(b'"text"', id="a string"),
        pytest.param(b"{}", id="no data"),
        pytest.param(b'{"data": null}', id="null data"),
        pytest.param(b'{"data": []}', id="data is a list"),
        pytest.param(b'{"data": {}}', id="no is_free_tier"),
        pytest.param(key_info(is_free_tier="false"), id="is_free_tier is a string"),
        pytest.param(key_info(is_free_tier=0), id="is_free_tier is a number"),
        pytest.param(key_info(is_free_tier=None), id="is_free_tier is null"),
        pytest.param(key_info(limit_remaining="5"), id="limit_remaining is a string"),
        pytest.param(key_info(limit_remaining=True), id="limit_remaining is a boolean"),
        pytest.param(key_info(limit_remaining=[5]), id="limit_remaining is a list"),
        pytest.param(key_info(limit_remaining=float("nan")), id="limit_remaining is NaN"),
        pytest.param(key_info(limit_remaining=float("inf")), id="limit_remaining is infinite"),
        pytest.param(b"[" * 60_000, id="nesting that exhausts the parser"),
        pytest.param(b'{"data": "' + b"x" * 70_000 + b'"}', id="a body over 64 KiB"),
    ],
)
def test_a_broken_answer_is_503_invalid_response_without_echo(
    client: TestClient,
    fake_provider: FakeProvider,
    caplog: pytest.LogCaptureFixture,
    body: bytes,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake_provider.reply(body=body)

    response = checked(client)

    assert_unavailable(response, "KEY_CHECK_INVALID_RESPONSE", INVALID_RESPONSE_TEXT)
    assert len(fake_provider.requests) == 1
    assert ECHO not in caplog.text
    assert MARKER not in caplog.text


MAX_ANSWER = 64 * 1024


def key_info_of_size(size: int) -> bytes:
    """A valid answer of exactly ``size`` bytes: the padding is in a field the check ignores."""
    empty = key_info(padding="")
    body = key_info(padding="x" * (size - len(empty)))
    assert len(body) == size
    return body


def test_an_answer_of_exactly_64_kib_is_accepted(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    assert openrouter_key.MAX_BODY_BYTES == MAX_ANSWER  # written out: the product limit
    fake_provider.reply(body=key_info_of_size(MAX_ANSWER))

    response = checked(client)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "state": "valid",
        "is_free_tier": False,
        "limit_remaining_usd": "87.5",
    }
    assert len(fake_provider.requests) == 1


def test_an_answer_one_byte_over_64_kib_is_503_invalid_response(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The same answer as above with one byte more: only its size is wrong."""
    fake_provider.reply(body=key_info_of_size(MAX_ANSWER + 1))

    assert_unavailable(checked(client), "KEY_CHECK_INVALID_RESPONSE", INVALID_RESPONSE_TEXT)
    assert len(fake_provider.requests) == 1


GZIP = {"Content-Encoding": "gzip"}


def test_a_gzip_body_from_a_server_that_ignores_identity_is_not_decoded(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A valid answer, compressed against the request. A client that decoded it would call the key
    valid; this one reads the wire bytes, and those are not JSON."""
    fake_provider.reply(body=gzip.compress(key_info()), headers=GZIP)

    assert_unavailable(checked(client), "KEY_CHECK_INVALID_RESPONSE", INVALID_RESPONSE_TEXT)
    [request] = fake_provider.requests
    assert request.headers["accept-encoding"] == "identity"  # asked for plain, got gzip anyway


def test_the_size_limit_counts_wire_bytes_so_a_small_gzip_of_a_big_body_is_within_it(
    fake_provider: FakeProvider,
) -> None:
    """The body inflates to over 64 KiB but travels in a few hundred bytes. It is not refused for
    its size, and it is not inflated: ``_fetch`` hands on exactly the bytes that were sent."""
    inflated = key_info_of_size(3 * MAX_ANSWER)
    wire = gzip.compress(inflated)
    assert len(wire) < MAX_ANSWER < len(inflated)  # the premise of the test
    fake_provider.reply(body=wire, headers=GZIP)

    status, content = openrouter_key._fetch(fake_provider.base_url + openrouter_key.KEY_PATH, KEY)

    assert (status, content) == (200, wire)
    assert len(fake_provider.requests) == 1


def test_a_gzip_body_of_more_than_64_kib_on_the_wire_is_refused(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """Random bytes do not compress: the gzip is over the limit on the wire, whatever it holds."""
    wire = gzip.compress(os.urandom(MAX_ANSWER + 4096))
    assert len(wire) > MAX_ANSWER  # the premise of the test
    fake_provider.reply(body=wire, headers=GZIP)

    with pytest.raises(openrouter_key.KeyCheckError) as refused:
        openrouter_key._fetch(fake_provider.base_url + openrouter_key.KEY_PATH, KEY)
    assert refused.value.kind == "invalid_response"

    assert_unavailable(checked(client), "KEY_CHECK_INVALID_RESPONSE", INVALID_RESPONSE_TEXT)
    assert len(fake_provider.requests) == 2


@pytest.mark.parametrize("checker", ["no checker", "test mode without a provider address"])
def test_without_a_provider_for_the_mode_nothing_is_sent(
    make_runtime: RuntimeFactory,
    fake_provider: FakeProvider,
    tmp_path: Path,
    checker: str,
) -> None:
    """Paid AI is never reachable from tests: no address, no request (``resolve_base_url``)."""
    key_checker = (
        None if checker == "no checker" else openrouter_key_checker(make_config(tmp_path, "test"))
    )
    other = client_for(make_runtime(with_tools=False, key_checker=key_checker))

    assert_unavailable(checked(other), "KEY_CHECK_NOT_SENT", NOT_SENT_TEXT)
    assert fake_provider.requests == []


def test_the_check_leaves_no_trace_of_the_key_or_of_the_answer(
    client: TestClient,
    fake_provider: FakeProvider,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(openrouter_key, "CHECK_TIMEOUT_S", 0.5)  # for the two slow scripts
    echoing = f"{KEY} {ECHO} {LABEL}".encode()
    scripts: list[dict[str, Any]] = [
        {},
        {"status": 401, "body": echoing},
        {"status": 500, "body": echoing},
        {"status": 403, "body": echoing},
        {"body": echoing},
        {"body": key_info(label=f"{KEY} {ECHO}", is_free_tier="x")},
        {"delay_s": 10},
        {"drip_s": 0.05},
    ]
    responses: list[Response] = []
    set_key(client)
    for script in scripts:
        fake_provider.reply(**script)
        responses.append(check(client))

    assert [r.status_code for r in responses] == [200, 200, 503, 503, 503, 503, 503, 503]
    for response in responses:
        for secret in (MARKER, ECHO, LABEL):
            assert secret not in response.text
            assert secret not in repr(dict(response.headers))
    for record in caplog.records:
        assert record.exc_info is None
        for secret in (MARKER, ECHO, LABEL):
            assert secret not in repr(vars(record))
            assert secret not in record.getMessage()


# --- the key never leaves memory -----------------------------------------------------------------


def exercise(client: TestClient) -> list[Response]:
    """Every kind of request that touches the key or a secret-looking value, valid or not."""
    json_header = {"Content-Type": "application/json"}
    return [
        set_key(client),
        client.get("/v1/settings"),
        client.patch("/v1/settings", json=CHOSEN),
        client.patch("/v1/settings", json={"stt_language": MARKER}),
        client.patch("/v1/settings", json={"run_budget_usd": KEY}),
        client.patch("/v1/settings", json={"openrouter_api_key": KEY}),
        set_key(client, f"{MARKER} with a space inside it"),
        set_key(client, MARKER),
        client.post("/v1/runtime/credentials", json={"openrouter_api_key": KEY, "extra": KEY}),
        client.post("/v1/runtime/credentials", json={"openrouter_api_key": [KEY]}),
        client.post("/v1/runtime/credentials", json=KEY),
        client.post(
            "/v1/runtime/credentials",
            content=b'{"openrouter_api_key": "' + KEY.encode() + b'"',
            headers=json_header,
        ),
        client.post("/v1/runtime/credentials", content=KEY.encode(), headers=json_header),
        client.get("/v1/runtime/credentials"),
        client.put("/v1/settings", json={"openrouter_api_key": KEY}),
        client.post("/v1/runtime/credentials/check"),
        client.get("/v1/runtime/credentials/check"),
        client.delete("/v1/runtime/credentials"),
        client.post("/v1/runtime/credentials/check"),  # no key: 409
        set_key(client),
    ]


def test_no_response_carries_the_key(client: TestClient) -> None:
    responses = exercise(client)

    assert {r.status_code for r in responses} >= {200, 204, 405, 409, 422}
    for response in responses:
        assert MARKER not in response.text
        assert MARKER not in repr(dict(response.headers))


def test_the_key_is_not_in_the_log(
    runtime: Runtime,
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    exercise(client)

    # A failure whose exception text is the key itself: the log and the 500 must not carry it.
    def fail(_key: str) -> None:
        raise RuntimeError(KEY)

    monkeypatch.setattr(runtime.credentials, "set_openrouter_key", fail)
    crashed = set_key(client)

    assert crashed.status_code == 500
    assert error_of(crashed)["code"] == "INTERNAL_ERROR"
    assert MARKER not in crashed.text
    assert "Unhandled RuntimeError" in caplog.text  # the capture works: this one was logged
    assert MARKER not in caplog.text
    for record in caplog.records:
        assert MARKER not in repr(vars(record))
        assert record.exc_info is None


def test_the_key_is_not_in_any_byte_of_the_workspace(runtime: Runtime, client: TestClient) -> None:
    exercise(client)
    data_dir = runtime.database.data_dir
    with closing(sqlite3.connect(data_dir / DB_FILENAME)) as raw:
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # The workspace lock is a byte-range-locked file with no content: it cannot be read.
    blobs = {
        path.relative_to(data_dir).as_posix(): path.read_bytes()
        for path in data_dir.rglob("*")
        if path.is_file() and path.name != LOCK_FILENAME
    }

    assert DB_FILENAME in blobs
    # The scan sees what the database stores: the setting written by `exercise` is there ...
    assert CHOSEN["analysis_model"].encode() in b"".join(blobs.values())
    # ... and nothing of the key is: not raw, not as a partial echo.
    for name, blob in blobs.items():
        assert MARKER.encode() not in blob, name
        assert KEY.encode() not in blob, name


def test_the_key_is_not_in_the_repr_of_the_runtime(runtime: Runtime, client: TestClient) -> None:
    set_key(client)

    for subject in (
        runtime,
        runtime.credentials,
        runtime.settings,
        vars(runtime),
        vars(runtime.credentials),
        vars(runtime.settings),
    ):
        assert MARKER not in repr(subject)
        assert MARKER not in str(subject)
    assert repr(runtime.credentials) == "CredentialStore(openrouter_key=set)"
    client.delete("/v1/runtime/credentials")
    assert repr(runtime.credentials) == "CredentialStore(openrouter_key=unset)"


# --- access --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/v1/settings", None),
        ("PATCH", "/v1/settings", {"stt_language": "en"}),
        ("POST", "/v1/runtime/credentials", {"openrouter_api_key": KEY}),
        ("DELETE", "/v1/runtime/credentials", None),
        ("POST", "/v1/runtime/credentials/check", None),
    ],
)
def test_without_the_session_token_nothing_is_done(
    runtime: Runtime,
    anonymous: TestClient,
    fake_provider: FakeProvider,
    method: str,
    path: str,
    body: Any,
) -> None:
    held = "k" * 30  # a key that is already set: nothing the anonymous caller does may touch it
    runtime.credentials.set_openrouter_key(held)

    response = anonymous.request(method, path, json=body)

    assert response.status_code == 401
    assert MARKER not in response.text
    assert runtime.credentials.openrouter_key() == held
    assert fake_provider.requests == []  # a check without the token does not reach the provider


# --- 429 for a local limit -----------------------------------------------------------------------


def test_an_exceeded_budget_is_http_429_with_its_domain_code(
    app: FastAPI, runtime: Runtime, client: TestClient
) -> None:
    ledger = SpendLedger(runtime.database)

    def over_budget() -> None:
        ledger.reserve("run-1", "req-1", MAI, Decimal("2"), Decimal("1"), {})

    app.add_api_route("/v1/test-over-budget", over_budget)

    response = client.get("/v1/test-over-budget")

    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": "BUDGET_EXCEEDED",
            "message": "Ліміт витрат вичерпано: наступний запит не вміщується в бюджет",
            "details": {
                "committed_micro_usd": 0,
                "requested_micro_usd": 2_000_000,
                "ceiling_micro_usd": 1_000_000,
            },
            "retryable": False,
        }
    }
