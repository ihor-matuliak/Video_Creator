"""Tests of the ``analysis`` job service (ADR 041 §9–§11, §19): create, estimate, reads, cancel,
resume, recovery and shutdown, and the key handling of ``KeyedChatClient``.

Every test uses fake chat clients: nothing is sent and nothing is paid. A worker thread runs the
jobs, so a test waits for the state it needs (at most 10 s). ``hold_first`` keeps the first request
on the wire until the test releases it, so a cancel or a shutdown can land while a window runs.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

import pytest
from sqlalchemy.orm import Session

from clipforge.adapters.openrouter_chat import (
    AnalysisProfile,
    ChatError,
    ChatResult,
    analysis_profiles,
    reservation_micro_usd,
)
from clipforge.config import ConfigError, Mode
from clipforge.domain.search_profile import parse_search_profile
from clipforge.services.analysis_jobs import (
    AnalysisEstimate,
    AnalysisJobService,
    CreatedAnalysisRun,
    KeyedChatClient,
    WindowCounts,
)
from clipforge.services.analysis_runs import AnalysisRunService, ChatClient
from clipforge.services.credentials import CredentialStore
from clipforge.services.errors import ErrorKind, ServiceError
from clipforge.services.jobs import JobView, job_view
from clipforge.services.settings import SettingsService
from clipforge.services.spend import SpendLedger
from clipforge.storage import (
    AnalysisRun,
    AnalysisRunRepository,
    AnalysisWindow,
    Clip,
    Database,
    JobRepository,
    NewSegment,
    TranscriptRepository,
    open_database,
)

from .test_storage_support import add_project, add_source

MODEL_ID = "test/selector"
MODEL: AnalysisProfile = analysis_profiles("test")[MODEL_ID]
KEY = "sk-or-test-0123456789abcdef"
BODY: Mapping[str, object] = {"model": MODEL_ID, "messages": []}
SOURCE_MS = 1_000_000
SEGMENT_MS = 100_000
EMPTY = '{"candidates":[]}'
WAIT_S = 10.0
ALL_JOB_STATES = (
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
)
PROFILE_JSON: dict[str, object] = {
    "categories": ["funny", "fails"],
    "custom_instruction": "",
    "min_duration_ms": 5_000,
    "max_duration_ms": 600_000,
    "max_candidates": 20,
    "allow_partial": False,
}
FULL_COVERAGE: list[dict[str, Any]] = [{"start_ms": 0, "end_ms": SOURCE_MS, "state": "succeeded"}]


def ordinal_of(index: int) -> int:
    """Gaps between the ordinals, so that the mapping back to positions is really exercised."""
    return 5 + 10 * index


def candidate(start: str, end: str, evidence: list[str], score: int = 4) -> str:
    """A valid answer with one candidate whose ids lie in the core of the window."""
    scores = dict.fromkeys(
        ("clarity", "hook", "completeness", "emotion", "novelty", "relevance"), score
    )
    return json.dumps(
        {
            "candidates": [
                {
                    "title": "Смішний момент",
                    "categories": ["funny"],
                    "start_id": start,
                    "end_id": end,
                    "evidence_ids": evidence,
                    "reason": "Причина відбору",
                    "scores": scores,
                    "visual_review_required": False,
                }
            ]
        },
        ensure_ascii=False,
    )


# --- fakes -----------------------------------------------------------------------------------


def ok(content: str = EMPTY) -> ChatResult:
    return ChatResult(
        content=content, finish_reason="stop", cost=Decimal("0.002"), generation_id="gen-test"
    )


type Answer = ChatResult | ChatError


class FakeChat:
    """Answers the calls in order; a call past the script gets an empty selection. With
    ``hold_first`` the first call waits until ``release`` is set (at most 10 s), and ``started`` is
    set when it begins, so a test can act while a request is on the wire."""

    def __init__(self, *answers: Answer, hold_first: bool = False) -> None:
        self.bodies: list[Mapping[str, object]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self._hold_first = hold_first
        self._answers: deque[Answer] = deque(answers)
        self._lock = threading.Lock()

    def complete(self, body: Mapping[str, object]) -> ChatResult:
        with self._lock:
            self.bodies.append(body)
            first = len(self.bodies) == 1
            answer = self._answers.popleft() if self._answers else ok()
        if first and self._hold_first:
            self.started.set()
            self.release.wait(WAIT_S)
        if isinstance(answer, ChatError):
            raise answer
        return answer


class RecordingFactory:
    """A ``ChatClientFactory`` that records the key it was given and returns a fake client, or
    raises ``refusal``."""

    def __init__(self, *, refusal: Exception | None = None) -> None:
        self.keys: list[str | None] = []
        self.client = FakeChat(ok())
        self._refusal = refusal

    def __call__(self, key: str | None) -> ChatClient:
        self.keys.append(key)
        if self._refusal is not None:
            raise self._refusal
        return self.client


def _no_sleep(seconds: float, cancel: threading.Event) -> bool:
    return cancel.is_set()


# --- fixtures and helpers --------------------------------------------------------------------


@dataclass
class Lab:
    db: Database
    mode: Mode
    profiles: Mapping[str, AnalysisProfile] | None
    credentials: CredentialStore
    settings: SettingsService
    runs: AnalysisRunService
    jobs: AnalysisJobService
    chat: FakeChat
    source_id: str
    transcript_id: str


@dataclass(frozen=True)
class StoredJob:
    type: str
    state: str
    checkpoint: dict[str, Any]
    progress: dict[str, Any]


def add_transcript(
    session: Session, source_id: str, units: int, coverage: list[dict[str, Any]]
) -> str:
    """``units`` segments of 100 s: segment ``i`` is ``s{i}`` with ordinal ``ordinal_of(i)``."""
    segments = [
        NewSegment(
            id=f"s{index}",
            ordinal=ordinal_of(index),
            start_ms=index * SEGMENT_MS,
            end_ms=(index + 1) * SEGMENT_MS,
            text=f"репліка {index}",
        )
        for index in range(units)
    ]
    revision = TranscriptRepository(session).add_revision(
        source_id=source_id,
        origin="import",
        timing_level="segment",
        language="uk",
        duration_ms=SOURCE_MS,
        coverage=coverage,
        metadata={"format": "normalized_json", "offset_ms": 0},
        segments=segments,
    )
    return revision.id


def add_source_with_transcript(
    db: Database, units: int = 3, coverage: list[dict[str, Any]] | None = None
) -> tuple[str, str]:
    """A new project, source and transcript; ``(source_id, transcript_id)``."""
    with db.write_session() as session:
        project_id = add_project(session).id
        source_id = add_source(session, project_id, duration_ms=SOURCE_MS).id
        transcript_id = add_transcript(
            session, source_id, units, FULL_COVERAGE if coverage is None else coverage
        )
    return source_id, transcript_id


def newer_revision(db: Database, source_id: str) -> str:
    """A newer, empty revision of the source; returns its id."""
    with db.write_session() as session:
        revision = TranscriptRepository(session).add_revision(
            source_id=source_id,
            origin="import",
            timing_level="word",
            language="uk",
            duration_ms=SOURCE_MS,
            coverage=FULL_COVERAGE,
            metadata={"format": "normalized_json", "offset_ms": 0},
        )
        return revision.id


def add_job(db: Database, job_type: str) -> str:
    with db.write_session() as session:
        return JobRepository(session).add(type=job_type, checkpoint_json={"run_id": "x"}).id


def wire(
    db: Database,
    chat: FakeChat,
    mode: Mode,
    profiles: Mapping[str, AnalysisProfile] | None,
) -> tuple[CredentialStore, SettingsService, AnalysisRunService, AnalysisJobService]:
    credentials = CredentialStore()
    settings = SettingsService(db, credentials)
    runs = AnalysisRunService(
        db, SpendLedger(db), client=chat, sleeper=_no_sleep, jitter=lambda: 0.0
    )
    jobs = AnalysisJobService(db, runs, settings, credentials, mode=mode, profiles=profiles)
    return credentials, settings, runs, jobs


class Rig:
    """Builds labs over fresh databases and restarts them. ``close`` stops every service and
    waits for its worker."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._databases: list[Database] = []
        self._services: list[AnalysisJobService] = []
        self._chats: list[FakeChat] = []

    def build(
        self,
        *answers: Answer,
        units: int = 3,
        mode: Mode = "test",
        profiles: Mapping[str, AnalysisProfile] | None = None,
        hold_first: bool = False,
        coverage: list[dict[str, Any]] | None = None,
    ) -> Lab:
        db = open_database(self._root / f"data{len(self._databases)}")
        self._databases.append(db)
        source_id, transcript_id = add_source_with_transcript(db, units, coverage)
        return self._lab(db, source_id, transcript_id, mode, profiles, answers, hold_first)

    def restart(self, lab: Lab, *answers: Answer, hold_first: bool = False) -> Lab:
        """The same database and source after a restart: the new process has no key and no
        worker, and the old process's jobs are its business until ``recover``."""
        return self._lab(
            lab.db, lab.source_id, lab.transcript_id, lab.mode, lab.profiles, answers, hold_first
        )

    def _lab(
        self,
        db: Database,
        source_id: str,
        transcript_id: str,
        mode: Mode,
        profiles: Mapping[str, AnalysisProfile] | None,
        answers: tuple[Answer, ...],
        hold_first: bool,
    ) -> Lab:
        chat = FakeChat(*answers, hold_first=hold_first)
        self._chats.append(chat)
        credentials, settings, runs, jobs = wire(db, chat, mode, profiles)
        self._services.append(jobs)
        return Lab(
            db=db,
            mode=mode,
            profiles=profiles,
            credentials=credentials,
            settings=settings,
            runs=runs,
            jobs=jobs,
            chat=chat,
            source_id=source_id,
            transcript_id=transcript_id,
        )

    def close(self) -> None:
        for chat in self._chats:
            chat.release.set()
        for jobs in self._services:
            jobs.shutdown()
        for db in self._databases:
            db.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    current = Rig(tmp_path)
    try:
        yield current
    finally:
        current.close()


