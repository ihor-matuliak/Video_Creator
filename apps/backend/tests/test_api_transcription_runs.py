"""POST /v1/transcription-runs, GET /v1/transcription-runs/{id} and the ``transcription`` job:
POST /v1/jobs/{id}/cancel and /resume (ADR 032, ADR 033, ADR 035).

The provider is a fake ``SttClient`` behind a recording key factory and the audio extraction is a
fake: nothing is sent over the network, nothing is paid for and FFmpeg does not run. What the tests
watch is when (and with which key) the fake is reached: a refused request, a restart or a refused
resume must never reach it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select

from clipforge.adapters.openrouter_stt import MAI_MODEL, SttError, SttResult
from clipforge.adapters.stt_audio import SttAudioRequest, SttAudioResult
from clipforge.config import Config, ConfigError
from clipforge.services.credentials import CredentialStore
from clipforge.services.errors import conflict
from clipforge.services.export_engine import ExportCancelledError
from clipforge.services.runtime import Runtime
from clipforge.services.transcription_jobs import (
    TRANSCRIPTION_JOB,
    KeyedSttClient,
    SttClientFactory,
    openrouter_stt_factory,
)
from clipforge.services.transcription_runs import (
    SttClient,
    SttRunOptions,
    TranscriptionRunService,
)
from clipforge.storage import (
    Job,
    JobRepository,
    ProjectRepository,
    SourceRepository,
    SpendRepository,
    TranscriptRun,
    TranscriptRunRepository,
)
from clipforge.storage.database import DB_FILENAME, LOCK_FILENAME

from .support import RuntimeFactory, client_for, make_config
from .test_api_sources import wait_job
from .test_transcription_runs import Call, FakeExtractor, FakeStt, Script, failing_at, speech

GEMINI = "google/gemini-3.5-transcribe"
# Part of every key the tests use: if any response, log line, row, file or repr contains it, the
# secret leaked.
MARKER = "Zq7Lk2Vx9Rm4"
KEY = f"sk-or-v1-{MARKER}0123456789abcdef"
NEW_KEY = f"sk-or-v1-{MARKER}fedcba9876543210"
WAIT_S = 20.0
CHUNKS = 3  # a 620 s source: cores of 300 s, 300 s and 20 s
# A 400 s source has two chunks: cores of 300 s and 100 s (upload windows 0-305 s and 295-400 s).
# With two workers both are in flight at once and no third chunk can take a freed slot, so a test
# that stops the run at a chosen moment has one outcome.
TWO_CHUNKS_MS = 400_000
MICRO_PER_SPEECH = 20  # `speech` reports a cost of 0.00002 USD
RESERVED_FIRST_OF_TWO = 30_500  # 305 s of upload at the upper tariff of 0.0001 USD per second


# --- fakes and helpers ------------------------------------------------------------------------


class RecordingFactory:
    """``api_key -> client``: remembers every key it was given and can be told to fail."""

    def __init__(self, stt: FakeStt) -> None:
        self.stt = stt
        self.keys: list[str] = []
        self.error: Exception | None = None

    def __call__(self, api_key: str) -> SttClient:
        self.keys.append(api_key)
        if self.error is not None:
            raise self.error
        return self.stt


class Gate:
    """A script that holds every request until ``open()``."""

    def __init__(self) -> None:
        self._opened = threading.Event()

    def wait(self) -> None:
        assert self._opened.wait(WAIT_S), "the gate was never opened"

    def __call__(self, call: Call) -> SttResult:
        self.wait()
        return speech(call)

    def open(self) -> None:
        self._opened.set()


def held_by(gate: Gate, script: Script) -> Script:
    """``script``, but a request waits at the gate first."""

    def held(call: Call) -> SttResult:
        gate.wait()
        return script(call)

    return held


def after_the_second_request_started(script: Script) -> Script:
    """The request of the first chunk (upload window starts at 0) runs ``script`` only once the
    request of the second chunk has started; the second one is answered with speech. With two
    workers on a two-chunk source the second request is then certainly on the wire when the first
    one ends, however it ends."""
    second_started = threading.Event()

    def ordered(call: Call) -> SttResult:
        if call.upload_start_ms == 0:
            assert second_started.wait(WAIT_S), "the second request never started"
            return script(call)
        second_started.set()
        return speech(call)

    return ordered


class ObservingExtractor(FakeExtractor):
    """Keeps the ``cancel`` event the run hands to the audio extraction. It is the halt of the run:
    set once the run has noticed that it was cancelled or stopped, so a test can tell that a
    shutdown reached the run while its requests are still held."""

    def __init__(self) -> None:
        super().__init__()
        self.halt: threading.Event | None = None

    def __call__(self, request: SttAudioRequest, *, cancel: threading.Event) -> SttAudioResult:
        self.halt = cancel
        return super().__call__(request, cancel=cancel)

    def halted(self) -> bool:
        return self.halt is not None and self.halt.is_set()


class FaultyClient:
    """An STT client whose ``transcribe`` raises a ``ValueError``: a defect that surfaces once the
    request may be on the wire (a request the adapter refuses is caught before the client is
    called). The text of the error holds the key."""

    def transcribe(
        self,
        *,
        model: str,
        audio_wav: bytes,
        language: str | None = None,
        provider_options: Mapping[str, object] | None = None,
    ) -> SttResult:
        raise ValueError(f"збій після відправлення: {KEY}")


class EmptyAudioExtractor(FakeExtractor):
    """An audio extraction that yields an empty file: the request the adapter refuses before it
    opens a connection."""

    def __call__(self, request: SttAudioRequest, *, cancel: threading.Event) -> SttAudioResult:
        path = request.output_dir / f"{request.task_id}.wav"
        path.write_bytes(b"")
        return SttAudioResult(
            path=path,
            size_bytes=0,
            upload_start_ms=request.upload_start_ms,
            upload_end_ms=request.upload_end_ms,
            seek_mode="full_decode",
            ffmpeg_version="fake",
            ffprobe_version="fake",
        )


class HangingExtractor:
    """An audio extraction that runs until it is cancelled, like an FFmpeg process the run owns."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def __call__(self, request: SttAudioRequest, *, cancel: threading.Event) -> SttAudioResult:
        self.started.set()
        assert cancel.wait(WAIT_S), "the extraction was never cancelled"
        self.cancelled.set()
        raise ExportCancelledError("Підготовку аудіо скасовано")


def wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def wait_state(client: TestClient, job_id: str, state: str) -> dict[str, Any]:
    holder: dict[str, Any] = {}

    def reached() -> bool:
        holder.update(client.get(f"/v1/jobs/{job_id}").json())
        return bool(holder["state"] == state)

    wait_until(reached, f"job {job_id} to become {state}")
    return holder


def add_source(
    runtime: Runtime,
    tmp_path: Path,
    name: str = "media",
    *,
    duration_ms: int = 620_000,
    audio_streams: list[dict[str, Any]] | None = None,
) -> str:
    """A registered source over a real (if meaningless) file: size and mtime match the row."""
    media = tmp_path / f"{name}.bin"
    media.write_bytes(b"not really a video")
    state = media.stat()
    with runtime.database.write_session() as session:
        project = ProjectRepository(session).add("Проєкт")
        source = SourceRepository(session).add(
            project_id=project.id,
            path=str(media),
            display_name=media.name,
            size=state.st_size,
            mtime=state.st_mtime_ns,
            quick_fingerprint=f"quick-{name}",
            hash_status="pending",
            duration_ms=duration_ms,
            video_stream_index=0,
            audio_streams=audio_streams or [{"index": 1, "codec": "aac", "default": True}],
            canonical_origin={"origin_ms": 0, "time_base": "1/1000"},
            probe_json={},
            availability="available",
        )
        return source.id


