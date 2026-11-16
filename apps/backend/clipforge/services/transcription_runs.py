"""STT run orchestration (FR-015, FR-018, FR-049, docs/05 sections 4 and 6, ADR 032).

``create_run`` stores a run and its chunks (``domain.chunking.plan_chunks``, all ``pending``).
``run`` then sends every chunk that is not ``succeeded`` and, when all chunks are in a final state,
merges the ``succeeded`` ones (``domain.transcript_merge``) into an immutable revision with
``origin = stt``. The whole state is in the database (``transcript_runs``, ``transcript_chunks``,
the spend ledger) and in app-owned files, so a repeated ``run`` resumes: a ``succeeded`` chunk whose
response file is still valid is never sent again (T-030).

Nothing here knows HTTP, FFmpeg or the clock of the machine: the STT client, the audio extraction,
the clock, the sleeper and the jitter are injected, so tests do not wait and do not pay.

One attempt = one request. Every attempt has a new ``request_id`` and its own reservation in the
spend ledger. The reservation is made only after the audio of the chunk has been extracted and
right before the request is sent; no reservation, no request. Nothing is reserved while FFmpeg
runs, so a failure or a death of the process during the extraction leaves no reservation behind.
How an attempt ends (ADR 032):

====================================  ========================  ==============================
outcome                               ledger                    chunk
====================================  ========================  ==============================
200, ``check_chunk`` passes           complete(cost)            succeeded
200, ``MERGE_SEGMENT_TOO_LONG``       complete(cost)            split in two (parent superseded)
200, any other ``check_chunk`` error  complete(cost)            failed (no retry)
invalid_response                      complete(None)            failed (no retry)
rate_limited, upstream, not_sent      release                   retry, at most 3 attempts
payload_too_large (413)               release                   split in two
auth, funds                           release                   failed; the run stops
invalid_request, model_unavailable    release                   failed (no retry)
unknown_outcome                       mark_unknown              unknown_outcome (no auto retry)
====================================  ========================  ==============================

``invalid_response`` is an answer that cannot be used and is most likely already paid for: the
reservation keeps counting in full (``complete(None)``, the cost is unknown) and the same request
would get the same answer, so it is not repeated.

A split that would leave a half shorter than ``MIN_CORE_MS`` fails the chunk with
``CHUNK_BELOW_MIN_CORE``. ``BUDGET_EXCEEDED`` (no reservation; the audio may already have been
extracted, but nothing is sent) fails the chunk and stops the run: the chunks not yet sent fail with
the same code. After ``auth``/``funds`` or a cancel they are ``cancelled``. A chunk of an earlier
call that is still ``running`` died with its process. If its request was reserved it is
``unknown_outcome`` (the request may have gone out); only ``run(..., retry_unknown=True)`` sends
such a chunk again. If it died before the reservation (during the audio extraction) nothing was
sent and the chunk is ``pending`` again.

Retries wait for ``Retry-After`` (at most ``MAX_RETRY_AFTER_S``), without it for an exponential
backoff with jitter (``backoff_seconds``). A 429 also publishes a cooldown that every worker of this
service honours before its next request (the rate limit is shared, docs/05 section 6).

At most ``MAX_CONCURRENCY`` requests are in flight; chunks are taken in order of ``core_start_ms``.
Cancelling ends the work that is owned here: the sleep, the FFmpeg extraction and the queue. A
request already on the wire cannot be recalled, so its answer is kept like any other.

The audio of a chunk is ``<data_dir>/stt/<run_id>/<chunk_id>.wav`` (removed when the chunk is done)
and its response ``<chunk_id>.json`` next to it; ``transcript_chunks.response_path`` names it. The
file holds what the adapter returned (no key, the key never reaches this module). The original media
is only read (``extract_stt_audio``); before the first request its size and modification time are
compared with what was registered.

Messages are fixed Ukrainian text; logs carry ids and codes, never provider text or paths.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import logging
import math
import os
import random
import stat
import threading
import time
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol, TypeGuard

from sqlalchemy.orm import Session

from clipforge.adapters.ffprobe import ProbeError
from clipforge.adapters.media_tools import MediaTools, MediaToolsUnavailableError
from clipforge.adapters.openrouter_stt import (
    GEMINI_MODEL,
    MAI_MODEL,
    PROFILES,
    SttError,
    SttErrorKind,
    SttResult,
)
from clipforge.adapters.stt_audio import SttAudioRequest, SttAudioResult, extract_stt_audio
from clipforge.domain.chunking import (
    ChunkingError,
    ChunkWindow,
    plan_chunks,
    split_chunk,
)
from clipforge.domain.interval import IntervalError
from clipforge.domain.transcript_merge import (
    MERGE_INVALID_PROVIDER_TEXT,
    MERGE_SEGMENT_TOO_LONG,
    ChunkItem,
    ChunkResult,
    ChunkState,
    MergedTranscript,
    MergeError,
    TimedText,
    check_chunk,
    merge_transcript,
)
from clipforge.services.errors import conflict, invalid, not_found, unavailable
from clipforge.services.export_engine import (
    TEMP_PREFIX,
    TEMP_SUFFIX,
    ExportCancelledError,
    ExportError,
)
from clipforge.services.spend import BudgetExceededError, SpendLedger
from clipforge.services.text import is_storable
from clipforge.storage import (
    Database,
    NewSegment,
    NewWord,
    Source,
    SourceRepository,
    SpendRepository,
    TranscriptChunk,
    TranscriptRepository,
    TranscriptRunRepository,
)
from clipforge.storage.util import new_id

PROVIDER: Final = "openrouter"
MAX_ATTEMPTS: Final = 3
MAX_CONCURRENCY: Final = 2
BACKOFF_BASE_S: Final = 2.0
BACKOFF_CAP_S: Final = 60.0
# A provider that asks for a longer pause than this is waited for this long (then the attempt may
# meet a 429 again and be used up): a wait that nobody can end must not hold a run for hours.
MAX_RETRY_AFTER_S: Final = 300.0

# Upper tariffs: three times the live probe of 2026-11-15, rounded up (ADR 032). A local control,
# not a guarantee of the provider's bill.
PRICE_BASIS: Final = "live_probe_2026-11-15_x3"
USD_PER_SECOND_UPPER: Final[Mapping[str, Decimal]] = MappingProxyType(
    {MAI_MODEL: Decimal("0.0001"), GEMINI_MODEL: Decimal("0.0003")}
)

RUN_QUEUED: Final = "queued"
RUN_RUNNING: Final = "running"
RUN_COMPLETED: Final = "completed"
RUN_PARTIAL: Final = "partial"
RUN_FAILED: Final = "failed"
RUN_CANCELLED: Final = "cancelled"

CODE_AUTH: Final = "STT_AUTH"
CODE_FUNDS: Final = "STT_FUNDS"
CODE_BUDGET: Final = "BUDGET_EXCEEDED"
CODE_AUDIO_FAILED: Final = "AUDIO_FAILED"
CODE_RESPONSE_NOT_SAVED: Final = "STT_RESPONSE_NOT_SAVED"
CODE_RESPONSE_LOST: Final = "STT_RESPONSE_UNAVAILABLE"

_KIND_CODES: Final[Mapping[SttErrorKind, str]] = MappingProxyType(
    {
        "auth": CODE_AUTH,
        "funds": CODE_FUNDS,
        "rate_limited": "STT_RATE_LIMITED",
        "payload_too_large": "STT_PAYLOAD_TOO_LARGE",
        "model_unavailable": "STT_MODEL_UNAVAILABLE",
        "invalid_request": "STT_INVALID_REQUEST",
        "upstream": "STT_UPSTREAM",
        "not_sent": "STT_NOT_SENT",
        "unknown_outcome": "STT_UNKNOWN_OUTCOME",
        "invalid_response": "STT_INVALID_RESPONSE",
    }
)
_UNKNOWN_CODE: Final = _KIND_CODES["unknown_outcome"]

_RESPONSE_SCHEMA_VERSION: Final = 1
_POLL_S: Final = 0.05
_LOGGER = logging.getLogger(__name__)

Clock = Callable[[], float]
"""Monotonic seconds."""
Sleeper = Callable[[float, threading.Event], bool]
"""Wait up to ``seconds``; return ``True`` as soon as the event is set (the wait was cut short)."""
Jitter = Callable[[], float]
"""A number in ``[0, 1)``."""


class SttClient(Protocol):
    """What the service needs from an STT adapter: ``OpenRouterStt.transcribe``."""

    def transcribe(
        self,
        *,
        model: str,
        audio_wav: bytes,
        language: str | None = None,
        provider_options: Mapping[str, object] | None = None,
    ) -> SttResult: ...


class AudioExtractor(Protocol):
    """Renders ``request`` into ``<output_dir>/<task_id>.wav``; see ``extract_stt_audio``."""

    def __call__(self, request: SttAudioRequest, *, cancel: threading.Event) -> SttAudioResult: ...


def ffmpeg_extractor(tools: MediaTools) -> AudioExtractor:
    """The real extraction: ``extract_stt_audio`` with these FFmpeg tools."""

    def extract(request: SttAudioRequest, *, cancel: threading.Event) -> SttAudioResult:
        return extract_stt_audio(tools, request, cancel=cancel)

    return extract


def cancellable_sleep(seconds: float, cancel: threading.Event) -> bool:
    """The production sleeper: ends early when ``cancel`` is set; ``True`` then."""
    return cancel.wait(seconds)


def backoff_seconds(attempt: int, unit: float) -> float:
    """Wait after the ``attempt``-th failed attempt (1, 2, ...): the exponential ceiling
    ``min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1))``, spread by ``unit`` in ``[0, 1]``
    over its upper half (equal jitter): never below half the ceiling and never above it."""
    ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2.0 ** max(attempt - 1, 0))
    return ceiling * (0.5 + 0.5 * min(max(unit, 0.0), 1.0))


@dataclass(frozen=True, slots=True)
class SttRunOptions:
    """What decides the content of the transcript besides the source and the model. Their hash is
    ``transcript_runs.options_hash``: the options are not stored, so ``run`` is given them again and
    refuses different ones."""

    audio_stream_index: int
    language: str | None = None
    provider_options: Mapping[str, object] | None = None

    def options_hash(self) -> str:
        try:
            document = json.dumps(
                {
                    "audio_stream_index": self.audio_stream_index,
                    "language": self.language,
                    "provider_options": (
                        dict(self.provider_options) if self.provider_options is not None else None
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise invalid(
                "INVALID_STT_OPTIONS",
                "Параметри розпізнавання не можна зберегти як JSON",
                field="provider_options",
            ) from None
        return hashlib.sha256(document.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkSummary:
    """A chunk of the plan that is in force (a superseded parent is not listed)."""

    id: str
    core_start_ms: int
    core_end_ms: int
    state: str
    attempts: int
    error_code: str | None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """The state of a run after one ``run`` call.

    ``status``: ``completed`` (every chunk succeeded), ``partial`` (a revision exists but some chunk
    did not succeed), ``failed`` (no chunk succeeded, or the merge failed), ``cancelled`` (the user
    cancelled and nothing succeeded). ``error_code`` is the run-wide reason (``STT_AUTH``,
    ``STT_FUNDS``, ``BUDGET_EXCEEDED`` or a merge code); the code of a chunk is on the chunk.
    ``revision_id`` is the ``origin = stt`` revision of this run, new or earlier.
    """

    run_id: str
    status: str
    revision_id: str | None
    error_code: str | None
    committed_micro_usd: int
    chunks: tuple[ChunkSummary, ...]


@dataclass(frozen=True, slots=True)
class _Spec:
    """A detached copy of the chunk a worker handles."""

    id: str
    window: ChunkWindow


@dataclass(frozen=True, slots=True)
class _Step:
    """What a worker hands back: the chunks a split created, and whether the chunk succeeded."""

    children: tuple[_Spec, ...] = ()
    succeeded: bool = False


@dataclass(frozen=True, slots=True)
class _Retry:
    code: str
    delay_s: float


class _Halted(Exception):
    """The run was cancelled or stopped; the chunk is settled by ``_settle_halted``."""


class _AudioFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class _Context:
    run_id: str
    model: str
    source_path: Path
    audio_dir: Path
    duration_ms: int
    options: SttRunOptions
    ceiling_usd: Decimal
    cancel: threading.Event
    # Set when the run is cancelled or stopped; every wait and the FFmpeg job end on it.
    halt: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop_code: str | None = None

    def halted(self) -> bool:
        return self.halt.is_set() or self.cancel.is_set()

    def request_stop(self, code: str) -> None:
        with self.lock:
            if self.stop_code is None:
                self.stop_code = code
        self.halt.set()


class TranscriptionRunService:
    def __init__(
        self,
        database: Database,
        ledger: SpendLedger,
        *,
        client: SttClient,
        extract_audio: AudioExtractor,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = cancellable_sleep,
        jitter: Jitter = random.random,
        concurrency: int = MAX_CONCURRENCY,
    ) -> None:
        if not 1 <= concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"Паралельність має бути від 1 до {MAX_CONCURRENCY}")
        self._database = database
        self._ledger = ledger
        self._client = client
        self._extract_audio = extract_audio
        self._clock = clock
        self._sleeper = sleeper
        self._jitter = jitter
        self._concurrency = concurrency
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._cooldown_until = 0.0

    # -- create ------------------------------------------------------------------------------

    def create_run(
        self,
        *,
        source_id: str,
        model: str,
        options: SttRunOptions,
        session: Session | None = None,
    ) -> str:
        """Store a ``queued`` run with a ``pending`` chunk per window of the plan; its id.

        Without ``session`` the run is written in a transaction of its own. With ``session`` (a
        ``Database.write_session``) the rows join the caller's transaction, which commits or rolls
        back the run together with whatever else the caller writes (the job of the run)."""
        if model not in PROFILES:
            raise invalid(
                "UNSUPPORTED_STT_MODEL", "Ця модель розпізнавання не підтримується", field="model"
            )
        if session is not None:
            return self._add_run(session, source_id, model, options)
        with self._database.write_session() as own:
            return self._add_run(own, source_id, model, options)

    def _add_run(self, session: Session, source_id: str, model: str, options: SttRunOptions) -> str:
        source = require_source(session, source_id, options.audio_stream_index)
        runs = TranscriptRunRepository(session)
        run = runs.add_run(
            source_id=source_id,
            model=model,
            provider=PROVIDER,
            options_hash=options.options_hash(),
            status=RUN_QUEUED,
        )
        for window in plan_chunks(source.duration_ms).active:
            runs.add_chunk(
                run_id=run.id,
                core_start_ms=window.core_start_ms,
                core_end_ms=window.core_end_ms,
                upload_start_ms=window.upload_start_ms,
                upload_end_ms=window.upload_end_ms,
            )
        return run.id

    # -- run ---------------------------------------------------------------------------------

    def run(
        self,
        run_id: str,
        *,
        options: SttRunOptions,
        ceiling_usd: Decimal,
        cancel: threading.Event,
        retry_unknown: bool = False,
    ) -> RunOutcome:
        """Send what is not yet transcribed and write the revision. Blocking: call it from a worker
        thread. A second call for the same run while one is running is a 409.

        ``ceiling_usd`` is the budget of the whole run, earlier requests included. A chunk that is
        ``unknown_outcome`` is sent again only when ``retry_unknown`` is true.
        """
        if not ceiling_usd.is_finite() or ceiling_usd < 0:
            raise invalid(
                "INVALID_BUDGET", "Бюджет має бути невід'ємним числом", field="budget_usd"
            )
        with self._lock:
            if run_id in self._active:
                raise conflict(
                    "TRANSCRIPTION_RUN_ACTIVE", "Розпізнавання цього запуску вже виконується"
                )
            self._active.add(run_id)
        try:
            context = self._prepare(run_id, options, ceiling_usd, cancel)
            self._recover_interrupted(context)
            queue = self._reset_for_run(context, retry_unknown=retry_unknown)
            self._set_status(run_id, RUN_RUNNING)
            succeeded = self._drive(context, queue)
            return self._finalize(context, succeeded)
        finally:
            with self._lock:
                self._active.discard(run_id)

    def _prepare(
        self, run_id: str, options: SttRunOptions, ceiling_usd: Decimal, cancel: threading.Event
    ) -> _Context:
        with self._database.session() as session:
            run = TranscriptRunRepository(session).get_run(run_id)
            if run is None:
                raise not_found("TRANSCRIPTION_RUN_NOT_FOUND", "Запуск розпізнавання не знайдено")
            source = SourceRepository(session).get(run.source_id)
            if source is None:
                raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
            model, expected_hash = run.model, run.options_hash
            path, size, mtime_ns, duration_ms = (
                Path(source.path),
                source.size,
                source.mtime,
                source.duration_ms,
            )
        if options.options_hash() != expected_hash:
            raise conflict(
                "RUN_OPTIONS_MISMATCH",
                "Параметри розпізнавання не збігаються з параметрами запуску",
            )
        _require_registered_source(path, size, mtime_ns)
        return _Context(
            run_id=run_id,
            model=model,
            source_path=path,
            audio_dir=self._database.data_dir / "stt" / run_id,
            duration_ms=duration_ms,
            options=options,
            ceiling_usd=ceiling_usd,
            cancel=cancel,
        )

    def recover_interrupted(self, run_id: str) -> None:
        """Settle the chunks a dead call left ``running`` (see ``_settle_interrupted``) without
        starting a run: the job layer does it at start-up, so that the states are true (and the
        ``unknown_outcome`` chunks can be counted) before the user decides to resume. Refused with
        409 while this service is running the run."""
        with self._lock:
            if run_id in self._active:
                raise conflict(
                    "TRANSCRIPTION_RUN_ACTIVE", "Розпізнавання цього запуску вже виконується"
                )
        self._settle_interrupted(run_id)

    def _recover_interrupted(self, context: _Context) -> None:
        self._settle_interrupted(context.run_id)

    def _settle_interrupted(self, run_id: str) -> None:
        """A chunk that is ``running`` belongs to a call that died. If its request was reserved (and
        so possibly sent) its outcome is unknown and stays so; if nothing was ever reserved or the
        reservation was released, nothing went out and the chunk is simply pending."""
        with self._database.session() as session:
            spend = SpendRepository(session)
            stale: list[tuple[str, str | None, str | None]] = []
            for chunk in TranscriptRunRepository(session).list_chunks(run_id):
                if chunk.state != "running":
                    continue
                entry = (
                    spend.find(run_id, chunk.request_id) if chunk.request_id is not None else None
                )
                stale.append(
                    (
                        chunk.id,
                        entry.id if entry is not None else None,
                        entry.state if entry is not None else None,
                    )
                )
        for chunk_id, entry_id, entry_state in stale:
            if entry_id is None or entry_state == "released":
                self._update_chunk(chunk_id, state="pending", error_code=None)
                continue
            if entry_state == "reserved":
                self._ledger.mark_unknown(entry_id)
            self._update_chunk(chunk_id, state="unknown_outcome", error_code=_UNKNOWN_CODE)

    def _reset_for_run(self, context: _Context, *, retry_unknown: bool) -> list[_Spec]:
        """The chunks this call must send, reset to ``pending``. A ``succeeded`` chunk with a valid
        response file is cache (T-030) and is left alone; one without is sent again."""
        queue: list[_Spec] = []
        with self._database.write_session() as session:
            runs = TranscriptRunRepository(session)
            for chunk in runs.list_chunks(context.run_id):
                spec = _spec_of(chunk)
                if chunk.state == "superseded":
                    continue
                if chunk.state == "succeeded":
                    if _load_response(chunk.response_path, context.audio_dir, spec) is not None:
                        continue
                elif chunk.state == "unknown_outcome" and not retry_unknown:
                    continue
                runs.update_chunk(chunk.id, state="pending", error_code=None, response_path=None)
                queue.append(spec)
        return queue

    # -- scheduling --------------------------------------------------------------------------

    def _drive(self, context: _Context, queue: list[_Spec]) -> int:
        """Run the queue with at most ``concurrency`` workers, first by ``core_start_ms``. Returns
        how many chunks succeeded in this call."""
        order = itertools.count()
        heap = [(spec.window.core_start_ms, next(order), spec) for spec in queue]
        heapq.heapify(heap)
        running: dict[Future[_Step], _Spec] = {}
        succeeded = 0
        with ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="stt") as pool:
            try:
                while heap or running:
                    if context.cancel.is_set():
                        context.halt.set()
                    while heap and not context.halted() and len(running) < self._concurrency:
                        _, _, spec = heapq.heappop(heap)
                        running[pool.submit(self._process, context, spec)] = spec
                    if not running:
                        break  # halted with chunks still in the queue
                    done, _pending = wait(
                        list(running), timeout=_POLL_S, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        del running[future]
                        step = future.result()
                        succeeded += int(step.succeeded)
                        for child in step.children:
                            heapq.heappush(heap, (child.window.core_start_ms, next(order), child))
            finally:
                # Inside the pool's block: leaving it waits for every worker, so a worker that
                # crashed would otherwise leave its sibling to sleep through a retry pause and then
                # send one more (paid) request. Halted, the sibling wakes and sends nothing.
                context.halt.set()
        if heap:
            self._settle_halted(context, [spec for _, _, spec in heap])
        return succeeded

    # -- one chunk ---------------------------------------------------------------------------

    def _process(self, context: _Context, spec: _Spec) -> _Step:
        audio_path = context.audio_dir / f"{spec.id}.wav"
        try:
            return self._attempts(context, spec, audio_path)
        except _Halted:
            self._settle_halted(context, [spec])
            return _Step()
        finally:
            _remove_file(audio_path)

    def _attempts(self, context: _Context, spec: _Spec, audio_path: Path) -> _Step:
        audio: bytes | None = None
        attempt = 0
        while True:
            self._wait_out_cooldown(context)
            if context.halted():
                raise _Halted
            attempt += 1
            request_id = new_id()
            # The chunk names the attempt before the audio is extracted and before anything is
            # reserved: a chunk that is `running` without a reservation never sent anything, so
            # one that dies during the FFmpeg extraction is pending again (`_recover_interrupted`).
            self._update_chunk(
                spec.id, state="running", request_id=request_id, error_code=None, add_attempt=True
            )
            if audio is None:
                try:
                    audio = self._extract(context, spec, audio_path)
                except _AudioFailed as failure:
                    return self._conclude(spec, "failed", failure.code)
            # A cancel or a stop that came while FFmpeg ran (and did not cut it short) ends here,
            # before anything is reserved or sent.
            if context.halted():
                raise _Halted
            # Reserved only now, with the audio ready: a request that is sent has been paid for in
            # the ledger first (BUDGET_EXCEEDED sends nothing).
            try:
                upper_usd, snapshot = _reservation(context.model, spec.window)
                entry = self._ledger.reserve(
                    context.run_id,
                    request_id,
                    context.model,
                    upper_usd,
                    context.ceiling_usd,
                    snapshot,
                )
            except BudgetExceededError:
                step = self._conclude(spec, "failed", CODE_BUDGET)
                context.request_stop(CODE_BUDGET)
                return step
            self._update_chunk(spec.id, add_reserved=entry.reserved_micro_usd)
            try:
                result = self._client.transcribe(
                    model=context.model,
                    audio_wav=audio,
                    language=context.options.language,
                    provider_options=context.options.provider_options,
                )
            except SttError as error:
                outcome = self._after_error(context, spec, entry.id, error, attempt)
            else:
                return self._after_success(context, spec, entry.id, result)
            if isinstance(outcome, _Step):
                return outcome
            if attempt >= MAX_ATTEMPTS:
                return self._conclude(spec, "failed", outcome.code)
            if self._sleeper(outcome.delay_s, context.halt):
                raise _Halted

    def _after_success(
        self, context: _Context, spec: _Spec, entry_id: str, result: SttResult
    ) -> _Step:
        paid = self._ledger.complete(entry_id, result.usage.cost)
        actual = paid.actual_micro_usd
        candidate = ChunkResult(
            window=spec.window,
            state="succeeded",
            words=result.words,
            segments=result.segments,
            has_word_timing=result.has_word_timing,
            language=result.language,
        )
        try:
            _validate(candidate)
        except MergeError as error:
            if error.code == MERGE_SEGMENT_TOO_LONG:
                return self._split(context, spec, reason=error.code, add_actual=actual)
            return self._conclude(spec, "failed", error.code, add_actual=actual)
        try:
            path = _store_response(context.audio_dir, spec, result)
        except OSError:
            return self._conclude(spec, "failed", CODE_RESPONSE_NOT_SAVED, add_actual=actual)
        self._update_chunk(
            spec.id,
            state="succeeded",
            response_path=str(path),
            error_code=None,
            add_actual=actual,
        )
        return _Step(succeeded=True)

    def _after_error(
        self, context: _Context, spec: _Spec, entry_id: str, error: SttError, attempt: int
    ) -> _Step | _Retry:
        code = _KIND_CODES[error.kind]
        if error.kind == "unknown_outcome":
            self._ledger.mark_unknown(entry_id)
            return self._conclude(spec, "unknown_outcome", code)
        if error.kind == "invalid_response":
            # A 200 with a body that cannot be used was most likely paid for: the reservation keeps
            # counting in full (no actual cost is known). The same request gets the same body.
            self._ledger.complete(entry_id, None)
            return self._conclude(spec, "failed", code)
        self._ledger.release(entry_id)
        if error.kind in ("rate_limited", "upstream", "not_sent"):
            delay = self._retry_delay(error, attempt)
            if error.kind == "rate_limited":
                self._publish_cooldown(delay)
            return _Retry(code, delay)
        if error.kind == "payload_too_large":
            return self._split(context, spec, reason=code)
        step = self._conclude(spec, "failed", code)
        if error.kind in ("auth", "funds"):
            context.request_stop(code)
        return step

    def _retry_delay(self, error: SttError, attempt: int) -> float:
        if error.kind == "rate_limited" and error.retry_after_s is not None:
            return min(max(error.retry_after_s, 0.0), MAX_RETRY_AFTER_S)
        return backoff_seconds(attempt, self._jitter())

    def _split(
        self, context: _Context, spec: _Spec, *, reason: str, add_actual: int | None = None
    ) -> _Step:
        """Replace the chunk by two halves of its core (docs/05 section 4): the parent is
        ``superseded``, the halves are new ``pending`` chunks with their context recomputed, and
        together they cover the parent's core exactly."""
        try:
            halves = split_chunk(spec.window, context.duration_ms)
        except ChunkingError as error:
            return self._conclude(spec, "failed", error.code, add_actual=add_actual)
        with self._database.write_session() as session:
            runs = TranscriptRunRepository(session)
            _apply(runs, spec.id, state="superseded", error_code=reason, add_actual=add_actual)
            children = tuple(
                _Spec(
                    runs.add_chunk(
                        run_id=context.run_id,
                        core_start_ms=half.core_start_ms,
                        core_end_ms=half.core_end_ms,
                        upload_start_ms=half.upload_start_ms,
                        upload_end_ms=half.upload_end_ms,
                    ).id,
                    half,
                )
                for half in halves
            )
        return _Step(children=children)

    def _conclude(
        self, spec: _Spec, state: str, code: str, *, add_actual: int | None = None
    ) -> _Step:
        """End the chunk in a final state other than ``succeeded``."""
        self._update_chunk(spec.id, state=state, error_code=code, add_actual=add_actual)
        _LOGGER.warning("STT chunk ended %s: chunk=%s code=%s", state, spec.id, code)
        return _Step()

    def _settle_halted(self, context: _Context, specs: Sequence[_Spec]) -> None:
        """Chunks that were not sent because the run was cancelled or stopped. After a budget stop
        they are ``failed`` with that code, otherwise ``cancelled``."""
        state = "failed" if context.stop_code == CODE_BUDGET else "cancelled"
        code = context.stop_code
        for spec in specs:
            self._update_chunk(spec.id, state=state, error_code=code)

    # -- audio and waiting -------------------------------------------------------------------

    def _extract(self, context: _Context, spec: _Spec, path: Path) -> bytes:
        window = spec.window
        try:
            context.audio_dir.mkdir(parents=True, exist_ok=True)
            # A temp file left by a dead extraction of this chunk is ours and would block this one.
            (context.audio_dir / f"{TEMP_PREFIX}{spec.id}{TEMP_SUFFIX}").unlink(missing_ok=True)
        except OSError:
            raise _AudioFailed("OUTPUT_NOT_WRITABLE") from None
        request = SttAudioRequest(
            task_id=spec.id,
            source=context.source_path,
            output_dir=context.audio_dir,
            audio_stream_index=context.options.audio_stream_index,
            upload_start_ms=window.upload_start_ms,
            upload_end_ms=window.upload_end_ms,
        )
        try:
            result = self._extract_audio(request, cancel=context.halt)
        except ExportCancelledError:
            raise _Halted from None
        except (ExportError, ProbeError) as error:
            raise _AudioFailed(error.code) from None
        except IntervalError:
            raise _AudioFailed("INVALID_INTERVAL") from None
        except MediaToolsUnavailableError:
            raise _AudioFailed("MEDIA_TOOLS_UNAVAILABLE") from None
        except ValueError:
            raise _AudioFailed(CODE_AUDIO_FAILED) from None
        if result.path != path:
            raise _AudioFailed(CODE_AUDIO_FAILED)
        try:
            return path.read_bytes()
        except OSError:
            raise _AudioFailed(CODE_AUDIO_FAILED) from None

    def _wait_out_cooldown(self, context: _Context) -> None:
        with self._lock:
            remaining = self._cooldown_until - self._clock()
        if remaining > 0 and self._sleeper(remaining, context.halt):
            raise _Halted

    def _publish_cooldown(self, delay_s: float) -> None:
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + delay_s)

    # -- state writes ------------------------------------------------------------------------

    def _update_chunk(
        self,
        chunk_id: str,
        *,
        add_attempt: bool = False,
        add_reserved: int = 0,
        add_actual: int | None = None,
        **changes: Any,
    ) -> None:
        with self._database.write_session() as session:
            _apply(
                TranscriptRunRepository(session),
                chunk_id,
                add_attempt=add_attempt,
                add_reserved=add_reserved,
                add_actual=add_actual,
                **changes,
            )

    def _set_status(self, run_id: str, status: str) -> None:
        with self._database.write_session() as session:
            TranscriptRunRepository(session).update_run(run_id, status=status)

    # -- finish ------------------------------------------------------------------------------

    def _finalize(self, context: _Context, succeeded: int) -> RunOutcome:
        """All chunks are final: merge the ``succeeded`` ones into a revision and set the run's
        status, coverage and usage. A call that gained nothing keeps the revision it already has."""
        with self._database.session() as session:
            runs = TranscriptRunRepository(session)
            run = runs.get_run(context.run_id)
            chunks = [c for c in runs.list_chunks(context.run_id) if c.state != "superseded"]
            previous = (run.usage or {}).get("revision_id") if run is not None else None
            source_id = run.source_id if run is not None else ""
        previous_revision = previous if isinstance(previous, str) else None

        results: list[ChunkResult] = []
        lost: list[str] = []
        for chunk in chunks:
            spec = _spec_of(chunk)
            if chunk.state == "succeeded":
                loaded = _load_response(chunk.response_path, context.audio_dir, spec)
                if loaded is None:
                    lost.append(chunk.id)
                    results.append(ChunkResult(window=spec.window, state="failed"))
                else:
                    results.append(loaded)
            else:
                results.append(ChunkResult(window=spec.window, state=_merge_state(chunk.state)))
        coverage = [
            {
                "start_ms": result.window.core_start_ms,
                "end_ms": result.window.core_end_ms,
                "state": result.state,
            }
            for result in results
        ]

        error_code = context.stop_code
        merged: MergedTranscript | None = None
        if any(result.state == "succeeded" for result in results):
            try:
                merged = merge_transcript(context.duration_ms, results)
            except MergeError as error:
                error_code = error.code
        committed = self._ledger.committed_micro_usd(context.run_id)

        with self._database.write_session() as session:
            runs = TranscriptRunRepository(session)
            for chunk_id in lost:
                _apply(runs, chunk_id, state="failed", error_code=CODE_RESPONSE_LOST)
            revision_id = previous_revision
            if merged is None:
                # Nothing succeeded now, but a revision an earlier call wrote still exists.
                status = (
                    RUN_CANCELLED
                    if context.cancel.is_set() and context.stop_code is None
                    else RUN_FAILED
                )
            else:
                status = (
                    RUN_COMPLETED
                    if all(result.state == "succeeded" for result in results)
                    else RUN_PARTIAL
                )
                if succeeded > 0 or lost or previous_revision is None:
                    revision_id = self._write_revision(session, context, source_id, merged, chunks)
            runs.update_run(
                context.run_id,
                status=status,
                coverage=coverage,
                usage={
                    "committed_micro_usd": committed,
                    "error_code": error_code,
                    "revision_id": revision_id,
                },
            )
        return RunOutcome(
            run_id=context.run_id,
            status=status,
            revision_id=revision_id,
            error_code=error_code,
            committed_micro_usd=committed,
            chunks=self._summaries(context.run_id),
        )

    def _write_revision(
        self,
        session: Session,
        context: _Context,
        source_id: str,
        merged: MergedTranscript,
        chunks: Sequence[TranscriptChunk],
    ) -> str:
        starts = [2 * chunk.core_start_ms for chunk in chunks]
        ids_by_core_start = {chunk.core_start_ms: chunk.id for chunk in chunks}
        words = [
            NewWord(
                id=word.id,
                ordinal=word.ordinal,
                start_ms=word.start_ms,
                end_ms=word.end_ms,
                text=word.text,
                chunk_id=_owner(word, chunks, starts, ids_by_core_start),
                speaker_local=word.speaker.label if word.speaker is not None else None,
                confidence=word.confidence,
            )
            for word in merged.words
        ]
        segments = [
            NewSegment(
                id=segment.id,
                ordinal=segment.ordinal,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                word_ids=word_ids,
            )
            for segment, word_ids in zip(
                merged.segments, _word_ids_by_segment(merged.words, merged.segments), strict=True
            )
        ]
        revision = TranscriptRepository(session).add_revision(
            source_id=source_id,
            origin="stt",
            run_id=context.run_id,
            timing_level=merged.timing_level,
            language=merged.language,
            duration_ms=merged.duration_ms,
            coverage=[
                {"start_ms": entry.start_ms, "end_ms": entry.end_ms, "state": entry.state}
                for entry in merged.coverage
            ],
            metadata={
                "model": context.model,
                "provider": PROVIDER,
                "diagnostics": [
                    {
                        "code": item.code,
                        "message": item.message,
                        "chunk_core_start_ms": item.chunk_core_start_ms,
                    }
                    for item in merged.diagnostics
                ],
            },
            words=words,
            segments=segments,
        )
        return revision.id

    def _summaries(self, run_id: str) -> tuple[ChunkSummary, ...]:
        with self._database.session() as session:
            return tuple(
                ChunkSummary(
                    id=chunk.id,
                    core_start_ms=chunk.core_start_ms,
                    core_end_ms=chunk.core_end_ms,
                    state=chunk.state,
                    attempts=chunk.attempts,
                    error_code=chunk.error_code,
                )
                for chunk in TranscriptRunRepository(session).list_chunks(run_id)
                if chunk.state != "superseded"
            )


