"""STT runs as ``transcription`` jobs (docs/04 sections 4 and 5, ADR 032 and 033).

``create`` validates the request, stores a ``queued`` run and its ``transcription`` job, and starts
one worker thread that runs the jobs one after another (like ``services/exports.py``). Nothing is
sent before every rule passed. The rules are checked in this order, and the first one that fails
decides the answer:

1. 422 for a bad ``model``, ``language`` or ``budget_usd`` (the rules of the settings, field named
   as in the request), and 422 ``BUDGET_REQUIRED`` when neither the request nor the settings give
   a budget;
2. 503 ``MEDIA_TOOLS_UNAVAILABLE`` when FFmpeg or FFprobe was not found at start-up;
3. 404 ``SOURCE_NOT_FOUND`` and 422 ``INVALID_AUDIO_STREAM``: the source is checked before the key;
4. 409 ``TRANSCRIPT_ALREADY_EXISTS`` (``details``: ``transcript_id``, ``run_id``) when the source
   already has a ``completed`` run with the same model, provider and options (audio stream,
   language) and its revision exists, unless ``force`` is true (FR-050, ADR 037). A ``partial`` or
   ``interrupted`` run does not count (it is continued with ``resume``), nor does an imported
   transcript;
5. 409 ``API_KEY_REQUIRED`` when ``CredentialStore`` holds no key;
6. under the admission lock: 503 ``SERVICE_STOPPING`` once ``shutdown`` has begun, then 409
   ``TRANSCRIPTION_RUN_ACTIVE`` when a job of this source is ``queued``, ``running`` or
   ``cancelling``.

``estimate`` answers what a run would cost before it is started (``POST
/v1/transcription-runs/estimate``, ADR 037). It follows rules 1 (a missing budget is not an error
there) and 3, needs neither FFmpeg nor a key, and writes and sends nothing.

The run (with its chunks) is added by ``TranscriptionRunService.create_run`` inside the transaction
that also writes the job: a failure leaves neither. The worker is started inside the admission lock
too, and ``shutdown`` sets its flag under the same lock, so a request is either refused (503,
nothing written) or its job is handed to the worker before the stop: a refused request leaves no
``queued`` job behind.

The job checkpoint is ``{run_id, audio_stream_index, language, budget_usd}``. It never holds the
key: ``KeyedSttClient`` reads the key from the store only when a request is sent, hands it to the
adapter and forgets it, so the key is in no row, file, log or exception. The settings (model,
language, budget) are read once, when the run is created; later changes do not touch a run.

Job states follow the run: ``completed`` -> ``succeeded``; ``cancelled`` (or a user cancel that
left something undone) -> ``cancelled``; ``partial`` and ``failed`` -> ``failed`` with an error
(``STT_AUTH``, ``STT_FUNDS``, ``BUDGET_EXCEEDED``, ``STT_UNKNOWN_OUTCOME``, a chunk code or
``TRANSCRIPTION_PARTIAL``). Job progress is ``{stage, total, succeeded, failed, unknown_outcome}``
(chunk counts, written when the run ends); the live state of every chunk is
``GET /v1/transcription-runs/{id}``.

Nothing is sent again by itself. After a restart ``JobRunner.recover`` makes the unfinished jobs
``interrupted`` and ``recover`` here settles their runs: a chunk whose request may have gone out
becomes ``unknown_outcome`` and the run ``interrupted``. Only ``resume`` (an explicit user request)
starts a worker again. It continues from the stored chunks; a chunk that is ``unknown_outcome`` is
sent again only with ``retry_unknown = true`` (otherwise 409 ``UNKNOWN_OUTCOME_REQUIRES_DECISION``
with the number of such chunks). A job can be resumed when it is ``interrupted``, ``failed`` or
``cancelled``; any other job is 409 ``JOB_NOT_RESUMABLE``. While another job of the same source is
``queued``, ``running`` or ``cancelling`` a resume is 409 ``TRANSCRIPTION_RUN_ACTIVE`` (as for
``create``). The checks of a resume are made in this order: 422 for a bad ``budget_usd``; under the
admission lock 503 ``SERVICE_STOPPING``, 404/409 for a job that cannot be resumed, 409
``TRANSCRIPTION_RUN_ACTIVE`` for another open job of the source, 409
``UNKNOWN_OUTCOME_REQUIRES_DECISION``, and last 409 ``API_KEY_REQUIRED`` (the key is memory-only, so
it is gone after a restart until the shell hands it over again). A refused resume changes nothing.
``budget_usd`` (optional, the rule of ``create``) replaces the budget of the checkpoint: a run that
stopped with ``BUDGET_EXCEEDED`` goes on with a higher ceiling.

Cancelling ends the work this process owns: a queued job at once, a running one through the event
the run service watches (the sleep, the FFmpeg extraction it started, the queue). A request already
on the wire cannot be recalled, so its answer is kept like any other.

``shutdown`` stops the same way and waits for the worker. A run that is not complete then ends
``interrupted`` with its job (the chunks that never went out are ``pending``, one that may have
gone out is ``unknown_outcome``), so the next start finds the same state as after a crash and sends
nothing before an explicit resume. A job the user had already cancelled ends ``cancelled``; a run
that was complete before the stop stays ``completed`` and its job ``succeeded``. An unexpected
error of the worker while the service stops also ends ``interrupted`` (the stop decides, see
``_abort``), whereas a ``ServiceError`` (a refusal such as a changed source) ends ``failed`` with
its code, stopping or not, because it would recur after the restart.

Messages are fixed Ukrainian text, never an exception text, a path or provider text.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, NoReturn

from sqlalchemy.orm import Session

from clipforge.adapters.media_tools import MediaTools, MediaToolsUnavailableError
from clipforge.adapters.openrouter_stt import (
    OpenRouterStt,
    SttError,
    SttResult,
    build_request_body,
    resolve_base_url,
)
from clipforge.adapters.stt_audio import SttAudioRequest
from clipforge.config import Config, ConfigError
from clipforge.domain.chunking import plan_chunks
from clipforge.services.credentials import CredentialStore
from clipforge.services.errors import (
    ErrorKind,
    ServiceError,
    conflict,
    invalid,
    media_tools_unavailable,
    not_found,
)
from clipforge.services.jobs import JobView, job_view
from clipforge.services.settings import (
    SettingsService,
    check_run_budget,
    check_stt_language,
    check_stt_model,
)
from clipforge.services.spend import SpendLedger, ceiling_to_micro_usd, to_micro_usd
from clipforge.services.transcription_runs import (
    PRICE_BASIS,
    PROVIDER,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_QUEUED,
    RUN_RUNNING,
    USD_PER_SECOND_UPPER,
    RunOutcome,
    SttClient,
    SttRunOptions,
    TranscriptionRunService,
    billed_seconds,
    require_source,
)
from clipforge.storage import (
    Database,
    JobRepository,
    SourceRepository,
    SpendRepository,
    TranscriptRepository,
    TranscriptRunRepository,
)

TRANSCRIPTION_JOB: Final = "transcription"
RUN_INTERRUPTED: Final = "interrupted"

CODE_PARTIAL: Final = "TRANSCRIPTION_PARTIAL"
CODE_FAILED: Final = "TRANSCRIPTION_FAILED"
CODE_UNKNOWN_OUTCOME: Final = "STT_UNKNOWN_OUTCOME"

_LOGGER = logging.getLogger(__name__)

_OPEN_JOB_STATES: Final = ("queued", "running", "cancelling")
_ACTIVE_JOB_STATES: Final = ("running", "cancelling")
_RESUMABLE_JOB_STATES: Final = ("interrupted", "failed", "cancelled")
_UNFINISHED_RUN_STATUSES: Final = (RUN_QUEUED, RUN_RUNNING, RUN_INTERRUPTED)

_JOB_ERRORS: Final[Mapping[str, str]] = {
    "STT_AUTH": (
        "OpenRouter не прийняв ключ API. Перевірте ключ у Налаштуваннях і відновіть розпізнавання"
    ),
    "STT_FUNDS": (
        "На рахунку OpenRouter недостатньо коштів: для аудіозапитів потрібен баланс "
        "щонайменше $0,50. Поповніть баланс і відновіть розпізнавання"
    ),
    "BUDGET_EXCEEDED": "Ліміт витрат запуску вичерпано: розпізнавання зупинено",
    CODE_UNKNOWN_OUTCOME: (
        "Для частини запитів невідомо, чи їх оброблено й оплачено. Автоматичного повтору немає: "
        "рішення про повтор приймає користувач"
    ),
    CODE_PARTIAL: "Розпізнано лише частину запису",
}
_JOB_FAILED_MESSAGE: Final = "Розпізнавання завершилося помилкою"
CODE_SERVICE_STOPPING: Final = "SERVICE_STOPPING"


# --- the provider client ----------------------------------------------------------------------


SttClientFactory = Callable[[str], SttClient]
"""``api_key -> client``: how the key becomes a client. Production: ``openrouter_stt_factory``;
tests pass a factory that returns a fake and never see a real key."""


def openrouter_stt_factory(config: Config) -> SttClientFactory:
    """The production factory:
    ``OpenRouterStt(key, mode=config.mode, base_url=resolve_base_url(config))``.
    The address is resolved when a request is sent, not here: test mode without a test provider is
    refused at that moment (paid AI is never reachable from tests), and nothing else is affected."""

    def build(api_key: str) -> SttClient:
        return OpenRouterStt(api_key, mode=config.mode, base_url=resolve_base_url(config))

    return build


def unconfigured_stt_factory(_api_key: str) -> NoReturn:
    """The factory of a runtime that was given no provider: every request is refused."""
    raise ConfigError("Постачальника розпізнавання не налаштовано")


class KeyedSttClient:
    """An ``SttClient`` that fetches the API key at the moment a request is sent.

    The key is a local variable of ``transcribe``: not an attribute, not in a job, not in a message.
    Every failure this method knows leaves as ``SttError``, so the run service settles its
    reservation as for any provider answer, and nothing was sent in any of these cases:

    * no key (deleted meanwhile) is ``auth``;
    * a provider that cannot be built in this mode (``ConfigError``, or ``ValueError`` for a key the
      adapter refuses) is ``invalid_request``;
    * a request the adapter would refuse before it opens a connection (an unsupported model, empty
      audio) is ``invalid_request`` too. It is found by building the request body here, before the
      client is called, so a ``ValueError`` is translated only at that point. Its text is dropped,
      as it could name the model or the audio.

    From the moment the client is called a request may be on the wire, and its answer may have been
    paid for. The client raises only ``SttError``; any other exception (a ``ValueError`` as well) is
    a defect and is not translated here: the worker settles the run (``_abort``) and the chunk is
    ``unknown_outcome``, its reservation still counted, instead of a released one.
    """

    def __init__(self, credentials: CredentialStore, factory: SttClientFactory) -> None:
        self._credentials = credentials
        self._factory = factory

    def transcribe(
        self,
        *,
        model: str,
        audio_wav: bytes,
        language: str | None = None,
        provider_options: Mapping[str, object] | None = None,
    ) -> SttResult:
        key = self._credentials.openrouter_key()
        if key is None:
            raise SttError("auth", detail="Ключ API не задано.")
        try:
            client = self._factory(key)
        except (ConfigError, ValueError):
            raise SttError(
                "invalid_request", detail="Постачальник розпізнавання недоступний у цьому режимі."
            ) from None
        try:
            build_request_body(
                model, audio_wav, language=language, provider_options=provider_options
            )
        except ValueError:
            raise SttError(
                "invalid_request", detail="Запит не пройшов перевірку до відправлення."
            ) from None
        # No ValueError handler from here on: the request may be on the wire (see the class doc).
        return client.transcribe(
            model=model,
            audio_wav=audio_wav,
            language=language,
            provider_options=provider_options,
        )

    def __repr__(self) -> str:
        return "KeyedSttClient()"


def missing_tools_extractor(request: SttAudioRequest, *, cancel: threading.Event) -> NoReturn:
    """The extractor of a machine without FFmpeg: ``create`` refuses first (503), this is the
    second line."""
    raise MediaToolsUnavailableError("FFmpeg або FFprobe недоступні")


# --- views ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreatedRun:
    id: str
    job_id: str
    status: str


@dataclass(frozen=True, slots=True)
class RunChunkView:
    id: str
    core_start_ms: int
    core_end_ms: int
    state: str
    attempts: int
    error_code: str | None


@dataclass(frozen=True, slots=True)
class RunView:
    """``revision_id`` is the transcript revision of the run (``None`` until one exists),
    ``error_code`` the reason the run stopped, ``committed_micro_usd`` what the spend ledger
    counts for it right now. ``chunks`` are those in force (a split parent is not listed)."""

    id: str
    source_id: str
    job_id: str | None
    model: str
    status: str
    revision_id: str | None
    error_code: str | None
    committed_micro_usd: int
    chunks: tuple[RunChunkView, ...]


@dataclass(frozen=True, slots=True)
class RunListItem:
    """A run of a source as ``RunView`` shows it, without the chunks, plus when it was created."""

    id: str
    source_id: str
    job_id: str | None
    model: str
    status: str
    revision_id: str | None
    error_code: str | None
    committed_micro_usd: int
    created_at: str


@dataclass(frozen=True, slots=True)
class ExistingTranscript:
    """The completed run (and its revision) that makes a repeated run a paid duplicate."""

    transcript_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class RunEstimate:
    """What a run would reserve before its first attempt (ADR 037): the plan of ``create``, every
    chunk priced at the upper tariff. ``budget_usd`` is the effective budget (``None``: none is
    set), ``within_budget`` compares the estimate with it (``None`` without a budget)."""

    source_id: str
    audio_stream_index: int
    model: str
    language: str | None
    duration_ms: int
    chunk_count: int
    billed_seconds: int
    usd_per_second_upper: str
    estimate_micro_usd: int
    price_basis: str
    budget_usd: str | None
    within_budget: bool | None
    existing: ExistingTranscript | None


@dataclass(frozen=True, slots=True)
class _Plan:
    run_id: str
    audio_stream_index: int
    language: str | None
    ceiling_usd: Decimal


class _CorruptCheckpoint(Exception):
    """The checkpoint of a job is not what ``create`` wrote."""


# --- service ----------------------------------------------------------------------------------


class TranscriptionJobService:
    def __init__(
        self,
        database: Database,
        runs: TranscriptionRunService,
        ledger: SpendLedger,
        settings: SettingsService,
        credentials: CredentialStore,
        media_tools: MediaTools | None,
    ) -> None:
        self._database = database
        self._runs = runs
        self._ledger = ledger
        self._settings = settings
        self._credentials = credentials
        self._tools = media_tools
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        # One decision at a time on whether a job may start (create, resume).
        self._admission = threading.Lock()
        self._stopping = threading.Event()
        # One worker: one run at a time (the run service has its own two request workers).
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcription")

    # -- create / read -----------------------------------------------------------------------

    def create(
        self,
        *,
        source_id: str,
        audio_stream_index: int,
        model: str | None,
        language: str | None,
        budget_usd: str | None,
        force: bool = False,
    ) -> CreatedRun:
        """``model``, ``language`` and ``budget_usd`` default to the settings when ``None``. The
        checks run in the order of the module docstring. ``force`` skips only the check for an
        existing transcript: the user has confirmed that a second payment is meant."""
        chosen_model, chosen_language, budget = self._resolve(model, language, budget_usd)
        if budget is None:
            raise invalid(
                "BUDGET_REQUIRED",
                "Вкажіть ліміт витрат для цього запуску або задайте його в Налаштуваннях",
                field="budget_usd",
            )
        if self._tools is None:
            raise media_tools_unavailable()
        options = SttRunOptions(audio_stream_index=audio_stream_index, language=chosen_language)
        # The request is checked against the stored source, and against a transcript the user
        # already paid for, before the state of the key is looked at.
        with self._database.session() as session:
            require_source(session, source_id, audio_stream_index)
            existing = None if force else _find_existing(session, source_id, chosen_model, options)
        if existing is not None:
            raise conflict(
                "TRANSCRIPT_ALREADY_EXISTS",
                "Для цього джерела вже є розпізнаний транскрипт із тими самими параметрами. "
                "Повторне розпізнавання буде оплачено вдруге: підтвердіть його явно",
                details={"transcript_id": existing.transcript_id, "run_id": existing.run_id},
            )
        if not self._credentials.has_openrouter_key:
            raise conflict("API_KEY_REQUIRED", "Спершу задайте ключ API OpenRouter у Налаштуваннях")

        with self._admission:
            self._require_accepting()
            self._require_no_open_job(source_id)
            # One transaction for the run, its chunks and the job: a failure leaves none of them.
            with self._database.write_session() as session:
                run_id = self._runs.create_run(
                    source_id=source_id, model=chosen_model, options=options, session=session
                )
                job = JobRepository(session).add(
                    type=TRANSCRIPTION_JOB,
                    progress_json={"stage": "queued"},
                    checkpoint_json={
                        "run_id": run_id,
                        "audio_stream_index": audio_stream_index,
                        "language": chosen_language,
                        "budget_usd": budget,
                    },
                )
                job_id = job.id
            # Inside the lock: ``shutdown`` cannot begin between the commit and the hand-over.
            self._submit(job_id, retry_unknown=False)
        return CreatedRun(id=run_id, job_id=job_id, status=RUN_QUEUED)

    def _resolve(
        self, model: str | None, language: str | None, budget_usd: str | None
    ) -> tuple[str, str | None, str | None]:
        """``(model, language, budget_usd)``: the request's value, else the setting, each checked
        by the rule of the setting (422 names the request field). The budget may still be ``None``:
        what that means is up to the caller."""
        defaults = self._settings.get()
        chosen_model = (
            defaults.stt_model if model is None else check_stt_model(model, field="model")
        )
        chosen_language = (
            defaults.stt_language
            if language is None
            else check_stt_language(language, field="language")
        )
        budget = (
            defaults.run_budget_usd
            if budget_usd is None
            else check_run_budget(budget_usd, field="budget_usd")
        )
        return chosen_model, chosen_language, budget

    def estimate(
        self,
        *,
        source_id: str,
        audio_stream_index: int,
        model: str | None,
        language: str | None,
        budget_usd: str | None,
    ) -> RunEstimate:
        """The cost a run with these values would reserve at most for one attempt per chunk
        (``POST /v1/transcription-runs/estimate``). Nothing is written or sent, and neither the key
        nor FFmpeg is needed. The plan is that of ``create`` (``plan_chunks(...).active``); retries,
        splits and the context that overlaps after a split can cost more, so this is not a bound
        of the provider's bill (docs/05 section 6). The values are checked as in ``create`` (422),
        then the source (404, 422 ``INVALID_AUDIO_STREAM``)."""
        chosen_model, chosen_language, budget = self._resolve(model, language, budget_usd)
        options = SttRunOptions(audio_stream_index=audio_stream_index, language=chosen_language)
        with self._database.session() as session:
            source = require_source(session, source_id, audio_stream_index)
            duration_ms = source.duration_ms
            existing = _find_existing(session, source_id, chosen_model, options)
        windows = plan_chunks(duration_ms).active
        seconds = sum(billed_seconds(window) for window in windows)
        rate = USD_PER_SECOND_UPPER[chosen_model]
        estimate = sum(to_micro_usd(rate * billed_seconds(window)) for window in windows)
        within_budget = (
            None if budget is None else estimate <= ceiling_to_micro_usd(Decimal(budget))
        )
        return RunEstimate(
            source_id=source_id,
            audio_stream_index=audio_stream_index,
            model=chosen_model,
            language=chosen_language,
            duration_ms=duration_ms,
            chunk_count=len(windows),
            billed_seconds=seconds,
            usd_per_second_upper=format(rate, "f"),
            estimate_micro_usd=estimate,
            price_basis=PRICE_BASIS,
            budget_usd=budget,
            within_budget=within_budget,
            existing=existing,
        )

    def list_runs(self, source_id: str) -> tuple[RunListItem, ...]:
        """Every run of the source, the newest first (``GET /v1/transcription-runs?source_id=``);
        404 ``SOURCE_NOT_FOUND`` for an unknown source."""
        with self._database.session() as session:
            if SourceRepository(session).get(source_id) is None:
                raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
            jobs = JobRepository(session)
            spend = SpendRepository(session)
            items: list[RunListItem] = []
            for run in TranscriptRunRepository(session).list_runs_for_source(source_id):
                job = jobs.find_by_checkpoint(
                    job_type=TRANSCRIPTION_JOB, key="run_id", value=run.id
                )
                usage = dict(run.usage or {})
                items.append(
                    RunListItem(
                        id=run.id,
                        source_id=run.source_id,
                        job_id=job.id if job is not None else None,
                        model=run.model,
                        status=run.status,
                        revision_id=_text(usage.get("revision_id")),
                        error_code=_text(usage.get("error_code")),
                        committed_micro_usd=spend.committed_micro_usd(run.id),
                        created_at=run.created_at,
                    )
                )
        return tuple(items)

    def get_run(self, run_id: str) -> RunView:
        with self._database.session() as session:
            runs = TranscriptRunRepository(session)
            run = runs.get_run(run_id)
            if run is None:
                raise not_found("TRANSCRIPTION_RUN_NOT_FOUND", "Запуск розпізнавання не знайдено")
            job = JobRepository(session).find_by_checkpoint(
                job_type=TRANSCRIPTION_JOB, key="run_id", value=run_id
            )
            usage = dict(run.usage or {})
            chunks = tuple(
                RunChunkView(
                    id=chunk.id,
                    core_start_ms=chunk.core_start_ms,
                    core_end_ms=chunk.core_end_ms,
                    state=chunk.state,
                    attempts=chunk.attempts,
                    error_code=chunk.error_code,
                )
                for chunk in runs.list_chunks(run_id)
                if chunk.state != "superseded"
            )
            source_id, model, status = run.source_id, run.model, run.status
            job_id = job.id if job is not None else None
            revision_id = _text(usage.get("revision_id"))
            error_code = _text(usage.get("error_code"))
        return RunView(
            id=run_id,
            source_id=source_id,
            job_id=job_id,
            model=model,
            status=status,
            revision_id=revision_id,
            error_code=error_code,
            # The ledger is the source of truth for money and is current while the run goes on.
            committed_micro_usd=self._ledger.committed_micro_usd(run_id),
            chunks=chunks,
        )

    def _require_no_open_job(self, source_id: str) -> None:
        with self._database.session() as session:
            runs = TranscriptRunRepository(session)
            for job in JobRepository(session).list_by_type(TRANSCRIPTION_JOB, _OPEN_JOB_STATES):
                run_id = _checkpoint_text(job.checkpoint_json, "run_id")
                run = runs.get_run(run_id) if run_id is not None else None
                if run is not None and run.source_id == source_id:
                    raise conflict(
                        "TRANSCRIPTION_RUN_ACTIVE",
                        "Для цього джерела розпізнавання вже виконується",
                        details={"run_id": run.id, "job_id": job.id},
                    )

    # -- cancel / resume ---------------------------------------------------------------------

    def cancel(self, job_id: str) -> JobView:
        """Cancel a transcription job (``POST /v1/jobs/{id}/cancel``).

        A queued job is cancelled at once (a run that never started with it). A running job
        becomes ``cancelling``: the run stops sending, the work this process owns ends and the
        worker then writes ``cancelled`` (what was transcribed is kept). Cancelling a job that is
        already ``cancelling`` or ``cancelled`` changes nothing.
        """
        signal: threading.Event | None = None
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None:
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
                        signal = self._events.get(job_id)
            elif job.state not in ("cancelling", "cancelled"):
                raise conflict("JOB_NOT_CANCELLABLE", "Завдання вже завершено або перервано")
            session.refresh(job)
            view = job_view(job)
        if signal is not None:
            signal.set()
        return view

    def resume(self, job_id: str, retry_unknown: bool, budget_usd: str | None = None) -> JobView:
        """Continue a job from its stored chunks (``POST /v1/jobs/{id}/resume``).

        ``budget_usd`` (the rule of ``create``, 422 ``INVALID_RUN_BUDGET``) replaces the budget of
        the checkpoint; ``None`` keeps it. 409 ``JOB_NOT_RESUMABLE`` unless the job is a
        ``transcription`` job that is ``interrupted``, ``failed`` or ``cancelled`` and has something
        left to do. 409 ``TRANSCRIPTION_RUN_ACTIVE`` (``details.run_id``, ``details.job_id`` of the
        open job) when another job of the same source is ``queued``, ``running`` or ``cancelling``.
        409 ``UNKNOWN_OUTCOME_REQUIRES_DECISION`` (``details.count``) when chunks are
        ``unknown_outcome`` and ``retry_unknown`` is false, 409 ``API_KEY_REQUIRED`` when there is
        no key: nothing is sent and nothing changes. 503 ``SERVICE_STOPPING`` (retryable) once
        ``shutdown`` has begun, also with nothing changed. The checks run in the order of the
        module docstring.
        """
        budget = None if budget_usd is None else check_run_budget(budget_usd, field="budget_usd")
        with self._admission:
            self._require_accepting()
            run_id, source_id = self._resumable_run(job_id)
            self._require_no_open_job(source_id)
            self._runs.recover_interrupted(run_id)
            unknown = self._count_unknown(run_id)
            if unknown and not retry_unknown:
                raise conflict(
                    "UNKNOWN_OUTCOME_REQUIRES_DECISION",
                    "Для частини запитів невідомо, чи їх оброблено й оплачено. Повтор може "
                    "оплатити їх удруге: підтвердіть його явно",
                    details={"count": unknown},
                )
            if not self._credentials.has_openrouter_key:
                # After a restart the key is gone until the shell hands it over again. Without it
                # every chunk would fail at once, and a chunk that was ``unknown_outcome`` would
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
            run = TranscriptRunRepository(session).get_run(run_id) if run_id is not None else None
            if (
                job.type != TRANSCRIPTION_JOB
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
                for chunk in TranscriptRunRepository(session).list_chunks(run_id)
                if chunk.state == "unknown_outcome"
            )

    def _requeue(self, job_id: str, budget_usd: str | None) -> JobView:
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None:
                raise not_found("JOB_NOT_FOUND", "Завдання не знайдено")
            checkpoint = {
                name: value
                for name, value in (job.checkpoint_json or {}).items()
                if name != "error"
            }
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
        becomes ``interrupted``, and its chunks that were ``running`` are settled: one whose
        request may have gone out is ``unknown_outcome`` (its reservation keeps counting), one that
        died before any request is ``pending`` again."""
        with self._database.session() as session:
            runs = TranscriptRunRepository(session)
            pending = []
            for job in JobRepository(session).list_by_type(TRANSCRIPTION_JOB, ("interrupted",)):
                run_id = _checkpoint_text(job.checkpoint_json, "run_id")
                run = runs.get_run(run_id) if run_id is not None else None
                if run is not None and run.status in (RUN_QUEUED, RUN_RUNNING):
                    pending.append(run.id)
        for run_id in pending:
            try:
                self._runs.recover_interrupted(run_id)
                with self._database.write_session() as session:
                    TranscriptRunRepository(session).update_run(run_id, status=RUN_INTERRUPTED)
            except Exception as exc:
                _LOGGER.error(
                    "Transcription run %s was not settled after the restart (%s)",
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
        ``_require_accepting`` passed, and ``shutdown`` needs that lock to stop the executor's
        input, so the executor is open here."""
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
                _LOGGER.error("Transcription job failed unexpectedly (%s)", type(exc).__name__)
                self._abort(job_id)
        except Exception as inner:
            _LOGGER.error("Transcription job could not be marked failed (%s)", type(inner).__name__)
        finally:
            with self._lock:
                if self._events.get(job_id) is cancel:
                    del self._events[job_id]

    def _execute(self, job_id: str, cancel: threading.Event, retry_unknown: bool) -> None:
        plan = self._begin(job_id)
        if plan is None:
            return
        try:
            outcome = self._runs.run(
                plan.run_id,
                options=SttRunOptions(
                    audio_stream_index=plan.audio_stream_index, language=plan.language
                ),
                ceiling_usd=plan.ceiling_usd,
                cancel=cancel,
                retry_unknown=retry_unknown,
            )
        except ServiceError as error:
            # Refused before anything was sent (the source changed, the options differ, ...) or
            # broken half way: the call may have left a chunk `running` with a reservation, so
            # settle the chunks exactly as after any other abort.
            self._settle_chunks(plan.run_id)
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
            plan = _plan_of(job.checkpoint_json)
            if plan is None:
                raise _CorruptCheckpoint
            if not jobs.transition(
                job_id, "running", from_states=("queued",), progress_json={"stage": "transcribing"}
            ):
                return None
            return plan

    def _finish(self, job_id: str, outcome: RunOutcome, *, cancelled: bool) -> None:
        counts = Counter(chunk.state for chunk in outcome.chunks)
        error: dict[str, str] | None = None
        if outcome.status == RUN_COMPLETED:
            state, stage = "succeeded", "done"
        elif outcome.status == RUN_CANCELLED or (cancelled and outcome.error_code is None):
            state, stage = "cancelled", "cancelled"
        else:
            state, stage = "failed", "failed"
            error = _job_error(_failure_code(outcome, counts))
        progress = {
            "stage": stage,
            "total": len(outcome.chunks),
            "succeeded": counts["succeeded"],
            "failed": counts["failed"],
            "unknown_outcome": counts["unknown_outcome"],
        }
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None:
                return
            checkpoint = {
                name: value
                for name, value in (job.checkpoint_json or {}).items()
                if name != "error"
            }
            if error is not None:
                checkpoint["error"] = error
            jobs.set_checkpoint(job_id, checkpoint)
            jobs.transition(job_id, state, from_states=_ACTIVE_JOB_STATES, progress_json=progress)

    def _finish_failed(self, job_id: str, run_id: str | None, code: str, message: str) -> None:
        """The job ends ``failed`` with a fixed ``code``/``message``; a run that has not ended
        becomes ``failed`` too."""
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None:
                return
            checkpoint: dict[str, Any] = dict(job.checkpoint_json or {})
            checkpoint["error"] = {"code": code, "message": message}
            jobs.set_checkpoint(job_id, checkpoint)
            jobs.transition(
                job_id,
                "failed",
                from_states=("queued", *_ACTIVE_JOB_STATES),
                progress_json={"stage": "failed"},
            )
            if run_id is not None:
                runs = TranscriptRunRepository(session)
                run = runs.get_run(run_id)
                if run is not None and run.status in _UNFINISHED_RUN_STATUSES:
                    usage = dict(run.usage or {})
                    usage["error_code"] = code
                    runs.update_run(run_id, status=RUN_FAILED, usage=usage)

    def _abort(self, job_id: str) -> None:
        """An unexpected error ended the worker's run: settle what the dead call left behind (its
        threads are gone) and mark the job failed. While the service is stopping the job is
        interrupted instead (``_interrupt``): the stop, not the defect, decides what the user sees,
        and the next start continues from the settled chunks."""
        with self._database.session() as session:
            job = JobRepository(session).get(job_id)
            run_id = _checkpoint_text(job.checkpoint_json, "run_id") if job is not None else None
        if run_id is not None:
            self._settle_chunks(run_id)
            if self._stopping.is_set() and self._interrupt(job_id, run_id):
                return
        self._finish_failed(job_id, run_id, CODE_FAILED, _JOB_FAILED_MESSAGE)

    def _settle_chunks(self, run_id: str) -> None:
        """Settle the chunks a ``run`` call that ended abnormally left ``running`` (the recovery
        rule of the run service): one whose request may have gone out is ``unknown_outcome`` and its
        reservation stays counted, one that never reserved anything is ``pending`` again. The
        threads of that call are gone, so nothing races with this."""
        try:
            self._runs.recover_interrupted(run_id)
        except Exception as exc:
            _LOGGER.error("Transcription run %s was not settled (%s)", run_id, type(exc).__name__)

    def _interrupt(self, job_id: str, run_id: str) -> bool:
        """The service is stopping and the run is not complete: the job and the run become
        ``interrupted``, as after a crash, and nothing is sent before an explicit resume. Chunks
        left `running` are settled first; those the halt kept from being sent (``cancelled``) were
        never sent either, so they are ``pending``. Answers that came in are kept, with the
        revision written from them.

        Returns ``False`` and changes nothing when the job is no longer ``running``: the user
        cancelled it meanwhile (``cancelling``), and that decision is finished by the caller."""
        self._settle_chunks(run_id)
        with self._database.write_session() as session:
            if not JobRepository(session).transition(
                job_id, "interrupted", from_states=("running",)
            ):
                return False
            runs = TranscriptRunRepository(session)
            run = runs.get_run(run_id)
            if run is not None and run.status != RUN_COMPLETED:
                for chunk in runs.list_chunks(run_id):
                    if chunk.state == "cancelled":
                        runs.update_chunk(chunk.id, state="pending", error_code=None)
                runs.update_run(run_id, status=RUN_INTERRUPTED)
        return True


# --- helpers ----------------------------------------------------------------------------------


def _not_resumable() -> ServiceError:
    return conflict("JOB_NOT_RESUMABLE", "Це завдання не можна відновити")


def _plan_of(checkpoint: dict[str, Any] | None) -> _Plan | None:
    data = checkpoint or {}
    run_id = data.get("run_id")
    index = data.get("audio_stream_index")
    language = data.get("language")
    budget = data.get("budget_usd")
    if (
        not isinstance(run_id, str)
        or isinstance(index, bool)
        or not isinstance(index, int)
        or not (language is None or isinstance(language, str))
        or not isinstance(budget, str)
    ):
        return None
    try:
        ceiling = Decimal(budget)
    except ArithmeticError:
        return None
    if not ceiling.is_finite() or ceiling < 0:
        return None
    return _Plan(run_id, index, language, ceiling)


def _find_existing(
    session: Session, source_id: str, model: str, options: SttRunOptions
) -> ExistingTranscript | None:
    """The newest ``completed`` run of the source with this model, provider and options whose
    revision exists: a second run with them would transcribe the same thing and pay again."""
    options_hash = options.options_hash()
    transcripts = TranscriptRepository(session)
    for run in TranscriptRunRepository(session).list_runs_for_source(source_id):
        if (
            run.status != RUN_COMPLETED
            or run.model != model
            or run.provider != PROVIDER
            or run.options_hash != options_hash
        ):
            continue
        revision_id = _text((run.usage or {}).get("revision_id"))
        if revision_id is not None and transcripts.get(revision_id) is not None:
            return ExistingTranscript(transcript_id=revision_id, run_id=run.id)
    return None


def _checkpoint_text(checkpoint: dict[str, Any] | None, key: str) -> str | None:
    return _text((checkpoint or {}).get(key))


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _failure_code(outcome: RunOutcome, counts: Counter[str]) -> str:
    """Why a run that did not complete is a failed job: the reason the run stopped, else an
    undecided chunk, else the code of the first chunk that failed, else the status."""
    if outcome.error_code is not None:
        return outcome.error_code
    if counts["unknown_outcome"]:
        return CODE_UNKNOWN_OUTCOME
    for chunk in outcome.chunks:
        if chunk.state == "failed" and chunk.error_code is not None:
            return chunk.error_code
    return CODE_PARTIAL if outcome.status == RUN_PARTIAL else CODE_FAILED


def _job_error(code: str) -> dict[str, str]:
    return {"code": code, "message": _JOB_ERRORS.get(code, _JOB_FAILED_MESSAGE)}


def _cancel_unstarted_run(session: Session, run_id: str) -> None:
    """A run whose job was cancelled before its first request: the run and its chunks are
    cancelled. A run that has already been through a worker keeps the status it has."""
    runs = TranscriptRunRepository(session)
    run = runs.get_run(run_id)
    if run is None or run.status != RUN_QUEUED:
        return
    for chunk in runs.list_chunks(run_id):
        if chunk.state == "pending":
            runs.update_chunk(chunk.id, state="cancelled")
    runs.update_run(run_id, status=RUN_CANCELLED)