def error_of(response: Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


def set_key(client: TestClient, key: str = KEY) -> None:
    assert (
        client.post("/v1/runtime/credentials", json={"openrouter_api_key": key}).status_code == 204
    )


def start(client: TestClient, source_id: str, **fields: Any) -> Response:
    body = {"source_id": source_id, "audio_stream_index": 1, "budget_usd": "5", **fields}
    return client.post("/v1/transcription-runs", json=body)


def started(client: TestClient, source_id: str, **fields: Any) -> dict[str, Any]:
    response = start(client, source_id, **fields)
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


def run_of(client: TestClient, run_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/transcription-runs/{run_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def chunk_states(run: dict[str, Any]) -> list[str]:
    return [chunk["state"] for chunk in run["chunks"]]


def resume(client: TestClient, job_id: str, **body: Any) -> Response:
    return client.post(f"/v1/jobs/{job_id}/resume", json=body)


def checkpoint_of(runtime: Runtime, job_id: str) -> dict[str, Any]:
    with runtime.database.session() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        return dict(job.checkpoint_json or {})


def seed_crashed_run(
    runtime: Runtime, source_id: str, *, job_state: str = "running"
) -> tuple[str, str]:
    """What a process that died mid-run leaves: the first chunk was reserved and may have gone out,
    the second was only being prepared (no reservation) and the third never started. With
    ``job_state="queued"`` nothing was started at all. Returns ``(run_id, job_id)``."""
    run_id = runtime.transcription_runs.create_run(
        source_id=source_id, model=MAI_MODEL, options=SttRunOptions(audio_stream_index=1)
    )
    sent = job_state != "queued"
    reserved = (
        runtime.spend.reserve(
            run_id, "request-sent", MAI_MODEL, Decimal("0.0305"), Decimal("5"), {}
        )
        if sent
        else None
    )
    with runtime.database.write_session() as session:
        runs = TranscriptRunRepository(session)
        first, second, _third = runs.list_chunks(run_id)
        if reserved is not None:
            runs.update_run(run_id, status="running")
            runs.update_chunk(
                first.id,
                state="running",
                request_id="request-sent",
                attempts=1,
                reserved_micro_usd=reserved.reserved_micro_usd,
            )
            runs.update_chunk(second.id, state="running", request_id="request-prepared", attempts=1)
        job = JobRepository(session).add(
            type=TRANSCRIPTION_JOB,
            state=job_state,
            progress_json={"stage": "transcribing" if sent else "queued"},
            checkpoint_json={
                "run_id": run_id,
                "audio_stream_index": 1,
                "language": None,
                "budget_usd": "5",
            },
        )
        return run_id, job.id


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def stt() -> FakeStt:
    return FakeStt(speech)


@pytest.fixture
def factory(stt: FakeStt) -> RecordingFactory:
    return RecordingFactory(stt)


@pytest.fixture
def extractor() -> ObservingExtractor:
    return ObservingExtractor()


@pytest.fixture
def runtime(
    make_runtime: RuntimeFactory, factory: RecordingFactory, extractor: FakeExtractor
) -> Runtime:
    return make_runtime(stt_client_factory=factory, extract_audio=extractor)


@pytest.fixture
def source_id(runtime: Runtime, tmp_path: Path) -> str:
    return add_source(runtime, tmp_path)


@pytest.fixture
def keyed(client: TestClient) -> TestClient:
    set_key(client)
    return client


@pytest.fixture
def gate(make_runtime: RuntimeFactory, stt: FakeStt) -> Iterator[Gate]:
    """Requests wait at the gate; it is opened at teardown, so no test leaves a worker blocked
    (``make_runtime`` is a dependency: its runtimes are closed after this teardown)."""
    held = Gate()
    stt.script = held
    yield held
    held.open()


# --- POST /v1/transcription-runs: success -----------------------------------------------------


def test_a_run_is_accepted_and_ends_with_a_revision(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt, factory: RecordingFactory
) -> None:
    created = started(keyed, source_id)

    assert set(created) == {"id", "job_id", "status"}
    assert created["status"] == "queued"
    job = wait_job(keyed, created["job_id"])
    assert job["type"] == "transcription"
    assert job["state"] == "succeeded"
    assert job["error"] is None
    assert job["progress"] == {
        "stage": "done",
        "total": CHUNKS,
        "succeeded": CHUNKS,
        "failed": 0,
        "unknown_outcome": 0,
    }

    run = run_of(keyed, created["id"])
    assert set(run) == {
        "id",
        "source_id",
        "job_id",
        "model",
        "status",
        "revision_id",
        "error_code",
        "committed_micro_usd",
        "chunks",
    }
    assert (run["id"], run["source_id"], run["job_id"]) == (
        created["id"],
        source_id,
        created["job_id"],
    )
    assert (run["model"], run["status"], run["error_code"]) == (MAI_MODEL, "completed", None)
    assert run["committed_micro_usd"] == CHUNKS * MICRO_PER_SPEECH
    assert all(
        set(chunk) == {"id", "core_start_ms", "core_end_ms", "state", "attempts", "error_code"}
        for chunk in run["chunks"]
    )
    assert [(c["core_start_ms"], c["core_end_ms"]) for c in run["chunks"]] == [
        (0, 300_000),
        (300_000, 600_000),
        (600_000, 620_000),
    ]
    assert chunk_states(run) == ["succeeded"] * CHUNKS
    assert [chunk["attempts"] for chunk in run["chunks"]] == [1] * CHUNKS

    transcript = keyed.get(f"/v1/transcripts/{run['revision_id']}").json()
    assert (transcript["origin"], transcript["run_id"], transcript["source_id"]) == (
        "stt",
        created["id"],
        source_id,
    )
    # The provider was reached once per chunk, with the default model and no language; the key
    # went to the factory per request and nowhere else.
    assert len(stt.calls) == CHUNKS
    assert {(call.model, call.language) for call in stt.calls} == {(MAI_MODEL, None)}
    assert factory.keys == [KEY] * CHUNKS


def test_the_job_checkpoint_holds_the_run_and_its_options_but_never_the_key(
    keyed: TestClient, runtime: Runtime, source_id: str
) -> None:
    created = started(keyed, source_id, language="es", budget_usd="1.5")
    wait_job(keyed, created["job_id"])

    assert checkpoint_of(runtime, created["job_id"]) == {
        "run_id": created["id"],
        "audio_stream_index": 1,
        "language": "es",
        "budget_usd": "1.5",
    }


def test_model_language_and_budget_default_to_the_settings(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt
) -> None:
    patched = keyed.patch(
        "/v1/settings", json={"stt_model": GEMINI, "stt_language": "en", "run_budget_usd": "3"}
    )
    assert patched.status_code == 200

    created = started_without_budget(keyed, source_id)
    wait_job(keyed, created["job_id"])

    assert run_of(keyed, created["id"])["model"] == GEMINI
    assert {(call.model, call.language) for call in stt.calls} == {(GEMINI, "en")}
    assert checkpoint_of(runtime, created["job_id"])["budget_usd"] == "3"
    assert checkpoint_of(runtime, created["job_id"])["language"] == "en"


def started_without_budget(client: TestClient, source_id: str) -> dict[str, Any]:
    response = client.post(
        "/v1/transcription-runs", json={"source_id": source_id, "audio_stream_index": 1}
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


def test_the_request_overrides_the_settings(
    keyed: TestClient, source_id: str, stt: FakeStt
) -> None:
    keyed.patch(
        "/v1/settings", json={"stt_model": GEMINI, "stt_language": "en", "run_budget_usd": "3"}
    )

    created = started(keyed, source_id, model=MAI_MODEL, language="es", budget_usd="2")
    wait_job(keyed, created["job_id"])

    assert run_of(keyed, created["id"])["model"] == MAI_MODEL
    assert {(call.model, call.language) for call in stt.calls} == {(MAI_MODEL, "es")}


def test_the_budget_is_a_ceiling_for_the_whole_run(
    keyed: TestClient, source_id: str, stt: FakeStt
) -> None:
    # The first request would reserve 0.0305 USD (305 s at the upper tariff): over a 0.01 ceiling.
    created = started(keyed, source_id, budget_usd="0.01")

    job = wait_job(keyed, created["job_id"])

    assert stt.calls == []
    assert job["state"] == "failed"
    assert job["error"] == {
        "code": "BUDGET_EXCEEDED",
        "message": "Ліміт витрат запуску вичерпано: розпізнавання зупинено",
    }
    run = run_of(keyed, created["id"])
    assert (run["status"], run["error_code"], run["revision_id"]) == (
        "failed",
        "BUDGET_EXCEEDED",
        None,
    )
    assert run["committed_micro_usd"] == 0
    assert chunk_states(run) == ["failed"] * CHUNKS


# --- POST /v1/transcription-runs: refusals ----------------------------------------------------


def nothing_was_created(runtime: Runtime) -> bool:
    with runtime.database.session() as session:
        return (
            session.scalars(select(Job)).first() is None
            and session.scalars(select(TranscriptRun)).first() is None
        )


def queued_jobs(runtime: Runtime) -> list[Job]:
    with runtime.database.session() as session:
        return JobRepository(session).list_by_type(TRANSCRIPTION_JOB, ("queued",))


def test_without_a_budget_anywhere_the_request_is_422(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt
) -> None:
    response = keyed.post(
        "/v1/transcription-runs", json={"source_id": source_id, "audio_stream_index": 1}
    )

    assert response.status_code == 422
    assert error_of(response)["code"] == "BUDGET_REQUIRED"
    assert error_of(response)["details"] == {"field": "budget_usd"}
    assert stt.calls == []
    assert nothing_was_created(runtime)


@pytest.mark.parametrize("budget", ["", "0", "0.009999", "1000.01", "1e2", "-1", "1.2345678", " 1"])
def test_a_budget_outside_the_settings_rule_is_422(
    keyed: TestClient, runtime: Runtime, source_id: str, budget: str
) -> None:
    response = start(keyed, source_id, budget_usd=budget)

    assert response.status_code == 422
    assert error_of(response)["code"] == "INVALID_RUN_BUDGET"
    assert error_of(response)["details"] == {"field": "budget_usd"}
    assert nothing_was_created(runtime)


@pytest.mark.parametrize("budget", [5, 5.5, True, ["5"]])
def test_a_budget_that_is_not_a_string_is_422(
    keyed: TestClient, runtime: Runtime, source_id: str, budget: Any
) -> None:
    response = start(keyed, source_id, budget_usd=budget)

    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"
    assert nothing_was_created(runtime)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("model", "openai/whisper-1", "INVALID_STT_MODEL"),
        ("model", "", "INVALID_STT_MODEL"),
        ("language", "eng", "INVALID_STT_LANGUAGE"),
        ("language", "EN", "INVALID_STT_LANGUAGE"),
        ("language", "", "INVALID_STT_LANGUAGE"),
    ],
)
def test_a_bad_model_or_language_is_422_naming_the_request_field(
    keyed: TestClient, runtime: Runtime, source_id: str, field: str, value: str, code: str
) -> None:
    response = start(keyed, source_id, **{field: value})

    assert response.status_code == 422
    assert error_of(response)["code"] == code
    assert error_of(response)["details"] == {"field": field}
    assert nothing_was_created(runtime)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"source_id": "s"},
        {"audio_stream_index": 1},
        {"source_id": "s", "audio_stream_index": "1"},
        {"source_id": "s", "audio_stream_index": True},
        {"source_id": "s", "audio_stream_index": 1, "openrouter_api_key": KEY},
        {"source_id": "s", "audio_stream_index": 1, "retry_unknown": True},
    ],
)
def test_a_body_of_the_wrong_shape_is_422_without_echoing_it(
    keyed: TestClient, body: dict[str, Any]
) -> None:
    response = keyed.post("/v1/transcription-runs", json=body)

    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"
    assert MARKER not in response.text