# --- helpers ----------------------------------------------------------------------------------


def require_source(session: Session, source_id: str, audio_stream_index: int) -> Source:
    """The registered source, or 404 ``SOURCE_NOT_FOUND`` / 422 ``INVALID_AUDIO_STREAM``."""
    source = SourceRepository(session).get(source_id)
    if source is None:
        raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
    if audio_stream_index not in {int(stream["index"]) for stream in source.audio_streams}:
        raise invalid(
            "INVALID_AUDIO_STREAM",
            "Вибрана аудіодоріжка відсутня у джерелі",
            field="audio_stream_index",
        )
    return source


def _spec_of(chunk: TranscriptChunk) -> _Spec:
    return _Spec(
        chunk.id,
        ChunkWindow(
            core_start_ms=chunk.core_start_ms,
            core_end_ms=chunk.core_end_ms,
            upload_start_ms=chunk.upload_start_ms,
            upload_end_ms=chunk.upload_end_ms,
        ),
    )


def _apply(
    runs: TranscriptRunRepository,
    chunk_id: str,
    *,
    add_attempt: bool = False,
    add_reserved: int = 0,
    add_actual: int | None = None,
    **changes: Any,
) -> None:
    """Update a chunk; the three ``add_*`` values are added to what is stored (an attempt, the
    reservation and the reported cost are summed over every attempt of the chunk)."""
    chunk = runs.get_chunk(chunk_id)
    if chunk is None:
        raise not_found("TRANSCRIPTION_CHUNK_NOT_FOUND", "Частину розпізнавання не знайдено")
    if add_attempt:
        changes["attempts"] = chunk.attempts + 1
    if add_reserved:
        changes["reserved_micro_usd"] = chunk.reserved_micro_usd + add_reserved
    if add_actual is not None:
        changes["actual_micro_usd"] = (chunk.actual_micro_usd or 0) + add_actual
    runs.update_chunk(chunk_id, **changes)