def start(
    lab: Lab,
    *,
    budget: str | int | float | None = "5",
    model: str | None = MODEL_ID,
    profile: object = PROFILE_JSON,
    source_id: str | None = None,
    transcript_id: str | None = None,
) -> CreatedAnalysisRun:
    return lab.jobs.create(
        source_id=source_id or lab.source_id,
        transcript_id=transcript_id or lab.transcript_id,
        profile=profile,
        model=model,
        budget_usd=budget,
    )


def estimate_of(
    lab: Lab,
    *,
    budget: str | None = None,
    source_id: str | None = None,
    transcript_id: str | None = None,
) -> AnalysisEstimate:
    return lab.jobs.estimate(
        source_id=source_id or lab.source_id,
        transcript_id=transcript_id or lab.transcript_id,
        profile=PROFILE_JSON,
        model=MODEL_ID,
        budget_usd=budget,
    )


def refusal(call: Callable[[], object]) -> ServiceError:
    with pytest.raises(ServiceError) as caught:
        call()
    return caught.value


def written(lab: Lab) -> tuple[int, int, int]:
    """(runs, windows, analysis jobs) stored so far: a refused request leaves all of them as they
    were."""
    with lab.db.session() as session:
        runs = AnalysisRunRepository(session)
        listed = runs.list_runs_for_source(lab.source_id)
        windows = sum(len(runs.list_windows(run.id)) for run in listed)
        jobs = JobRepository(session).list_by_type("analysis", ALL_JOB_STATES)
    return len(listed), windows, len(jobs)


