"""AI selection runs as ``analysis`` jobs (ADR 041 §9–§11, §19; as ``transcription_jobs``).

``create`` checks the request, stores a ``queued`` run with its ``pending`` windows and its
``analysis`` job in one write transaction, and hands the job to one worker thread that runs the
jobs one after another. Nothing is sent before every rule has passed. The rules are checked in the
order of ADR 041 §10, and the first one that fails decides the answer:

1. 422 for the body: ``profile`` (``details.field`` is ``profile.<field>``), ``model`` (the
   request's, else ``analysis_model`` of the settings; it must be in the table of the mode, and
   ``ANALYSIS_MODEL_REQUIRED`` when there is none) and ``budget_usd`` (the rule of the settings;
   ``BUDGET_REQUIRED`` when neither the request nor the settings give one);
2. 404 ``SOURCE_NOT_FOUND``, then 404 ``TRANSCRIPT_NOT_FOUND``;
3. 422 ``TRANSCRIPT_SOURCE_MISMATCH``, then 422 ``ANALYSIS_MODEL_NOT_SUPPORTED`` when the model
   cannot take the smallest window (ADR 041 §16). That check needs the transcript, so it comes
   after the two 404s;
4. 409 ``TRANSCRIPT_REVISION_STALE`` (``details.latest_id``) when the transcript is not the newest
   revision of its source;
5. 409 ``TRANSCRIPT_PARTIAL`` when a coverage span is not ``succeeded`` and ``allow_partial`` is
   false;
6. 409 ``API_KEY_REQUIRED`` in production without a key (test mode needs none, ADR 041 §2);
7. under the admission lock: 503 ``SERVICE_STOPPING`` once ``shutdown`` has begun, then 409
   ``ANALYSIS_RUN_ACTIVE`` (``details``: ``run_id``, ``job_id``) when a job of this source is
   ``queued``, ``running`` or ``cancelling``.

``estimate`` answers what a run would reserve before its first request (``POST
/v1/analysis-runs/estimate``). It applies rules 1 to 3 (a missing budget is not an error there),
needs neither a key nor a provider, and writes and sends nothing.

Under the write transaction ``create`` checks rule 4 again, because a revision may have been
imported meanwhile, and writes the run, its windows and the job together: a failure leaves none of
them. The job is handed to the worker after that commit and inside the admission lock, so
``shutdown`` cannot begin in between.

The checkpoint is ``{run_id, budget_usd}``; ``error`` is added after a failure (the convention of
``services/jobs.py``). It never holds the key: ``KeyedChatClient`` reads the key from the store when
a request is sent, hands it to the factory and forgets it. The model and the search profile are read
from the run, not from the checkpoint.

Job states follow the run: ``completed`` -> ``succeeded``; ``cancelled`` (or a user cancel that left
something undone) -> ``cancelled``; ``failed`` -> ``failed`` with the run's error code as ``error``;
a run stopped by ``shutdown`` -> ``interrupted``. Progress is ``{stage, total, succeeded, failed,
unknown_outcome}`` (window counts), written by ``_finish`` when the run ends by itself, cancelled
included. ``_interrupt`` (shutdown) and ``_finish_failed`` write no counts: an interrupted job keeps
the stage it had; a job ``_finish_failed`` ends carries ``{stage: failed}`` only. The live state of
every window is in ``get_run``.

Nothing is sent again by itself. After a restart ``JobRunner.recover`` makes the unfinished jobs
``interrupted``, and ``recover`` here settles their runs
(``AnalysisRunService.recover_interrupted``). Only ``resume`` starts a worker again. A job can be
resumed when it is ``interrupted``, ``failed`` or ``cancelled`` and its run is not ``completed``.
The checks of a resume are made in this order: 422 for a bad ``budget_usd``; under the admission
lock 503 ``SERVICE_STOPPING``; 404 or 409 ``JOB_NOT_RESUMABLE``; 409 ``ANALYSIS_RUN_ACTIVE`` for
another open job of the source; then ``recover_interrupted`` settles the windows a dead call left
``running``; 409 ``UNKNOWN_OUTCOME_REQUIRES_DECISION`` (``details.count``) while windows are
``unknown_outcome`` and ``retry_unknown`` is false; and last 409 ``API_KEY_REQUIRED``. A refused
resume changes no job and sends nothing, but it may first have settled interrupted windows: the two
last refusals come after that settling, so a window may already be ``unknown_outcome`` or
``pending`` when they are returned. A window that failed starts again from its first request (ADR
041 §9).

``cancel`` ends the work this process owns: a queued job at once, a running one through the event
the run service watches (the windows not yet sent). A request already on the wire cannot be
recalled, so its answer is kept like any other. ``shutdown`` stops the same way and waits for the
worker: a run that is not complete then ends ``interrupted`` with its job, and the windows that were
never sent are ``pending`` again.

Messages are fixed Ukrainian text, never an exception text, a path or provider text.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, NoReturn

from sqlalchemy import literal_column, select
from sqlalchemy.orm import Session

from clipforge.adapters.openrouter_chat import (
    AnalysisProfile,
    ChatError,
    ChatResult,
    OpenRouterChat,
    analysis_profiles,
)
from clipforge.adapters.openrouter_stt import resolve_base_url
from clipforge.config import Config, ConfigError, Mode
from clipforge.domain.search_profile import ProfileError, SearchProfile, parse_search_profile
from clipforge.services.analysis_plan import (
    MODEL_NOT_SUPPORTED,
    RUN_QUEUED,
    AnalysisPlan,
    add_run,
    plan_run,
)
from clipforge.services.analysis_runs import (
    CODE_AUTH,
    CODE_BUDGET,
    CODE_FUNDS,
    CODE_INVALID_OUTPUT,
    CODE_REQUEST_REJECTED,
    CODE_UNKNOWN_OUTCOME,
    CODE_UPSTREAM,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_RUNNING,
    AnalysisRunOutcome,
    AnalysisRunService,
    ChatClient,
)
from clipforge.services.credentials import CredentialStore
from clipforge.services.errors import ErrorKind, ServiceError, conflict, invalid, not_found
from clipforge.services.jobs import JobView, job_view
from clipforge.services.settings import SettingsService, check_run_budget
from clipforge.services.spend import ceiling_to_micro_usd
from clipforge.storage import (
    AnalysisRunRepository,
    AnalysisWindow,
    Clip,
    Database,
    JobRepository,
    SourceRepository,
    SpendRepository,
    TranscriptRepository,
    TranscriptRevision,
)
from clipforge.storage.repositories import MAX_PAGE_SIZE

ANALYSIS_JOB: Final = "analysis"
RUN_INTERRUPTED: Final = "interrupted"
CODE_FAILED: Final = "ANALYSIS_FAILED"
CODE_SERVICE_STOPPING: Final = "SERVICE_STOPPING"
CODE_UNKNOWN_OUTCOME_REQUIRES_DECISION: Final = "UNKNOWN_OUTCOME_REQUIRES_DECISION"

_PENDING: Final = "pending"
_CANCELLED_WINDOW: Final = "cancelled"
_SUCCEEDED: Final = "succeeded"
_UNKNOWN: Final = "unknown_outcome"

_OPEN_JOB_STATES: Final = ("queued", "running", "cancelling")
_ACTIVE_JOB_STATES: Final = ("running", "cancelling")
_RESUMABLE_JOB_STATES: Final = ("interrupted", "failed", "cancelled")
_UNFINISHED_RUN_STATUSES: Final = (RUN_QUEUED, RUN_RUNNING, RUN_INTERRUPTED)
_DIAGNOSTIC_SECTIONS: Final = ("dropped", "rejected", "suppressed")

_JOB_ERRORS: Final[Mapping[str, str]] = {
    CODE_AUTH: "OpenRouter не прийняв ключ API. Перевірте ключ у Налаштуваннях і відновіть аналіз",
    CODE_FUNDS: (
        "На рахунку OpenRouter недостатньо коштів для аналізу. Поповніть баланс і відновіть аналіз"
    ),
    CODE_BUDGET: "Ліміт витрат запуску вичерпано: аналіз зупинено",
    CODE_UNKNOWN_OUTCOME: (
        "Для частини запитів невідомо, чи їх оброблено й оплачено. Автоматичного повтору немає: "
        "рішення про повтор приймає користувач"
    ),
    CODE_UPSTREAM: "Провайдер моделі тимчасово недоступний. Відновіть аналіз пізніше",
    CODE_REQUEST_REJECTED: (
        "Провайдер відхилив запит аналізу: модель може не підтримувати структуровану відповідь. "
        "Оберіть іншу перевірену модель"
    ),
    CODE_INVALID_OUTPUT: (
        "Модель двічі повернула відповідь, яку не вдалося перевірити. Моменти не збережено; "
        "відновлення надішле частину повторно (платно)"
    ),
}
_JOB_FAILED_MESSAGE: Final = "Аналіз завершився помилкою"
_MODEL_GONE_MESSAGE: Final = "Модель аналізу більше не підтримується. Оберіть перевірену модель"

_LOGGER = logging.getLogger(__name__)


# --- the chat client --------------------------------------------------------------------------

ChatClientFactory = Callable[[str | None], ChatClient]
"""``api_key -> client``: how the key becomes a client. ``None`` reaches it only in test mode,
where the loopback test provider needs no key (ADR 041 §2). Tests pass a factory that returns a
fake and never see a real key."""


def openrouter_chat_factory(config: Config) -> ChatClientFactory:
    """The production factory:
    ``OpenRouterChat(key, mode=config.mode, base_url=resolve_base_url(config))``.
    The address is resolved when a request is sent, not here: test mode without a test provider is
    refused at that moment (paid AI is never reachable from tests), and nothing else is affected."""

    def build(api_key: str | None) -> ChatClient:
        return OpenRouterChat(api_key, mode=config.mode, base_url=resolve_base_url(config))

    return build


def unconfigured_chat_factory(_api_key: str | None) -> NoReturn:
    """The factory of a runtime that was given no provider: every request is refused."""
    raise ConfigError("Постачальника аналізу не налаштовано")


class KeyedChatClient:
    """A ``ChatClient`` that fetches the API key at the moment a request is sent (the pattern of
    ``KeyedSttClient``). The key is a local variable of ``complete``: not an attribute, not in a
    job, not in a message.

    * production without a key is ``ChatError("auth")``, and nothing is sent (ADR 041 §2);
    * test mode without a key gives the factory ``None``;
    * a factory that cannot build a client in this mode (``ConfigError``, or ``ValueError`` for a
      key the adapter refuses) is ``invalid_request``, and nothing is sent.

    From the moment the client's ``complete`` is called a request may be on the wire. Its
    ``ChatError`` passes through unchanged; any other exception is a defect and is not translated
    here, so the worker settles the run and the window stays ``unknown_outcome``.
    """

    def __init__(
        self, credentials: CredentialStore, factory: ChatClientFactory, *, mode: Mode
    ) -> None:
        self._credentials = credentials
        self._factory = factory
        self._mode = mode

    def complete(self, body: Mapping[str, object]) -> ChatResult:
        key = self._credentials.openrouter_key()
        if key is None and self._mode == "production":
            raise ChatError("auth", detail="Ключ API не задано.")
        try:
            client = self._factory(key)
        except (ConfigError, ValueError):
            raise ChatError(
                "invalid_request", detail="Постачальник аналізу недоступний у цьому режимі."
            ) from None
        return client.complete(body)

    def __repr__(self) -> str:
        return "KeyedChatClient()"


# --- views ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreatedAnalysisRun:
    id: str
    job_id: str
    status: str


@dataclass(frozen=True, slots=True)
class AnalysisEstimate:
    """What a run would reserve before its first request (ADR 041 §10). ``estimate_micro_usd`` is
    the sum of the first reservations; the prices are the decimal texts sent to the provider.
    ``budget_usd`` is the effective budget (``None``: none is set) and ``within_budget`` compares
    the estimate with it (``None`` without a budget)."""

    model: str
    unit_level: str
    unit_count: int
    window_count: int
    estimate_micro_usd: int
    prompt_usd_per_mtok: str
    completion_usd_per_mtok: str
    max_tokens: int
    price_basis: str
    budget_usd: str | None
    within_budget: bool | None


@dataclass(frozen=True, slots=True)
class WindowCounts:
    """Windows by answer. ``pending`` holds every window that has no answer yet: ``pending``,
    ``running`` and ``cancelled`` (a cancelled window is sent again by ``resume``)."""

    total: int
    succeeded: int
    failed: int
    unknown_outcome: int
    pending: int


@dataclass(frozen=True, slots=True)
class AnalysisRunView:
    """``GET /v1/analysis-runs/{id}``. ``clip_ids`` are the suggested clips in the order they were
    kept (ADR 041 §7). ``stale`` is true when the run's transcript is not the newest revision of
    its source (ADR 041 §11). ``diagnostics_summary`` counts the entries of each diagnostics
    section by reason."""

    id: str
    source_id: str
    transcript_id: str
    transcript_revision: int
    model: str
    provider: str
    prompt_version: str
    schema_version: int
    unit_level: str
    status: str
    error_code: str | None
    created_at: str
    updated_at: str
    job_id: str | None
    committed_micro_usd: int
    windows: WindowCounts
    clip_ids: tuple[str, ...]
    stale: bool
    diagnostics_summary: Mapping[str, Mapping[str, int]]


@dataclass(frozen=True, slots=True)
class AnalysisRunListItem:
    """A run of a source as ``GET /v1/analysis-runs?source_id=`` lists it: without windows and
    clips, with its job and the money committed so far."""

    id: str
    source_id: str
    transcript_id: str
    transcript_revision: int
    model: str
    status: str
    error_code: str | None
    job_id: str | None
    committed_micro_usd: int
    created_at: str


@dataclass(frozen=True, slots=True)
class _Plan:
    """What a worker needs for one run: the run, its model and its budget."""

    run_id: str
    model: str
    ceiling_usd: Decimal


class _CorruptCheckpoint(Exception):
    """The checkpoint of a job is not what ``create`` wrote."""


# --- service ----------------------------------------------------------------------------------


class AnalysisJobService:
    def __init__(
        self,
        database: Database,
        runs: AnalysisRunService,
        settings: SettingsService,
        credentials: CredentialStore,
        *,
        mode: Mode,
        profiles: Mapping[str, AnalysisProfile] | None = None,
    ) -> None:
        """``profiles`` is the table of models the service accepts; ``None`` takes the table of
        ``mode`` (production: only live-probed models, ADR 041 §1, §24)."""
        self._database = database
        self._runs = runs
        self._settings = settings
        self._credentials = credentials
        self._key_required = mode == "production"
        self._profiles: Mapping[str, AnalysisProfile] = (
            analysis_profiles(mode) if profiles is None else profiles
        )
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        # One decision at a time on whether a job may start (create, resume).
        self._admission = threading.Lock()
        self._stopping = threading.Event()
        # One worker: one run at a time (the run service sends the windows of a run in turn).
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")

    # -- create / estimate -------------------------------------------------------------------

    def create(
        self,
        *,
        source_id: str,
        transcript_id: str,
        profile: object,
        model: str | None = None,
        budget_usd: str | int | float | None = None,
    ) -> CreatedAnalysisRun:
        """Start a run of ``transcript_id`` (``POST /v1/analysis-runs``). The checks run in the
        order of the module docstring; nothing is written before the last one passed."""
        search_profile = _parse_profile(profile)
        analysis = self._resolve_model(model)
        budget = self._resolve_budget(budget_usd)
        if budget is None:
            raise invalid(
                "BUDGET_REQUIRED",
                "Вкажіть ліміт витрат для цього запуску або задайте його в Налаштуваннях",
                field="budget_usd",
            )
        with self._database.session() as session:
            revision, plan = _read_plan(
                session,
                source_id=source_id,
                transcript_id=transcript_id,
                search_profile=search_profile,
                analysis=analysis,
            )
            _require_latest(TranscriptRepository(session), revision)
            if not search_profile.allow_partial and _is_partial(revision.coverage):
                raise conflict(
                    "TRANSCRIPT_PARTIAL",
                    "Транскрипт розпізнано не повністю. Дозвольте аналіз часткового транскрипту "
                    "або розпізнайте решту",
                )
        if self._key_required and not self._credentials.has_openrouter_key:
            raise conflict("API_KEY_REQUIRED", "Спершу задайте ключ API OpenRouter у Налаштуваннях")

        with self._admission:
            self._require_accepting()
            self._require_no_open_job(source_id)
            run_id, job_id = self._write_run(
                source_id=source_id,
                transcript_id=transcript_id,
                search_profile=search_profile,
                analysis=analysis,
                plan=plan,
                budget=budget,
            )
            # Inside the lock: ``shutdown`` cannot begin between the commit and the hand-over.
            self._submit(job_id, retry_unknown=False)
        return CreatedAnalysisRun(id=run_id, job_id=job_id, status=RUN_QUEUED)

    def _write_run(
        self,
        *,
        source_id: str,
        transcript_id: str,
        search_profile: SearchProfile,
        analysis: AnalysisProfile,
        plan: AnalysisPlan,
        budget: str,
    ) -> tuple[str, str]:
        """The run, its windows and its job in one transaction; ``(run_id, job_id)``."""
        with self._database.write_session() as session:
            transcripts = TranscriptRepository(session)
            revision = _require_revision(transcripts, transcript_id)
            _require_latest(transcripts, revision)
            run_id = add_run(
                session,
                plan,
                source_id=source_id,
                revision=revision,
                search_profile=search_profile,
                analysis_profile=analysis,
            )
            job = JobRepository(session).add(
                type=ANALYSIS_JOB,
                progress_json={"stage": "queued"},
                checkpoint_json={"run_id": run_id, "budget_usd": budget},
            )
            return run_id, job.id

    def estimate(
        self,
        *,
        source_id: str,
        transcript_id: str,
        profile: object,
        model: str | None = None,
        budget_usd: str | int | float | None = None,
    ) -> AnalysisEstimate:
        """The cost a run with these values would reserve for its first requests (``POST
        /v1/analysis-runs/estimate``). Nothing is written or sent, and neither the key nor a
        provider is needed. The values are checked as in ``create`` (422), then the source and the
        transcript (404, 422)."""
        search_profile = _parse_profile(profile)
        analysis = self._resolve_model(model)
        budget = self._resolve_budget(budget_usd)
        with self._database.session() as session:
            _, plan = _read_plan(
                session,
                source_id=source_id,
                transcript_id=transcript_id,
                search_profile=search_profile,
                analysis=analysis,
            )
        within_budget = (
            None
            if budget is None
            else plan.estimate_micro_usd <= ceiling_to_micro_usd(Decimal(budget))
        )
        return AnalysisEstimate(
            model=analysis.model,
            unit_level=plan.unit_level,
            unit_count=len(plan.units),
            window_count=len(plan.windows),
            estimate_micro_usd=plan.estimate_micro_usd,
            prompt_usd_per_mtok=str(analysis.prompt_usd_per_mtok),
            completion_usd_per_mtok=str(analysis.completion_usd_per_mtok),
            max_tokens=analysis.max_tokens,
            price_basis=analysis.price_basis,
            budget_usd=budget,
            within_budget=within_budget,
        )

    def _resolve_model(self, model: str | None) -> AnalysisProfile:
        """The request's model, else the settings' ``analysis_model``; it must be in the table."""
        chosen = self._settings.get().analysis_model if model is None else model
        if chosen is None:
            raise invalid(
                "ANALYSIS_MODEL_REQUIRED",
                "Оберіть модель аналізу в Налаштуваннях або вкажіть її в запиті",
                field="model",
            )
        profile = self._profiles.get(chosen)
        if profile is None:
            raise invalid(
                MODEL_NOT_SUPPORTED,
                "Ця модель аналізу не підтримується. Оберіть перевірену модель",
                field="model",
            )
        return profile

    def _resolve_budget(self, budget_usd: str | int | float | None) -> str | None:
        """The request's budget, else the settings' ``run_budget_usd``, each checked by the rule of
        the setting. ``None`` means that neither gives one: what that means is the caller's."""
        if budget_usd is None:
            return self._settings.get().run_budget_usd
        return check_run_budget(_budget_text(budget_usd), field="budget_usd")

    # -- reads -------------------------------------------------------------------------------

    def list_runs(self, source_id: str) -> tuple[AnalysisRunListItem, ...]:
        """Every run of the source, the newest first (``GET /v1/analysis-runs?source_id=``); 404
        ``SOURCE_NOT_FOUND`` for an unknown source."""
        with self._database.session() as session:
            if SourceRepository(session).get(source_id) is None:
                raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
            jobs = JobRepository(session)
            spend = SpendRepository(session)
            items: list[AnalysisRunListItem] = []
            for run in AnalysisRunRepository(session).list_runs_for_source(source_id):
                job = jobs.find_by_checkpoint(job_type=ANALYSIS_JOB, key="run_id", value=run.id)
                items.append(
                    AnalysisRunListItem(
                        id=run.id,
                        source_id=run.source_id,
                        transcript_id=run.transcript_id,
                        transcript_revision=run.transcript_revision,
                        model=run.model,
                        status=run.status,
                        error_code=run.error_code,
                        job_id=job.id if job is not None else None,
                        committed_micro_usd=spend.committed_micro_usd(run.id),
                        created_at=run.created_at,
                    )
                )
        return tuple(items)

    def get_run(self, run_id: str) -> AnalysisRunView:
        """One run with its windows, clips, money and diagnostics (``GET
        /v1/analysis-runs/{id}``); 404 ``ANALYSIS_RUN_NOT_FOUND`` for an unknown run."""
        with self._database.session() as session:
            runs = AnalysisRunRepository(session)
            run = runs.get_run(run_id)
            if run is None:
                raise not_found("ANALYSIS_RUN_NOT_FOUND", "Запуск аналізу не знайдено")
            job = JobRepository(session).find_by_checkpoint(
                job_type=ANALYSIS_JOB, key="run_id", value=run_id
            )
            latest = _latest_revision(TranscriptRepository(session), run.source_id)
            return AnalysisRunView(
                id=run.id,
                source_id=run.source_id,
                transcript_id=run.transcript_id,
                transcript_revision=run.transcript_revision,
                model=run.model,
                provider=run.provider,
                prompt_version=run.prompt_version,
                schema_version=run.schema_version,
                unit_level=run.unit_level,
                status=run.status,
                error_code=run.error_code,
                created_at=run.created_at,
                updated_at=run.updated_at,
                job_id=job.id if job is not None else None,
                # The ledger is the source of truth for money; this is the same sum.
                committed_micro_usd=SpendRepository(session).committed_micro_usd(run_id),
                windows=_window_counts(runs.list_windows(run_id)),
                clip_ids=_clip_ids(session, run_id),
                stale=latest is None or latest.id != run.transcript_id,
                diagnostics_summary=_diagnostics_summary(run.diagnostics_json),
            )

    def _require_no_open_job(self, source_id: str) -> None:
        with self._database.session() as session:
            runs = AnalysisRunRepository(session)
            for job in JobRepository(session).list_by_type(ANALYSIS_JOB, _OPEN_JOB_STATES):
                run_id = _checkpoint_text(job.checkpoint_json, "run_id")
                run = runs.get_run(run_id) if run_id is not None else None
                if run is not None and run.source_id == source_id:
                    raise conflict(
                        "ANALYSIS_RUN_ACTIVE",
                        "Для цього джерела аналіз уже виконується",
                        details={"run_id": run.id, "job_id": job.id},
                    )

    # -- cancel / resume ---------------------------------------------------------------------

    def cancel(self, job_id: str) -> JobView:
        """Cancel an ``analysis`` job (``POST /v1/jobs/{id}/cancel``).

        A queued job is cancelled at once, with its run and windows (nothing was sent). A running
        job becomes ``cancelling``: the windows not yet sent are cancelled, the answer on the wire
        is kept, and the worker then writes ``cancelled``. Cancelling a job that is already
        ``cancelling`` or ``cancelled`` changes nothing. 404 ``JOB_NOT_FOUND`` also for a job of
        another type, which this service does not own.
        """
        wake: threading.Event | None = None
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None or job.type != ANALYSIS_JOB:
                raise not_found("JOB_NOT_FOUND", "Завдання не знайдено")
            if job.state == "queued":
                if jobs.transition(
                    job_id,
                    "cancelled",
                    from_states=("queued",),
                    progress_json={"stage": "cancelled"},
                ):
                    run_id = _checkpoint_text(job.checkpoint_json, "run_id")
                    if run_id is not None:
                        _cancel_unstarted_run(session, run_id)
            elif job.state == "running":
                if jobs.transition(job_id, "cancelling", from_states=("running",)):
                    with self._lock:
                        wake = self._events.get(job_id)
            elif job.state not in ("cancelling", "cancelled"):
                raise conflict("JOB_NOT_CANCELLABLE", "Завдання вже завершено або перервано")
            session.refresh(job)
            view = job_view(job)
        if wake is not None:
            wake.set()
        return view

    def resume(
        self,
        job_id: str,
        retry_unknown: bool,
        budget_usd: str | int | float | None = None,
    ) -> JobView:
        """Continue a job from its stored windows (``POST /v1/jobs/{id}/resume``). The checks run
        in the order of the module docstring; a refused resume may have settled interrupted
        windows already (see there) but sends nothing. ``budget_usd``
        (the rule of ``create``) replaces the budget of the checkpoint; ``None`` keeps it."""
        budget = (
            None
            if budget_usd is None
            else check_run_budget(_budget_text(budget_usd), field="budget_usd")
        )
        with self._admission:
            self._require_accepting()
            run_id, source_id = self._resumable_run(job_id)
            self._require_no_open_job(source_id)
            self._runs.recover_interrupted(run_id)
            unknown = self._count_unknown(run_id)
            if unknown and not retry_unknown:
                raise conflict(
                    CODE_UNKNOWN_OUTCOME_REQUIRES_DECISION,
                    "Для частини запитів невідомо, чи їх оброблено й оплачено. Повтор може "
                    "оплатити їх удруге: підтвердіть його явно",
                    details={"count": unknown},
                )
            if self._key_required and not self._credentials.has_openrouter_key:
                # After a restart the key is gone until the shell hands it over again. Without it
                # every window would fail at once, and a window that was ``unknown_outcome`` would
                # lose that marker.
                raise conflict(
                    "API_KEY_REQUIRED", "Спершу задайте ключ API OpenRouter у Налаштуваннях"
                )
            view = self._requeue(job_id, budget)
            # Inside the lock: ``shutdown`` cannot begin between ``queued`` and the hand-over.
            self._submit(job_id, retry_unknown=retry_unknown)
        return view

    def _resumable_run(self, job_id: str) -> tuple[str, str]:
        """``(run_id, source_id)`` of a job that may be resumed."""
        with self._database.session() as session:
            job = JobRepository(session).get(job_id)
            if job is None:
                raise not_found("JOB_NOT_FOUND", "Завдання не знайдено")
            run_id = _checkpoint_text(job.checkpoint_json, "run_id")
            run = AnalysisRunRepository(session).get_run(run_id) if run_id is not None else None
            if (
                job.type != ANALYSIS_JOB
                or job.state not in _RESUMABLE_JOB_STATES
                or run_id is None
                or run is None
                or run.status == RUN_COMPLETED
            ):
                raise _not_resumable()
            return run_id, run.source_id

    def _count_unknown(self, run_id: str) -> int:
        with self._database.session() as session:
            return sum(
                1
                for window in AnalysisRunRepository(session).list_windows(run_id)
                if window.state == _UNKNOWN
            )

    def _requeue(self, job_id: str, budget_usd: str | None) -> JobView:
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None:
                raise not_found("JOB_NOT_FOUND", "Завдання не знайдено")
            checkpoint = _without_error(job.checkpoint_json)
            if budget_usd is not None:
                checkpoint["budget_usd"] = budget_usd
            if not jobs.transition(
                job_id,
                "queued",
                from_states=_RESUMABLE_JOB_STATES,
                progress_json={"stage": "queued"},
            ):
                raise _not_resumable()
            jobs.set_checkpoint(job_id, checkpoint)
            session.refresh(job)
            return job_view(job)

    # -- recovery / shutdown -----------------------------------------------------------------

    def recover(self) -> None:
        """Start-up recovery, after ``JobRunner.recover`` made the unfinished jobs ``interrupted``.

        Nothing is sent. The run of every interrupted job that was still ``queued`` or ``running``
        becomes ``interrupted``, and its windows that were ``running`` are settled by the run
        service: one whose request may have gone out is ``unknown_outcome``, one that died before
        any request is ``pending`` again."""
        with self._database.session() as session:
            runs = AnalysisRunRepository(session)
            pending: list[str] = []
            for job in JobRepository(session).list_by_type(ANALYSIS_JOB, ("interrupted",)):
                run_id = _checkpoint_text(job.checkpoint_json, "run_id")
                run = runs.get_run(run_id) if run_id is not None else None
                if run is not None and run.status in (RUN_QUEUED, RUN_RUNNING):
                    pending.append(run.id)
        for run_id in pending:
            try:
                self._runs.recover_interrupted(run_id)
                with self._database.write_session() as session:
                    AnalysisRunRepository(session).update_run(run_id, status=RUN_INTERRUPTED)
            except Exception as exc:
                _LOGGER.error(
                    "Analysis run %s was not settled after the restart (%s)",
                    run_id,
                    type(exc).__name__,
                )

    def shutdown(self) -> None:
        """Stop sending, end the owned work and wait for the worker. An answer that is already on
        its way is waited for (it cannot be recalled) and kept. A run that is not complete is left
        ``interrupted`` together with its job (see ``_interrupt``); a job the user had cancelled
        ends ``cancelled``. Jobs that never started stay ``queued``: the next start recovers them
        like after a crash.

        The flag is set under the admission lock: a ``create`` or ``resume`` that is under way
        finishes first (its job reaches the worker and is stopped with it), every later one is
        refused with 503 before it writes anything."""
        with self._admission:
            self._stopping.set()
        with self._lock:
            for event in self._events.values():
                event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _require_accepting(self) -> None:
        """503 ``SERVICE_STOPPING`` (retryable: after the next start) once ``shutdown`` has begun.
        Called under the admission lock and before anything is written, so a refused request leaves
        no ``queued`` job that no worker will ever pick up."""
        if self._stopping.is_set():
            raise ServiceError(
                ErrorKind.UNAVAILABLE,
                CODE_SERVICE_STOPPING,
                "Застосунок завершує роботу. Повторіть запит після наступного запуску",
                retryable=True,
            )

    # -- worker ------------------------------------------------------------------------------

    def _submit(self, job_id: str, *, retry_unknown: bool) -> None:
        """Hand a ``queued`` job to the worker. Called under the admission lock after
        ``_require_accepting`` passed; ``shutdown`` needs that lock to stop the executor's input, so
        the executor is open here."""
        event = threading.Event()
        with self._lock:
            self._events[job_id] = event
        self._executor.submit(self._run, job_id, event, retry_unknown)

    def _run(self, job_id: str, cancel: threading.Event, retry_unknown: bool) -> None:
        with self._lock:
            if self._events.get(job_id) is not cancel:
                return  # a later attempt of this job replaced this one while it waited
        try:
            try:
                self._execute(job_id, cancel, retry_unknown)
            except Exception as exc:
                # Never log the exception text: it may carry a path or provider text.
                _LOGGER.error("Analysis job failed unexpectedly (%s)", type(exc).__name__)
                self._abort(job_id)
        except Exception as inner:
            _LOGGER.error("Analysis job could not be marked failed (%s)", type(inner).__name__)
        finally:
            with self._lock:
                if self._events.get(job_id) is cancel:
                    del self._events[job_id]

    def _execute(self, job_id: str, cancel: threading.Event, retry_unknown: bool) -> None:
        plan = self._begin(job_id)
        if plan is None:
            return
        profile = self._profiles.get(plan.model)
        if profile is None:
            # The model left the table after the run was created: nothing was sent for it.
            self._finish_failed(job_id, plan.run_id, MODEL_NOT_SUPPORTED, _MODEL_GONE_MESSAGE)
            return
        try:
            outcome = self._runs.run(
                plan.run_id,
                analysis_profile=profile,
                ceiling_usd=plan.ceiling_usd,
                cancel=cancel,
                retry_unknown=retry_unknown,
            )
        except ServiceError as error:
            # Refused before anything was sent (the options differ, the run is active, ...) or
            # broken half way: settle the windows exactly as after any other abort.
            self._settle_windows(plan.run_id)
            self._finish_failed(job_id, plan.run_id, error.code, error.message)
            return
        # Shutting down: the halt is not the user's decision, so the run is not `cancelled`. If the
        # user did cancel meanwhile, `_interrupt` declines and that decision is finished below.
        if (
            self._stopping.is_set()
            and outcome.status != RUN_COMPLETED
            and self._interrupt(job_id, plan.run_id)
        ):
            return
        self._finish(job_id, outcome, cancelled=cancel.is_set())

    def _begin(self, job_id: str) -> _Plan | None:
        """Move the job to ``running``; ``None`` when it is no longer queued (cancelled while it
        waited)."""
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None or job.state != "queued":
                return None
            checkpoint = _checkpoint_of(job.checkpoint_json)
            if checkpoint is None:
                raise _CorruptCheckpoint
            run_id, ceiling = checkpoint
            run = AnalysisRunRepository(session).get_run(run_id)
            if run is None:
                raise _CorruptCheckpoint
            if not jobs.transition(
                job_id, "running", from_states=("queued",), progress_json={"stage": "analyzing"}
            ):
                return None
            return _Plan(run_id=run_id, model=run.model, ceiling_usd=ceiling)

    def _finish(self, job_id: str, outcome: AnalysisRunOutcome, *, cancelled: bool) -> None:
        counts = Counter(window.state for window in outcome.windows)
        error: dict[str, str] | None = None
        if outcome.status == RUN_COMPLETED:
            state, stage = "succeeded", "done"
        elif outcome.status == RUN_CANCELLED or (cancelled and outcome.error_code is None):
            state, stage = "cancelled", "cancelled"
        else:
            state, stage = "failed", "failed"
            error = _job_error(outcome.error_code or CODE_FAILED)
        progress: dict[str, Any] = {
            "stage": stage,
            "total": len(outcome.windows),
            "succeeded": counts[_SUCCEEDED],
            "failed": counts["failed"],
            "unknown_outcome": counts[_UNKNOWN],
        }
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None:
                return
            checkpoint = _without_error(job.checkpoint_json)
            if error is not None:
                checkpoint["error"] = error
            jobs.set_checkpoint(job_id, checkpoint)
            jobs.transition(job_id, state, from_states=_ACTIVE_JOB_STATES, progress_json=progress)

    def _finish_failed(self, job_id: str, run_id: str | None, code: str, message: str) -> None:
        """The job ends ``failed`` with a fixed ``code``/``message``; a run that has not ended
        becomes ``failed`` too, with the same code."""
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None:
                return
            checkpoint = dict(job.checkpoint_json or {})
            checkpoint["error"] = {"code": code, "message": message}
            jobs.set_checkpoint(job_id, checkpoint)
            jobs.transition(
                job_id,
                "failed",
                from_states=("queued", *_ACTIVE_JOB_STATES),
                progress_json={"stage": "failed"},
            )
            if run_id is not None:
                runs = AnalysisRunRepository(session)
                run = runs.get_run(run_id)
                if run is not None and run.status in _UNFINISHED_RUN_STATUSES:
                    runs.update_run(run_id, status=RUN_FAILED, error_code=code)

    def _abort(self, job_id: str) -> None:
        """An unexpected error ended the worker's run: settle what the dead call left behind (its
        threads are gone) and mark the job failed. While the service is stopping the job is
        interrupted instead (``_interrupt``): the stop, not the defect, decides what the user sees,
        and the next start continues from the settled windows."""
        with self._database.session() as session:
            job = JobRepository(session).get(job_id)
            run_id = _checkpoint_text(job.checkpoint_json, "run_id") if job is not None else None
        if run_id is not None:
            self._settle_windows(run_id)
            if self._stopping.is_set() and self._interrupt(job_id, run_id):
                return
        self._finish_failed(job_id, run_id, CODE_FAILED, _JOB_FAILED_MESSAGE)

    def _settle_windows(self, run_id: str) -> None:
        """Settle the windows a ``run`` call that ended abnormally left ``running`` (the recovery
        rule of the run service). The threads of that call are gone, so nothing races with this."""
        try:
            self._runs.recover_interrupted(run_id)
        except Exception as exc:
            _LOGGER.error("Analysis run %s was not settled (%s)", run_id, type(exc).__name__)

    def _interrupt(self, job_id: str, run_id: str) -> bool:
        """The service is stopping and the run is not complete: the job and the run become
        ``interrupted``, as after a crash, and nothing is sent before an explicit resume. Windows
        left ``running`` are settled first; those the halt kept from being sent (``cancelled``)
        were never sent either, so they are ``pending`` again. Answers that came in are kept.

        Returns ``False`` and changes nothing when the job is no longer ``running``: the user
        cancelled it meanwhile (``cancelling``), and that decision is finished by the caller."""
        self._settle_windows(run_id)
        with self._database.write_session() as session:
            if not JobRepository(session).transition(
                job_id, "interrupted", from_states=("running",)
            ):
                return False
            runs = AnalysisRunRepository(session)
            run = runs.get_run(run_id)
            if run is not None and run.status != RUN_COMPLETED:
                for window in runs.list_windows(run_id):
                    if window.state == _CANCELLED_WINDOW:
                        runs.update_window(window.id, state=_PENDING, error_code=None)
                runs.update_run(run_id, status=RUN_INTERRUPTED)
        return True