def test_without_a_key_the_request_is_409_and_nothing_is_created(
    client: TestClient, runtime: Runtime, source_id: str, stt: FakeStt
) -> None:
    response = start(client, source_id)

    assert response.status_code == 409
    assert error_of(response)["code"] == "API_KEY_REQUIRED"
    assert stt.calls == []
    assert nothing_was_created(runtime)

    set_key(client)
    client.delete("/v1/runtime/credentials")
    assert error_of(start(client, source_id))["code"] == "API_KEY_REQUIRED"


def test_an_unknown_source_is_404_and_a_missing_audio_stream_is_422(
    keyed: TestClient, runtime: Runtime, source_id: str
) -> None:
    missing = start(keyed, "no-such-source")
    wrong_stream = start(keyed, source_id, audio_stream_index=7)

    assert (missing.status_code, error_of(missing)["code"]) == (404, "SOURCE_NOT_FOUND")
    assert (wrong_stream.status_code, error_of(wrong_stream)["code"]) == (
        422,
        "INVALID_AUDIO_STREAM",
    )
    assert error_of(wrong_stream)["details"] == {"field": "audio_stream_index"}
    assert nothing_was_created(runtime)


def test_the_source_is_checked_before_the_key(
    client: TestClient, runtime: Runtime, source_id: str, stt: FakeStt
) -> None:
    # No key has been given: a request that names no source, or a stream the source lacks, is
    # still refused for that reason (404 / 422), and only a valid one is told about the key (409).
    missing = start(client, "no-such-source")
    wrong_stream = start(client, source_id, audio_stream_index=7)
    valid = start(client, source_id)

    assert (missing.status_code, error_of(missing)["code"]) == (404, "SOURCE_NOT_FOUND")
    assert (wrong_stream.status_code, error_of(wrong_stream)["code"]) == (
        422,
        "INVALID_AUDIO_STREAM",
    )
    assert (valid.status_code, error_of(valid)["code"]) == (409, "API_KEY_REQUIRED")
    assert stt.calls == []
    assert nothing_was_created(runtime)


def test_the_run_and_its_job_are_written_in_one_transaction(
    keyed: TestClient,
    runtime: Runtime,
    source_id: str,
    stt: FakeStt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def disk_full(self: JobRepository, **fields: Any) -> Job:
        raise RuntimeError("disk full")

    monkeypatch.setattr(JobRepository, "add", disk_full)

    response = start(keyed, source_id)

    assert response.status_code == 500
    assert stt.calls == []
    assert nothing_was_created(runtime)  # no run without its job


def test_without_ffmpeg_the_request_is_503(
    make_runtime: RuntimeFactory, factory: RecordingFactory, tmp_path: Path
) -> None:
    bare = make_runtime(with_tools=False, stt_client_factory=factory)
    client = client_for(bare)
    set_key(client)

    response = start(client, add_source(bare, tmp_path))

    assert response.status_code == 503
    assert error_of(response)["code"] == "MEDIA_TOOLS_UNAVAILABLE"
    assert nothing_was_created(bare)


def outcome(response: Response) -> tuple[int, str]:
    return response.status_code, error_of(response)["code"]


def test_a_bad_value_is_422_before_the_503_for_missing_ffmpeg_and_that_503_comes_before_the_source(
    make_runtime: RuntimeFactory, factory: RecordingFactory
) -> None:
    bare = make_runtime(with_tools=False, stt_client_factory=factory)
    client = client_for(bare)  # no key, and the source below does not exist: every rule fails

    assert outcome(start(client, "no-such-source", budget_usd="0")) == (422, "INVALID_RUN_BUDGET")
    assert outcome(start(client, "no-such-source", model="openai/whisper-1")) == (
        422,
        "INVALID_STT_MODEL",
    )
    assert outcome(start(client, "no-such-source", language="eng")) == (
        422,
        "INVALID_STT_LANGUAGE",
    )
    no_budget = client.post(
        "/v1/transcription-runs", json={"source_id": "no-such-source", "audio_stream_index": 1}
    )
    assert outcome(no_budget) == (422, "BUDGET_REQUIRED")
    # Every value is fine: now the missing FFmpeg is what is reported (not the source, not the key).
    assert outcome(start(client, "no-such-source")) == (503, "MEDIA_TOOLS_UNAVAILABLE")
    assert nothing_was_created(bare)


def test_the_checks_of_a_create_are_made_in_the_order_value_source_key_open_job(
    client: TestClient, runtime: Runtime, source_id: str, stt: FakeStt
) -> None:
    # The source has an open job (queued, no worker) and the request names a stream it lacks, has a
    # bad budget, and no key was given: every rule fails at once, and each fix shows the next one.
    seed_crashed_run(runtime, source_id, job_state="queued")

    assert outcome(start(client, source_id, audio_stream_index=7, budget_usd="0")) == (
        422,
        "INVALID_RUN_BUDGET",
    )
    assert outcome(start(client, source_id, audio_stream_index=7)) == (422, "INVALID_AUDIO_STREAM")
    assert outcome(start(client, source_id)) == (409, "API_KEY_REQUIRED")
    set_key(client)
    assert outcome(start(client, source_id)) == (409, "TRANSCRIPTION_RUN_ACTIVE")
    assert len(queued_jobs(runtime)) == 1  # the seeded one: the refusals created nothing
    assert stt.calls == []


def test_a_second_run_for_a_source_with_an_open_job_is_409(
    keyed: TestClient, runtime: Runtime, source_id: str, tmp_path: Path, stt: FakeStt, gate: Gate
) -> None:
    first = started(keyed, source_id)
    wait_until(lambda: stt.active > 0, "the first run to reach the provider")

    again = start(keyed, source_id)
    other = started(keyed, add_source(runtime, tmp_path, "other"))  # another source is allowed

    assert again.status_code == 409
    assert error_of(again)["code"] == "TRANSCRIPTION_RUN_ACTIVE"
    assert error_of(again)["details"] == {"run_id": first["id"], "job_id": first["job_id"]}
    assert other["status"] == "queued"
    assert keyed.get(f"/v1/jobs/{other['job_id']}").json()["state"] == "queued"  # one worker

    gate.open()
    assert wait_job(keyed, first["job_id"])["state"] == "succeeded"
    assert wait_job(keyed, other["job_id"])["state"] == "succeeded"
    # Once nothing is open, the source can be transcribed again. Its transcript already exists
    # (ADR 037), so a second paid run needs the explicit `force`; without it the answer is not
    # TRANSCRIPTION_RUN_ACTIVE any more but TRANSCRIPT_ALREADY_EXISTS.
    assert error_of(start(keyed, source_id))["code"] == "TRANSCRIPT_ALREADY_EXISTS"
    assert start(keyed, source_id, force=True).status_code == 202


def test_an_unknown_run_is_404(client: TestClient) -> None:
    response = client.get("/v1/transcription-runs/nope")

    assert response.status_code == 404
    assert error_of(response)["code"] == "TRANSCRIPTION_RUN_NOT_FOUND"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/transcription-runs", {"source_id": "s", "audio_stream_index": 1}),
        ("GET", "/v1/transcription-runs/x", None),
        ("POST", "/v1/jobs/x/resume", {"retry_unknown": False}),
    ],
)
def test_without_the_session_token_nothing_is_done(
    anonymous: TestClient, method: str, path: str, body: Any
) -> None:
    assert anonymous.request(method, path, json=body).status_code == 401


# --- failed jobs ------------------------------------------------------------------------------


def test_a_rejected_key_fails_the_job_with_a_fixed_message_and_a_resume_continues_it(
    keyed: TestClient, runtime: Runtime, tmp_path: Path, stt: FakeStt
) -> None:
    # The first chunk is rejected while the second request is on the wire: the second answer is
    # kept, so the outcome is the same on every run.
    stt.script = after_the_second_request_started(failing_at(0, SttError("auth")))
    two_chunks = add_source(runtime, tmp_path, "two-chunks", duration_ms=TWO_CHUNKS_MS)
    created = started(keyed, two_chunks)

    job = wait_job(keyed, created["job_id"])

    assert job["state"] == "failed"
    assert job["error"] == {
        "code": "STT_AUTH",
        "message": (
            "OpenRouter не прийняв ключ API. Перевірте ключ у Налаштуваннях і відновіть "
            "розпізнавання"
        ),
    }
    run = run_of(keyed, created["id"])
    assert (run["status"], run["error_code"]) == ("partial", "STT_AUTH")
    assert chunk_states(run) == ["failed", "succeeded"]
    assert run["chunks"][0]["error_code"] == "STT_AUTH"
    assert len(stt.calls) == 2

    # The user fixes the key and resumes: what succeeded is kept, only the rejected chunk is sent.
    set_key(keyed, NEW_KEY)
    assert resume(keyed, created["job_id"]).status_code == 202
    assert wait_job(keyed, created["job_id"])["state"] == "succeeded"

    done = run_of(keyed, created["id"])
    assert (done["status"], done["error_code"]) == ("completed", None)
    assert chunk_states(done) == ["succeeded", "succeeded"]
    assert len(stt.calls) == 3
    assert checkpoint_of(runtime, created["job_id"])["budget_usd"] == "5"  # a resume keeps it


