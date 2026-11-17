"""AI selection run orchestration (ADR 041 §4, §5, §9, §17, §18): windows in order, one request per
attempt, one repair built by the planner, retries, stops, unknown outcomes, budget, cancellation and
recovery.

The chat client, the clock and the sleeper are fakes: nothing is sent, nothing is paid for and
nothing waits. Transcripts are written straight into a temporary workspace database.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.adapters.openrouter_chat import (
    REPAIR_SYSTEM_PROMPT,
    SELECT_SYSTEM_PROMPT,
    AnalysisProfile,
    ChatError,
    ChatResult,
    analysis_profiles,
)
from clipforge.domain.search_profile import SearchProfile, parse_search_profile
from clipforge.services import analysis_runs
from clipforge.services.analysis_runs import AnalysisRunOutcome, AnalysisRunService
from clipforge.services.errors import ErrorKind, ServiceError
from clipforge.services.spend import SpendLedger
from clipforge.storage import (
    AnalysisAttempt,
    AnalysisRun,
    AnalysisRunRepository,
    AnalysisWindow,
    Clip,
    Database,
    NewSegment,
    SpendEntry,
    SpendRepository,
    TranscriptRepository,
    open_database,
)

from .test_storage_support import add_project, add_source

MODEL: AnalysisProfile = analysis_profiles("test")["test/selector"]
MODEL_ID = "test/selector"
PROFILE: SearchProfile = parse_search_profile(
    {
        "categories": ["funny", "fails"],
        "custom_instruction": "",
        "min_duration_ms": 5_000,
        "max_duration_ms": 600_000,
        "max_candidates": 20,
        "allow_partial": False,
    }
)
SOURCE_MS = 1_000_000
SEGMENT_MS = 100_000
EMPTY = '{"candidates":[]}'
SCORES = {
    "clarity": 4,
    "hook": 4,
    "completeness": 4,
    "emotion": 4,
    "novelty": 4,
    "relevance": 4,
}


def ordinal_of(index: int) -> int:
    """Gaps between the ordinals, so that the mapping back to positions is really exercised."""
    return 5 + 10 * index


def candidate(start: str, end: str, evidence: list[str]) -> str:
    """A valid answer with one candidate; its ids must lie in the core of the window."""
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
                    "scores": SCORES,
                    "visual_review_required": False,
                }
            ]
        },
        ensure_ascii=False,
    )


# --- fakes -------------------------------------------------------------------------------------


def ok(
    content: str = EMPTY, *, cost: Decimal | None = Decimal("0.002"), finish: str = "stop"
) -> ChatResult:
    return ChatResult(content=content, finish_reason=finish, cost=cost, generation_id="gen-test")


type Answer = ChatResult | ChatError | Callable[[Mapping[str, object]], ChatResult]


class FakeChat:
    """Answers the calls in order; a call past the script gets an empty selection."""

    def __init__(self, *answers: Answer) -> None:
        self.bodies: list[Mapping[str, object]] = []
        self._answers: deque[Answer] = deque(answers)
        self._lock = threading.Lock()

    def complete(self, body: Mapping[str, object]) -> ChatResult:
        with self._lock:
            self.bodies.append(body)
            answer = self._answers.popleft() if self._answers else ok()
        if isinstance(answer, ChatError):
            raise answer
        if isinstance(answer, ChatResult):
            return answer
        return answer(body)


class FakeTime:
    """A clock that moves only when the sleeper is called; ``on_sleep`` runs inside every sleep."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[], None] | None = None
        self._lock = threading.Lock()

    def clock(self) -> float:
        with self._lock:
            return self.now

    def sleeper(self, seconds: float, cancel: threading.Event) -> bool:
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()
        return cancel.is_set()


# --- harness -----------------------------------------------------------------------------------


