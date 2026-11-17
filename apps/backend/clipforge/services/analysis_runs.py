"""AI selection run orchestration (ADR 041 §4, §9, §14, §17; the shape of ``transcription_runs``).

``create_run`` stores a ``queued`` run with a ``pending`` window for every window of the plan
(``services.analysis_plan``). ``run`` sends every window that is not ``succeeded``, one after the
other, in the caller's thread. When all windows have succeeded, ``aggregate_run`` writes the clips
and the ``completed`` status in one write transaction. The state is in the database
(``analysis_runs``, ``analysis_windows``, ``analysis_attempts``, the spend ledger), so a repeated
``run`` resumes from what is stored.

Nothing here knows HTTP or the clock of the machine: the chat client, the clock, the sleeper and the
jitter are injected, so tests do not wait and do not pay.

One HTTP request is one ``analysis_attempts`` row. The row is written after the reservation was
made and right before the request is sent: no reservation, no row, no request. How a request ends:

- 200 with a usable answer: ``complete(cost)``; the window is ``succeeded``.
- 200 whose answer is not usable (not JSON, schema or grounding errors, ``finish_reason`` length):
  ``complete(cost)``; one repair request, then ``failed`` with ``ANALYSIS_INVALID_OUTPUT``.
- ``invalid_response`` (a 200 whose body cannot be used): ``complete(None)``, which keeps the
  reservation in full; it counts as an unusable answer and gets the one repair request.
- ``rate_limited``, ``upstream``, ``not_sent``: ``release``; retried after the wait (Retry-After
  at most ``MAX_RETRY_AFTER_S``, otherwise exponential backoff), at most ``MAX_ATTEMPTS``
  requests, then ``failed`` with ``ANALYSIS_UPSTREAM``.
- ``auth``, ``funds``: ``release``; the window fails with ``ANALYSIS_AUTH`` or
  ``ANALYSIS_FUNDS`` and the run stops.
- ``invalid_request``, ``model_unavailable``: ``release``; the window fails with
  ``ANALYSIS_REQUEST_REJECTED`` and is not retried.
- ``unknown_outcome``: ``mark_unknown``; the window is ``unknown_outcome`` and is not sent again
  unless ``retry_unknown`` is given.
- Budget: a request that does not fit leaves no reservation and no row; its window stays
  ``pending``, the run stops with ``BUDGET_EXCEEDED``.

A window that fails or is unknown does not stop the other windows: their answers are kept and a
later ``run`` sends only what is not ``succeeded``; a failed window starts again from its first
request. Auth, funds and budget stop the run, and the windows that were not sent stay ``pending``.
A cancel ends the retry wait and the windows not yet sent (``cancelled``). A request already on the
wire cannot be recalled, so its answer is kept like any other.

Recovery (``_settle_interrupted``, ADR 041 §22 (7)): a window ``running`` after a dead call is
judged by its last attempt. An attempt whose ledger entry exists and is not released (reserved,
completed or unknown) may have been sent or paid for, so the window is ``unknown_outcome`` and the
attempt keeps its state. No attempt, or a released entry, means nothing was paid: the window is
``pending``. A reservation with no attempt row was never sent and is released.

Messages are fixed Ukrainian text; logs carry ids and codes, never provider text or content.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol

from sqlalchemy.orm import Session

from clipforge.adapters.openrouter_chat import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    SELECT_SYSTEM_PROMPT,
    AnalysisProfile,
    ChatError,
    ChatErrorKind,
    ChatResult,
    build_chat_body,
    reservation_micro_usd,
)
from clipforge.domain.analysis_windows import Unit, Window
from clipforge.domain.search_profile import SearchProfile, parse_search_profile, profile_to_json
from clipforge.domain.selection_output import (
    SelectionOutputError,
    UnitRef,
    parse_selection_output,
    validate_candidates,
)
from clipforge.services.analysis_aggregate import aggregate_run, window_result_json
from clipforge.services.analysis_plan import (
    add_run,
    load_units,
    plan_run,
    price_snapshot,
    repair_body,
    storable_text,
    user_payload,
)
from clipforge.services.errors import ServiceError, conflict, invalid, not_found
from clipforge.services.spend import BudgetExceededError, SpendLedger
from clipforge.services.transcription_runs import (
    MAX_RETRY_AFTER_S,
    Clock,
    Jitter,
    Sleeper,
    backoff_seconds,
    cancellable_sleep,
)
from clipforge.storage import (
    SPEND_RELEASED,
    SPEND_RESERVED,
    AnalysisAttempt,
    AnalysisRun,
    AnalysisRunRepository,
    AnalysisWindow,
    Database,
    SourceRepository,
    SpendRepository,
    TranscriptRepository,
    TranscriptRevision,
)
from clipforge.storage.util import new_id

PROVIDER: Final = "openrouter"
MAX_ATTEMPTS: Final = 3

RUN_RUNNING: Final = "running"
RUN_COMPLETED: Final = "completed"
RUN_FAILED: Final = "failed"
RUN_CANCELLED: Final = "cancelled"

CODE_AUTH: Final = "ANALYSIS_AUTH"
CODE_FUNDS: Final = "ANALYSIS_FUNDS"
CODE_REQUEST_REJECTED: Final = "ANALYSIS_REQUEST_REJECTED"
CODE_UPSTREAM: Final = "ANALYSIS_UPSTREAM"
CODE_UNKNOWN_OUTCOME: Final = "ANALYSIS_UNKNOWN_OUTCOME"
CODE_INVALID_OUTPUT: Final = "ANALYSIS_INVALID_OUTPUT"
CODE_BUDGET: Final = "BUDGET_EXCEEDED"

# The states of a window and of an attempt (both use the same vocabulary, ADR 041 §8).
_PENDING: Final = "pending"
_RUNNING: Final = "running"
_SUCCEEDED: Final = "succeeded"
_FAILED: Final = "failed"
_UNKNOWN: Final = "unknown_outcome"
_CANCELLED: Final = "cancelled"

_KIND_CODES: Final[Mapping[ChatErrorKind, str]] = MappingProxyType(
    {
        "auth": CODE_AUTH,
        "funds": CODE_FUNDS,
        "rate_limited": CODE_UPSTREAM,
        "upstream": CODE_UPSTREAM,
        "not_sent": CODE_UPSTREAM,
        "invalid_request": CODE_REQUEST_REJECTED,
        "model_unavailable": CODE_REQUEST_REJECTED,
        "unknown_outcome": CODE_UNKNOWN_OUTCOME,
        "invalid_response": CODE_INVALID_OUTPUT,
    }
)
# Failures before the request was sent (or a 429/5xx that the provider refused): repeated.
_RETRIABLE: Final = frozenset({"rate_limited", "upstream", "not_sent"})
_LOGGER = logging.getLogger(__name__)


class ChatClient(Protocol):
    """What the service needs from the chat adapter: ``OpenRouterChat.complete``."""

    def complete(self, body: Mapping[str, object]) -> ChatResult: ...


@dataclass(frozen=True, slots=True)
class WindowSummary:
    idx: int
    state: str
    attempts: int
    repair_used: bool
    error_code: str | None


@dataclass(frozen=True, slots=True)
class AnalysisRunOutcome:
    """The state of a run after one ``run`` call.

    ``status``: ``completed`` (every window succeeded and the clips were written), ``failed`` (a
    window failed or a stop happened; ``error_code`` says why), ``cancelled`` (the user cancelled
    before every window succeeded). ``clip_ids`` are the suggested clips in the order they were
    kept.
    """

    run_id: str
    status: str
    error_code: str | None
    clip_ids: tuple[str, ...]
    committed_micro_usd: int
    windows: tuple[WindowSummary, ...]


@dataclass(frozen=True, slots=True)
class _WindowSpec:
    """A detached copy of a window: its id, its span in positions and its core in ordinals."""

    id: str
    window: Window
    core_ordinals: tuple[int, int]


@dataclass(slots=True)
class _Context:
    run_id: str
    profile: AnalysisProfile
    search_profile: SearchProfile
    revision: TranscriptRevision
    unit_level: str
    units: tuple[Unit, ...]
    position_of: Mapping[int, int]
    ceiling_usd: Decimal
    cancel: threading.Event
    snapshot: dict[str, object]
    stop_code: str | None = None

    def request_stop(self, code: str) -> None:
        if self.stop_code is None:
            self.stop_code = code


@dataclass(frozen=True, slots=True)
class _Reply:
    """A 200 answer. ``content`` is ``None`` when the body could not be used at all."""

    attempt_id: str
    content: str | None
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class _Ended:
    """The window stops here: ``state`` is its new state, ``code`` its error (``None`` if none)."""

    state: str
    code: str | None


@dataclass(frozen=True, slots=True)
class _Retry:
    delay_s: float


@dataclass(frozen=True, slots=True)
class _Accepted:
    entries: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _Rejected:
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Settle:
    """A window that was ``running`` when its call died: the attempt that decides it (its live
    attempt, else its last one; ``live`` says which) and that attempt's ledger entry."""

    window_id: str
    attempt_id: str | None
    live: bool
    entry_id: str | None
    entry_state: str | None