def test_a_key_deleted_before_the_send_is_never_used_and_nothing_goes_out(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt, extractor: FakeExtractor
) -> None:
    extractor.fail = lambda _request: runtime.credentials.clear_openrouter_key()
    created = started(keyed, source_id)

    job = wait_job(keyed, created["job_id"])

    assert stt.calls == []
    assert job["state"] == "failed"
    assert job["error"]["code"] == "STT_AUTH"
    run = run_of(keyed, created["id"])
    assert run["committed_micro_usd"] == 0  # every reservation was given back
    assert run["revision_id"] is None


def test_a_provider_that_cannot_be_built_fails_the_chunks_without_a_send(
    keyed: TestClient, source_id: str, stt: FakeStt, factory: RecordingFactory
) -> None:
    factory.error = ConfigError(f"текст помилки з ключем {KEY}")
    created = started(keyed, source_id)

    job = wait_job(keyed, created["job_id"])

    assert stt.calls == []
    assert job["state"] == "failed"
    assert job["error"]["code"] == "STT_INVALID_REQUEST"
    assert MARKER not in str(job)
    run = run_of(keyed, created["id"])
    assert run["committed_micro_usd"] == 0
    assert chunk_states(run) == ["failed"] * CHUNKS


def test_a_request_the_adapter_would_refuse_fails_the_chunks_before_the_client_is_called(
    make_runtime: RuntimeFactory, factory: RecordingFactory, tmp_path: Path
) -> None:
    refusing = make_runtime(stt_client_factory=factory, extract_audio=EmptyAudioExtractor())
    client = client_for(refusing)
    set_key(client)
    created = started(client, add_source(refusing, tmp_path))

    job = wait_job(client, created["job_id"])

    assert factory.stt.calls == []  # empty audio never reached the client, so nothing was sent
    assert job["state"] == "failed"
    assert job["error"]["code"] == "STT_INVALID_REQUEST"
    assert MARKER not in str(job)
    run = run_of(client, created["id"])
    assert chunk_states(run) == ["failed"] * CHUNKS
    assert {chunk["error_code"] for chunk in run["chunks"]} == {"STT_INVALID_REQUEST"}
    assert run["committed_micro_usd"] == 0  # every reservation was released
    assert MARKER not in str(run)


def test_a_value_error_after_the_client_was_called_is_a_defect_not_a_free_refusal(
    keyed: TestClient, runtime: Runtime, tmp_path: Path, stt: FakeStt
) -> None:
    def broken(call: Call) -> SttResult:
        raise ValueError(f"збій після відправлення: {KEY}")

    # The first request has been reserved (and may have been answered and paid for) when the client
    # breaks; the second one is answered meanwhile.
    stt.script = after_the_second_request_started(broken)
    two_chunks = add_source(runtime, tmp_path, "two-chunks", duration_ms=TWO_CHUNKS_MS)
    created = started(keyed, two_chunks)

    job = wait_job(keyed, created["job_id"])

    assert job["state"] == "failed"
    assert job["error"] == {
        "code": "TRANSCRIPTION_FAILED",
        "message": "Розпізнавання завершилося помилкою",
    }
    run = run_of(keyed, created["id"])
    assert (run["status"], run["error_code"]) == ("failed", "TRANSCRIPTION_FAILED")
    # Not `STT_INVALID_REQUEST` with a released reservation: the money of that request still counts.
    assert chunk_states(run) == ["unknown_outcome", "succeeded"]
    assert run["chunks"][0]["error_code"] == "STT_UNKNOWN_OUTCOME"
    assert run["committed_micro_usd"] == RESERVED_FIRST_OF_TWO + MICRO_PER_SPEECH
    assert MARKER not in str(job) + str(run)


def test_a_service_error_from_the_worker_settles_the_chunk_it_left_running(
    keyed: TestClient, runtime: Runtime, tmp_path: Path, stt: FakeStt
) -> None:
    def broken(call: Call) -> SttResult:
        raise conflict("TEST_FAULT", "Збій під час запиту")

    # The first request has been reserved (and may have gone out) when it breaks; the second one is
    # answered meanwhile.
    stt.script = after_the_second_request_started(broken)
    two_chunks = add_source(runtime, tmp_path, "two-chunks", duration_ms=TWO_CHUNKS_MS)
    created = started(keyed, two_chunks)

    job = wait_job(keyed, created["job_id"])

    assert job["state"] == "failed"
    assert job["error"] == {"code": "TEST_FAULT", "message": "Збій під час запиту"}
    run = run_of(keyed, created["id"])
    assert (run["status"], run["error_code"]) == ("failed", "TEST_FAULT")
    # Nothing is left `running`: the chunk whose request may have gone out is undecided and its
    # reservation still counts, exactly as after a crash.
    assert chunk_states(run) == ["unknown_outcome", "succeeded"]
    assert run["chunks"][0]["error_code"] == "STT_UNKNOWN_OUTCOME"
    assert run["committed_micro_usd"] == RESERVED_FIRST_OF_TWO + MICRO_PER_SPEECH
    with runtime.database.session() as session:
        entries = SpendRepository(session).list_for_run(created["id"])
    assert sorted(entry.state for entry in entries) == ["completed", "unknown_outcome"]


def test_a_runtime_without_a_provider_refuses_every_request(
    make_runtime: RuntimeFactory, extractor: FakeExtractor, tmp_path: Path
) -> None:
    bare = make_runtime(extract_audio=extractor)  # no stt_client_factory
    client = client_for(bare)
    set_key(client)

    created = started(client, add_source(bare, tmp_path))

    job = wait_job(client, created["job_id"])
    assert (job["state"], job["error"]["code"]) == ("failed", "STT_INVALID_REQUEST")
    assert run_of(client, created["id"])["committed_micro_usd"] == 0


def test_a_source_changed_after_registration_fails_the_job_before_anything_is_sent(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt
) -> None:
    with runtime.database.session() as session:
        source = SourceRepository(session).get(source_id)
        assert source is not None
        path = Path(source.path)
    path.write_bytes(b"another file entirely")

    created = started(keyed, source_id)
    job = wait_job(keyed, created["job_id"])

    assert stt.calls == []
    assert job["state"] == "failed"
    assert job["error"] == {
        "code": "SOURCE_CHANGED",
        "message": "Файл джерела змінився після реєстрації",
    }
    assert run_of(keyed, created["id"])["status"] == "failed"


def test_an_unknown_outcome_waits_for_the_users_decision(
    keyed: TestClient, source_id: str, stt: FakeStt
) -> None:
    stt.script = failing_at(0, SttError("unknown_outcome"))
    created = started(keyed, source_id)

    job = wait_job(keyed, created["job_id"])

    assert job["state"] == "failed"
    assert job["error"]["code"] == "STT_UNKNOWN_OUTCOME"
    run = run_of(keyed, created["id"])
    assert run["status"] == "partial"
    assert chunk_states(run) == ["unknown_outcome", "succeeded", "succeeded"]
    assert run["chunks"][0]["error_code"] == "STT_UNKNOWN_OUTCOME"
    first_revision = run["revision_id"]
    sent = len(stt.calls)

    for body in ({}, {"retry_unknown": False}):
        refused = resume(keyed, created["job_id"], **body)
        assert refused.status_code == 409
        assert error_of(refused)["code"] == "UNKNOWN_OUTCOME_REQUIRES_DECISION"
        assert error_of(refused)["details"] == {"count": 1}
    assert len(stt.calls) == sent  # the decision is the user's: nothing was sent
    assert keyed.get(f"/v1/jobs/{created['job_id']}").json()["state"] == "failed"

    accepted = resume(keyed, created["job_id"], retry_unknown=True)
    assert accepted.status_code == 202
    assert accepted.json()["state"] == "queued"
    assert wait_job(keyed, created["job_id"])["state"] == "succeeded"
    done = run_of(keyed, created["id"])
    assert (done["status"], done["error_code"]) == ("completed", None)
    assert chunk_states(done) == ["succeeded"] * CHUNKS
    assert len(stt.calls) == sent + 1  # only the undecided chunk was sent again
    assert done["revision_id"] not in (None, first_revision)
    # The reservation of the first attempt keeps counting: the money may have been spent.
    assert done["committed_micro_usd"] > CHUNKS * MICRO_PER_SPEECH


# --- cancel -----------------------------------------------------------------------------------


def test_cancelling_a_running_job_stops_sending_and_keeps_what_was_transcribed(
    keyed: TestClient, source_id: str, stt: FakeStt, gate: Gate
) -> None:
    created = started(keyed, source_id)
    wait_until(lambda: stt.active == 2, "two requests in flight")

    cancelled = keyed.post(f"/v1/jobs/{created['job_id']}/cancel", json={})
    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "cancelling"
    gate.open()  # the two requests on the wire cannot be recalled: their answers are kept

    job = wait_state(keyed, created["job_id"], "cancelled")
    assert job["error"] is None
    assert len(stt.calls) == 2  # the third chunk was never sent
    run = run_of(keyed, created["id"])
    assert run["status"] == "partial"
    assert chunk_states(run) == ["succeeded", "succeeded", "cancelled"]
    assert run["revision_id"] is not None
    # Cancelling again changes nothing.
    again = keyed.post(f"/v1/jobs/{created['job_id']}/cancel", json={})
    assert (again.status_code, again.json()["state"]) == (202, "cancelled")