def billed_seconds(window: ChunkWindow) -> int:
    """The seconds one request of ``window`` is priced at: its upload window, rounded up."""
    return math.ceil(window.upload_duration_ms / 1000)


def _reservation(model: str, window: ChunkWindow) -> tuple[Decimal, dict[str, Any]]:
    """The upper estimate of one request: whole seconds of the upload window at the upper tariff."""
    seconds = billed_seconds(window)
    rate = USD_PER_SECOND_UPPER[model]
    return rate * seconds, {
        "usd_per_second_upper": rate,
        "billed_seconds": seconds,
        "upload_ms": window.upload_duration_ms,
        "basis": PRICE_BASIS,
    }


def _merge_state(state: str) -> ChunkState:
    """A chunk state as the transcript document knows it (``contracts/transcript.schema.json``):
    a chunk that never ran, was cancelled or is still running is ``pending``."""
    if state == "failed":
        return "failed"
    if state == "unknown_outcome":
        return "unknown_outcome"
    return "pending"


def _validate(candidate: ChunkResult) -> None:
    """``check_chunk`` plus what only the database cares about: stored text must be storable."""
    check_chunk(candidate)
    if not all(is_storable(item.text) for item in (*candidate.words, *candidate.segments)):
        raise MergeError(
            MERGE_INVALID_PROVIDER_TEXT,
            "Відповідь провайдера містить недопустимі символи",
            chunk_core_start_ms=candidate.window.core_start_ms,
        )