@dataclass
class Harness:
    db: Database
    ledger: SpendLedger
    chat: FakeChat
    time: FakeTime
    service: AnalysisRunService
    source_id: str
    transcript_id: str
    run_id: str

    def run(
        self,
        *,
        ceiling: str = "10",
        cancel: threading.Event | None = None,
        retry_unknown: bool = False,
    ) -> AnalysisRunOutcome:
        return self.service.run(
            self.run_id,
            analysis_profile=MODEL,
            ceiling_usd=Decimal(ceiling),
            cancel=cancel if cancel is not None else threading.Event(),
            retry_unknown=retry_unknown,
        )

    def windows(self) -> list[AnalysisWindow]:
        with self.db.session() as session:
            return AnalysisRunRepository(session).list_windows(self.run_id)

    def attempts(self, window: AnalysisWindow) -> list[AnalysisAttempt]:
        with self.db.session() as session:
            return AnalysisRunRepository(session).list_attempts(window.id)

    def entries(self) -> list[SpendEntry]:
        with self.db.session() as session:
            return SpendRepository(session).list_for_run(self.run_id)

    def run_row(self) -> AnalysisRun:
        with self.db.session() as session:
            row = AnalysisRunRepository(session).get_run(self.run_id)
        assert row is not None
        return row

    def clips(self) -> list[Clip]:
        with self.db.session() as session:
            return list(session.scalars(select(Clip).where(Clip.analysis_run_id == self.run_id)))


MakeHarness = Callable[..., Harness]


def _add_transcript(session: Session, source_id: str, units: int) -> str:
    """``units`` segments of 100 s each: segment ``i`` is ``s{i}``, ordinal ``ordinal_of(i)``."""
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
        timing_level="segment" if units else "word",
        language="uk",
        duration_ms=SOURCE_MS,
        coverage=[{"start_ms": 0, "end_ms": SOURCE_MS, "state": "succeeded"}],
        metadata={"format": "normalized_json", "offset_ms": 0},
        segments=segments,
    )
    return revision.id


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "data")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def make(db: Database) -> MakeHarness:
    """A harness over a transcript of ``units`` segments (3 units make one window, 10 make three:
    cores ``0..3``, ``4..7`` and ``8..9`` by position) and the given answers, in call order."""

    def build(units: int = 3, *answers: Answer) -> Harness:
        with db.write_session() as session:
            project_id = add_project(session).id
            source_id = add_source(session, project_id, duration_ms=SOURCE_MS).id
            transcript_id = _add_transcript(session, source_id, units)
        ledger = SpendLedger(db)
        chat = FakeChat(*answers)
        fake_time = FakeTime()
        service = AnalysisRunService(
            db,
            ledger,
            client=chat,
            clock=fake_time.clock,
            sleeper=fake_time.sleeper,
            jitter=lambda: 0.0,
        )
        run_id = service.create_run(
            source_id=source_id,
            transcript_id=transcript_id,
            search_profile=PROFILE,
            analysis_profile=MODEL,
        )
        return Harness(
            db=db,
            ledger=ledger,
            chat=chat,
            time=fake_time,
            service=service,
            source_id=source_id,
            transcript_id=transcript_id,
            run_id=run_id,
        )

    return build


def service_error(call: Callable[[], object]) -> ServiceError:
    with pytest.raises(ServiceError) as caught:
        call()
    return caught.value


def _message(body: Mapping[str, object], index: int) -> Mapping[str, object]:
    messages = body["messages"]
    assert isinstance(messages, list)
    message = messages[index]
    assert isinstance(message, dict)
    return message


def _system(body: Mapping[str, object]) -> object:
    return _message(body, 0)["content"]


def _user(body: Mapping[str, object]) -> dict[str, Any]:
    content = _message(body, 1)["content"]
    assert isinstance(content, str)
    loaded = json.loads(content)
    assert isinstance(loaded, dict)
    return loaded


def _states(outcome: AnalysisRunOutcome) -> list[tuple[str, str | None]]:
    return [(window.state, window.error_code) for window in outcome.windows]


# --- a run ----------------------------------------------------------------------------------------


def test_answered_windows_become_clips_and_the_run_completes_with_the_paid_costs(
    make: MakeHarness,
) -> None:
    h = make(10, ok(candidate("s1", "s2", ["s1"])), ok(), ok())

    outcome = h.run()

    assert outcome.status == "completed"
    assert outcome.error_code is None
    assert _states(outcome) == [("succeeded", None)] * 3
    assert outcome.committed_micro_usd == 3 * 2_000
    assert len(h.chat.bodies) == 3
    assert _system(h.chat.bodies[0]) == SELECT_SYSTEM_PROMPT
    (clip,) = h.clips()
    assert outcome.clip_ids == (clip.id,)
    assert clip.analysis_run_id == h.run_id
    assert clip.review_status == "suggested"
    # The evidence runs from the start unit's start to the end unit's end: s1 starts at 100 s and
    # s2 ends at 300 s.
    assert (clip.evidence_start_ms, clip.evidence_end_ms) == (100_000, 300_000)
    assert h.run_row().status == "completed"