def stored_job(db: Database, job_id: str) -> StoredJob:
    with db.session() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        return StoredJob(
            type=job.type,
            state=job.state,
            checkpoint=dict(job.checkpoint_json or {}),
            progress=dict(job.progress_json or {}),
        )


def stored_run(db: Database, run_id: str) -> AnalysisRun:
    with db.session() as session:
        run = AnalysisRunRepository(session).get_run(run_id)
        assert run is not None
        return run


def stored_windows(db: Database, run_id: str) -> list[AnalysisWindow]:
    with db.session() as session:
        return AnalysisRunRepository(session).list_windows(run_id)


def window_states(db: Database, run_id: str) -> list[str]:
    return [window.state for window in stored_windows(db, run_id)]


def clip_rows(db: Database, clip_ids: tuple[str, ...]) -> list[Clip]:
    clips: list[Clip] = []
    with db.session() as session:
        for clip_id in clip_ids:
            clip = session.get(Clip, clip_id)
            assert clip is not None
            clips.append(clip)
    return clips


def wait_for_job(db: Database, job_id: str, states: set[str]) -> JobView:
    deadline = time.monotonic() + WAIT_S
    while True:
        with db.session() as session:
            job = JobRepository(session).get(job_id)
            if job is not None and job.state in states:
                return job_view(job)
        assert time.monotonic() < deadline, f"job {job_id} did not reach {sorted(states)}"
        time.sleep(0.01)


def wait_for_stop(jobs: AnalysisJobService, job_id: str) -> None:
    """Returns once the cancel signal of the job's worker is set. ``shutdown`` sets it, and the
    worker checks it before its next window; no public call shows the signal, so the test reads
    it."""
    deadline = time.monotonic() + WAIT_S
    while True:
        event = jobs._events.get(job_id)
        if event is not None and event.is_set():
            return
        assert time.monotonic() < deadline, f"job {job_id} was not told to stop"
        time.sleep(0.005)


def shut_down_while_held(lab: Lab, job_id: str) -> None:
    """Shut the service down while its first request is held, then let that request finish."""
    stopper = threading.Thread(target=lab.jobs.shutdown)
    stopper.start()
    wait_for_stop(lab.jobs, job_id)
    lab.chat.release.set()
    stopper.join(WAIT_S)
    assert not stopper.is_alive()


# --- create: the order of the checks (ADR 041 §10) -------------------------------------------


@pytest.mark.parametrize(
    ("profile", "code", "field"),
    [
        ({**PROFILE_JSON, "categories": []}, "CATEGORIES_REQUIRED", "profile.categories"),
        ({**PROFILE_JSON, "max_candidates": 0}, "INVALID_MAX_CANDIDATES", "profile.max_candidates"),
        ("not an object", "PROFILE_INVALID", "profile"),
    ],
)
def test_a_bad_profile_is_422_naming_its_field_and_nothing_is_written(
    rig: Rig, profile: object, code: str, field: str
) -> None:
    lab = rig.build()
    error = refusal(lambda: start(lab, profile=profile))
    assert error.kind is ErrorKind.INVALID
    assert error.code == code
    assert error.details == {"field": field}
    assert written(lab) == (0, 0, 0)


@pytest.mark.parametrize(
    ("model", "code"),
    [(None, "ANALYSIS_MODEL_REQUIRED"), ("other/model", "ANALYSIS_MODEL_NOT_SUPPORTED")],
)
def test_the_model_must_be_given_and_must_be_in_the_table(
    rig: Rig, model: str | None, code: str
) -> None:
    lab = rig.build()
    error = refusal(lambda: start(lab, model=model))
    assert (error.kind, error.code, error.details) == (
        ErrorKind.INVALID,
        code,
        {"field": "model"},
    )
    assert written(lab) == (0, 0, 0)


def test_production_refuses_a_model_outside_the_table_and_writes_nothing(rig: Rig) -> None:
    lab = rig.build(mode="production")
    error = refusal(lambda: start(lab, model="other/model"))
    assert (error.kind, error.code) == (ErrorKind.INVALID, "ANALYSIS_MODEL_NOT_SUPPORTED")
    assert written(lab) == (0, 0, 0)


def test_production_accepts_the_probed_model_and_stops_on_the_missing_key(rig: Rig) -> None:
    lab = rig.build(mode="production")
    error = refusal(lambda: start(lab, model="anthropic/claude-haiku-5.5"))
    assert (error.kind, error.code) == (ErrorKind.CONFLICT, "API_KEY_REQUIRED")
    assert written(lab) == (0, 0, 0)
    assert lab.chat.bodies == []


@pytest.mark.parametrize(
    ("budget", "code"),
    [("0.001", "INVALID_RUN_BUDGET"), (True, "INVALID_RUN_BUDGET"), (None, "BUDGET_REQUIRED")],
)
def test_the_budget_follows_the_settings_rule_and_must_be_given(
    rig: Rig, budget: str | int | None, code: str
) -> None:
    lab = rig.build()
    error = refusal(lambda: start(lab, budget=budget))
    assert (error.kind, error.code, error.details) == (
        ErrorKind.INVALID,
        code,
        {"field": "budget_usd"},
    )
    assert written(lab) == (0, 0, 0)


def test_the_settings_give_the_model_and_the_budget_when_the_request_gives_none(
    rig: Rig,
) -> None:
    lab = rig.build()
    lab.settings.update({"analysis_model": MODEL_ID, "run_budget_usd": "2"})
    created = lab.jobs.create(
        source_id=lab.source_id, transcript_id=lab.transcript_id, profile=PROFILE_JSON
    )
    wait_for_job(lab.db, created.job_id, {"succeeded"})
    assert stored_job(lab.db, created.job_id).checkpoint == {
        "run_id": created.id,
        "budget_usd": "2",
    }