class _Cancelled(Exception):
    """The run was cancelled; the window in hand is settled as ``cancelled``."""


class AnalysisRunService:
    def __init__(
        self,
        database: Database,
        ledger: SpendLedger,
        *,
        client: ChatClient,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = cancellable_sleep,
        jitter: Jitter = random.random,
    ) -> None:
        self._database = database
        self._ledger = ledger
        self._client = client
        self._clock = clock
        self._sleeper = sleeper
        self._jitter = jitter
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._cooldown_until = 0.0

    # -- create ------------------------------------------------------------------------------

    def create_run(
        self,
        *,
        source_id: str,
        transcript_id: str,
        search_profile: SearchProfile,
        analysis_profile: AnalysisProfile,
        session: Session | None = None,
    ) -> str:
        """Store a ``queued`` run with its ``pending`` windows and return its id.

        Without ``session`` the run is written in a transaction of its own. With ``session`` (a
        ``Database.write_session``) the rows join the caller's transaction, which commits or rolls
        back the run together with the job that owns it."""
        if session is not None:
            return self._add_run(
                session, source_id, transcript_id, search_profile, analysis_profile
            )
        with self._database.write_session() as own:
            return self._add_run(own, source_id, transcript_id, search_profile, analysis_profile)

    def _add_run(
        self,
        session: Session,
        source_id: str,
        transcript_id: str,
        search_profile: SearchProfile,
        analysis_profile: AnalysisProfile,
    ) -> str:
        if SourceRepository(session).get(source_id) is None:
            raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
        revision = TranscriptRepository(session).get(transcript_id)
        if revision is None:
            raise not_found("TRANSCRIPT_NOT_FOUND", "Транскрипт не знайдено")
        if revision.source_id != source_id:
            raise invalid(
                "TRANSCRIPT_SOURCE_MISMATCH",
                "Транскрипт належить іншому джерелу",
                field="transcript_id",
            )
        plan = plan_run(session, transcript_id, search_profile, analysis_profile)
        return add_run(
            session,
            plan,
            source_id=source_id,
            revision=revision,
            search_profile=search_profile,
            analysis_profile=analysis_profile,
        )

    # -- run ---------------------------------------------------------------------------------

    def run(
        self,
        run_id: str,
        *,
        analysis_profile: AnalysisProfile,
        search_profile: SearchProfile | None = None,
        ceiling_usd: Decimal,
        cancel: threading.Event,
        retry_unknown: bool = False,
    ) -> AnalysisRunOutcome:
        """Send what is not yet answered and write the clips once every window has succeeded.
        Blocking: call it from a worker thread. A second call for the same run while one is
        running is a 409.

        ``analysis_profile`` must be the profile the run was created with (its model and caps), and
        ``search_profile``, when given, the profile the run stores. ``ceiling_usd`` is the budget of
        the whole run, earlier requests included. A window that is ``unknown_outcome`` is sent again
        only when ``retry_unknown`` is true."""
        if not ceiling_usd.is_finite() or ceiling_usd < 0:
            raise invalid(
                "INVALID_BUDGET", "Бюджет має бути невід'ємним числом", field="budget_usd"
            )
        self._admit(run_id)
        try:
            context = self._prepare(run_id, analysis_profile, search_profile, ceiling_usd, cancel)
            self._settle_interrupted(run_id)
            queue = self._reset_for_run(context, retry_unknown=retry_unknown)
            self._set_run(run_id, status=RUN_RUNNING, error_code=None)
            self._drive(context, queue)
            return self._finalize(context)
        finally:
            with self._lock:
                self._active.discard(run_id)

    def recover_interrupted(self, run_id: str) -> None:
        """Settle the windows a dead call left ``running`` (see ``_settle_interrupted``) without
        starting a run, so that the states are true before the user decides to resume. Refused
        with 409 while this service is running the run. The lock is held so that a run cannot
        start while its windows are being settled."""
        with self._lock:
            if run_id in self._active:
                raise _active_conflict(run_id)
            self._settle_interrupted(run_id)

    def _admit(self, run_id: str) -> None:
        with self._lock:
            if run_id in self._active:
                raise _active_conflict(run_id)
            self._active.add(run_id)

    def _prepare(
        self,
        run_id: str,
        analysis_profile: AnalysisProfile,
        search_profile: SearchProfile | None,
        ceiling_usd: Decimal,
        cancel: threading.Event,
    ) -> _Context:
        with self._database.session() as session:
            run = AnalysisRunRepository(session).get_run(run_id)
            if run is None:
                raise not_found("ANALYSIS_RUN_NOT_FOUND", "Запуск аналізу не знайдено")
            if run.status == RUN_COMPLETED:
                raise conflict("ANALYSIS_RUN_COMPLETED", "Запуск аналізу вже завершено")
            stored = parse_search_profile(run.profile_json)
            snapshot = price_snapshot(analysis_profile)
            # ADR 041 §22 (2): a run is never continued under another prompt or output schema.
            matches = (
                run.model == analysis_profile.model
                and run.price_snapshot_json == snapshot
                and run.prompt_version == PROMPT_VERSION
                and run.schema_version == SCHEMA_VERSION
            )
            if search_profile is not None and profile_to_json(search_profile) != run.profile_json:
                matches = False
            if not matches:
                raise conflict(
                    "RUN_OPTIONS_MISMATCH",
                    "Параметри аналізу не збігаються з параметрами запуску",
                )
            unit_level, units = load_units(session, run.transcript_id)
            revision = TranscriptRepository(session).get(run.transcript_id)
            if revision is None:
                raise not_found("TRANSCRIPT_NOT_FOUND", "Транскрипт не знайдено")
        return _Context(
            run_id=run_id,
            profile=analysis_profile,
            search_profile=stored,
            revision=revision,
            unit_level=unit_level,
            units=units,
            position_of={unit.ordinal: position for position, unit in enumerate(units)},
            ceiling_usd=ceiling_usd,
            cancel=cancel,
            snapshot=snapshot,
        )

    def _settle_interrupted(self, run_id: str) -> None:
        """Settle what a dead call left behind, as ``transcription_runs`` does (ADR 041 §22 (7)). A
        ``running`` window is judged by its attempt: the live one, else its last one. When that
        attempt's ledger entry exists and is not released (reserved, completed or unknown), the
        request may have been sent or paid for: the window is ``unknown_outcome``, and a live
        attempt becomes ``unknown_outcome`` too while a finished one keeps its state. A window with
        no attempt, or whose entry was released, sent nothing: it is ``pending``, and a live attempt
        becomes ``cancelled``. A reservation that no attempt row refers to was never sent and is
        released."""
        settles: list[_Settle] = []
        orphans: list[str] = []
        with self._database.session() as session:
            runs = AnalysisRunRepository(session)
            spend = SpendRepository(session)
            known: set[str] = set()
            for window in runs.list_windows(run_id):
                attempts = runs.list_attempts(window.id)
                known.update(a.request_id for a in attempts if a.request_id is not None)
                live = [a for a in attempts if a.state == _RUNNING]
                if live or window.state == _RUNNING:
                    attempt = live[-1] if live else (attempts[-1] if attempts else None)
                    entry = (
                        spend.find(run_id, attempt.request_id)
                        if attempt is not None and attempt.request_id is not None
                        else None
                    )
                    settles.append(
                        _Settle(
                            window_id=window.id,
                            attempt_id=attempt.id if attempt is not None else None,
                            live=bool(live),
                            entry_id=entry.id if entry is not None else None,
                            entry_state=entry.state if entry is not None else None,
                        )
                    )
            orphans = [
                entry.id
                for entry in spend.list_for_run(run_id)
                if entry.state == SPEND_RESERVED and entry.request_id not in known
            ]
        for settle in settles:
            if settle.entry_state in (None, SPEND_RELEASED):
                if settle.live and settle.attempt_id is not None:
                    self._update_attempt(settle.attempt_id, state=_CANCELLED)
                self._update_window(settle.window_id, state=_PENDING, error_code=None)
                continue
            if settle.entry_id is not None and settle.entry_state == SPEND_RESERVED:
                self._ledger.mark_unknown(settle.entry_id)
            if settle.live and settle.attempt_id is not None:
                self._update_attempt(
                    settle.attempt_id, state=_UNKNOWN, error_code=CODE_UNKNOWN_OUTCOME
                )
            self._update_window(settle.window_id, state=_UNKNOWN, error_code=CODE_UNKNOWN_OUTCOME)
            _LOGGER.warning("Analysis window settled unknown: window=%s", settle.window_id)
        for entry_id in orphans:
            self._ledger.release(entry_id)

    def _reset_for_run(self, context: _Context, *, retry_unknown: bool) -> list[_WindowSpec]:
        """The windows this call must send, reset to ``pending``: every window that is not
        ``succeeded``, except an ``unknown_outcome`` one unless ``retry_unknown`` is given."""
        queue: list[_WindowSpec] = []
        with self._database.write_session() as session:
            runs = AnalysisRunRepository(session)
            for window in runs.list_windows(context.run_id):
                if window.state == _SUCCEEDED:
                    continue
                if window.state == _UNKNOWN and not retry_unknown:
                    continue
                runs.update_window(
                    window.id, state=_PENDING, error_code=None, repair_used=False, result_json=None
                )
                queue.append(_spec_of(context, window))
        return queue

    # -- scheduling --------------------------------------------------------------------------

    def _drive(self, context: _Context, queue: Sequence[_WindowSpec]) -> None:
        """Run the windows one after another, in index order."""
        for number, spec in enumerate(queue):
            if context.stop_code is not None:
                return  # what was not sent stays pending
            if context.cancel.is_set():
                self._cancel_windows(queue[number:])
                return
            try:
                self._window(context, spec)
            except _Cancelled:
                self._cancel_windows(queue[number:])
                return

    def _cancel_windows(self, specs: Sequence[_WindowSpec]) -> None:
        for spec in specs:
            self._update_window(spec.id, state=_CANCELLED, error_code=None)

    # -- one window --------------------------------------------------------------------------

    def _window(self, context: _Context, spec: _WindowSpec) -> None:
        """The first request of the window, then at most one repair request (ADR 041 §5)."""
        self._update_window(spec.id, state=_RUNNING)
        payload = user_payload(
            context.revision, context.search_profile, context.unit_level, context.units, spec.window
        )
        first_body = build_chat_body(
            context.profile, system_prompt=SELECT_SYSTEM_PROMPT, user_payload=payload
        )
        first = self._request(context, spec, "first", first_body)
        if isinstance(first, _Ended):
            self._end(spec, first)
            return
        verdict = self._judge(context, spec, first)
        if isinstance(verdict, _Accepted):
            self._accept(spec, verdict)
            return
        # The repair body is built by the planner: its previous answer is cut so that the request
        # fits the model's context (ADR 041 §18). A body that cannot fit is never sent.
        try:
            repair = repair_body(context.profile, payload, verdict.errors, first.content or "")
        except ValueError:
            self._end(spec, _Ended(_FAILED, CODE_INVALID_OUTPUT))
            return
        self._update_window(spec.id, repair_used=True)
        second = self._request(context, spec, "repair", repair)
        if isinstance(second, _Ended):
            self._end(spec, second)
            return
        checked = self._judge(context, spec, second)
        if isinstance(checked, _Accepted):
            self._accept(spec, checked)
            return
        self._end(spec, _Ended(_FAILED, CODE_INVALID_OUTPUT))

    def _request(
        self,
        context: _Context,
        spec: _WindowSpec,
        kind: Literal["first", "repair"],
        body: Mapping[str, object],
    ) -> _Reply | _Ended:
        """Send one request for the window until it is answered or the window ends (ADR 041 §9)."""
        upper = reservation_micro_usd(body, context.profile)
        number = 0
        while True:
            self._wait_out_cooldown(context)
            if context.cancel.is_set():
                raise _Cancelled
            number += 1
            request_id = new_id()
            try:
                entry = self._ledger.reserve(
                    context.run_id,
                    request_id,
                    context.profile.model,
                    _usd(upper),
                    context.ceiling_usd,
                    context.snapshot,
                )
            except BudgetExceededError:
                # Nothing is reserved and nothing is sent: the window waits for a bigger budget.
                context.request_stop(CODE_BUDGET)
                return _Ended(_PENDING, None)
            if context.cancel.is_set():
                self._ledger.release(entry.id)
                raise _Cancelled
            attempt_id = self._begin_attempt(spec.id, kind, request_id, entry.id)
            try:
                result = self._client.complete(body)
            except ChatError as error:
                outcome = self._after_error(context, spec, attempt_id, entry.id, error, number)
            else:
                self._ledger.complete(entry.id, result.cost)
                self._update_attempt(
                    attempt_id,
                    state=_SUCCEEDED,
                    http_status=200,
                    raw_output=storable_text(result.content),
                    usage_json={
                        "cost": _text(result.cost),
                        "finish_reason": result.finish_reason,
                        "generation_id": result.generation_id,
                    },
                )
                return _Reply(attempt_id, result.content, result.finish_reason)
            if not isinstance(outcome, _Retry):
                return outcome
            if self._sleeper(outcome.delay_s, context.cancel):
                raise _Cancelled

    def _after_error(
        self,
        context: _Context,
        spec: _WindowSpec,
        attempt_id: str,
        entry_id: str,
        error: ChatError,
        number: int,
    ) -> _Reply | _Ended | _Retry:
        """Settle the ledger and the attempt of a failed request and say what happens next."""
        kind = error.kind
        code = _KIND_CODES[kind]
        if kind == "unknown_outcome":
            self._ledger.mark_unknown(entry_id)
            self._update_attempt(attempt_id, state=_UNKNOWN, error_code=code)
            return _Ended(_UNKNOWN, code)
        if kind == "invalid_response":
            # A 200 with a body that cannot be used: most likely paid for, so the reservation keeps
            # counting in full. The repair request tells the model that nothing could be read.
            self._ledger.complete(entry_id, None)
            self._update_attempt(
                attempt_id, state=_FAILED, http_status=error.status, error_code=code
            )
            return _Reply(attempt_id, None, None)
        self._ledger.release(entry_id)
        self._update_attempt(attempt_id, state=_FAILED, http_status=error.status, error_code=code)
        if kind in _RETRIABLE:
            if number >= MAX_ATTEMPTS:
                return _Ended(_FAILED, code)
            delay = self._retry_delay(error, number)
            if kind == "rate_limited":
                self._publish_cooldown(delay)
            return _Retry(delay)
        if kind in ("auth", "funds"):
            context.request_stop(code)
        return _Ended(_FAILED, code)

    def _judge(self, context: _Context, spec: _WindowSpec, reply: _Reply) -> _Accepted | _Rejected:
        verdict = _check(context, spec, reply.content, reply.finish_reason)
        if isinstance(verdict, _Rejected) and reply.content is not None:
            self._update_attempt(reply.attempt_id, state=_FAILED, error_code=CODE_INVALID_OUTPUT)
        return verdict

    def _accept(self, spec: _WindowSpec, verdict: _Accepted) -> None:
        self._update_window(spec.id, state=_SUCCEEDED, error_code=None, result_json=verdict.entries)

    def _end(self, spec: _WindowSpec, ended: _Ended) -> None:
        self._update_window(spec.id, state=ended.state, error_code=ended.code)
        if ended.code is not None:
            _LOGGER.warning(
                "Analysis window ended %s: window=%s code=%s", ended.state, spec.id, ended.code
            )

    def _retry_delay(self, error: ChatError, number: int) -> float:
        if error.kind == "rate_limited" and error.retry_after_s is not None:
            return min(max(error.retry_after_s, 0.0), MAX_RETRY_AFTER_S)
        return backoff_seconds(number, self._jitter())

    def _wait_out_cooldown(self, context: _Context) -> None:
        with self._lock:
            remaining = self._cooldown_until - self._clock()
        if remaining > 0 and self._sleeper(remaining, context.cancel):
            raise _Cancelled

    def _publish_cooldown(self, delay_s: float) -> None:
        """A 429 pauses every run of this service: the rate limit is shared."""
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + delay_s)

    # -- finish ------------------------------------------------------------------------------

    def _finalize(self, context: _Context) -> AnalysisRunOutcome:
        run_id = context.run_id
        with self._database.session() as session:
            windows = AnalysisRunRepository(session).list_windows(run_id)
        clip_ids: tuple[str, ...] = ()
        if all(window.state == _SUCCEEDED for window in windows):
            with self._database.write_session() as session:
                clip_ids = aggregate_run(session, run_id)
            status: str = RUN_COMPLETED
            error_code: str | None = None
        else:
            status, error_code = _unfinished(context, windows)
            self._set_run(run_id, status=status, error_code=error_code)
        return AnalysisRunOutcome(
            run_id=run_id,
            status=status,
            error_code=error_code,
            clip_ids=clip_ids,
            committed_micro_usd=self._ledger.committed_micro_usd(run_id),
            windows=tuple(_summary(window) for window in windows),
        )

    # -- state writes ------------------------------------------------------------------------

    def _begin_attempt(
        self, window_id: str, kind: Literal["first", "repair"], request_id: str, entry_id: str
    ) -> str:
        """The row of a request that is about to be sent; the window counts it as an attempt."""
        with self._database.write_session() as session:
            runs = AnalysisRunRepository(session)
            attempt = runs.add_attempt(
                window_id=window_id,
                kind=kind,
                state=_RUNNING,
                request_id=request_id,
                spend_entry_id=entry_id,
            )
            runs.update_window(
                window_id, state=_RUNNING, attempts=len(runs.list_attempts(window_id))
            )
            return attempt.id

    def _update_attempt(self, attempt_id: str, **changes: Any) -> AnalysisAttempt:
        with self._database.write_session() as session:
            return AnalysisRunRepository(session).update_attempt(attempt_id, **changes)

    def _update_window(self, window_id: str, **changes: Any) -> AnalysisWindow:
        with self._database.write_session() as session:
            return AnalysisRunRepository(session).update_window(window_id, **changes)

    def _set_run(self, run_id: str, **changes: Any) -> AnalysisRun:
        with self._database.write_session() as session:
            return AnalysisRunRepository(session).update_run(run_id, **changes)