def _require_registered_source(path: Path, size: int, mtime_ns: int) -> None:
    """The file must still be what was registered (FR-038): nothing is sent for another file."""
    try:
        state = path.stat()
    except OSError:
        raise unavailable("SOURCE_UNAVAILABLE", "Файл джерела недоступний") from None
    if not stat.S_ISREG(state.st_mode):
        raise unavailable("SOURCE_UNAVAILABLE", "Файл джерела недоступний")
    if state.st_size != size or state.st_mtime_ns != mtime_ns:
        raise conflict("SOURCE_CHANGED", "Файл джерела змінився після реєстрації")


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        _LOGGER.warning("A temporary STT audio file could not be removed")


def _owner(
    word: TimedText,
    chunks: Sequence[TranscriptChunk],
    doubled_starts: Sequence[int],
    ids_by_core_start: Mapping[int, str],
) -> str | None:
    """The chunk a word belongs to: the one named by its speaker label when it has one, otherwise
    the one whose core contains the midpoint (the cores tile the source, docs/05 section 4)."""
    if word.speaker is not None:
        return ids_by_core_start.get(word.speaker.chunk_core_start_ms)
    index = bisect_right(doubled_starts, word.start_ms + word.end_ms) - 1
    return chunks[max(index, 0)].id if chunks else None