def test_cancelling_ends_the_owned_audio_extraction_and_sends_nothing(
    make_runtime: RuntimeFactory, tmp_path: Path, factory: RecordingFactory
) -> None:
    hanging = HangingExtractor()
    runtime = make_runtime(stt_client_factory=factory, extract_audio=hanging)
    client = client_for(runtime)
    set_key(client)
    created = started(client, add_source(runtime, tmp_path))
    wait_until(hanging.started.is_set, "the extraction to start")

    assert client.post(f"/v1/jobs/{created['job_id']}/cancel", json={}).status_code == 202

    job = wait_state(client, created["job_id"], "cancelled")
    assert hanging.cancelled.is_set()
    assert factory.stt.calls == []
    run = run_of(client, created["id"])
    assert (run["status"], run["revision_id"], run["committed_micro_usd"]) == ("cancelled", None, 0)
    assert job["progress"]["stage"] == "cancelled"


def test_a_queued_job_is_cancelled_at_once_with_its_run(
    keyed: TestClient, runtime: Runtime, source_id: str, tmp_path: Path, stt: FakeStt, gate: Gate
) -> None:
    first = started(keyed, source_id)
    wait_until(lambda: stt.active > 0, "the first run to reach the provider")
    queued = started(keyed, add_source(runtime, tmp_path, "other"))

    cancelled = keyed.post(f"/v1/jobs/{queued['job_id']}/cancel", json={})

    assert (cancelled.status_code, cancelled.json()["state"]) == (202, "cancelled")
    run = run_of(keyed, queued["id"])
    assert run["status"] == "cancelled"
    assert chunk_states(run) == ["cancelled"] * CHUNKS
    gate.open()
    assert wait_job(keyed, first["job_id"])["state"] == "succeeded"
    assert keyed.get(f"/v1/jobs/{queued['job_id']}").json()["state"] == "cancelled"
    assert len(stt.calls) == CHUNKS  # only the first run was ever sent


def test_a_finished_job_cannot_be_cancelled(keyed: TestClient, source_id: str) -> None:
    created = started(keyed, source_id)
    wait_job(keyed, created["job_id"])

    response = keyed.post(f"/v1/jobs/{created['job_id']}/cancel", json={})

    assert response.status_code == 409
    assert error_of(response)["code"] == "JOB_NOT_CANCELLABLE"


def test_a_cancelled_job_can_be_resumed(
    keyed: TestClient, source_id: str, stt: FakeStt, gate: Gate
) -> None:
    created = started(keyed, source_id)
    wait_until(lambda: stt.active == 2, "two requests in flight")
    keyed.post(f"/v1/jobs/{created['job_id']}/cancel", json={})
    gate.open()
    wait_state(keyed, created["job_id"], "cancelled")

    assert resume(keyed, created["job_id"], retry_unknown=False).status_code == 202

    assert wait_job(keyed, created["job_id"])["state"] == "succeeded"
    assert len(stt.calls) == CHUNKS  # the two answers that were kept were not paid for again
    assert chunk_states(run_of(keyed, created["id"])) == ["succeeded"] * CHUNKS


# --- restart: nothing is sent until an explicit resume ----------------------------------------


def restart(
    runtime: Runtime,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: FakeExtractor,
) -> Runtime:
    """The process died: the next one opens the same workspace (and has no key)."""
    runtime.close()
    return make_runtime(
        data_dir=runtime.database.data_dir, stt_client_factory=factory, extract_audio=extractor
    )


def test_after_a_restart_the_job_is_interrupted_and_nothing_is_sent_until_a_resume(
    runtime: Runtime,
    source_id: str,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: FakeExtractor,
    stt: FakeStt,
) -> None:
    run_id, job_id = seed_crashed_run(runtime, source_id)

    second = restart(runtime, make_runtime, factory, extractor)
    client = client_for(second)
    time.sleep(0.3)  # long enough for anything that wanted to start by itself

    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "interrupted"
    run = run_of(client, run_id)
    assert run["status"] == "interrupted"
    # The request of the first chunk may have gone out: its outcome is unknown and its reservation
    # keeps counting. The second chunk died before anything was reserved: it is simply pending.
    assert chunk_states(run) == ["unknown_outcome", "pending", "pending"]
    assert run["chunks"][0]["error_code"] == "STT_UNKNOWN_OUTCOME"
    assert run["committed_micro_usd"] == 30_500
    assert stt.calls == []
    assert factory.keys == []  # not even the key was asked for


def test_a_resume_after_a_restart_asks_for_the_decision_and_then_sends_only_what_it_may(
    runtime: Runtime,
    source_id: str,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: FakeExtractor,
    stt: FakeStt,
) -> None:
    run_id, job_id = seed_crashed_run(runtime, source_id)
    client = client_for(restart(runtime, make_runtime, factory, extractor))
    set_key(client)  # the shell hands the key over again after a start

    for body in ({}, {"retry_unknown": False}):
        refused = resume(client, job_id, **body)
        assert refused.status_code == 409
        assert error_of(refused)["code"] == "UNKNOWN_OUTCOME_REQUIRES_DECISION"
        assert error_of(refused)["details"] == {"count": 1}
    refused_without_body = client.post(f"/v1/jobs/{job_id}/resume")
    assert error_of(refused_without_body)["code"] == "UNKNOWN_OUTCOME_REQUIRES_DECISION"
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "interrupted"
    assert stt.calls == []
    assert factory.keys == []

    accepted = resume(client, job_id, retry_unknown=True)

    assert accepted.status_code == 202
    assert accepted.json() == {
        "id": job_id,
        "type": "transcription",
        "state": "queued",
        "progress": {"stage": "queued"},
        "error": None,
    }
    assert wait_job(client, job_id)["state"] == "succeeded"
    done = run_of(client, run_id)
    assert (done["status"], chunk_states(done)) == ("completed", ["succeeded"] * CHUNKS)
    assert len(stt.calls) == CHUNKS  # the undecided chunk too, because the user said so
    # The old reservation was never forgiven: it counts next to the three new answers.
    assert done["committed_micro_usd"] == 30_500 + CHUNKS * MICRO_PER_SPEECH


def test_a_resume_without_an_undecided_chunk_needs_no_decision(
    runtime: Runtime,
    tmp_path: Path,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: FakeExtractor,
    stt: FakeStt,
) -> None:
    run_id, job_id = seed_crashed_run(
        runtime, add_source(runtime, tmp_path, "never-started"), job_state="queued"
    )
    client = client_for(restart(runtime, make_runtime, factory, extractor))
    set_key(client)

    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "interrupted"
    assert run_of(client, run_id)["status"] == "interrupted"
    assert chunk_states(run_of(client, run_id)) == ["pending"] * CHUNKS
    assert stt.calls == []

    assert resume(client, job_id, retry_unknown=False).status_code == 202

    assert wait_job(client, job_id)["state"] == "succeeded"
    assert len(stt.calls) == CHUNKS


def test_a_resume_after_a_restart_needs_the_key_and_changes_nothing_without_it(
    runtime: Runtime,
    source_id: str,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: FakeExtractor,
    stt: FakeStt,
) -> None:
    run_id, job_id = seed_crashed_run(runtime, source_id)
    client = client_for(restart(runtime, make_runtime, factory, extractor))

    response = resume(client, job_id, retry_unknown=True)

    assert response.status_code == 409
    assert error_of(response)["code"] == "API_KEY_REQUIRED"
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "interrupted"
    assert chunk_states(run_of(client, run_id)) == ["unknown_outcome", "pending", "pending"]
    assert stt.calls == []


def test_a_second_restart_keeps_the_settled_states(
    runtime: Runtime,
    source_id: str,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: FakeExtractor,
) -> None:
    run_id, job_id = seed_crashed_run(runtime, source_id)
    second = restart(runtime, make_runtime, factory, extractor)
    third = restart(second, make_runtime, factory, extractor)
    client = client_for(third)

    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "interrupted"
    run = run_of(client, run_id)
    assert (run["status"], chunk_states(run), run["committed_micro_usd"]) == (
        "interrupted",
        ["unknown_outcome", "pending", "pending"],
        30_500,
    )


# --- shutdown: Runtime.close() with a worker that is busy -------------------------------------


def close_in_background(runtime: Runtime) -> threading.Thread:
    """``Runtime.close()`` waits for the answer of a request that is on the wire, so a test that
    holds that request closes the runtime from another thread and lets the answer come meanwhile."""
    closer = threading.Thread(target=runtime.close, name="close-runtime")
    closer.start()
    return closer


def reopen(runtime: Runtime, make_runtime: RuntimeFactory, **fakes: Any) -> Runtime:
    """The next start of the closed ``runtime``'s workspace (it has no key)."""
    return make_runtime(data_dir=runtime.database.data_dir, **fakes)