# --- helpers ----------------------------------------------------------------------------------


def _active_conflict(run_id: str) -> ServiceError:
    return conflict(
        "ANALYSIS_RUN_ACTIVE",
        "Аналіз цього запуску вже виконується",
        details={"run_id": run_id},
    )


def _usd(micro_usd: int) -> Decimal:
    """Whole micro-USD as the USD ``Decimal`` the ledger takes; exact, so no digit is lost."""
    return Decimal(micro_usd).scaleb(-6)


def _text(cost: Decimal | None) -> str | None:
    return str(cost) if cost is not None else None


def _position(context: _Context, ordinal: int) -> int:
    try:
        return context.position_of[ordinal]
    except KeyError:
        raise ValueError("Вікно посилається на одиницю, якої немає в транскрипті") from None


def _spec_of(context: _Context, window: AnalysisWindow) -> _WindowSpec:
    """A window of the stored run as positions in the units of its transcript (ADR 041 §15)."""
    return _WindowSpec(
        id=window.id,
        window=Window(
            index=window.idx,
            first=_position(context, window.first),
            core_first=_position(context, window.core_first),
            core_last=_position(context, window.core_last),
            last=_position(context, window.last),
        ),
        core_ordinals=(window.core_first, window.core_last),
    )


def _unit_refs(context: _Context, window: Window) -> tuple[UnitRef, ...]:
    """The units of one request: positions ``first..last``."""
    return tuple(
        UnitRef(id=unit.id, ordinal=unit.ordinal, start_ms=unit.start_ms, end_ms=unit.end_ms)
        for unit in context.units[window.first : window.last + 1]
    )