# --- helpers ----------------------------------------------------------------------------------


def _parse_profile(value: object) -> SearchProfile:
    """The profile of the body, or 422 naming ``profile.<field>`` (ADR 041 §10)."""
    try:
        return parse_search_profile(value)
    except ProfileError as error:
        field = "profile" if error.field is None else f"profile.{error.field}"
        raise invalid(error.code, error.message_uk, field=field) from None


def _budget_text(value: str | int | float) -> object:
    """A JSON number is read as its decimal text (ADR 041 §2): 5 gives "5", 0.5 gives "0.5". A
    string stays as it is, and a bool is not a number: the rule of the budget refuses it."""
    if isinstance(value, bool | str):
        return value
    return str(value)


def _read_plan(
    session: Session,
    *,
    source_id: str,
    transcript_id: str,
    search_profile: SearchProfile,
    analysis: AnalysisProfile,
) -> tuple[TranscriptRevision, AnalysisPlan]:
    """Rules 2 and 3 of ``create`` (ADR 041 §10, §16): the revision and the plan of its windows."""
    if SourceRepository(session).get(source_id) is None:
        raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
    revision = _require_revision(TranscriptRepository(session), transcript_id)
    if revision.source_id != source_id:
        raise invalid(
            "TRANSCRIPT_SOURCE_MISMATCH",
            "Транскрипт належить іншому джерелу",
            field="transcript_id",
        )
    return revision, plan_run(session, transcript_id, search_profile, analysis)