def _word_ids_by_segment(
    words: Sequence[TimedText], segments: Sequence[TimedText]
) -> list[list[str]]:
    """For each segment the ids of the words inside it (``seg.start <= word.start`` and
    ``word.end <= seg.end``), derived from time like for an imported transcript (ADR 030)."""
    starts = [word.start_ms for word in words]
    mapping: list[list[str]] = []
    for segment in segments:
        ids: list[str] = []
        for index in range(bisect_left(starts, segment.start_ms), len(words)):
            word = words[index]
            if word.start_ms >= segment.end_ms:
                break
            if word.end_ms <= segment.end_ms:
                ids.append(word.id)
        mapping.append(ids)
    return mapping


# --- response files ---------------------------------------------------------------------------


def _store_response(audio_dir: Path, spec: _Spec, result: SttResult) -> Path:
    """Write what the adapter returned to ``<chunk_id>.json`` (a temp file first, then renamed)."""
    window = spec.window
    document = {
        "schema_version": _RESPONSE_SCHEMA_VERSION,
        "window": {
            "core_start_ms": window.core_start_ms,
            "core_end_ms": window.core_end_ms,
            "upload_start_ms": window.upload_start_ms,
            "upload_end_ms": window.upload_end_ms,
        },
        "text": result.text,
        "language": result.language,
        "duration_s": result.duration_s,
        "has_word_timing": result.has_word_timing,
        "generation_id": result.generation_id,
        "usage": {
            "cost": str(result.usage.cost) if result.usage.cost is not None else None,
            "seconds": result.usage.seconds,
            "tokens": result.usage.tokens,
        },
        "words": [_item_document(item) for item in result.words],
        "segments": [_item_document(item) for item in result.segments],
    }
    audio_dir.mkdir(parents=True, exist_ok=True)
    final = audio_dir / f"{spec.id}.json"
    temp = audio_dir / f"{spec.id}.json.part"
    try:
        temp.write_text(json.dumps(document, ensure_ascii=True), encoding="ascii")
        os.replace(temp, final)
    except OSError:
        _remove_file(temp)
        raise
    return final


