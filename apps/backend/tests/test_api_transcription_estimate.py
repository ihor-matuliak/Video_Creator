"""POST /v1/transcription-runs/estimate, ``force`` and 409 TRANSCRIPT_ALREADY_EXISTS on
POST /v1/transcription-runs, GET /v1/transcription-runs?source_id= (FR-050, ADR 037).

The provider is a fake ``SttClient`` and the audio extraction is a fake: nothing goes over the
network and nothing is paid for. The numbers of the estimate are written out by hand from the plan
of ``domain.chunking`` (cores of 300 s with 5 s of context on each side, clipped to the source) and
the upper tariffs of ADR 032 (0.0001 USD/s for MAI, 0.0003 USD/s for Gemini = 100 and 300 micro-USD
per second): for a 620 s source the upload windows are 0-305 s, 295-605 s and 595-620 s, that is
305 + 310 + 25 = 640 billed seconds in 3 chunks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select

from clipforge.adapters.openrouter_stt import MAI_MODEL
from clipforge.services.runtime import Runtime
from clipforge.services.transcription_runs import SttRunOptions
from clipforge.storage import (
    Job,
    SpendEntry,
    SpendRepository,
    TranscriptChunk,
    TranscriptRepository,
    TranscriptRevision,
    TranscriptRun,
    TranscriptRunRepository,
)
from clipforge.storage.util import utc_now

from .support import RuntimeFactory, client_for
from .test_api_sources import wait_job
from .test_api_transcription_runs import (
    CHUNKS,
    GEMINI,
    RecordingFactory,
    add_source,
    error_of,
    outcome,
    run_of,
    seed_crashed_run,
    set_key,
    start,
    started,
)
from .test_transcription_runs import FakeExtractor, FakeStt, speech

DURATION_MS = 620_000
TWO_STREAMS = [
    {"index": 1, "codec": "aac", "default": True},
    {"index": 2, "codec": "aac", "default": False},
]
SRT = """1
00:00:01,000 --> 00:00:03,000
Hello there
"""


# --- helpers ----------------------------------------------------------------------------------


def estimate(client: TestClient, source_id: str, **fields: Any) -> Response:
    body = {"source_id": source_id, "audio_stream_index": 1, **fields}
    return client.post("/v1/transcription-runs/estimate", json=body)


def estimated(client: TestClient, source_id: str, **fields: Any) -> dict[str, Any]:
    response = estimate(client, source_id, **fields)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def listed(client: TestClient, source_id: str) -> list[dict[str, Any]]:
    response = client.get("/v1/transcription-runs", params={"source_id": source_id})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def completed(client: TestClient, source_id: str, **fields: Any) -> dict[str, Any]:
    """A run started through the API and finished: its ``GET`` body (``revision_id`` is set)."""
    created = started(client, source_id, **fields)
    assert wait_job(client, created["job_id"])["state"] == "succeeded"
    run = run_of(client, created["id"])
    assert run["status"] == "completed"
    assert run["revision_id"] is not None
    return run


def row_counts(runtime: Runtime) -> dict[str, int]:
    """How much of everything a run writes there is: a refused request must leave these alone."""
    with runtime.database.session() as session:
        return {
            "runs": int(session.scalar(select(func.count()).select_from(TranscriptRun)) or 0),
            "chunks": int(session.scalar(select(func.count()).select_from(TranscriptChunk)) or 0),
            "revisions": int(
                session.scalar(select(func.count()).select_from(TranscriptRevision)) or 0
            ),
            "jobs": int(session.scalar(select(func.count()).select_from(Job)) or 0),
            "spend": int(session.scalar(select(func.count()).select_from(SpendEntry)) or 0),
        }


def seed_run(
    runtime: Runtime,
    source_id: str,
    *,
    status: str = "completed",
    model: str = MAI_MODEL,
    provider: str = "openrouter",
    language: str | None = None,
    stream: int = 1,
    revision: str = "stored",
    error_code: str | None = None,
    now: Callable[[], str] | None = None,
) -> tuple[str, str | None]:
    """A run row as the run service leaves it, with the hash of its options. ``revision``:
    ``stored`` (an ``origin = stt`` revision exists and ``usage`` names it), ``none`` (``usage``
    names none) or ``dangling`` (``usage`` names a revision that does not exist). No job and no
    chunks. Returns ``(run_id, revision_id the usage names)``."""
    options_hash = SttRunOptions(audio_stream_index=stream, language=language).options_hash()
    with runtime.database.write_session() as session:
        runs = TranscriptRunRepository(session, now=now or utc_now)
        run = runs.add_run(
            source_id=source_id,
            model=model,
            provider=provider,
            options_hash=options_hash,
            status=status,
        )
        revision_id: str | None = None
        if revision == "stored":
            revision_id = (
                TranscriptRepository(session)
                .add_revision(
                    source_id=source_id,
                    origin="stt",
                    run_id=run.id,
                    timing_level="segment",
                    language="en",
                    duration_ms=DURATION_MS,
                    coverage=[{"start_ms": 0, "end_ms": DURATION_MS, "state": "succeeded"}],
                    metadata={},
                )
                .id
            )
        elif revision == "dangling":
            revision_id = "no-such-revision"
        runs.update_run(
            run.id,
            usage={"committed_micro_usd": 0, "error_code": error_code, "revision_id": revision_id},
        )
        return run.id, revision_id


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def stt() -> FakeStt:
    return FakeStt(speech)


@pytest.fixture
def factory(stt: FakeStt) -> RecordingFactory:
    return RecordingFactory(stt)


@pytest.fixture
def extractor() -> FakeExtractor:
    return FakeExtractor()


@pytest.fixture
def runtime(
    make_runtime: RuntimeFactory, factory: RecordingFactory, extractor: FakeExtractor
) -> Runtime:
    return make_runtime(stt_client_factory=factory, extract_audio=extractor)


@pytest.fixture
def source_id(runtime: Runtime, tmp_path: Path) -> str:
    """A 620 s source with two audio streams (1 and 2)."""
    return add_source(runtime, tmp_path, duration_ms=DURATION_MS, audio_streams=TWO_STREAMS)


@pytest.fixture
def keyed(client: TestClient) -> TestClient:
    set_key(client)
    return client


# --- POST /v1/transcription-runs/estimate: the numbers ----------------------------------------


@pytest.mark.parametrize(
    ("duration_ms", "model", "chunk_count", "billed_seconds", "rate", "estimate_micro_usd"),
    [
        # 305 + 310 + 25 s in three chunks.
        pytest.param(620_000, MAI_MODEL, 3, 640, "0.0001", 64_000, id="mai-620s"),
        pytest.param(620_000, GEMINI, 3, 640, "0.0003", 192_000, id="gemini-620s"),
        # One chunk, its upload window 90.5 s is billed as 91 s.
        pytest.param(90_500, MAI_MODEL, 1, 91, "0.0001", 9_100, id="mai-90.5s"),
        pytest.param(90_500, GEMINI, 1, 91, "0.0003", 27_300, id="gemini-90.5s"),
        # A core of 300 s plus a core of 1 ms: windows of 300.001 s (301 s) and 5.001 s (6 s).
        pytest.param(300_001, MAI_MODEL, 2, 307, "0.0001", 30_700, id="mai-300.001s"),
        pytest.param(300_001, GEMINI, 2, 307, "0.0003", 92_100, id="gemini-300.001s"),
    ],
)
def test_the_estimate_prices_every_chunk_at_the_upper_tariff(
    keyed: TestClient,
    runtime: Runtime,
    tmp_path: Path,
    duration_ms: int,
    model: str,
    chunk_count: int,
    billed_seconds: int,
    rate: str,
    estimate_micro_usd: int,
) -> None:
    source = add_source(runtime, tmp_path, "priced", duration_ms=duration_ms)

    body = estimated(keyed, source, model=model, budget_usd="5")

    assert body == {
        "source_id": source,
        "audio_stream_index": 1,
        "model": model,
        "language": None,
        "duration_ms": duration_ms,
        "chunk_count": chunk_count,
        "billed_seconds": billed_seconds,
        "usd_per_second_upper": rate,
        "estimate_micro_usd": estimate_micro_usd,
        "price_basis": "live_probe_2026-11-15_x3",
        "budget_usd": "5",
        "within_budget": True,
        "existing": None,
    }


def test_the_default_model_is_mai_and_no_budget_anywhere_is_not_an_error(
    keyed: TestClient, source_id: str
) -> None:
    body = estimated(keyed, source_id)

    assert body["model"] == MAI_MODEL
    assert body["estimate_micro_usd"] == 64_000
    assert body["budget_usd"] is None
    assert body["within_budget"] is None


@pytest.mark.parametrize(
    ("budget", "within"),
    [
        pytest.param("0.064", True, id="exactly-the-estimate"),
        pytest.param("0.063999", False, id="one-micro-usd-short"),
        pytest.param("0.01", False, id="the-lowest-budget"),
        pytest.param("5", True, id="plenty"),
        pytest.param("1000", True, id="the-highest-budget"),
    ],
)
def test_within_budget_compares_the_estimate_with_the_budget(
    keyed: TestClient, source_id: str, budget: str, within: bool
) -> None:
    body = estimated(keyed, source_id, budget_usd=budget)

    assert body["estimate_micro_usd"] == 64_000
    assert (body["budget_usd"], body["within_budget"]) == (budget, within)


def test_model_language_and_budget_default_to_the_settings_and_the_request_overrides_them(
    keyed: TestClient, source_id: str
) -> None:
    patched = keyed.patch(
        "/v1/settings", json={"stt_model": GEMINI, "stt_language": "en", "run_budget_usd": "0.19"}
    )
    assert patched.status_code == 200

    from_settings = estimated(keyed, source_id)
    overridden = estimated(keyed, source_id, model=MAI_MODEL, language="es", budget_usd="0.07")

    assert (
        from_settings["model"],
        from_settings["language"],
        from_settings["estimate_micro_usd"],
        from_settings["budget_usd"],
        from_settings["within_budget"],
    ) == (GEMINI, "en", 192_000, "0.19", False)
    assert (
        overridden["model"],
        overridden["language"],
        overridden["estimate_micro_usd"],
        overridden["budget_usd"],
        overridden["within_budget"],
    ) == (MAI_MODEL, "es", 64_000, "0.07", True)


def test_the_estimate_is_the_sum_of_the_first_reservations_of_a_run(
    keyed: TestClient, runtime: Runtime, source_id: str
) -> None:
    body = estimated(keyed, source_id, budget_usd="5")
    run = completed(keyed, source_id)

    with runtime.database.session() as session:
        reserved = [
            entry.reserved_micro_usd for entry in SpendRepository(session).list_for_run(run["id"])
        ]
    assert sorted(reserved) == [2_500, 30_500, 31_000]  # 25 s, 305 s and 310 s at 100 micro/s
    assert sum(reserved) == body["estimate_micro_usd"] == 64_000


# --- POST /v1/transcription-runs/estimate: refusals -------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("model", "openai/whisper-1", "INVALID_STT_MODEL"),
        ("model", "", "INVALID_STT_MODEL"),
        ("language", "eng", "INVALID_STT_LANGUAGE"),
        ("language", "EN", "INVALID_STT_LANGUAGE"),
        ("language", "", "INVALID_STT_LANGUAGE"),
        ("budget_usd", "0", "INVALID_RUN_BUDGET"),
        ("budget_usd", "0.009999", "INVALID_RUN_BUDGET"),
        ("budget_usd", "1000.01", "INVALID_RUN_BUDGET"),
        ("budget_usd", "1e2", "INVALID_RUN_BUDGET"),
        ("budget_usd", "", "INVALID_RUN_BUDGET"),
    ],
)
def test_a_bad_value_is_422_naming_the_request_field(
    keyed: TestClient, source_id: str, field: str, value: str, code: str
) -> None:
    response = estimate(keyed, source_id, **{field: value})

    assert response.status_code == 422
    assert error_of(response)["code"] == code
    assert error_of(response)["details"] == {"field": field}


def test_the_values_are_checked_before_the_source(keyed: TestClient, source_id: str) -> None:
    assert outcome(estimate(keyed, "no-such-source", budget_usd="0")) == (
        422,
        "INVALID_RUN_BUDGET",
    )
    assert outcome(estimate(keyed, "no-such-source", model="openai/whisper-1")) == (
        422,
        "INVALID_STT_MODEL",
    )
    assert outcome(estimate(keyed, "no-such-source")) == (404, "SOURCE_NOT_FOUND")


def test_an_unknown_source_is_404_and_a_missing_audio_stream_is_422(
    keyed: TestClient, source_id: str
) -> None:
    missing = estimate(keyed, "no-such-source")
    wrong_stream = estimate(keyed, source_id, audio_stream_index=7)

    assert outcome(missing) == (404, "SOURCE_NOT_FOUND")
    assert outcome(wrong_stream) == (422, "INVALID_AUDIO_STREAM")
    assert error_of(wrong_stream)["details"] == {"field": "audio_stream_index"}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"source_id": "s"},
        {"audio_stream_index": 1},
        {"source_id": "s", "audio_stream_index": "1"},
        {"source_id": "s", "audio_stream_index": True},
        {"source_id": "s", "audio_stream_index": 1, "budget_usd": 5},
        {"source_id": "s", "audio_stream_index": 1, "force": True},
        {"source_id": "s", "audio_stream_index": 1, "openrouter_api_key": "k" * 24},
    ],
)
def test_a_body_of_the_wrong_shape_is_422(keyed: TestClient, body: dict[str, Any]) -> None:
    response = keyed.post("/v1/transcription-runs/estimate", json=body)

    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_an_estimate_needs_neither_a_key_nor_ffmpeg_and_writes_and_sends_nothing(
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: FakeExtractor,
    stt: FakeStt,
    tmp_path: Path,
) -> None:
    bare = make_runtime(with_tools=False, stt_client_factory=factory, extract_audio=extractor)
    client = client_for(bare)  # no key was given
    source = add_source(bare, tmp_path, duration_ms=DURATION_MS)
    before = row_counts(bare)

    body = estimated(client, source, budget_usd="5")

    assert body["estimate_micro_usd"] == 64_000
    assert row_counts(bare) == before
    assert (stt.calls, factory.keys, extractor.requests) == ([], [], [])


def test_without_the_session_token_nothing_is_done(anonymous: TestClient, source_id: str) -> None:
    body = {"source_id": source_id, "audio_stream_index": 1}

    assert anonymous.post("/v1/transcription-runs/estimate", json=body).status_code == 401
    assert (
        anonymous.get("/v1/transcription-runs", params={"source_id": source_id}).status_code == 401
    )


# --- existing ---------------------------------------------------------------------------------


def test_a_completed_run_is_reported_as_existing(keyed: TestClient, source_id: str) -> None:
    assert estimated(keyed, source_id)["existing"] is None

    run = completed(keyed, source_id)

    existing = estimated(keyed, source_id)["existing"]
    assert existing == {"transcript_id": run["revision_id"], "run_id": run["id"]}
    assert keyed.get(f"/v1/transcripts/{run['revision_id']}").status_code == 200


@pytest.mark.parametrize(
    ("seed", "request_fields", "blocks"),
    [
        pytest.param({}, {}, True, id="same-values"),
        pytest.param({"language": "es"}, {"language": "es"}, True, id="same-language"),
        pytest.param({"model": GEMINI}, {"model": GEMINI}, True, id="same-model"),
        pytest.param({"stream": 2}, {"audio_stream_index": 2}, True, id="same-stream"),
        pytest.param({"status": "partial"}, {}, False, id="partial-run"),
        pytest.param({"status": "interrupted"}, {}, False, id="interrupted-run"),
        pytest.param({"status": "failed"}, {}, False, id="failed-run"),
        pytest.param({"status": "cancelled"}, {}, False, id="cancelled-run"),
        pytest.param({"language": "es"}, {}, False, id="was-es-now-auto"),
        pytest.param({}, {"language": "es"}, False, id="was-auto-now-es"),
        pytest.param({"model": GEMINI}, {}, False, id="was-gemini-now-mai"),
        pytest.param({}, {"model": GEMINI}, False, id="was-mai-now-gemini"),
        pytest.param({"stream": 2}, {}, False, id="was-stream-2-now-1"),
        pytest.param({}, {"audio_stream_index": 2}, False, id="was-stream-1-now-2"),
        pytest.param({"provider": "other-provider"}, {}, False, id="other-provider"),
        pytest.param({"revision": "none"}, {}, False, id="no-revision"),
        pytest.param({"revision": "dangling"}, {}, False, id="revision-does-not-exist"),
    ],
)
def test_only_a_completed_run_with_the_same_model_provider_and_options_blocks(
    keyed: TestClient,
    runtime: Runtime,
    source_id: str,
    seed: dict[str, Any],
    request_fields: dict[str, Any],
    blocks: bool,
) -> None:
    run_id, revision_id = seed_run(runtime, source_id, **seed)
    expected = {"transcript_id": revision_id, "run_id": run_id} if blocks else None
    before = row_counts(runtime)

    assert estimated(keyed, source_id, **request_fields)["existing"] == expected
    response = start(keyed, source_id, **request_fields)

    if blocks:
        assert outcome(response) == (409, "TRANSCRIPT_ALREADY_EXISTS")
        assert error_of(response)["details"] == expected
        assert row_counts(runtime) == before
    else:
        assert response.status_code == 202, response.text
        assert wait_job(keyed, response.json()["job_id"])["state"] == "succeeded"


def test_an_imported_transcript_does_not_block_and_is_not_existing(
    keyed: TestClient, source_id: str
) -> None:
    imported = keyed.post(
        "/v1/transcripts/import", json={"source_id": source_id, "format": "srt", "text": SRT}
    )
    assert imported.status_code == 201, imported.text

    assert estimated(keyed, source_id)["existing"] is None
    created = start(keyed, source_id)
    assert created.status_code == 202, created.text
    assert wait_job(keyed, created.json()["job_id"])["state"] == "succeeded"


def test_a_run_of_another_source_does_not_block(
    keyed: TestClient, runtime: Runtime, source_id: str, tmp_path: Path
) -> None:
    other = add_source(runtime, tmp_path, "other", duration_ms=DURATION_MS)
    seed_run(runtime, other)

    assert estimated(keyed, source_id)["existing"] is None
    created = start(keyed, source_id)
    assert created.status_code == 202, created.text
    assert wait_job(keyed, created.json()["job_id"])["state"] == "succeeded"


# --- POST /v1/transcription-runs: 409 TRANSCRIPT_ALREADY_EXISTS and force ----------------------


def test_a_repeated_create_is_409_and_nothing_is_written_or_sent(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt, factory: RecordingFactory
) -> None:
    first = completed(keyed, source_id)
    before = row_counts(runtime)
    assert before["runs"] == 1
    assert len(stt.calls) == CHUNKS

    response = start(keyed, source_id)

    assert response.status_code == 409
    error = error_of(response)
    assert error["code"] == "TRANSCRIPT_ALREADY_EXISTS"
    assert error["details"] == {"transcript_id": first["revision_id"], "run_id": first["id"]}
    assert error["retryable"] is False
    assert row_counts(runtime) == before
    assert len(stt.calls) == CHUNKS
    assert len(factory.keys) == CHUNKS
    # An explicit `force: false` is the default.
    assert outcome(start(keyed, source_id, force=False)) == (409, "TRANSCRIPT_ALREADY_EXISTS")
    assert row_counts(runtime) == before


def test_force_starts_a_second_run_whose_transcript_is_a_new_revision(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt
) -> None:
    first = completed(keyed, source_id)

    created = started(keyed, source_id, force=True)

    assert set(created) == {"id", "job_id", "status"}
    assert created["status"] == "queued"
    assert created["id"] != first["id"]
    assert wait_job(keyed, created["job_id"])["state"] == "succeeded"
    second = run_of(keyed, created["id"])
    assert second["status"] == "completed"
    assert second["revision_id"] not in (None, first["revision_id"])
    assert len(stt.calls) == 2 * CHUNKS  # the second payment really went out
    counts = row_counts(runtime)
    assert (counts["runs"], counts["jobs"], counts["revisions"]) == (2, 2, 2)
    # The newest completed run is the one reported, and the guard holds again without force.
    assert estimated(keyed, source_id)["existing"] == {
        "transcript_id": second["revision_id"],
        "run_id": second["id"],
    }
    assert outcome(start(keyed, source_id)) == (409, "TRANSCRIPT_ALREADY_EXISTS")


@pytest.mark.parametrize("force", [1, 0, "true", "false", None, [True]])
def test_force_must_be_a_boolean(
    keyed: TestClient, runtime: Runtime, source_id: str, force: Any
) -> None:
    response = start(keyed, source_id, force=force)

    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"
    assert row_counts(runtime)["runs"] == 0


def test_the_checks_of_a_create_are_made_in_the_order_source_existing_transcript_key(
    client: TestClient, runtime: Runtime, source_id: str, tmp_path: Path, stt: FakeStt
) -> None:
    set_key(client)
    completed(client, source_id)
    other = add_source(runtime, tmp_path, "other", duration_ms=DURATION_MS)
    client.delete("/v1/runtime/credentials")  # from here on no key is held
    before = row_counts(runtime)
    calls = len(stt.calls)

    # Every rule fails at once for the first request; each answer shows the next rule.
    assert outcome(start(client, "no-such-source")) == (404, "SOURCE_NOT_FOUND")
    assert outcome(start(client, source_id, audio_stream_index=7)) == (422, "INVALID_AUDIO_STREAM")
    assert outcome(start(client, source_id, budget_usd="0")) == (422, "INVALID_RUN_BUDGET")
    assert outcome(start(client, source_id)) == (409, "TRANSCRIPT_ALREADY_EXISTS")
    assert outcome(start(client, source_id, force=True)) == (409, "API_KEY_REQUIRED")
    assert outcome(start(client, other)) == (409, "API_KEY_REQUIRED")

    assert row_counts(runtime) == before
    assert len(stt.calls) == calls


def test_missing_ffmpeg_is_reported_before_an_existing_transcript(
    make_runtime: RuntimeFactory, factory: RecordingFactory, tmp_path: Path
) -> None:
    bare = make_runtime(with_tools=False, stt_client_factory=factory)
    client = client_for(bare)
    set_key(client)
    source = add_source(bare, tmp_path, duration_ms=DURATION_MS)
    run_id, revision_id = seed_run(bare, source)

    assert outcome(start(client, source)) == (503, "MEDIA_TOOLS_UNAVAILABLE")
    # The estimate does not need FFmpeg and still reports the transcript.
    assert estimated(client, source)["existing"] == {"transcript_id": revision_id, "run_id": run_id}


def test_force_skips_only_the_existing_transcript_check(
    client: TestClient, runtime: Runtime, source_id: str
) -> None:
    # A completed run exists, and another job of the same source is open (queued, no worker).
    seed_run(runtime, source_id)
    set_key(client)
    seeded_run_id, _job_id = seed_crashed_run(runtime, source_id, job_state="queued")
    before = row_counts(runtime)

    assert outcome(start(client, source_id)) == (409, "TRANSCRIPT_ALREADY_EXISTS")
    refused = start(client, source_id, force=True)

    assert outcome(refused) == (409, "TRANSCRIPTION_RUN_ACTIVE")
    assert error_of(refused)["details"]["run_id"] == seeded_run_id
    assert row_counts(runtime) == before


@pytest.mark.parametrize("job_state", ["running", "cancelling"])
def test_force_with_a_running_or_cancelling_job_of_the_source_is_409_run_active(
    client: TestClient, runtime: Runtime, source_id: str, job_state: str
) -> None:
    """Only the check for an existing transcript is skipped: an open job of the source (a run
    that is going on, or one that is being cancelled and has not stopped yet) still refuses."""
    seed_run(runtime, source_id)
    set_key(client)
    seeded_run_id, seeded_job_id = seed_crashed_run(runtime, source_id, job_state=job_state)
    before = row_counts(runtime)

    refused = start(client, source_id, force=True)

    assert outcome(refused) == (409, "TRANSCRIPTION_RUN_ACTIVE")
    assert error_of(refused)["details"] == {"run_id": seeded_run_id, "job_id": seeded_job_id}
    assert row_counts(runtime) == before


def test_an_existing_transcript_is_409_before_the_service_stopping_is_503(
    client: TestClient, runtime: Runtime, source_id: str
) -> None:
    """Rule 4 (an existing transcript) comes before rule 6 (the admission lock): while the service
    stops, a request that would be refused anyway is told so, and only a forced one is 503."""
    run_id, revision_id = seed_run(runtime, source_id)
    set_key(client)
    runtime.transcription_jobs.shutdown()
    before = row_counts(runtime)

    refused = start(client, source_id)
    forced = start(client, source_id, force=True)

    assert outcome(refused) == (409, "TRANSCRIPT_ALREADY_EXISTS")
    assert error_of(refused)["details"] == {"transcript_id": revision_id, "run_id": run_id}
    assert outcome(forced) == (503, "SERVICE_STOPPING")
    assert row_counts(runtime) == before


# --- GET /v1/transcription-runs?source_id= ----------------------------------------------------


def test_the_runs_of_a_source_are_listed_newest_first_then_by_id(
    keyed: TestClient, runtime: Runtime, source_id: str, tmp_path: Path
) -> None:
    other = add_source(runtime, tmp_path, "other", duration_ms=DURATION_MS)
    morning = "2026-11-16T10:00:00.000000Z"
    noon = "2026-11-16T12:00:00.000000Z"
    evening = "2026-11-16T20:30:15.123456Z"
    # Written out of order: the answer follows the time, not the order of writing.
    noon_run, _ = seed_run(runtime, source_id, status="partial", now=lambda: noon)
    morning_run, _ = seed_run(runtime, source_id, now=lambda: morning)
    evening_run, _ = seed_run(runtime, source_id, status="failed", now=lambda: evening)
    tied_run, _ = seed_run(runtime, source_id, status="interrupted", now=lambda: noon)
    seed_run(runtime, other, now=lambda: evening)

    items = listed(keyed, source_id)

    assert [item["id"] for item in items] == [
        evening_run,
        *sorted([noon_run, tied_run], reverse=True),
        morning_run,
    ]
    assert [item["created_at"] for item in items] == [evening, noon, noon, morning]


def test_a_listed_run_has_the_fields_of_the_run_without_its_chunks_and_with_created_at(
    keyed: TestClient, runtime: Runtime, source_id: str
) -> None:
    run = completed(keyed, source_id)
    failed_id, _ = seed_run(
        runtime, source_id, status="failed", revision="none", error_code="STT_AUTH"
    )
    reserved = runtime.spend.reserve(
        failed_id, "request-1", MAI_MODEL, Decimal("0.0305"), Decimal("5"), {}
    )
    assert reserved.reserved_micro_usd == 30_500

    by_id = {item["id"]: item for item in listed(keyed, source_id)}

    assert set(by_id) == {run["id"], failed_id}
    assert all(
        set(item)
        == {
            "id",
            "source_id",
            "job_id",
            "model",
            "status",
            "revision_id",
            "error_code",
            "committed_micro_usd",
            "created_at",
        }
        for item in by_id.values()
    )
    done = by_id[run["id"]]
    assert (done["source_id"], done["job_id"], done["model"], done["status"]) == (
        source_id,
        run["job_id"],
        MAI_MODEL,
        "completed",
    )
    assert (done["revision_id"], done["error_code"], done["committed_micro_usd"]) == (
        run["revision_id"],
        None,
        60,  # three chunks, each answered with a cost of 20 micro-USD
    )
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", done["created_at"])
    failed = by_id[failed_id]
    assert (failed["job_id"], failed["status"], failed["revision_id"], failed["error_code"]) == (
        None,
        "failed",
        None,
        "STT_AUTH",
    )
    assert failed["committed_micro_usd"] == 30_500


def test_a_source_without_runs_has_an_empty_list(keyed: TestClient, source_id: str) -> None:
    response = keyed.get("/v1/transcription-runs", params={"source_id": source_id})

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_the_list_needs_a_source_id_and_the_source_must_exist(
    keyed: TestClient, source_id: str
) -> None:
    for response in (
        keyed.get("/v1/transcription-runs"),
        keyed.get("/v1/transcription-runs", params={"source_id": ""}),
    ):
        assert response.status_code == 422
        assert error_of(response)["code"] == "VALIDATION_ERROR"
    unknown = keyed.get("/v1/transcription-runs", params={"source_id": "no-such-source"})
    assert outcome(unknown) == (404, "SOURCE_NOT_FOUND")