def test_closing_the_runtime_while_requests_are_in_flight_interrupts_the_run_and_keeps_the_answers(
    keyed: TestClient,
    runtime: Runtime,
    source_id: str,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: ObservingExtractor,
    stt: FakeStt,
    gate: Gate,
) -> None:
    created = started(keyed, source_id)
    wait_until(lambda: stt.active == 2, "two requests in flight")

    closer = close_in_background(runtime)
    wait_until(extractor.halted, "the run to notice the shutdown")
    assert closer.is_alive()  # the answers on the wire are waited for, not abandoned
    gate.open()
    closer.join(WAIT_S)
    assert not closer.is_alive()

    client = client_for(
        reopen(runtime, make_runtime, stt_client_factory=factory, extract_audio=extractor)
    )
    assert client.get(f"/v1/jobs/{created['job_id']}").json()["state"] == "interrupted"
    run = run_of(client, created["id"])
    # Not `partial` and not `cancelled`: nobody decided to stop, the process was. The two answers
    # that came in are kept (with the revision made from them); the third chunk never went out.
    assert run["status"] == "interrupted"
    assert chunk_states(run) == ["succeeded", "succeeded", "pending"]
    assert run["revision_id"] is not None
    assert run["committed_micro_usd"] == 2 * MICRO_PER_SPEECH
    assert len(stt.calls) == 2

    # An explicit resume sends only what is left.
    set_key(client)
    assert resume(client, created["job_id"]).status_code == 202
    assert wait_job(client, created["job_id"])["state"] == "succeeded"
    assert chunk_states(run_of(client, created["id"])) == ["succeeded"] * CHUNKS
    assert len(stt.calls) == CHUNKS


def test_closing_the_runtime_during_a_request_that_times_out_leaves_it_undecided(
    keyed: TestClient,
    runtime: Runtime,
    source_id: str,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: ObservingExtractor,
    stt: FakeStt,
    gate: Gate,
) -> None:
    stt.script = held_by(gate, failing_at(0, SttError("unknown_outcome")))
    created = started(keyed, source_id)
    wait_until(lambda: stt.active == 2, "two requests in flight")

    closer = close_in_background(runtime)
    wait_until(extractor.halted, "the run to notice the shutdown")
    gate.open()
    closer.join(WAIT_S)
    assert not closer.is_alive()

    client = client_for(
        reopen(runtime, make_runtime, stt_client_factory=factory, extract_audio=extractor)
    )
    assert client.get(f"/v1/jobs/{created['job_id']}").json()["state"] == "interrupted"
    run = run_of(client, created["id"])
    assert run["status"] == "interrupted"
    assert chunk_states(run) == ["unknown_outcome", "succeeded", "pending"]
    assert run["committed_micro_usd"] == RESERVED_FIRST_OF_TWO + MICRO_PER_SPEECH

    # The undecided request is sent again only on the user's word.
    set_key(client)
    refused = resume(client, created["job_id"])
    assert (refused.status_code, error_of(refused)["code"]) == (
        409,
        "UNKNOWN_OUTCOME_REQUIRES_DECISION",
    )
    assert len(stt.calls) == 2
    assert resume(client, created["job_id"], retry_unknown=True).status_code == 202
    assert wait_job(client, created["job_id"])["state"] == "succeeded"


def test_closing_the_runtime_during_the_audio_extraction_sends_nothing_and_leaves_pending_chunks(
    make_runtime: RuntimeFactory, factory: RecordingFactory, tmp_path: Path
) -> None:
    hanging = HangingExtractor()
    runtime = make_runtime(stt_client_factory=factory, extract_audio=hanging)
    client = client_for(runtime)
    set_key(client)
    created = started(client, add_source(runtime, tmp_path))
    wait_until(hanging.started.is_set, "the extraction to start")

    runtime.close()  # the extraction the run owns ends with the cancel that the shutdown sets

    assert hanging.cancelled.is_set()
    second = reopen(
        runtime, make_runtime, stt_client_factory=factory, extract_audio=FakeExtractor()
    )
    client = client_for(second)
    assert client.get(f"/v1/jobs/{created['job_id']}").json()["state"] == "interrupted"
    run = run_of(client, created["id"])
    assert run["status"] == "interrupted"
    assert chunk_states(run) == ["pending"] * CHUNKS
    assert (run["revision_id"], run["committed_micro_usd"]) == (None, 0)
    assert factory.stt.calls == []
    assert factory.keys == []

    set_key(client)
    assert resume(client, created["job_id"]).status_code == 202
    assert wait_job(client, created["job_id"])["state"] == "succeeded"
    assert len(factory.stt.calls) == CHUNKS


def test_a_cancel_that_arrives_while_the_runtime_closes_is_finished_not_left_cancelling(
    keyed: TestClient,
    runtime: Runtime,
    source_id: str,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: ObservingExtractor,
    stt: FakeStt,
    gate: Gate,
) -> None:
    created = started(keyed, source_id)
    wait_until(lambda: stt.active == 2, "two requests in flight")
    closer = close_in_background(runtime)
    wait_until(extractor.halted, "the run to notice the shutdown")

    # The worker still waits for the answers, so the job is `running` and the cancel is accepted.
    cancelled = keyed.post(f"/v1/jobs/{created['job_id']}/cancel", json={})
    assert (cancelled.status_code, cancelled.json()["state"]) == (202, "cancelling")
    gate.open()
    closer.join(WAIT_S)
    assert not closer.is_alive()

    client = client_for(
        reopen(runtime, make_runtime, stt_client_factory=factory, extract_audio=extractor)
    )
    # The user's decision stands: the job ends `cancelled` as it would without the shutdown.
    job = client.get(f"/v1/jobs/{created['job_id']}").json()
    assert (job["state"], job["error"]) == ("cancelled", None)
    run = run_of(client, created["id"])
    assert (run["status"], chunk_states(run)) == (
        "partial",
        ["succeeded", "succeeded", "cancelled"],
    )
    assert len(stt.calls) == 2


def test_a_run_that_is_complete_when_the_runtime_closes_stays_completed_and_succeeded(
    keyed: TestClient,
    runtime: Runtime,
    tmp_path: Path,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: ObservingExtractor,
    stt: FakeStt,
    gate: Gate,
) -> None:
    two_chunks = add_source(runtime, tmp_path, "two-chunks", duration_ms=TWO_CHUNKS_MS)
    created = started(keyed, two_chunks)
    wait_until(lambda: stt.active == 2, "both requests in flight")

    closer = close_in_background(runtime)
    wait_until(extractor.halted, "the run to notice the shutdown")
    gate.open()  # both answers come in after the stop began: nothing is left to send
    closer.join(WAIT_S)
    assert not closer.is_alive()

    client = client_for(
        reopen(runtime, make_runtime, stt_client_factory=factory, extract_audio=extractor)
    )
    job = client.get(f"/v1/jobs/{created['job_id']}").json()
    assert (job["state"], job["error"]) == ("succeeded", None)  # not `interrupted`, not `cancelled`
    run = run_of(client, created["id"])
    assert (run["status"], chunk_states(run)) == ("completed", ["succeeded", "succeeded"])
    assert run["revision_id"] is not None
    assert run["committed_micro_usd"] == 2 * MICRO_PER_SPEECH
    assert len(stt.calls) == 2


def defect(call: Call) -> SttResult:
    """An unexpected error out of the client once its request is on the wire."""
    raise RuntimeError(f"збій клієнта: {KEY}")


def broken(call: Call) -> SttResult:
    """A ``ServiceError`` out of the run (what a refusal of the service looks like)."""
    raise conflict("TEST_FAULT", "Збій під час запиту")