def test_an_unusable_answer_gets_one_repair_request_that_carries_the_errors(
    make: MakeHarness,
) -> None:
    h = make(3, ok("не JSON"), ok(candidate("s1", "s2", ["s1"])))

    outcome = h.run()

    assert outcome.status == "completed"
    (window,) = outcome.windows
    assert (window.state, window.attempts, window.repair_used) == ("succeeded", 2, True)
    _first, repair = h.chat.bodies
    assert _system(repair) == REPAIR_SYSTEM_PROMPT
    user = _user(repair)
    assert user["previous_output"] == "не JSON"
    assert user["validation_errors"]
    assert len(outcome.clip_ids) == 1


def test_an_answer_cut_at_the_token_limit_is_repaired(make: MakeHarness) -> None:
    cut = candidate("s1", "s2", ["s1"])
    h = make(3, ok(cut, finish="length"), ok(EMPTY))

    outcome = h.run()

    assert outcome.status == "completed"
    assert _user(h.chat.bodies[1])["previous_output"] == cut


def test_two_unusable_answers_fail_the_window_and_the_run_and_a_later_run_starts_it_again(
    make: MakeHarness,
) -> None:
    h = make(3, ok("не JSON"), ok("ще не JSON"))

    failed = h.run()

    assert failed.status == "failed"
    assert failed.error_code == "ANALYSIS_INVALID_OUTPUT"
    assert _states(failed) == [("failed", "ANALYSIS_INVALID_OUTPUT")]
    assert failed.windows[0].repair_used is True
    assert failed.clip_ids == ()
    assert h.clips() == []
    assert len(h.chat.bodies) == 2

    resumed = h.run()

    assert resumed.status == "completed"
    assert len(h.chat.bodies) == 3
    assert resumed.windows[0].repair_used is False


def test_a_rate_limit_waits_for_retry_after_and_the_request_is_repeated(
    make: MakeHarness,
) -> None:
    h = make(3, ChatError("rate_limited", status=429, retry_after_s=7.0), ok())

    outcome = h.run()

    assert outcome.status == "completed"
    assert h.time.sleeps == [7.0]
    assert len(h.chat.bodies) == 2
    assert sorted(entry.state for entry in h.entries()) == ["completed", "released"]
    assert outcome.committed_micro_usd == 2_000
    assert outcome.windows[0].attempts == 2


def test_three_upstream_errors_end_the_window_with_analysis_upstream(make: MakeHarness) -> None:
    h = make(
        3,
        ChatError("upstream", status=503),
        ChatError("upstream", status=503),
        ChatError("upstream", status=503),
    )

    outcome = h.run()

    assert outcome.status == "failed"
    assert outcome.error_code == "ANALYSIS_UPSTREAM"
    assert _states(outcome) == [("failed", "ANALYSIS_UPSTREAM")]
    assert outcome.windows[0].attempts == 3
    assert len(h.chat.bodies) == 3
    assert h.time.sleeps == [1.0, 2.0]
    assert outcome.committed_micro_usd == 0


def test_an_authorization_error_stops_the_run_before_the_next_window(make: MakeHarness) -> None:
    h = make(10, ChatError("auth", status=401))

    outcome = h.run()

    assert outcome.status == "failed"
    assert outcome.error_code == "ANALYSIS_AUTH"
    assert _states(outcome) == [
        ("failed", "ANALYSIS_AUTH"),
        ("pending", None),
        ("pending", None),
    ]
    assert len(h.chat.bodies) == 1
    assert outcome.committed_micro_usd == 0