@pytest.mark.parametrize(("budget", "stored"), [(5, "5"), (0.5, "0.5"), ("2.50", "2.50")])
def test_a_numeric_budget_is_stored_as_its_decimal_text(
    rig: Rig, budget: str | int | float, stored: str
) -> None:
    lab = rig.build()
    created = start(lab, budget=budget)
    wait_for_job(lab.db, created.job_id, {"succeeded"})
    assert stored_job(lab.db, created.job_id).checkpoint["budget_usd"] == stored


def test_an_unknown_source_is_404(rig: Rig) -> None:
    lab = rig.build()
    error = refusal(lambda: start(lab, source_id="missing"))
    assert (error.kind, error.code) == (ErrorKind.NOT_FOUND, "SOURCE_NOT_FOUND")
    assert written(lab) == (0, 0, 0)


def test_an_unknown_transcript_is_404(rig: Rig) -> None:
    lab = rig.build()
    error = refusal(lambda: start(lab, transcript_id="missing"))
    assert (error.kind, error.code) == (ErrorKind.NOT_FOUND, "TRANSCRIPT_NOT_FOUND")
    assert written(lab) == (0, 0, 0)


def test_a_transcript_of_another_source_is_422_naming_the_transcript(rig: Rig) -> None:
    lab = rig.build()
    other_source, _ = add_source_with_transcript(lab.db)
    error = refusal(lambda: start(lab, source_id=other_source))
    assert error.code == "TRANSCRIPT_SOURCE_MISMATCH"
    assert error.details == {"field": "transcript_id"}
    assert written(lab) == (0, 0, 0)


def test_a_newer_revision_makes_the_transcript_stale_and_the_refusal_names_it(rig: Rig) -> None:
    lab = rig.build()
    newest = newer_revision(lab.db, lab.source_id)
    error = refusal(lambda: start(lab))
    assert (error.kind, error.code) == (ErrorKind.CONFLICT, "TRANSCRIPT_REVISION_STALE")
    assert error.details == {"latest_id": newest}
    assert written(lab) == (0, 0, 0)


def test_a_partial_transcript_needs_allow_partial(rig: Rig) -> None:
    partial: list[dict[str, Any]] = [
        {"start_ms": 0, "end_ms": 500_000, "state": "succeeded"},
        {"start_ms": 500_000, "end_ms": SOURCE_MS, "state": "failed"},
    ]
    lab = rig.build(coverage=partial)
    error = refusal(lambda: start(lab))
    assert (error.kind, error.code) == (ErrorKind.CONFLICT, "TRANSCRIPT_PARTIAL")
    assert written(lab) == (0, 0, 0)
    created = start(lab, profile={**PROFILE_JSON, "allow_partial": True})
    assert created.status == "queued"
    wait_for_job(lab.db, created.job_id, {"succeeded"})


def test_production_needs_the_key_before_anything_is_written(rig: Rig) -> None:
    lab = rig.build(mode="production", profiles={MODEL_ID: MODEL})
    error = refusal(lambda: start(lab))
    assert (error.kind, error.code) == (ErrorKind.CONFLICT, "API_KEY_REQUIRED")
    assert written(lab) == (0, 0, 0)
    lab.credentials.set_openrouter_key(KEY)
    created = start(lab)
    wait_for_job(lab.db, created.job_id, {"succeeded"})


def test_test_mode_needs_no_key(rig: Rig) -> None:
    lab = rig.build()
    assert not lab.credentials.has_openrouter_key
    created = start(lab)
    wait_for_job(lab.db, created.job_id, {"succeeded"})


def test_a_second_run_of_the_source_is_refused_while_one_is_open(rig: Rig) -> None:
    lab = rig.build(hold_first=True)
    first = start(lab)
    assert lab.chat.started.wait(WAIT_S)
    error = refusal(lambda: start(lab))
    assert (error.kind, error.code) == (ErrorKind.CONFLICT, "ANALYSIS_RUN_ACTIVE")
    assert error.details == {"run_id": first.id, "job_id": first.job_id}
    assert written(lab) == (1, 1, 1)
    lab.chat.release.set()
    wait_for_job(lab.db, first.job_id, {"succeeded"})


def test_shutdown_refuses_a_create_and_a_resume_with_503_and_writes_nothing(rig: Rig) -> None:
    lab = rig.build()
    lab.jobs.shutdown()
    error = refusal(lambda: start(lab))
    assert (error.kind, error.code, error.retryable) == (
        ErrorKind.UNAVAILABLE,
        "SERVICE_STOPPING",
        True,
    )
    assert written(lab) == (0, 0, 0)
    resume_error = refusal(lambda: lab.jobs.resume("missing", retry_unknown=False))
    assert resume_error.code == "SERVICE_STOPPING"