def _check(
    context: _Context, spec: _WindowSpec, content: str | None, finish_reason: str | None
) -> _Accepted | _Rejected:
    """Parse and ground an answer (ADR 041 §5). Any hard error makes the answer unusable."""
    if content is None:
        return _Rejected(("provider response is not readable",))
    if finish_reason == "length":
        return _Rejected(("the answer was cut at the output token limit (finish_reason length)",))
    try:
        candidates = parse_selection_output(content)
    except SelectionOutputError as error:
        return _Rejected(error.errors)
    refs = _unit_refs(context, spec.window)
    result = validate_candidates(
        candidates, refs, spec.core_ordinals, context.search_profile.categories
    )
    if result.errors:
        return _Rejected(result.errors)
    return _Accepted(window_result_json(result, candidates, {ref.id: ref for ref in refs}))


def _unfinished(context: _Context, windows: Sequence[AnalysisWindow]) -> tuple[str, str | None]:
    """Status and code of a run that did not get every window: a stop wins, then a cancel."""
    if context.stop_code is not None:
        return RUN_FAILED, context.stop_code
    if context.cancel.is_set():
        return RUN_CANCELLED, None
    return RUN_FAILED, _first_failure(windows)


def _first_failure(windows: Sequence[AnalysisWindow]) -> str | None:
    if any(window.state == _UNKNOWN for window in windows):
        return CODE_UNKNOWN_OUTCOME
    return next(
        (
            window.error_code
            for window in windows
            if window.state == _FAILED and window.error_code is not None
        ),
        None,
    )


def _summary(window: AnalysisWindow) -> WindowSummary:
    return WindowSummary(
        idx=window.idx,
        state=window.state,
        attempts=window.attempts,
        repair_used=window.repair_used,
        error_code=window.error_code,
    )