def test_an_unknown_outcome_is_not_sent_again_without_an_explicit_decision(
    make: MakeHarness,
) -> None:
    h = make(3, ChatError("unknown_outcome"))

    first = h.run()

    assert first.status == "failed"
    assert first.error_code == "ANALYSIS_UNKNOWN_OUTCOME"
    assert _states(first) == [("unknown_outcome", "ANALYSIS_UNKNOWN_OUTCOME")]
    assert [entry.state for entry in h.entries()] == ["unknown_outcome"]
    assert first.committed_micro_usd > 0
    assert len(h.chat.bodies) == 1

    again = h.run()

    assert again.status == "failed"
    assert again.error_code == "ANALYSIS_UNKNOWN_OUTCOME"
    assert len(h.chat.bodies) == 1

    resent = h.run(retry_unknown=True)

    assert resent.status == "completed"
    assert len(h.chat.bodies) == 2
    assert sorted(entry.state for entry in h.entries()) == ["completed", "unknown_outcome"]


def test_an_unreadable_body_is_paid_in_full_and_gets_the_repair_request(
    make: MakeHarness,
) -> None:
    h = make(3, ChatError("invalid_response", status=200), ok(candidate("s1", "s2", ["s1"])))

    outcome = h.run()

    assert outcome.status == "completed"
    assert len(h.chat.bodies) == 2
    user = _user(h.chat.bodies[1])
    assert user["previous_output"] == ""
    assert user["validation_errors"] == ["provider response is not readable"]
    (unknown_cost,) = [entry for entry in h.entries() if entry.actual_micro_usd is None]
    assert unknown_cost.state == "completed"
    assert unknown_cost.reserved_micro_usd > 0
    assert outcome.committed_micro_usd == unknown_cost.reserved_micro_usd + 2_000


def test_a_budget_that_does_not_fit_the_next_request_sends_nothing(make: MakeHarness) -> None:
    h = make(3, ok())

    outcome = h.run(ceiling="0.000001")

    assert outcome.status == "failed"
    assert outcome.error_code == "BUDGET_EXCEEDED"
    assert _states(outcome) == [("pending", None)]
    assert outcome.windows[0].attempts == 0
    assert h.chat.bodies == []
    assert h.entries() == []


def test_an_empty_transcript_completes_without_a_request(make: MakeHarness) -> None:
    h = make(0)

    outcome = h.run()

    assert outcome.status == "completed"
    assert outcome.windows == ()
    assert outcome.clip_ids == ()
    assert h.chat.bodies == []


def test_a_completed_run_is_not_run_again(make: MakeHarness) -> None:
    h = make(3)
    assert h.run().status == "completed"

    error = service_error(h.run)

    assert error.code == "ANALYSIS_RUN_COMPLETED"
    assert len(h.chat.bodies) == 1


# --- cancellation and concurrency -----------------------------------------------------------------


def test_a_cancel_between_windows_keeps_the_answered_window_and_cancels_the_rest(
    make: MakeHarness,
) -> None:
    cancel = threading.Event()

    def answer_then_cancel(body: Mapping[str, object]) -> ChatResult:
        cancel.set()
        return ok()

    h = make(10, answer_then_cancel)

    outcome = h.run(cancel=cancel)

    assert outcome.status == "cancelled"
    assert outcome.error_code is None
    assert _states(outcome) == [("succeeded", None), ("cancelled", None), ("cancelled", None)]
    assert len(h.chat.bodies) == 1
    assert h.clips() == []


def test_a_cancel_during_the_retry_wait_ends_it_and_gives_the_reservation_back(
    make: MakeHarness,
) -> None:
    h = make(3, ChatError("rate_limited", status=429, retry_after_s=5.0), ok())
    cancel = threading.Event()
    h.time.on_sleep = cancel.set

    outcome = h.run(cancel=cancel)

    assert outcome.status == "cancelled"
    assert _states(outcome) == [("cancelled", None)]
    assert h.time.sleeps == [5.0]
    assert len(h.chat.bodies) == 1
    assert [entry.state for entry in h.entries()] == ["released"]


def test_a_run_cancelled_before_it_starts_sends_nothing(make: MakeHarness) -> None:
    h = make(10, ok())
    cancel = threading.Event()
    cancel.set()

    outcome = h.run(cancel=cancel)

    assert outcome.status == "cancelled"
    assert _states(outcome) == [("cancelled", None)] * 3
    assert h.chat.bodies == []