def test_the_run_and_its_job_are_written_together_or_not_at_all(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = rig.build()

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("the job row cannot be written")

    # The run and its windows are already in the transaction when the job row fails.
    monkeypatch.setattr(JobRepository, "add", fail)
    with pytest.raises(RuntimeError, match="job row"):
        start(lab)
    assert written(lab) == (0, 0, 0)


def test_a_created_run_writes_its_run_windows_and_job_with_a_checkpoint_of_run_and_budget(
    rig: Rig,
) -> None:
    lab = rig.build()
    created = start(lab)
    job = wait_for_job(lab.db, created.job_id, {"succeeded"})
    assert written(lab) == (1, 1, 1)
    stored = stored_job(lab.db, created.job_id)
    assert stored.type == "analysis"
    assert stored.checkpoint == {"run_id": created.id, "budget_usd": "5"}
    assert job.progress == {
        "stage": "done",
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "unknown_outcome": 0,
    }


def test_the_key_is_in_no_row_of_the_job_or_the_run(rig: Rig) -> None:
    lab = rig.build()
    lab.credentials.set_openrouter_key(KEY)
    created = start(lab)
    wait_for_job(lab.db, created.job_id, {"succeeded"})
    stored = stored_job(lab.db, created.job_id)
    run = stored_run(lab.db, created.id)
    rows: list[object] = [
        stored.checkpoint,
        stored.progress,
        run.profile_json,
        run.price_snapshot_json,
        run.diagnostics_json,
        [window.result_json for window in stored_windows(lab.db, created.id)],
    ]
    for row in rows:
        assert KEY not in json.dumps(row, ensure_ascii=False)


# --- estimate --------------------------------------------------------------------------------


def test_an_estimate_describes_the_plan_and_writes_and_sends_nothing(rig: Rig) -> None:
    lab = rig.build()
    estimate = estimate_of(lab, budget="1")
    assert estimate.model == MODEL_ID
    assert estimate.unit_level == "segment"
    assert (estimate.unit_count, estimate.window_count) == (3, 1)
    assert estimate.prompt_usd_per_mtok == str(MODEL.prompt_usd_per_mtok)
    assert estimate.completion_usd_per_mtok == str(MODEL.completion_usd_per_mtok)
    assert estimate.max_tokens == MODEL.max_tokens
    assert estimate.price_basis == "test"
    assert (estimate.budget_usd, estimate.within_budget) == ("1", True)
    assert written(lab) == (0, 0, 0)
    assert lab.chat.bodies == []
    created = start(lab, budget="1")
    wait_for_job(lab.db, created.job_id, {"succeeded"})
    # The estimate is the reservation of the first request the run actually sends.
    assert reservation_micro_usd(lab.chat.bodies[0], MODEL) == estimate.estimate_micro_usd


def test_an_estimate_says_whether_the_plan_fits_the_budget(rig: Rig) -> None:
    lab = rig.build()
    cent = estimate_of(lab, budget="0.01")
    # Premise of the test: one request does not fit one cent.
    assert cent.estimate_micro_usd > 10_000
    assert cent.within_budget is False
    assert estimate_of(lab, budget="1").within_budget is True
    no_budget = estimate_of(lab)
    assert (no_budget.budget_usd, no_budget.within_budget) == (None, None)


def test_an_estimate_refuses_an_unknown_or_foreign_transcript(rig: Rig) -> None:
    lab = rig.build()
    other_source, _ = add_source_with_transcript(lab.db)
    unknown = refusal(lambda: estimate_of(lab, transcript_id="missing"))
    assert (unknown.kind, unknown.code) == (ErrorKind.NOT_FOUND, "TRANSCRIPT_NOT_FOUND")
    foreign = refusal(lambda: estimate_of(lab, source_id=other_source))
    assert (foreign.kind, foreign.code) == (ErrorKind.INVALID, "TRANSCRIPT_SOURCE_MISMATCH")
    assert written(lab) == (0, 0, 0)


# --- reads -----------------------------------------------------------------------------------


def test_get_run_reports_windows_clips_money_and_staleness(rig: Rig) -> None:
    lab = rig.build(ok(candidate("s0", "s1", ["s0"])))
    created = start(lab)
    job = wait_for_job(lab.db, created.job_id, {"succeeded"})
    view = lab.jobs.get_run(created.id)
    assert (view.status, view.error_code, view.job_id) == ("completed", None, created.job_id)
    assert view.windows == WindowCounts(
        total=1, succeeded=1, failed=0, unknown_outcome=0, pending=0
    )
    assert len(view.clip_ids) == 1
    assert view.committed_micro_usd == 2000
    assert view.stale is False
    assert view.diagnostics_summary == {"dropped": {}, "rejected": {}, "suppressed": {}}
    assert job.state == "succeeded"
    newer_revision(lab.db, lab.source_id)
    assert lab.jobs.get_run(created.id).stale is True


def test_get_run_of_an_unknown_run_is_404(rig: Rig) -> None:
    lab = rig.build()
    error = refusal(lambda: lab.jobs.get_run("missing"))
    assert (error.kind, error.code) == (ErrorKind.NOT_FOUND, "ANALYSIS_RUN_NOT_FOUND")


def test_clip_ids_are_in_the_order_the_run_kept_them(rig: Rig) -> None:
    # Window 0 scores 75 (rank), windows 1 and 2 score 100. The run keeps window 1 first (the
    # best rank, the earlier start), then window 2, then window 0, so the order is not the order
    # of the times and not the order of the ids.
    lab = rig.build(
        ok(candidate("s0", "s1", ["s0"], score=3)),
        ok(candidate("s4", "s5", ["s4"])),
        ok(candidate("s8", "s9", ["s8"])),
        units=10,
    )
    created = start(lab)
    wait_for_job(lab.db, created.job_id, {"succeeded"})
    view = lab.jobs.get_run(created.id)
    assert view.windows.total == 3
    clips = clip_rows(lab.db, view.clip_ids)
    assert [clip.evidence_start_ms for clip in clips] == [400_000, 800_000, 0]


def test_list_runs_is_newest_first_and_an_unknown_source_is_404(rig: Rig) -> None:
    lab = rig.build()
    first = start(lab)
    wait_for_job(lab.db, first.job_id, {"succeeded"})
    time.sleep(0.05)  # the clock of Windows ticks about every 15 ms; the order needs distinct times
    second = start(lab)
    wait_for_job(lab.db, second.job_id, {"succeeded"})
    items = lab.jobs.list_runs(lab.source_id)
    assert [item.id for item in items] == [second.id, first.id]
    assert [item.job_id for item in items] == [second.job_id, first.job_id]
    assert {item.status for item in items} == {"completed"}
    error = refusal(lambda: lab.jobs.list_runs("missing"))
    assert (error.kind, error.code) == (ErrorKind.NOT_FOUND, "SOURCE_NOT_FOUND")


# --- cancel ----------------------------------------------------------------------------------


def test_cancelling_a_queued_job_cancels_its_run_and_windows_and_it_never_starts(
    rig: Rig,
) -> None:
    lab = rig.build(hold_first=True)
    first = start(lab)
    assert lab.chat.started.wait(WAIT_S)
    other_source, other_transcript = add_source_with_transcript(lab.db)
    queued = start(lab, source_id=other_source, transcript_id=other_transcript)
    assert stored_job(lab.db, queued.job_id).state == "queued"
    view = lab.jobs.cancel(queued.job_id)
    assert (view.state, view.progress) == ("cancelled", {"stage": "cancelled"})
    assert stored_run(lab.db, queued.id).status == "cancelled"
    assert window_states(lab.db, queued.id) == ["cancelled"]
    lab.chat.release.set()
    wait_for_job(lab.db, first.job_id, {"succeeded"})
    assert stored_job(lab.db, queued.job_id).state == "cancelled"
    assert len(lab.chat.bodies) == 1


def test_cancelling_a_running_job_stops_before_its_next_window_and_keeps_the_answer(
    rig: Rig,
) -> None:
    lab = rig.build(ok(candidate("s0", "s1", ["s0"])), units=10, hold_first=True)
    created = start(lab)
    assert lab.chat.started.wait(WAIT_S)
    assert lab.jobs.cancel(created.job_id).state == "cancelling"
    # A repeated cancel of a job that is already cancelling changes nothing.
    assert lab.jobs.cancel(created.job_id).state == "cancelling"
    lab.chat.release.set()
    wait_for_job(lab.db, created.job_id, {"cancelled"})
    assert stored_run(lab.db, created.id).status == "cancelled"
    assert window_states(lab.db, created.id) == ["succeeded", "cancelled", "cancelled"]
    assert len(lab.chat.bodies) == 1


def test_cancel_refuses_a_finished_job_an_unknown_one_and_a_job_of_another_kind(
    rig: Rig,
) -> None:
    lab = rig.build()
    created = start(lab)
    wait_for_job(lab.db, created.job_id, {"succeeded"})
    finished = refusal(lambda: lab.jobs.cancel(created.job_id))
    assert (finished.kind, finished.code) == (ErrorKind.CONFLICT, "JOB_NOT_CANCELLABLE")
    assert refusal(lambda: lab.jobs.cancel("missing")).code == "JOB_NOT_FOUND"
    transcription_job = add_job(lab.db, "transcription")
    assert refusal(lambda: lab.jobs.cancel(transcription_job)).code == "JOB_NOT_FOUND"


# --- resume ----------------------------------------------------------------------------------


def test_resume_continues_an_interrupted_job_from_its_answered_windows(rig: Rig) -> None:
    lab = rig.build(ok(candidate("s0", "s1", ["s0"])), units=10, hold_first=True)
    created = start(lab)
    assert lab.chat.started.wait(WAIT_S)
    shut_down_while_held(lab, created.job_id)
    assert stored_job(lab.db, created.job_id).state == "interrupted"
    assert stored_run(lab.db, created.id).status == "interrupted"
    assert window_states(lab.db, created.id) == ["succeeded", "pending", "pending"]
    assert len(lab.chat.bodies) == 1

    restarted = rig.restart(lab, ok(), ok())
    restarted.jobs.recover()
    assert stored_job(restarted.db, created.job_id).state == "interrupted"
    assert window_states(restarted.db, created.id) == ["succeeded", "pending", "pending"]
    assert restarted.chat.bodies == []
    resumed = restarted.jobs.resume(created.job_id, retry_unknown=False)
    assert resumed.state == "queued"
    wait_for_job(restarted.db, created.job_id, {"succeeded"})
    assert window_states(restarted.db, created.id) == ["succeeded", "succeeded", "succeeded"]
    assert len(restarted.chat.bodies) == 2
    assert len(restarted.jobs.get_run(created.id).clip_ids) == 1


def test_resume_refuses_a_finished_job_and_a_job_of_another_kind(rig: Rig) -> None:
    lab = rig.build()
    created = start(lab)
    wait_for_job(lab.db, created.job_id, {"succeeded"})
    finished = refusal(lambda: lab.jobs.resume(created.job_id, retry_unknown=False))
    assert (finished.kind, finished.code) == (ErrorKind.CONFLICT, "JOB_NOT_RESUMABLE")
    transcription_job = add_job(lab.db, "transcription")
    other = refusal(lambda: lab.jobs.resume(transcription_job, retry_unknown=False))
    assert other.code == "JOB_NOT_RESUMABLE"


def test_an_unknown_outcome_is_sent_again_only_with_an_explicit_decision(rig: Rig) -> None:
    lab = rig.build(ChatError("unknown_outcome"))
    created = start(lab)
    failed = wait_for_job(lab.db, created.job_id, {"failed"})
    assert failed.error is not None
    assert failed.error["code"] == "ANALYSIS_UNKNOWN_OUTCOME"
    error = refusal(lambda: lab.jobs.resume(created.job_id, retry_unknown=False))
    assert (error.kind, error.code) == (ErrorKind.CONFLICT, "UNKNOWN_OUTCOME_REQUIRES_DECISION")
    assert error.details == {"count": 1}
    assert stored_job(lab.db, created.job_id).state == "failed"
    assert len(lab.chat.bodies) == 1
    lab.jobs.resume(created.job_id, retry_unknown=True)
    wait_for_job(lab.db, created.job_id, {"succeeded"})
    assert window_states(lab.db, created.id) == ["succeeded"]
    assert len(lab.chat.bodies) == 2


def test_a_run_stopped_at_its_budget_is_resumed_with_a_higher_one(rig: Rig) -> None:
    lab = rig.build()
    # Premise of the test: one request does not fit one cent.
    assert estimate_of(lab, budget="1").estimate_micro_usd > 10_000
    created = start(lab, budget="0.01")
    failed = wait_for_job(lab.db, created.job_id, {"failed"})
    assert failed.error is not None
    assert failed.error["code"] == "BUDGET_EXCEEDED"
    assert lab.chat.bodies == []
    lab.jobs.resume(created.job_id, retry_unknown=False, budget_usd="1")
    wait_for_job(lab.db, created.job_id, {"succeeded"})
    assert stored_job(lab.db, created.job_id).checkpoint["budget_usd"] == "1"
    assert len(lab.chat.bodies) == 1


def test_resume_refuses_a_budget_out_of_range_and_changes_nothing(rig: Rig) -> None:
    lab = rig.build()
    created = start(lab, budget="0.01")
    wait_for_job(lab.db, created.job_id, {"failed"})
    error = refusal(
        lambda: lab.jobs.resume(created.job_id, retry_unknown=False, budget_usd="0.001")
    )
    assert (error.kind, error.code) == (ErrorKind.INVALID, "INVALID_RUN_BUDGET")
    assert error.details == {"field": "budget_usd"}
    stored = stored_job(lab.db, created.job_id)
    assert (stored.state, stored.checkpoint["budget_usd"]) == ("failed", "0.01")


def test_resume_is_refused_while_another_job_of_the_source_is_open(rig: Rig) -> None:
    lab = rig.build(hold_first=True)
    failed = start(lab, budget="0.01")
    wait_for_job(lab.db, failed.job_id, {"failed"})
    opened = start(lab, budget="5")
    assert lab.chat.started.wait(WAIT_S)
    error = refusal(lambda: lab.jobs.resume(failed.job_id, retry_unknown=False, budget_usd="1"))
    assert (error.kind, error.code) == (ErrorKind.CONFLICT, "ANALYSIS_RUN_ACTIVE")
    assert error.details == {"run_id": opened.id, "job_id": opened.job_id}
    assert stored_job(lab.db, failed.job_id).state == "failed"
    lab.chat.release.set()
    wait_for_job(lab.db, opened.job_id, {"succeeded"})


def test_production_resume_needs_the_key_and_a_refusal_keeps_the_budget(rig: Rig) -> None:
    lab = rig.build(mode="production", profiles={MODEL_ID: MODEL})
    lab.credentials.set_openrouter_key(KEY)
    created = start(lab, budget="0.01")
    wait_for_job(lab.db, created.job_id, {"failed"})
    lab.credentials.clear_openrouter_key()
    error = refusal(lambda: lab.jobs.resume(created.job_id, retry_unknown=False, budget_usd="1"))
    assert (error.kind, error.code) == (ErrorKind.CONFLICT, "API_KEY_REQUIRED")
    stored = stored_job(lab.db, created.job_id)
    assert (stored.state, stored.checkpoint["budget_usd"]) == ("failed", "0.01")
    lab.credentials.set_openrouter_key(KEY)
    lab.jobs.resume(created.job_id, retry_unknown=False, budget_usd="1")
    wait_for_job(lab.db, created.job_id, {"succeeded"})


# --- recovery and shutdown -------------------------------------------------------------------


def test_recovery_settles_the_run_a_restart_left_behind_and_resume_continues_the_queued_one(
    rig: Rig,
) -> None:
    lab = rig.build(hold_first=True)
    running = start(lab)
    assert lab.chat.started.wait(WAIT_S)
    other_source, other_transcript = add_source_with_transcript(lab.db)
    queued = start(lab, source_id=other_source, transcript_id=other_transcript)
    # The process dies here. The start-up of the next process makes the unfinished jobs
    # interrupted (``JobRunner.recover``), then ``AnalysisJobService.recover`` runs.
    with lab.db.write_session() as session:
        JobRepository(session).interrupt_unfinished()
    restarted = rig.restart(lab, ok())
    restarted.jobs.recover()

    # The window whose request may have gone out is undecided, not sent again.
    assert stored_run(lab.db, running.id).status == "interrupted"
    assert window_states(lab.db, running.id) == ["unknown_outcome"]
    with lab.db.session() as session:
        attempts = AnalysisRunRepository(session).list_attempts(
            stored_windows(lab.db, running.id)[0].id
        )
        assert [attempt.state for attempt in attempts] == ["unknown_outcome"]
    # The queued run never sent anything: its window is pending again.
    assert stored_run(lab.db, queued.id).status == "interrupted"
    assert window_states(lab.db, queued.id) == ["pending"]
    assert restarted.chat.bodies == []

    restarted.jobs.resume(queued.job_id, retry_unknown=False)
    wait_for_job(restarted.db, queued.job_id, {"succeeded"})
    assert len(restarted.chat.bodies) == 1
    assert window_states(restarted.db, running.id) == ["unknown_outcome"]


@pytest.mark.parametrize(
    ("attempt_state", "error_code", "paid_usd"),
    [("succeeded", None, Decimal("0.002")), ("failed", "ANALYSIS_INVALID_OUTPUT", None)],
)
def test_after_a_restart_a_paid_answer_is_sent_again_only_with_an_explicit_decision(
    rig: Rig, attempt_state: str, error_code: str | None, paid_usd: Decimal | None
) -> None:
    # ADR 041 §22 (7): the call died after its answer was paid and recorded, before the window took
    # it. The restart leaves the window unknown_outcome, and a resume without retry_unknown sends
    # no request for it.
    lab = rig.build()
    run_id = lab.runs.create_run(
        source_id=lab.source_id,
        transcript_id=lab.transcript_id,
        search_profile=parse_search_profile(PROFILE_JSON),
        analysis_profile=MODEL,
    )
    ledger = SpendLedger(lab.db)
    entry = ledger.reserve(run_id, "dead-request", MODEL_ID, Decimal("0.0005"), Decimal("5"), {})
    ledger.complete(entry.id, paid_usd)
    with lab.db.write_session() as session:
        runs = AnalysisRunRepository(session)
        runs.update_run(run_id, status="running")
        (window,) = runs.list_windows(run_id)
        runs.update_window(window.id, state="running", attempts=1)
        runs.add_attempt(
            window_id=window.id,
            kind="first",
            state=attempt_state,
            request_id="dead-request",
            spend_entry_id=entry.id,
            error_code=error_code,
        )
        job = JobRepository(session).add(
            type="analysis",
            state="interrupted",
            checkpoint_json={"run_id": run_id, "budget_usd": "5"},
        )
        job_id = job.id
    lab.jobs.recover()
    assert stored_run(lab.db, run_id).status == "interrupted"
    assert window_states(lab.db, run_id) == ["unknown_outcome"]

    error = refusal(lambda: lab.jobs.resume(job_id, retry_unknown=False))
    assert (error.kind, error.code, error.details) == (
        ErrorKind.CONFLICT,
        "UNKNOWN_OUTCOME_REQUIRES_DECISION",
        {"count": 1},
    )
    assert lab.chat.bodies == []
    assert window_states(lab.db, run_id) == ["unknown_outcome"]
    assert stored_job(lab.db, job_id).state == "interrupted"


def test_shutdown_interrupts_a_running_job_before_its_next_window(rig: Rig) -> None:
    lab = rig.build(ok(candidate("s0", "s1", ["s0"])), units=10, hold_first=True)
    created = start(lab)
    assert lab.chat.started.wait(WAIT_S)
    shut_down_while_held(lab, created.job_id)
    assert stored_job(lab.db, created.job_id).state == "interrupted"
    assert stored_run(lab.db, created.id).status == "interrupted"
    assert window_states(lab.db, created.id) == ["succeeded", "pending", "pending"]
    assert len(lab.chat.bodies) == 1


def test_an_authorization_error_stops_the_run_and_fails_its_job_with_the_code(rig: Rig) -> None:
    lab = rig.build(ChatError("auth"), units=10)
    created = start(lab)
    failed = wait_for_job(lab.db, created.job_id, {"failed"})
    assert failed.error is not None
    assert failed.error["code"] == "ANALYSIS_AUTH"
    run = stored_run(lab.db, created.id)
    assert (run.status, run.error_code) == ("failed", "ANALYSIS_AUTH")
    assert window_states(lab.db, created.id) == ["failed", "pending", "pending"]
    assert len(lab.chat.bodies) == 1


@pytest.mark.parametrize(
    ("answers", "code", "message"),
    [
        (
            (ChatError("upstream"), ChatError("upstream"), ChatError("upstream")),
            "ANALYSIS_UPSTREAM",
            "Провайдер моделі тимчасово недоступний. Відновіть аналіз пізніше",
        ),
        (
            (ChatError("invalid_request"),),
            "ANALYSIS_REQUEST_REJECTED",
            "Провайдер відхилив запит аналізу: модель може не підтримувати "
            "структуровану відповідь. Оберіть іншу перевірену модель",
        ),
        (
            (ChatError("invalid_response"), ChatError("invalid_response")),
            "ANALYSIS_INVALID_OUTPUT",
            "Модель двічі повернула відповідь, яку не вдалося перевірити. Моменти не збережено; "
            "відновлення надішле частину повторно (платно)",
        ),
    ],
)
def test_a_failed_job_gives_the_advice_text_of_its_provider_code(
    rig: Rig, answers: tuple[ChatError, ...], code: str, message: str
) -> None:
    # ADR 041 §22 (6): each of these codes has its own text on the job, word for word.
    lab = rig.build(*answers)
    created = start(lab)
    failed = wait_for_job(lab.db, created.job_id, {"failed"})
    assert failed.error == {"code": code, "message": message}
    run = stored_run(lab.db, created.id)
    assert (run.status, run.error_code) == ("failed", code)


# --- the keyed client (the key is read when a request is sent) -------------------------------


def test_the_keyed_client_refuses_in_production_without_a_key_and_sends_nothing() -> None:
    factory = RecordingFactory()
    client = KeyedChatClient(CredentialStore(), factory, mode="production")
    with pytest.raises(ChatError) as caught:
        client.complete(BODY)
    assert caught.value.kind == "auth"
    assert factory.keys == []
    assert factory.client.bodies == []


def test_the_keyed_client_reads_the_key_at_each_request_and_gives_none_in_test_mode() -> None:
    credentials = CredentialStore()
    factory = RecordingFactory()
    client = KeyedChatClient(credentials, factory, mode="test")
    assert client.complete(BODY).content == EMPTY
    credentials.set_openrouter_key(KEY)
    client.complete(BODY)
    assert factory.keys == [None, KEY]
    assert len(factory.client.bodies) == 2
    assert KEY not in repr(client)


@pytest.mark.parametrize(
    "failure",
    [ConfigError("the mode has no provider"), ValueError("the key is refused")],
)
def test_a_factory_refusal_is_invalid_request_and_leaves_the_key_out(failure: Exception) -> None:
    credentials = CredentialStore()
    credentials.set_openrouter_key(KEY)
    factory = RecordingFactory(refusal=failure)
    client = KeyedChatClient(credentials, factory, mode="test")
    with pytest.raises(ChatError) as caught:
        client.complete(BODY)
    assert caught.value.kind == "invalid_request"
    assert KEY not in str(caught.value)
    assert factory.keys == [KEY]
    assert factory.client.bodies == []