def _require_revision(transcripts: TranscriptRepository, transcript_id: str) -> TranscriptRevision:
    revision = transcripts.get(transcript_id)
    if revision is None:
        raise not_found("TRANSCRIPT_NOT_FOUND", "Транскрипт не знайдено")
    return revision


def _latest_revision(
    transcripts: TranscriptRepository, source_id: str
) -> TranscriptRevision | None:
    """The newest revision of the source, read page by page (as ``transcripts`` does)."""
    latest: TranscriptRevision | None = None
    cursor: str | None = None
    while True:
        page = transcripts.list_page_for_source(source_id, limit=MAX_PAGE_SIZE, after=cursor)
        if page.items:
            latest = page.items[-1]
        if page.next_cursor is None:
            return latest
        cursor = page.next_cursor


def _require_latest(transcripts: TranscriptRepository, revision: TranscriptRevision) -> None:
    """409 ``TRANSCRIPT_REVISION_STALE`` unless ``revision`` is the newest of its source."""
    latest = _latest_revision(transcripts, revision.source_id)
    if latest is not None and latest.id == revision.id:
        return
    raise conflict(
        "TRANSCRIPT_REVISION_STALE",
        "Транскрипт уже має новішу ревізію. Оновіть дані та повторіть",
        details={"latest_id": latest.id if latest is not None else None},
    )