def settled_after_the_stop(
    runtime: Runtime,
    created: dict[str, Any],
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: FakeExtractor,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The next start of the workspace of ``runtime`` (closed by now): the job and the run of
    ``created``, which was the first request of a two-chunk run (its answer was lost to a fault,
    the second one was answered)."""
    client = client_for(
        reopen(runtime, make_runtime, stt_client_factory=factory, extract_audio=extractor)
    )
    job: dict[str, Any] = client.get(f"/v1/jobs/{created['job_id']}").json()
    run = run_of(client, created["id"])
    # Whatever the end of the job, the request that broke may have been paid for: it is undecided
    # and its reservation counts, exactly as after a crash.
    assert chunk_states(run) == ["unknown_outcome", "succeeded"]
    assert run["committed_micro_usd"] == RESERVED_FIRST_OF_TWO + MICRO_PER_SPEECH
    assert MARKER not in str(job) + str(run)
    return job, run


def test_a_defect_of_the_worker_while_the_runtime_closes_interrupts_the_job_instead_of_failing_it(
    keyed: TestClient,
    runtime: Runtime,
    tmp_path: Path,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: ObservingExtractor,
    stt: FakeStt,
    gate: Gate,
) -> None:
    stt.script = held_by(gate, after_the_second_request_started(defect))
    two_chunks = add_source(runtime, tmp_path, "two-chunks", duration_ms=TWO_CHUNKS_MS)
    created = started(keyed, two_chunks)
    wait_until(lambda: stt.active == 2, "both requests in flight")

    closer = close_in_background(runtime)
    wait_until(extractor.halted, "the run to notice the shutdown")
    gate.open()  # the first request breaks now, while the service is stopping
    closer.join(WAIT_S)
    assert not closer.is_alive()

    job, run = settled_after_the_stop(runtime, created, make_runtime, factory, extractor)
    # The stop, not the defect, decides what the user sees: the same state as after a crash.
    assert (job["state"], job["error"]) == ("interrupted", None)
    assert (run["status"], run["error_code"]) == ("interrupted", None)


def test_a_stop_that_begins_while_a_defect_is_being_settled_still_interrupts_the_job(
    keyed: TestClient,
    runtime: Runtime,
    tmp_path: Path,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: ObservingExtractor,
    stt: FakeStt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stt.script = after_the_second_request_started(defect)
    closers: list[threading.Thread] = []
    settle = TranscriptionRunService.recover_interrupted

    def stop_while_settling(self: TranscriptionRunService, run_id: str) -> None:
        # The worker is in `_abort` (the first call of this method): the service starts to stop now.
        if not closers:
            closers.append(close_in_background(runtime))
            # The flag is private: nothing public tells that a stop has begun but the refusal of
            # the next request, and a request made from here would race the worker.
            wait_until(runtime.transcription_jobs._stopping.is_set, "the stop to begin")
        settle(self, run_id)

    monkeypatch.setattr(TranscriptionRunService, "recover_interrupted", stop_while_settling)
    two_chunks = add_source(runtime, tmp_path, "two-chunks", duration_ms=TWO_CHUNKS_MS)
    created = started(keyed, two_chunks)
    wait_until(lambda: bool(closers), "the worker to settle its defect")
    closers[0].join(WAIT_S)
    assert not closers[0].is_alive()

    job, run = settled_after_the_stop(runtime, created, make_runtime, factory, extractor)
    assert (job["state"], job["error"]) == ("interrupted", None)
    assert (run["status"], run["error_code"]) == ("interrupted", None)


def test_a_service_error_while_the_runtime_closes_fails_the_job_with_its_code(
    keyed: TestClient,
    runtime: Runtime,
    tmp_path: Path,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: ObservingExtractor,
    stt: FakeStt,
    gate: Gate,
) -> None:
    stt.script = held_by(gate, after_the_second_request_started(broken))
    two_chunks = add_source(runtime, tmp_path, "two-chunks", duration_ms=TWO_CHUNKS_MS)
    created = started(keyed, two_chunks)
    wait_until(lambda: stt.active == 2, "both requests in flight")

    closer = close_in_background(runtime)
    wait_until(extractor.halted, "the run to notice the shutdown")
    gate.open()  # the first request breaks now, while the service is stopping
    closer.join(WAIT_S)
    assert not closer.is_alive()

    job, run = settled_after_the_stop(runtime, created, make_runtime, factory, extractor)
    # A refusal of the service is not the stop's doing and would come back after a restart: the job
    # ends `failed` with its code, stopping or not, and its chunks are settled as after a crash.
    assert job["state"] == "failed"
    assert job["error"] == {"code": "TEST_FAULT", "message": "Збій під час запиту"}
    assert (run["status"], run["error_code"]) == ("failed", "TEST_FAULT")


def test_after_the_service_stopped_a_new_run_is_503_and_nothing_is_created(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt
) -> None:
    runtime.transcription_jobs.shutdown()

    response = start(keyed, source_id)

    assert outcome(response) == (503, "SERVICE_STOPPING")
    assert error_of(response)["retryable"] is True
    assert nothing_was_created(runtime)  # no run and no job: nothing is left `queued`
    assert stt.calls == []


def test_after_the_service_stopped_a_resume_is_503_and_changes_nothing(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt
) -> None:
    stt.script = failing_at(0, SttError("auth"))
    created = started(keyed, source_id)
    assert wait_job(keyed, created["job_id"])["state"] == "failed"
    before = (run_of(keyed, created["id"]), checkpoint_of(runtime, created["job_id"]))
    sent = len(stt.calls)
    runtime.transcription_jobs.shutdown()

    response = resume(keyed, created["job_id"], retry_unknown=True)

    assert outcome(response) == (503, "SERVICE_STOPPING")
    assert error_of(response)["retryable"] is True
    assert keyed.get(f"/v1/jobs/{created['job_id']}").json()["state"] == "failed"
    assert (run_of(keyed, created["id"]), checkpoint_of(runtime, created["job_id"])) == before
    assert queued_jobs(runtime) == []  # a job that no worker would pick up
    assert len(stt.calls) == sent


def test_a_run_that_is_being_created_when_the_service_stops_is_finished_before_the_stop(
    keyed: TestClient,
    runtime: Runtime,
    source_id: str,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: FakeExtractor,
    stt: FakeStt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_create, release = threading.Event(), threading.Event()
    add_job = JobRepository.add

    def slow_add(self: JobRepository, **fields: Any) -> Job:
        in_create.set()
        assert release.wait(WAIT_S), "the create was never released"
        return add_job(self, **fields)

    monkeypatch.setattr(JobRepository, "add", slow_add)
    answers: list[Response] = []
    requester = threading.Thread(target=lambda: answers.append(start(keyed, source_id)))
    requester.start()
    wait_until(in_create.is_set, "the create to reach the write of its job")

    closer = close_in_background(runtime)
    time.sleep(0.3)  # long enough for the stop to overtake a create that did not hold it back
    assert closer.is_alive()  # the create has passed its checks: the stop waits for it
    release.set()
    requester.join(WAIT_S)
    closer.join(WAIT_S)
    assert not requester.is_alive()
    assert not closer.is_alive()

    assert answers[0].status_code == 202  # accepted before the stop, so not refused
    second = reopen(runtime, make_runtime, stt_client_factory=factory, extract_audio=extractor)
    client = client_for(second)
    created = answers[0].json()
    # Handed to the worker (and interrupted with it) or never started and recovered by the next
    # start: either way the job is `interrupted`, not `queued`, and nothing was sent.
    assert client.get(f"/v1/jobs/{created['job_id']}").json()["state"] == "interrupted"
    run = run_of(client, created["id"])
    assert (run["status"], chunk_states(run)) == ("interrupted", ["pending"] * CHUNKS)
    assert queued_jobs(second) == []
    assert stt.calls == []


def test_an_unknown_job_cannot_be_resumed(client: TestClient) -> None:
    response = resume(client, "nope")

    assert response.status_code == 404
    assert error_of(response)["code"] == "JOB_NOT_FOUND"


def test_only_a_transcription_job_that_is_interrupted_failed_or_cancelled_can_be_resumed(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt, gate: Gate
) -> None:
    with runtime.database.write_session() as session:
        other_id = JobRepository(session).add(type="source_hash", state="interrupted").id
    running = started(keyed, source_id)
    wait_until(lambda: stt.active > 0, "the run to reach the provider")

    for job_id in (other_id, running["job_id"]):  # another type; a job that is running
        refused = resume(keyed, job_id, retry_unknown=True)
        assert refused.status_code == 409
        assert error_of(refused)["code"] == "JOB_NOT_RESUMABLE"

    gate.open()
    assert wait_job(keyed, running["job_id"])["state"] == "succeeded"
    refused = resume(keyed, running["job_id"])  # succeeded: there is nothing left to do
    assert (refused.status_code, error_of(refused)["code"]) == (409, "JOB_NOT_RESUMABLE")
    assert len(stt.calls) == CHUNKS  # the refusals sent nothing


def test_a_second_resume_of_the_same_job_is_refused(
    keyed: TestClient, source_id: str, stt: FakeStt, gate: Gate
) -> None:
    stt.script = failing_at(0, SttError("auth"))
    created = started(keyed, source_id)
    wait_job(keyed, created["job_id"])
    stt.script = gate

    assert resume(keyed, created["job_id"]).status_code == 202
    again = resume(keyed, created["job_id"])

    assert again.status_code == 409
    assert error_of(again)["code"] == "JOB_NOT_RESUMABLE"


def test_a_resume_is_refused_while_another_job_of_the_same_source_is_open(
    keyed: TestClient, source_id: str, stt: FakeStt, gate: Gate
) -> None:
    stt.script = failing_at(0, SttError("auth"))
    first = started(keyed, source_id)
    assert wait_job(keyed, first["job_id"])["state"] == "failed"  # no longer open
    stt.script = gate
    second = started(keyed, source_id)  # so the source may be transcribed again
    wait_until(lambda: stt.active == 2, "the second run to hold two requests")
    sent = len(stt.calls)
    before = run_of(keyed, first["id"])

    refused = resume(keyed, first["job_id"], retry_unknown=True)

    assert refused.status_code == 409
    assert error_of(refused)["code"] == "TRANSCRIPTION_RUN_ACTIVE"
    assert error_of(refused)["details"] == {"run_id": second["id"], "job_id": second["job_id"]}
    assert keyed.get(f"/v1/jobs/{first['job_id']}").json()["state"] == "failed"  # nothing changed
    assert run_of(keyed, first["id"]) == before
    assert len(stt.calls) == sent  # and nothing was sent

    gate.open()
    assert wait_job(keyed, second["job_id"])["state"] == "succeeded"
    # Once the other job is over the first one can go on.
    assert resume(keyed, first["job_id"]).status_code == 202
    assert wait_job(keyed, first["job_id"])["state"] == "succeeded"


def test_the_checks_of_a_resume_are_made_in_the_order_open_job_unknown_outcome_key(
    runtime: Runtime,
    source_id: str,
    make_runtime: RuntimeFactory,
    factory: RecordingFactory,
    extractor: FakeExtractor,
    stt: FakeStt,
) -> None:
    _run_id, job_id = seed_crashed_run(runtime, source_id)  # one chunk with an unknown outcome
    second = restart(runtime, make_runtime, factory, extractor)
    client = client_for(second)  # a new process: no key
    # Another job of the same source is open (queued, no worker). Every rule fails at once, and
    # each fix shows the next one.
    other_run_id, other_job_id = seed_crashed_run(second, source_id, job_state="queued")

    refused = resume(client, job_id)
    assert outcome(refused) == (409, "TRANSCRIPTION_RUN_ACTIVE")
    assert error_of(refused)["details"] == {"run_id": other_run_id, "job_id": other_job_id}

    assert client.post(f"/v1/jobs/{other_job_id}/cancel", json={}).status_code == 202
    assert outcome(resume(client, job_id)) == (409, "UNKNOWN_OUTCOME_REQUIRES_DECISION")
    assert outcome(resume(client, job_id, retry_unknown=True)) == (409, "API_KEY_REQUIRED")
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "interrupted"  # nothing changed
    assert stt.calls == []
    assert factory.keys == []

    set_key(client)
    assert resume(client, job_id, retry_unknown=True).status_code == 202
    assert wait_job(client, job_id)["state"] == "succeeded"


def test_a_resume_with_a_higher_budget_continues_a_run_the_budget_stopped(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt
) -> None:
    created = started(keyed, source_id, budget_usd="0.01")  # the first request needs 0.0305
    stopped = wait_job(keyed, created["job_id"])
    assert (stopped["state"], stopped["error"]["code"]) == ("failed", "BUDGET_EXCEEDED")
    assert stt.calls == []
    assert checkpoint_of(runtime, created["job_id"])["budget_usd"] == "0.01"

    accepted = resume(keyed, created["job_id"], budget_usd="5")

    assert accepted.status_code == 202
    assert checkpoint_of(runtime, created["job_id"])["budget_usd"] == "5"
    assert wait_job(keyed, created["job_id"])["state"] == "succeeded"
    done = run_of(keyed, created["id"])
    assert (done["status"], done["error_code"]) == ("completed", None)
    assert chunk_states(done) == ["succeeded"] * CHUNKS
    assert len(stt.calls) == CHUNKS


@pytest.mark.parametrize("budget", ["", "0", "0.009999", "1000.01", "1e2", "-1", " 1", KEY])
def test_a_resume_with_a_bad_budget_is_422_and_changes_nothing(
    keyed: TestClient, runtime: Runtime, source_id: str, stt: FakeStt, budget: str
) -> None:
    created = started(keyed, source_id, budget_usd="0.01")
    wait_job(keyed, created["job_id"])
    before = (run_of(keyed, created["id"]), checkpoint_of(runtime, created["job_id"]))

    response = resume(keyed, created["job_id"], budget_usd=budget)

    assert response.status_code == 422
    assert error_of(response)["code"] == "INVALID_RUN_BUDGET"
    assert error_of(response)["details"] == {"field": "budget_usd"}
    assert MARKER not in response.text
    assert keyed.get(f"/v1/jobs/{created['job_id']}").json()["state"] == "failed"
    assert (run_of(keyed, created["id"]), checkpoint_of(runtime, created["job_id"])) == before
    assert stt.calls == []


def test_a_resume_with_a_body_of_the_wrong_shape_is_422(keyed: TestClient, source_id: str) -> None:
    created = started(keyed, source_id)
    wait_job(keyed, created["job_id"])

    for body in (
        {"retry_unknown": "yes"},
        {"retry_unknown": 1},
        {"other": True},
        {"budget_usd": 5},
        {"budget_usd": True},
        {"budget_usd": ["5"]},
    ):
        response = resume(keyed, created["job_id"], **body)
        assert response.status_code == 422
        assert error_of(response)["code"] == "VALIDATION_ERROR"


# --- the key stays in memory ------------------------------------------------------------------


def test_the_key_is_in_no_response_row_file_log_or_repr(
    keyed: TestClient,
    runtime: Runtime,
    tmp_path: Path,
    stt: FakeStt,
    factory: RecordingFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    responses: list[Response] = []
    job_ids: list[str] = []

    def run(name: str, **fields: Any) -> dict[str, Any]:
        response = start(keyed, add_source(runtime, tmp_path, name), **fields)
        responses.append(response)
        created: dict[str, Any] = response.json()
        job = wait_job(keyed, created["job_id"])
        responses.extend(
            [
                keyed.get(f"/v1/jobs/{created['job_id']}"),
                keyed.get(f"/v1/transcription-runs/{created['id']}"),
            ]
        )
        job_ids.append(created["job_id"])
        assert job["state"] in ("succeeded", "failed")
        return created

    run("success")
    stt.script = failing_at(0, SttError("unknown_outcome"))
    undecided = run("unknown")
    stt.script = failing_at(0, SttError("auth"))
    run("auth")
    stt.script = speech
    factory.error = ConfigError(KEY)  # an error text that holds the key must not travel
    run("config")
    factory.error = ValueError(KEY)
    run("value")
    factory.error = None
    responses.append(resume(keyed, undecided["job_id"], retry_unknown=True))
    wait_job(keyed, undecided["job_id"])
    responses += [
        start(keyed, "no-such-source"),
        start(keyed, undecided["id"], model=KEY),
        start(keyed, undecided["id"], language=KEY),
        start(keyed, undecided["id"], budget_usd=KEY),
        keyed.post("/v1/transcription-runs", json={"source_id": "s", "openrouter_api_key": KEY}),
    ]

    assert KEY in factory.keys  # the factory was reached with the key, and only the factory
    for response in responses:
        assert MARKER not in response.text
    for job_id in job_ids:
        assert MARKER not in repr(checkpoint_of(runtime, job_id))
    assert MARKER not in caplog.text
    for record in caplog.records:
        assert MARKER not in repr(vars(record))
        assert record.exc_info is None
    for subject in (
        runtime,
        vars(runtime),
        runtime.transcription_jobs,
        vars(runtime.transcription_jobs),
        runtime.transcription_runs,
        vars(runtime.transcription_runs),
    ):
        assert MARKER not in repr(subject)

    data_dir = runtime.database.data_dir
    with closing(sqlite3.connect(data_dir / DB_FILENAME)) as raw:
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blobs = {
        path.relative_to(data_dir).as_posix(): path.read_bytes()
        for path in data_dir.rglob("*")
        if path.is_file() and path.name != LOCK_FILENAME
    }
    assert DB_FILENAME in blobs
    assert any(name.startswith("stt/") and name.endswith(".json") for name in blobs)
    for name, blob in blobs.items():
        assert MARKER.encode() not in blob, name


def test_the_keyed_client_reads_the_key_only_when_it_sends_and_keeps_none() -> None:
    store = CredentialStore()
    stt = FakeStt(speech)
    factory = RecordingFactory(stt)
    client = KeyedSttClient(store, factory)
    assert factory.keys == []  # building the client asked for nothing

    store.set_openrouter_key(KEY)
    call = Call(MAI_MODEL, None, None, 0, 1000)
    result = client.transcribe(model=MAI_MODEL, audio_wav=b"RIFF0:1000")
    assert result.text == speech(call).text
    assert factory.keys == [KEY]
    assert MARKER not in repr(client)
    assert MARKER not in repr(vars(client))

    store.clear_openrouter_key()
    with pytest.raises(SttError) as caught:
        client.transcribe(model=MAI_MODEL, audio_wav=b"RIFF0:1000")
    assert caught.value.kind == "auth"
    assert factory.keys == [KEY]  # no key, no client, no request


@pytest.mark.parametrize(
    ("model", "audio"),
    [(MAI_MODEL, b""), ("openai/whisper-1", b"RIFF0:1000")],
    ids=["empty audio", "unsupported model"],
)
def test_the_keyed_client_turns_a_refusal_before_sending_into_an_invalid_request(
    model: str, audio: bytes
) -> None:
    store = CredentialStore()
    store.set_openrouter_key(KEY)
    stt = FakeStt(speech)
    client = KeyedSttClient(store, RecordingFactory(stt))

    with pytest.raises(SttError) as caught:
        client.transcribe(model=model, audio_wav=audio)

    assert caught.value.kind == "invalid_request"
    assert stt.calls == []  # refused before the client was called
    # Only a fixed text: the text of the ValueError (it names the model) is dropped.
    assert "whisper" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_the_keyed_client_does_not_translate_a_value_error_of_the_client_it_called() -> None:
    store = CredentialStore()
    store.set_openrouter_key(KEY)
    client = KeyedSttClient(store, lambda _key: FaultyClient())

    # The request may be on the wire by now: it is not a refusal that frees the reservation.
    with pytest.raises(ValueError, match="збій після відправлення"):
        client.transcribe(model=MAI_MODEL, audio_wav=b"RIFF0:1000")


def test_the_production_factory_follows_the_mode_of_the_config(tmp_path: Path) -> None:
    production = Config(mode="production", data_dir=tmp_path, session_token="t" * 32)
    test_without_provider = make_config(tmp_path, "test")
    test_with_provider = Config(
        mode="test",
        data_dir=tmp_path,
        session_token="t" * 32,
        test_provider_base_url="http://127.0.0.1:9/v1",
    )
    factories: dict[str, SttClientFactory] = {
        "production": openrouter_stt_factory(production),
        "test without provider": openrouter_stt_factory(test_without_provider),
        "test with provider": openrouter_stt_factory(test_with_provider),
    }  # building a factory resolves nothing

    assert (
        repr(factories["production"](KEY))
        == "OpenRouterStt(base_url='https://openrouter.ai/api/v1')"
    )
    assert (
        repr(factories["test with provider"](KEY))
        == "OpenRouterStt(base_url='http://127.0.0.1:9/v1')"
    )
    with pytest.raises(ConfigError):  # paid AI is unreachable from tests
        factories["test without provider"](KEY)
