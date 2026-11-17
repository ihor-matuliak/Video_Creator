"""POST /v1/analysis-runs, POST /v1/analysis-runs/estimate, GET /v1/analysis-runs and
GET /v1/analysis-runs/{id}, and the ``analysis`` job through POST /v1/jobs/{id}/cancel and
/resume (ADR 041 §9–§11, §19, §20).

Test mode only: the chat client is a fake behind a recording key factory, so nothing goes over the
network and nothing is paid for. The fake answers a window with one candidate that spans the
window's units. A transcript of three segments is one window, so a run completes with one suggested
clip. A key, when one is given, reaches the factory and no response.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import deque
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select

from clipforge.adapters.openrouter_chat import (
    AnalysisProfile,
    ChatError,
    ChatResult,
    analysis_profiles,
)
from clipforge.config import ConfigError
from clipforge.domain.search_profile import parse_search_profile
from clipforge.services.analysis_jobs import ANALYSIS_JOB
from clipforge.services.analysis_runs import ChatClient
from clipforge.services.runtime import Runtime
from clipforge.storage import (
    AnalysisRun,
    Clip,
    Job,
    JobRepository,
    NewSegment,
    ProjectRepository,
    SourceRepository,
    TranscriptRepository,
)

from .support import RuntimeFactory, client_for
from .test_api_sources import wait_job

MODEL_ID = "test/selector"
MODEL: AnalysisProfile = analysis_profiles("test")[MODEL_ID]
# Part of the key every test uses: if a response, a row or a log holds it, the secret leaked.
MARKER = "Zq7Lk2Vx9Rm4"
KEY = f"sk-or-v1-{MARKER}0123456789abcdef"
WAIT_S = 10.0
DURATION_MS = 60_000
CUE_MS = 10_000
CUES = 3
SCORES = ("clarity", "hook", "completeness", "emotion", "novelty", "relevance")
PROFILE: dict[str, object] = {
    "categories": ["funny", "fails"],
    "custom_instruction": "",
    "min_duration_ms": 5_000,
    "max_duration_ms": 600_000,
    "max_candidates": 20,
    "allow_partial": False,
}


# --- fakes -----------------------------------------------------------------------------------


def units_of(body: Mapping[str, object]) -> list[dict[str, Any]]:
    """The units of the user message of a request body."""
    messages = body["messages"]
    assert isinstance(messages, list)
    user = messages[1]
    assert isinstance(user, dict)
    payload: dict[str, Any] = json.loads(user["content"])
    units: list[dict[str, Any]] = payload["units"]
    return units


def selection(body: Mapping[str, object]) -> ChatResult:
    """One candidate from the first unit to the last one, with the first unit as its only evidence.
    Every unit of the window is in its core here (one window), so the answer is valid."""
    units = units_of(body)
    first, last = units[0], units[-1]
    candidate = {
        "title": "Смішний момент",
        "categories": ["funny"],
        "start_id": first["id"],
        "end_id": last["id"],
        "evidence_ids": [first["id"]],
        "reason": "Причина відбору",
        "scores": dict.fromkeys(SCORES, 4),
        "visual_review_required": False,
    }
    return ChatResult(
        content=json.dumps({"candidates": [candidate]}, ensure_ascii=False),
        finish_reason="stop",
        cost=Decimal("0.002"),
        generation_id="gen-test",
    )


class ScriptedChat:
    """The fake provider. A call takes the next ``ChatError`` queued in ``failures``; otherwise it
    is answered with the selection of its window. Every call is recorded in ``bodies``. While
    ``holding`` is set a call waits (at most ``WAIT_S``) until ``release`` is set; ``started``
    is set when the first held call begins, so a test can act while that request is on the wire."""

    def __init__(self) -> None:
        self.bodies: list[Mapping[str, object]] = []
        self.failures: deque[ChatError] = deque()
        self.holding = False
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def complete(self, body: Mapping[str, object]) -> ChatResult:
        with self._lock:
            self.bodies.append(body)
            failure = self.failures.popleft() if self.failures else None
        if self.holding:
            self.started.set()
            self.release.wait(WAIT_S)
        if failure is not None:
            raise failure
        return selection(body)


class RecordingChats:
    """The chat factory of the runtime. It records the key it was given (``None`` in test mode
    without a key) and returns the fake, or raises ``refusal`` when one is set."""

    def __init__(self, chat: ScriptedChat) -> None:
        self.chat = chat
        self.keys: list[str | None] = []
        self.refusal: Exception | None = None

    def __call__(self, api_key: str | None) -> ChatClient:
        self.keys.append(api_key)
        if self.refusal is not None:
            raise self.refusal
        return self.chat


# --- workspace helpers -----------------------------------------------------------------------


def add_source(runtime: Runtime, tmp_path: Path, name: str = "media") -> str:
    """A registered source of ``DURATION_MS``. Its file exists, so a hash job after a restart finds
    it, as it would for a real source."""
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
            duration_ms=DURATION_MS,
            video_stream_index=0,
            audio_streams=[{"index": 1, "codec": "aac", "default": True}],
            canonical_origin={"origin_ms": 0, "time_base": "1/1000"},
            probe_json={},
            availability="available",
        )
        return source.id


def add_transcript(runtime: Runtime, source_id: str, *, coverage_state: str = "succeeded") -> str:
    """A new revision of the source with ``CUES`` segments; segment ids are unique across
    revisions."""
    segments = [
        NewSegment(
            id=uuid.uuid4().hex,
            ordinal=5 + 10 * index,
            start_ms=index * CUE_MS,
            end_ms=(index + 1) * CUE_MS,
            text=f"репліка {index}",
        )
        for index in range(CUES)
    ]
    with runtime.database.write_session() as session:
        revision = TranscriptRepository(session).add_revision(
            source_id=source_id,
            origin="import",
            timing_level="segment",
            language="uk",
            duration_ms=DURATION_MS,
            coverage=[{"start_ms": 0, "end_ms": DURATION_MS, "state": coverage_state}],
            metadata={"format": "normalized_json", "offset_ms": 0},
            segments=segments,
        )
        return revision.id


def row_counts(runtime: Runtime) -> dict[str, int]:
    """What a refused request must leave alone."""
    with runtime.database.session() as session:
        return {
            "runs": int(session.scalar(select(func.count()).select_from(AnalysisRun)) or 0),
            "jobs": int(session.scalar(select(func.count()).select_from(Job)) or 0),
            "clips": int(session.scalar(select(func.count()).select_from(Clip)) or 0),
        }


def checkpoint_of(runtime: Runtime, job_id: str) -> dict[str, Any]:
    with runtime.database.session() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        return dict(job.checkpoint_json or {})


def restart(runtime: Runtime, make_runtime: RuntimeFactory, factory: RecordingChats) -> Runtime:
    """The process died: the next one opens the same workspace (and has no key)."""
    runtime.close()
    return make_runtime(
        with_tools=False,
        data_dir=runtime.database.data_dir,
        mode="test",
        chat_client_factory=factory,
    )


# --- request helpers -------------------------------------------------------------------------


def set_key(client: TestClient, key: str = KEY) -> None:
    response = client.post("/v1/runtime/credentials", json={"openrouter_api_key": key})
    assert response.status_code == 204


def run_body(source_id: str, transcript_id: str, **fields: Any) -> dict[str, Any]:
    """A valid request body with a model and a budget. ``fields`` replace them; ``None`` is what
    JSON calls an absent value, so the settings decide."""
    body: dict[str, Any] = {
        "source_id": source_id,
        "transcript_id": transcript_id,
        "profile": PROFILE,
        "model": MODEL_ID,
        "budget_usd": "5",
    }
    body.update(fields)
    return body


def error_of(response: Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


def outcome(response: Response) -> tuple[int, str]:
    return response.status_code, error_of(response)["code"]


def started(client: TestClient, body: Mapping[str, Any]) -> dict[str, Any]:
    response = client.post("/v1/analysis-runs", json=body)
    assert response.status_code == 202, response.text
    created: dict[str, Any] = response.json()
    return created


def estimated(client: TestClient, body: Mapping[str, Any]) -> dict[str, Any]:
    response = client.post("/v1/analysis-runs/estimate", json=body)
    assert response.status_code == 200, response.text
    estimate: dict[str, Any] = response.json()
    return estimate


def run_of(client: TestClient, run_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/analysis-runs/{run_id}")
    assert response.status_code == 200, response.text
    run: dict[str, Any] = response.json()
    return run


def listed(client: TestClient, source_id: str) -> list[dict[str, Any]]:
    response = client.get("/v1/analysis-runs", params={"source_id": source_id})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


# --- fixtures --------------------------------------------------------------------------------


@pytest.fixture
def chat() -> ScriptedChat:
    return ScriptedChat()


@pytest.fixture
def factory(chat: ScriptedChat) -> RecordingChats:
    return RecordingChats(chat)


@pytest.fixture
def runtime(make_runtime: RuntimeFactory, factory: RecordingChats) -> Runtime:
    """Test mode without FFmpeg: analysis needs neither."""
    return make_runtime(with_tools=False, mode="test", chat_client_factory=factory)


@pytest.fixture
def source_id(runtime: Runtime, tmp_path: Path) -> str:
    return add_source(runtime, tmp_path)


@pytest.fixture
def transcript_id(runtime: Runtime, source_id: str) -> str:
    return add_transcript(runtime, source_id)


@pytest.fixture
def keyed(client: TestClient) -> TestClient:
    set_key(client)
    return client


@pytest.fixture
def held(runtime: Runtime, chat: ScriptedChat) -> Iterator[ScriptedChat]:
    """Every chat call waits until the test releases it, or until teardown does. It depends on
    ``runtime``, so it is released before the runtime is closed: no worker may stay blocked then."""
    chat.holding = True
    yield chat
    chat.release.set()


# --- POST /v1/analysis-runs: a run is accepted, sent and completed ---------------------------


def test_a_run_is_accepted_and_ends_with_a_suggested_clip_without_any_key(
    client: TestClient, source_id: str, transcript_id: str, chat: ScriptedChat
) -> None:
    created = started(client, run_body(source_id, transcript_id))

    assert set(created) == {"id", "job_id", "status"}
    assert created["status"] == "queued"
    job = wait_job(client, created["job_id"])
    assert (job["type"], job["state"], job["error"]) == (ANALYSIS_JOB, "succeeded", None)
    assert job["progress"] == {
        "stage": "done",
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "unknown_outcome": 0,
    }

    run = run_of(client, created["id"])
    assert set(run) == {
        "id",
        "source_id",
        "transcript_id",
        "transcript_revision",
        "model",
        "provider",
        "prompt_version",
        "schema_version",
        "unit_level",
        "status",
        "error_code",
        "created_at",
        "updated_at",
        "job_id",
        "committed_micro_usd",
        "windows",
        "clip_ids",
        "stale",
        "diagnostics_summary",
    }
    assert (run["status"], run["error_code"], run["job_id"], run["stale"]) == (
        "completed",
        None,
        created["job_id"],
        False,
    )
    assert (run["source_id"], run["transcript_id"], run["transcript_revision"]) == (
        source_id,
        transcript_id,
        1,
    )
    assert (run["model"], run["provider"], run["unit_level"]) == (
        MODEL_ID,
        "openrouter",
        "segment",
    )
    assert run["windows"] == {
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "unknown_outcome": 0,
        "pending": 0,
    }
    assert run["committed_micro_usd"] == 2_000  # the cost the fake reports: 0.002 USD
    assert len(run["clip_ids"]) == 1
    assert run["diagnostics_summary"] == {"dropped": {}, "rejected": {}, "suppressed": {}}
    assert len(chat.bodies) == 1
    assert chat.bodies[0]["model"] == MODEL_ID
    assert chat.bodies[0]["stream"] is False


def test_a_key_reaches_the_client_at_each_request_and_no_response(
    keyed: TestClient, source_id: str, transcript_id: str, factory: RecordingChats
) -> None:
    created = started(keyed, run_body(source_id, transcript_id))
    wait_job(keyed, created["job_id"])

    responses = [
        keyed.get(f"/v1/jobs/{created['job_id']}"),
        keyed.get(f"/v1/analysis-runs/{created['id']}"),
        keyed.get("/v1/analysis-runs", params={"source_id": source_id}),
    ]
    assert factory.keys == [KEY]
    assert all(MARKER not in response.text for response in responses)


def test_a_run_turns_stale_when_a_newer_revision_arrives(
    client: TestClient, runtime: Runtime, source_id: str, transcript_id: str
) -> None:
    created = started(client, run_body(source_id, transcript_id))
    wait_job(client, created["job_id"])
    assert run_of(client, created["id"])["stale"] is False

    add_transcript(runtime, source_id)

    run = run_of(client, created["id"])
    assert (run["stale"], run["transcript_id"]) == (True, transcript_id)


def test_a_numeric_budget_is_accepted_and_kept_as_its_decimal_text(
    client: TestClient, runtime: Runtime, source_id: str, transcript_id: str
) -> None:
    created = started(client, run_body(source_id, transcript_id, budget_usd=5))
    wait_job(client, created["job_id"])

    assert checkpoint_of(runtime, created["job_id"])["budget_usd"] == "5"


def test_the_settings_give_the_model_and_the_budget_when_the_request_gives_none(
    client: TestClient, runtime: Runtime, source_id: str, transcript_id: str
) -> None:
    patched = client.patch("/v1/settings", json={"analysis_model": MODEL_ID, "run_budget_usd": "3"})
    assert patched.status_code == 200, patched.text

    created = started(client, run_body(source_id, transcript_id, model=None, budget_usd=None))
    wait_job(client, created["job_id"])

    assert run_of(client, created["id"])["model"] == MODEL_ID
    assert checkpoint_of(runtime, created["job_id"]) == {"run_id": created["id"], "budget_usd": "3"}


# --- POST /v1/analysis-runs and /estimate: refusals of the body ------------------------------


@pytest.mark.parametrize(
    ("fields", "code", "field"),
    [
        ({"model": None}, "ANALYSIS_MODEL_REQUIRED", "model"),
        ({"model": "openai/gpt-4o"}, "ANALYSIS_MODEL_NOT_SUPPORTED", "model"),
        ({"budget_usd": "0"}, "INVALID_RUN_BUDGET", "budget_usd"),
        ({"budget_usd": "1e2"}, "INVALID_RUN_BUDGET", "budget_usd"),
        ({"budget_usd": ""}, "INVALID_RUN_BUDGET", "budget_usd"),
        ({"budget_usd": True}, "INVALID_RUN_BUDGET", "budget_usd"),
        ({"budget_usd": False}, "INVALID_RUN_BUDGET", "budget_usd"),
    ],
)
def test_a_bad_model_or_budget_is_422_for_the_create_and_the_estimate(
    client: TestClient,
    runtime: Runtime,
    source_id: str,
    transcript_id: str,
    chat: ScriptedChat,
    fields: dict[str, Any],
    code: str,
    field: str,
) -> None:
    body = run_body(source_id, transcript_id, **fields)
    before = row_counts(runtime)

    for response in (
        client.post("/v1/analysis-runs", json=body),
        client.post("/v1/analysis-runs/estimate", json=body),
    ):
        assert outcome(response) == (422, code)
        assert error_of(response)["details"] == {"field": field}
    assert row_counts(runtime) == before
    assert chat.bodies == []


def test_without_a_budget_anywhere_create_is_422_and_the_estimate_is_not(
    client: TestClient, runtime: Runtime, source_id: str, transcript_id: str
) -> None:
    body = run_body(source_id, transcript_id, budget_usd=None)
    before = row_counts(runtime)

    refused = client.post("/v1/analysis-runs", json=body)

    assert outcome(refused) == (422, "BUDGET_REQUIRED")
    assert error_of(refused)["details"] == {"field": "budget_usd"}
    assert row_counts(runtime) == before
    assert estimated(client, body)["within_budget"] is None


@pytest.mark.parametrize(
    ("profile", "field"),
    [
        ({**PROFILE, "categories": []}, "profile.categories"),
        ({**PROFILE, "max_candidates": 0}, "profile.max_candidates"),
        ("not an object", "profile"),
    ],
)
def test_a_bad_profile_is_422_naming_its_field_and_nothing_is_written(
    client: TestClient,
    runtime: Runtime,
    source_id: str,
    transcript_id: str,
    chat: ScriptedChat,
    profile: object,
    field: str,
) -> None:
    before = row_counts(runtime)

    response = client.post(
        "/v1/analysis-runs", json=run_body(source_id, transcript_id, profile=profile)
    )

    assert outcome(response)[0] == 422
    assert error_of(response)["details"] == {"field": field}
    assert row_counts(runtime) == before
    assert chat.bodies == []


@pytest.mark.parametrize("extra", [{"retry_unknown": True}, {"openrouter_api_key": KEY}])
def test_a_body_with_an_unknown_field_is_422_without_echoing_it(
    client: TestClient, source_id: str, transcript_id: str, extra: dict[str, Any]
) -> None:
    response = client.post(
        "/v1/analysis-runs", json={**run_body(source_id, transcript_id), **extra}
    )

    assert outcome(response) == (422, "VALIDATION_ERROR")
    assert MARKER not in response.text


def test_a_body_without_a_profile_is_422(
    client: TestClient, source_id: str, transcript_id: str
) -> None:
    body = run_body(source_id, transcript_id)
    del body["profile"]

    assert outcome(client.post("/v1/analysis-runs", json=body)) == (422, "VALIDATION_ERROR")


def test_the_estimate_describes_the_plan_and_writes_and_sends_nothing(
    client: TestClient, runtime: Runtime, source_id: str, transcript_id: str, chat: ScriptedChat
) -> None:
    before = row_counts(runtime)

    body = estimated(client, run_body(source_id, transcript_id))

    assert set(body) == {
        "model",
        "unit_level",
        "unit_count",
        "window_count",
        "estimate_micro_usd",
        "prompt_usd_per_mtok",
        "completion_usd_per_mtok",
        "max_tokens",
        "price_basis",
        "budget_usd",
        "within_budget",
    }
    assert (body["model"], body["unit_level"], body["unit_count"], body["window_count"]) == (
        MODEL_ID,
        "segment",
        CUES,
        1,
    )
    assert (
        body["prompt_usd_per_mtok"],
        body["completion_usd_per_mtok"],
        body["max_tokens"],
        body["price_basis"],
    ) == ("1", "2", 4096, "test")
    # The completion cap alone is 4096 tokens at 2 micro-USD each; the prompt side adds more.
    assert body["estimate_micro_usd"] > 4096 * 2
    assert (body["budget_usd"], body["within_budget"]) == ("5", True)
    assert row_counts(runtime) == before
    assert chat.bodies == []


def test_the_estimate_says_whether_the_plan_fits_the_budget(
    client: TestClient, source_id: str, transcript_id: str
) -> None:
    no_budget = estimated(client, run_body(source_id, transcript_id, budget_usd=None))
    small = estimated(client, run_body(source_id, transcript_id, budget_usd="0.01"))

    assert (no_budget["budget_usd"], no_budget["within_budget"]) == (None, None)
    # The lowest budget (0.01 USD = 10 000 micro-USD) is below the completion cap plus the prompt.
    assert (small["budget_usd"], small["within_budget"]) == ("0.01", False)
    assert small["estimate_micro_usd"] == no_budget["estimate_micro_usd"] > 10_000


# --- POST /v1/analysis-runs: refusals of the stored state ------------------------------------


def test_an_unknown_source_or_transcript_is_404_and_a_foreign_one_is_422(
    client: TestClient, runtime: Runtime, source_id: str, transcript_id: str, tmp_path: Path
) -> None:
    other = add_source(runtime, tmp_path, "other")
    foreign = add_transcript(runtime, other)
    before = row_counts(runtime)

    unknown_source = client.post(
        "/v1/analysis-runs", json=run_body("no-such-source", transcript_id)
    )
    unknown_transcript = client.post(
        "/v1/analysis-runs", json=run_body(source_id, "no-such-transcript")
    )
    mismatch = client.post("/v1/analysis-runs", json=run_body(source_id, foreign))
    unknown_estimate = client.post(
        "/v1/analysis-runs/estimate", json=run_body("no-such-source", transcript_id)
    )

    assert outcome(unknown_source) == (404, "SOURCE_NOT_FOUND")
    assert outcome(unknown_transcript) == (404, "TRANSCRIPT_NOT_FOUND")
    assert outcome(mismatch) == (422, "TRANSCRIPT_SOURCE_MISMATCH")
    assert error_of(mismatch)["details"] == {"field": "transcript_id"}
    assert outcome(unknown_estimate) == (404, "SOURCE_NOT_FOUND")
    assert row_counts(runtime) == before


def test_a_newer_revision_refuses_the_older_transcript_and_names_the_newest(
    client: TestClient, runtime: Runtime, source_id: str, transcript_id: str
) -> None:
    newest = add_transcript(runtime, source_id)

    stale = client.post("/v1/analysis-runs", json=run_body(source_id, transcript_id))

    assert outcome(stale) == (409, "TRANSCRIPT_REVISION_STALE")
    assert error_of(stale)["details"] == {"latest_id": newest}


def test_a_partial_transcript_needs_allow_partial(
    client: TestClient, runtime: Runtime, source_id: str
) -> None:
    partial = add_transcript(runtime, source_id, coverage_state="failed")

    refused = client.post("/v1/analysis-runs", json=run_body(source_id, partial))
    assert outcome(refused) == (409, "TRANSCRIPT_PARTIAL")

    allowed = started(
        client, run_body(source_id, partial, profile={**PROFILE, "allow_partial": True})
    )
    assert wait_job(client, allowed["job_id"])["state"] == "succeeded"


def test_a_second_run_of_the_source_is_409_while_one_is_open_and_another_source_may_queue(
    keyed: TestClient,
    runtime: Runtime,
    source_id: str,
    transcript_id: str,
    tmp_path: Path,
    held: ScriptedChat,
) -> None:
    first = started(keyed, run_body(source_id, transcript_id))
    assert held.started.wait(WAIT_S)

    again = keyed.post("/v1/analysis-runs", json=run_body(source_id, transcript_id))
    other = add_source(runtime, tmp_path, "other")
    queued = started(keyed, run_body(other, add_transcript(runtime, other)))

    assert outcome(again) == (409, "ANALYSIS_RUN_ACTIVE")
    assert error_of(again)["details"] == {"run_id": first["id"], "job_id": first["job_id"]}
    assert keyed.get(f"/v1/jobs/{queued['job_id']}").json()["state"] == "queued"

    held.release.set()
    assert wait_job(keyed, first["job_id"])["state"] == "succeeded"
    assert wait_job(keyed, queued["job_id"])["state"] == "succeeded"


def test_after_shutdown_a_create_is_503_and_writes_nothing(
    client: TestClient, runtime: Runtime, source_id: str, transcript_id: str
) -> None:
    runtime.analysis_jobs.shutdown()
    before = row_counts(runtime)

    response = client.post("/v1/analysis-runs", json=run_body(source_id, transcript_id))

    assert outcome(response) == (503, "SERVICE_STOPPING")
    assert error_of(response)["retryable"] is True
    assert row_counts(runtime) == before


# --- jobs: cancel, resume, restart -----------------------------------------------------------


def test_a_queued_run_is_cancelled_through_its_job_and_is_never_sent(
    client: TestClient,
    runtime: Runtime,
    source_id: str,
    transcript_id: str,
    tmp_path: Path,
    held: ScriptedChat,
) -> None:
    first = started(client, run_body(source_id, transcript_id))
    assert held.started.wait(WAIT_S)
    other = add_source(runtime, tmp_path, "other")
    queued = started(client, run_body(other, add_transcript(runtime, other)))

    cancelled = client.post(f"/v1/jobs/{queued['job_id']}/cancel", json={})

    assert (cancelled.status_code, cancelled.json()["state"]) == (202, "cancelled")
    run = run_of(client, queued["id"])
    assert (run["status"], run["windows"]["pending"]) == ("cancelled", 1)
    held.release.set()
    assert wait_job(client, first["job_id"])["state"] == "succeeded"
    assert client.get(f"/v1/jobs/{queued['job_id']}").json()["state"] == "cancelled"
    assert len(held.bodies) == 1  # the cancelled run was never sent


def test_a_failed_run_is_resumed_through_its_job(
    client: TestClient, source_id: str, transcript_id: str, chat: ScriptedChat
) -> None:
    chat.failures.append(ChatError("auth"))
    created = started(client, run_body(source_id, transcript_id))

    failed = wait_job(client, created["job_id"])
    assert (failed["state"], failed["error"]["code"]) == ("failed", "ANALYSIS_AUTH")
    run = run_of(client, created["id"])
    assert (run["status"], run["error_code"], run["windows"]["failed"]) == (
        "failed",
        "ANALYSIS_AUTH",
        1,
    )

    resumed = client.post(f"/v1/jobs/{created['job_id']}/resume", json={})

    assert (resumed.status_code, resumed.json()["state"]) == (202, "queued")
    assert wait_job(client, created["job_id"])["state"] == "succeeded"
    assert run_of(client, created["id"])["status"] == "completed"
    assert len(chat.bodies) == 2


def test_a_provider_that_cannot_be_built_fails_the_run_without_a_send_and_leaks_no_key(
    keyed: TestClient,
    source_id: str,
    transcript_id: str,
    factory: RecordingChats,
    chat: ScriptedChat,
) -> None:
    factory.refusal = ConfigError(f"текст помилки з ключем {KEY}")
    created = started(keyed, run_body(source_id, transcript_id))

    job = wait_job(keyed, created["job_id"])

    assert (job["state"], job["error"]["code"]) == ("failed", "ANALYSIS_REQUEST_REJECTED")
    assert chat.bodies == []
    assert MARKER not in str(job)
    run = run_of(keyed, created["id"])
    assert (run["status"], run["error_code"], run["committed_micro_usd"]) == (
        "failed",
        "ANALYSIS_REQUEST_REJECTED",
        0,
    )
    assert MARKER not in str(run)


def test_a_queued_job_is_interrupted_by_a_restart_and_runs_only_when_resumed(
    runtime: Runtime,
    source_id: str,
    transcript_id: str,
    make_runtime: RuntimeFactory,
    factory: RecordingChats,
    chat: ScriptedChat,
) -> None:
    """The run and its job were stored, but no worker ever took the job (a crash before it started).
    After the restart the job is interrupted and nothing is sent until the explicit resume."""
    run_id = runtime.analysis_runs.create_run(
        source_id=source_id,
        transcript_id=transcript_id,
        search_profile=parse_search_profile(PROFILE),
        analysis_profile=MODEL,
    )
    with runtime.database.write_session() as session:
        job_id = (
            JobRepository(session)
            .add(
                type=ANALYSIS_JOB,
                state="queued",
                progress_json={"stage": "queued"},
                checkpoint_json={"run_id": run_id, "budget_usd": "5"},
            )
            .id
        )

    client = client_for(restart(runtime, make_runtime, factory))

    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "interrupted"
    run = run_of(client, run_id)
    assert (run["status"], run["windows"]["pending"]) == ("interrupted", 1)
    assert (chat.bodies, factory.keys) == ([], [])

    resumed = client.post(f"/v1/jobs/{job_id}/resume", json={})

    assert resumed.status_code == 202
    assert wait_job(client, job_id)["state"] == "succeeded"
    assert run_of(client, run_id)["status"] == "completed"
    assert factory.keys == [None]  # test mode: the client is built once, without a key


# --- reads -----------------------------------------------------------------------------------


def test_the_runs_of_a_source_are_listed_newest_first_and_only_those(
    client: TestClient, runtime: Runtime, source_id: str, transcript_id: str, tmp_path: Path
) -> None:
    first = started(client, run_body(source_id, transcript_id))
    wait_job(client, first["job_id"])
    second = started(client, run_body(source_id, transcript_id))
    wait_job(client, second["job_id"])
    other = add_source(runtime, tmp_path, "other")
    add_transcript(runtime, other)

    runs = [run_of(client, first["id"]), run_of(client, second["id"])]
    expected = [
        run["id"]
        for run in sorted(runs, key=lambda run: (run["created_at"], run["id"]), reverse=True)
    ]
    items = listed(client, source_id)

    assert [item["id"] for item in items] == expected
    assert set(items[0]) == {
        "id",
        "source_id",
        "transcript_id",
        "transcript_revision",
        "model",
        "status",
        "error_code",
        "job_id",
        "committed_micro_usd",
        "created_at",
    }
    assert listed(client, other) == []


def test_the_list_needs_a_source_id_and_the_source_must_exist(client: TestClient) -> None:
    for response in (
        client.get("/v1/analysis-runs"),
        client.get("/v1/analysis-runs", params={"source_id": ""}),
    ):
        assert outcome(response) == (422, "VALIDATION_ERROR")
    unknown = client.get("/v1/analysis-runs", params={"source_id": "no-such-source"})
    assert outcome(unknown) == (404, "SOURCE_NOT_FOUND")


def test_an_unknown_run_is_404(client: TestClient) -> None:
    assert outcome(client.get("/v1/analysis-runs/no-such-run")) == (
        404,
        "ANALYSIS_RUN_NOT_FOUND",
    )


def test_without_the_session_token_nothing_is_done(
    anonymous: TestClient, source_id: str, transcript_id: str
) -> None:
    assert (
        anonymous.post("/v1/analysis-runs", json=run_body(source_id, transcript_id)).status_code
        == 401
    )
    assert (
        anonymous.post(
            "/v1/analysis-runs/estimate", json=run_body(source_id, transcript_id)
        ).status_code
        == 401
    )
    assert anonymous.get("/v1/analysis-runs", params={"source_id": source_id}).status_code == 401
    assert anonymous.get("/v1/analysis-runs/no-such-run").status_code == 401