def _is_partial(coverage: Sequence[Mapping[str, Any]]) -> bool:
    """True when some span of the revision is not ``succeeded`` (ADR 041 §10)."""
    return any(span.get("state") != "succeeded" for span in coverage)


def _window_counts(windows: Sequence[AnalysisWindow]) -> WindowCounts:
    counts = Counter(window.state for window in windows)
    succeeded = counts[_SUCCEEDED]
    failed = counts["failed"]
    unknown = counts[_UNKNOWN]
    return WindowCounts(
        total=len(windows),
        succeeded=succeeded,
        failed=failed,
        unknown_outcome=unknown,
        pending=len(windows) - succeeded - failed - unknown,
    )


def _clip_ids(session: Session, run_id: str) -> tuple[str, ...]:
    """The clips of the run in the order they were kept. The aggregation writes them in one
    transaction in that order, and the rowid grows with each insert; ids are random and times can
    share one tick of the clock, so neither can give the order."""
    statement = (
        select(Clip.id)
        .where(Clip.analysis_run_id == run_id)
        .order_by(literal_column("clips.rowid"))
    )
    return tuple(session.scalars(statement))


def _diagnostics_summary(
    diagnostics: Mapping[str, Any] | None,
) -> dict[str, dict[str, int]]:
    """How many entries of each diagnostics section are for each reason (ADR 041 §10)."""
    summary: dict[str, dict[str, int]] = {}
    for section in _DIAGNOSTIC_SECTIONS:
        reasons: Counter[str] = Counter()
        entries = (diagnostics or {}).get(section)
        for entry in entries if isinstance(entries, list) else []:
            reason = entry.get("reason") if isinstance(entry, dict) else None
            if isinstance(reason, str):
                reasons[reason] += 1
        summary[section] = dict(reasons)
    return summary