def test_a_second_call_for_a_run_is_refused_while_one_is_running(make: MakeHarness) -> None:
    started = threading.Event()
    release = threading.Event()

    def hold(body: Mapping[str, object]) -> ChatResult:
        started.set()
        release.wait(10)
        return ok()

    h = make(3, hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(h.run)
        assert started.wait(10)
        refused = service_error(h.run)
        release.set()
        outcome = running.result(timeout=10)

    assert refused.kind is ErrorKind.CONFLICT
    assert refused.code == "ANALYSIS_RUN_ACTIVE"
    assert outcome.status == "completed"


# --- recovery and the profile -------------------------------------------------------------------


def test_recovery_makes_a_reserved_running_window_unknown_and_an_unreserved_one_pending(
    make: MakeHarness,
) -> None:
    h = make(10)
    dead = h.ledger.reserve(
        h.run_id, "dead-request", MODEL_ID, Decimal("0.0005"), Decimal("10"), {}
    )
    h.ledger.reserve(h.run_id, "orphan-request", MODEL_ID, Decimal("0.0003"), Decimal("10"), {})
    with h.db.write_session() as session:
        runs = AnalysisRunRepository(session)
        first, second, _third = runs.list_windows(h.run_id)
        runs.update_window(first.id, state="running", attempts=1)
        runs.add_attempt(
            window_id=first.id,
            kind="first",
            state="running",
            request_id="dead-request",
            spend_entry_id=dead.id,
        )
        runs.update_window(second.id, state="running")

    h.service.recover_interrupted(h.run_id)

    windows = h.windows()
    assert [(w.state, w.error_code) for w in windows] == [
        ("unknown_outcome", "ANALYSIS_UNKNOWN_OUTCOME"),
        ("pending", None),
        ("pending", None),
    ]
    assert [attempt.state for attempt in h.attempts(windows[0])] == ["unknown_outcome"]
    assert {entry.request_id: entry.state for entry in h.entries()} == {
        "dead-request": "unknown_outcome",
        "orphan-request": "released",
    }
    assert h.ledger.committed_micro_usd(h.run_id) == 500
    assert h.chat.bodies == []


def _leave_dead_call(
    h: Harness,
    *,
    attempt_state: str,
    error_code: str | None,
    paid_usd: Decimal | None = Decimal("0.002"),
    released: bool = False,
) -> None:
    """What a call leaves when it dies after its request was reserved and recorded: the window is
    ``running`` with one attempt in ``attempt_state``, and that attempt's ledger entry is released,
    or completed with ``paid_usd`` (``None``: the cost is unknown, as after an unreadable 200)."""
    entry = h.ledger.reserve(
        h.run_id, "dead-request", MODEL_ID, Decimal("0.0005"), Decimal("10"), {}
    )
    if released:
        h.ledger.release(entry.id)
    else:
        h.ledger.complete(entry.id, paid_usd)
    with h.db.write_session() as session:
        runs = AnalysisRunRepository(session)
        (window,) = runs.list_windows(h.run_id)
        runs.update_window(window.id, state="running", attempts=1)
        runs.add_attempt(
            window_id=window.id,
            kind="first",
            state=attempt_state,
            request_id="dead-request",
            spend_entry_id=entry.id,
            error_code=error_code,
        )


def test_a_crash_after_the_answer_was_recorded_leaves_the_window_unknown_and_nothing_is_sent(
    make: MakeHarness,
) -> None:
    # ADR 041 §22 (7): the answer was paid (completed) and the window never took it. The window is
    # not pending, so a run without a decision sends nothing for it.
    h = make(3)
    _leave_dead_call(h, attempt_state="succeeded", error_code=None)

    h.service.recover_interrupted(h.run_id)

    (window,) = h.windows()
    assert (window.state, window.error_code) == ("unknown_outcome", "ANALYSIS_UNKNOWN_OUTCOME")
    (attempt,) = h.attempts(window)
    assert attempt.state == "succeeded"
    assert [entry.state for entry in h.entries()] == ["completed"]
    assert h.ledger.committed_micro_usd(h.run_id) == 2_000
    outcome = h.run()
    assert (outcome.status, outcome.error_code) == ("failed", "ANALYSIS_UNKNOWN_OUTCOME")
    assert h.chat.bodies == []


def test_a_crash_after_an_unreadable_answer_was_recorded_leaves_the_window_unknown(
    make: MakeHarness,
) -> None:
    # ADR 041 §22 (7): an unreadable 200 is paid in full (completed, cost unknown) and its attempt
    # is failed. The crash came before the repair request, so the window is unknown.
    h = make(3)
    _leave_dead_call(h, attempt_state="failed", error_code="ANALYSIS_INVALID_OUTPUT", paid_usd=None)

    h.service.recover_interrupted(h.run_id)

    (window,) = h.windows()
    assert (window.state, window.error_code) == ("unknown_outcome", "ANALYSIS_UNKNOWN_OUTCOME")
    (attempt,) = h.attempts(window)
    assert (attempt.state, attempt.error_code) == ("failed", "ANALYSIS_INVALID_OUTPUT")
    (entry,) = h.entries()
    assert (entry.state, entry.actual_micro_usd) == ("completed", None)
    outcome = h.run()
    assert (outcome.status, outcome.error_code) == ("failed", "ANALYSIS_UNKNOWN_OUTCOME")
    assert h.chat.bodies == []


def test_a_crash_after_a_released_attempt_leaves_the_window_pending_and_it_is_sent(
    make: MakeHarness,
) -> None:
    # Nothing was paid: the provider refused the last attempt (a 429 or a 5xx) and its entry was
    # released, so the window starts again.
    h = make(3)
    _leave_dead_call(h, attempt_state="failed", error_code="ANALYSIS_UPSTREAM", released=True)

    h.service.recover_interrupted(h.run_id)

    (window,) = h.windows()
    assert (window.state, window.error_code) == ("pending", None)
    (attempt,) = h.attempts(window)
    assert attempt.state == "failed"
    assert [entry.state for entry in h.entries()] == ["released"]
    assert h.run().status == "completed"
    assert len(h.chat.bodies) == 1


def test_a_crash_between_the_ledger_and_the_attempt_update_leaves_the_window_unknown(
    make: MakeHarness,
) -> None:
    # ADR 041 §22 (7): the crash came after ledger.complete and before the attempt was marked
    # succeeded, so the attempt is still running and its entry is completed. The request was paid:
    # the window and the attempt are unknown, and nothing is sent.
    h = make(3)
    _leave_dead_call(h, attempt_state="running", error_code=None)

    h.service.recover_interrupted(h.run_id)

    (window,) = h.windows()
    assert (window.state, window.error_code) == ("unknown_outcome", "ANALYSIS_UNKNOWN_OUTCOME")
    (attempt,) = h.attempts(window)
    assert (attempt.state, attempt.error_code) == ("unknown_outcome", "ANALYSIS_UNKNOWN_OUTCOME")
    (entry,) = h.entries()
    assert entry.state == "completed"
    assert h.ledger.committed_micro_usd(h.run_id) == 2_000
    assert h.chat.bodies == []


def test_a_crash_after_a_live_attempt_was_released_leaves_the_window_pending(
    make: MakeHarness,
) -> None:
    # ADR 041 §22 (7): the crash came after ledger.release and before the attempt was marked
    # failed, so the attempt is still running and its entry is released. Nothing was paid: the
    # attempt is cancelled, the window is pending, and nothing is sent.
    h = make(3)
    _leave_dead_call(h, attempt_state="running", error_code=None, released=True)

    h.service.recover_interrupted(h.run_id)

    (window,) = h.windows()
    assert (window.state, window.error_code) == ("pending", None)
    (attempt,) = h.attempts(window)
    assert attempt.state == "cancelled"
    assert [entry.state for entry in h.entries()] == ["released"]
    assert h.ledger.committed_micro_usd(h.run_id) == 0
    assert h.chat.bodies == []


ONE_TICK = "2026-11-17T10:00:00.000000Z"


@dataclass(frozen=True)
class _Leftover:
    """An attempt that a dead call left in a window. ``entry`` is the state of its ledger entry:
    ``reserved``, ``completed`` (``paid_usd`` is the cost the provider reported, ``None`` when it
    reported none) or ``released``. Its request id is ``<attempt_id>-request``."""

    attempt_id: str
    kind: Literal["first", "repair"]
    state: str
    error_code: str | None
    entry: Literal["reserved", "completed", "released"]
    paid_usd: Decimal | None = None


def _leave_attempts_in_one_tick(h: Harness, *leftovers: _Leftover) -> None:
    """What a dead call leaves when a window had several attempts (ADR 041 §22 (7)): the window is
    ``running`` and every attempt carries the same ``created_at``, so the insertion order alone
    decides which attempt is the last. The ids are the test's own and sort against the insertion
    order, so a list ordered by id names a different last attempt."""
    entry_ids: list[str] = []
    for leftover in leftovers:
        entry = h.ledger.reserve(
            h.run_id,
            f"{leftover.attempt_id}-request",
            MODEL_ID,
            Decimal("0.0005"),
            Decimal("10"),
            {},
        )
        if leftover.entry == "released":
            h.ledger.release(entry.id)
        elif leftover.entry == "completed":
            h.ledger.complete(entry.id, leftover.paid_usd)
        entry_ids.append(entry.id)
    with h.db.write_session() as session:
        runs = AnalysisRunRepository(session)
        (window,) = runs.list_windows(h.run_id)
        runs.update_window(window.id, state="running", attempts=len(leftovers))
        for leftover, entry_id in zip(leftovers, entry_ids, strict=True):
            session.add(
                AnalysisAttempt(
                    id=leftover.attempt_id,
                    window_id=window.id,
                    kind=leftover.kind,
                    request_id=f"{leftover.attempt_id}-request",
                    spend_entry_id=entry_id,
                    state=leftover.state,
                    http_status=None,
                    error_code=leftover.error_code,
                    raw_output=None,
                    usage_json=None,
                    created_at=ONE_TICK,
                )
            )
            session.flush()


def test_a_reserved_repair_made_last_makes_the_window_unknown_when_its_id_sorts_first(
    make: MakeHarness,
) -> None:
    # ADR 041 §22 (7): the last attempt is the one made last, the repair, although its id sorts
    # before the select attempt's. Its entry is reserved, so the request may have been sent: the
    # window is unknown and the entry is marked so. The select attempt's entry is not touched.
    h = make(3)
    _leave_attempts_in_one_tick(
        h,
        _Leftover(
            attempt_id="b-select",
            kind="first",
            state="failed",
            error_code="ANALYSIS_INVALID_OUTPUT",
            entry="completed",
        ),
        _Leftover(
            attempt_id="a-repair",
            kind="repair",
            state="failed",
            error_code=None,
            entry="reserved",
        ),
    )

    h.service.recover_interrupted(h.run_id)

    (window,) = h.windows()
    assert (window.state, window.error_code) == ("unknown_outcome", "ANALYSIS_UNKNOWN_OUTCOME")
    assert [(attempt.id, attempt.state) for attempt in h.attempts(window)] == [
        ("b-select", "failed"),
        ("a-repair", "failed"),
    ]
    assert {entry.request_id: entry.state for entry in h.entries()} == {
        "b-select-request": "completed",
        "a-repair-request": "unknown_outcome",
    }
    assert h.ledger.committed_micro_usd(h.run_id) == 1_000
    assert h.chat.bodies == []
    outcome = h.run()
    assert (outcome.status, outcome.error_code) == ("failed", "ANALYSIS_UNKNOWN_OUTCOME")
    assert h.chat.bodies == []


def test_a_completed_repair_made_last_keeps_the_window_unknown_over_a_released_first_attempt(
    make: MakeHarness,
) -> None:
    # ADR 041 §22 (7): the repair was answered and paid, and the window never took the answer. The
    # first attempt was released and its id sorts after the repair's. The window is unknown, not
    # pending, because the last attempt is the paid one.
    h = make(3)
    _leave_attempts_in_one_tick(
        h,
        _Leftover(
            attempt_id="b-select",
            kind="first",
            state="failed",
            error_code="ANALYSIS_UPSTREAM",
            entry="released",
        ),
        _Leftover(
            attempt_id="a-repair",
            kind="repair",
            state="succeeded",
            error_code=None,
            entry="completed",
            paid_usd=Decimal("0.002"),
        ),
    )

    h.service.recover_interrupted(h.run_id)

    (window,) = h.windows()
    assert (window.state, window.error_code) == ("unknown_outcome", "ANALYSIS_UNKNOWN_OUTCOME")
    assert [(attempt.id, attempt.state) for attempt in h.attempts(window)] == [
        ("b-select", "failed"),
        ("a-repair", "succeeded"),
    ]
    assert {entry.request_id: entry.state for entry in h.entries()} == {
        "b-select-request": "released",
        "a-repair-request": "completed",
    }
    assert h.ledger.committed_micro_usd(h.run_id) == 2_000
    assert h.chat.bodies == []
    outcome = h.run()
    assert (outcome.status, outcome.error_code) == ("failed", "ANALYSIS_UNKNOWN_OUTCOME")
    assert h.chat.bodies == []


def test_a_profile_that_does_not_price_like_the_run_is_refused(make: MakeHarness) -> None:
    h = make(3)
    other = replace(MODEL, max_tokens=2048)

    error = service_error(
        lambda: h.service.run(
            h.run_id,
            analysis_profile=other,
            ceiling_usd=Decimal("10"),
            cancel=threading.Event(),
        )
    )

    assert error.code == "RUN_OPTIONS_MISMATCH"
    assert h.chat.bodies == []


@pytest.mark.parametrize("field", ["prompt_version", "schema_version"])
def test_a_run_made_under_another_prompt_or_schema_is_refused(
    make: MakeHarness, db: Database, field: str
) -> None:
    # ADR 041 §22 (2): a run is not continued under a prompt or schema other than the stored ones.
    h = make(3)
    with db.write_session() as session:
        row = session.get(AnalysisRun, h.run_id)
        assert row is not None
        if field == "prompt_version":
            row.prompt_version = "sha256:0000000000000000"
        else:
            row.schema_version = 2

    error = service_error(h.run)

    assert (error.kind, error.code) == (ErrorKind.CONFLICT, "RUN_OPTIONS_MISMATCH")
    assert h.chat.bodies == []
    assert {window.state for window in h.windows()} == {"pending"}


def test_create_run_refuses_an_unknown_transcript_and_one_of_another_source(
    make: MakeHarness,
) -> None:
    h = make(3)
    with h.db.write_session() as session:
        other_project = add_project(session, "Другий проєкт").id
        other_source = add_source(session, other_project, duration_ms=SOURCE_MS).id
        other_transcript = _add_transcript(session, other_source, 3)

    unknown = service_error(
        lambda: h.service.create_run(
            source_id=h.source_id,
            transcript_id="немає-такої-ревізії",
            search_profile=PROFILE,
            analysis_profile=MODEL,
        )
    )
    foreign = service_error(
        lambda: h.service.create_run(
            source_id=h.source_id,
            transcript_id=other_transcript,
            search_profile=PROFILE,
            analysis_profile=MODEL,
        )
    )

    assert (unknown.code, unknown.kind) == ("TRANSCRIPT_NOT_FOUND", ErrorKind.NOT_FOUND)
    assert (foreign.code, foreign.kind) == ("TRANSCRIPT_SOURCE_MISMATCH", ErrorKind.INVALID)


# --- the repair request (ADR 041 §18) -------------------------------------------------------------


def test_the_repair_request_is_built_by_the_planner_and_cuts_a_long_answer(
    make: MakeHarness,
) -> None:
    long_answer = "x" * 200_000
    h = make(3, ok(long_answer), ok(EMPTY))

    outcome = h.run()

    assert outcome.status == "completed"
    previous = _user(h.chat.bodies[1])["previous_output"]
    assert isinstance(previous, str)
    assert previous == "x" * len(previous)
    assert 60_000 < len(previous.encode("utf-8")) <= 65_536
    sent = json.dumps(h.chat.bodies[1], ensure_ascii=False, separators=(",", ":"))
    assert len(sent.encode("utf-8")) + MODEL.max_tokens <= MODEL.context_tokens


def test_a_repair_body_that_does_not_fit_fails_the_window_without_sending_it(
    make: MakeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ValueError("does not fit")

    monkeypatch.setattr(analysis_runs, "repair_body", refuse)
    h = make(3, ok("не JSON"))

    outcome = h.run()

    assert outcome.status == "failed"
    assert outcome.error_code == "ANALYSIS_INVALID_OUTPUT"
    assert _states(outcome) == [("failed", "ANALYSIS_INVALID_OUTPUT")]
    assert outcome.windows[0].repair_used is False
    assert len(h.chat.bodies) == 1