def _item_document(item: ChunkItem) -> dict[str, Any]:
    return {
        "start_s": item.start_s,
        "end_s": item.end_s,
        "text": item.text,
        "speaker": item.speaker,
        "confidence": item.confidence,
    }


def _load_response(path_text: str | None, audio_dir: Path, spec: _Spec) -> ChunkResult | None:
    """The stored response of a chunk as a ``ChunkResult``, or ``None`` when it cannot be trusted:
    the file is missing, is not exactly ``<audio_dir>/<chunk_id>.json``, is not what
    ``_store_response`` writes, belongs to another window or no longer passes ``_validate``."""
    if path_text is None or Path(path_text) != audio_dir / f"{spec.id}.json":
        return None
    try:
        document: object = json.loads(Path(path_text).read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema_version") != _RESPONSE_SCHEMA_VERSION:
        return None
    window = spec.window
    if document.get("window") != {
        "core_start_ms": window.core_start_ms,
        "core_end_ms": window.core_end_ms,
        "upload_start_ms": window.upload_start_ms,
        "upload_end_ms": window.upload_end_ms,
    }:
        return None
    words = _load_items(document.get("words"))
    segments = _load_items(document.get("segments"))
    language = document.get("language")
    has_word_timing = document.get("has_word_timing")
    if (
        words is None
        or segments is None
        or not isinstance(has_word_timing, bool)
        or not (language is None or isinstance(language, str))
    ):
        return None
    result = ChunkResult(
        window=window,
        state="succeeded",
        words=words,
        segments=segments,
        has_word_timing=has_word_timing,
        language=language,
    )
    try:
        _validate(result)
    except MergeError:
        return None
    return result


def _load_items(raw: object) -> tuple[ChunkItem, ...] | None:
    if not isinstance(raw, list):
        return None
    items: list[ChunkItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        start, end, text = entry.get("start_s"), entry.get("end_s"), entry.get("text")
        speaker, confidence = entry.get("speaker"), entry.get("confidence")
        if (
            not _is_number(start)
            or not _is_number(end)
            or not isinstance(text, str)
            or not (speaker is None or isinstance(speaker, str))
            or not (confidence is None or _is_number(confidence))
        ):
            return None
        items.append(
            ChunkItem(
                start_s=float(start),
                end_s=float(end),
                text=text,
                speaker=speaker,
                confidence=float(confidence) if confidence is not None else None,
            )
        )
    return tuple(items)


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