def _not_resumable() -> ServiceError:
    return conflict("JOB_NOT_RESUMABLE", "Це завдання не можна відновити")


def _checkpoint_text(checkpoint: Mapping[str, Any] | None, key: str) -> str | None:
    value = (checkpoint or {}).get(key)
    return value if isinstance(value, str) else None


def _checkpoint_of(checkpoint: Mapping[str, Any] | None) -> tuple[str, Decimal] | None:
    """``(run_id, budget)`` of a checkpoint that ``create`` or ``resume`` wrote, else ``None``."""
    run_id = _checkpoint_text(checkpoint, "run_id")
    budget = (checkpoint or {}).get("budget_usd")
    if run_id is None or not isinstance(budget, str):
        return None
    try:
        ceiling = Decimal(budget)
    except ArithmeticError:
        return None
    if not ceiling.is_finite() or ceiling < 0:
        return None
    return run_id, ceiling


def _without_error(checkpoint: Mapping[str, Any] | None) -> dict[str, Any]:
    return {name: value for name, value in (checkpoint or {}).items() if name != "error"}


def _job_error(code: str) -> dict[str, str]:
    return {"code": code, "message": _JOB_ERRORS.get(code, _JOB_FAILED_MESSAGE)}


def _cancel_unstarted_run(session: Session, run_id: str) -> None:
    """A run whose job was cancelled before its first request: the run and its pending windows
    are cancelled. A run that has already been through a worker keeps the status it has."""
    runs = AnalysisRunRepository(session)
    run = runs.get_run(run_id)
    if run is None or run.status != RUN_QUEUED:
        return
    for window in runs.list_windows(run_id):
        if window.state == _PENDING:
            runs.update_window(window.id, state=_CANCELLED_WINDOW)
    runs.update_run(run_id, status=RUN_CANCELLED)
