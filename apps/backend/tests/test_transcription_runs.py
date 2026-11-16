"""STT run orchestration (ADR 032, T-025..T-030): retries, stops, splits, unknown outcomes, partial
transcripts, the cache, the budget, concurrency, cancellation and recovery.

The STT client, the audio extraction, the clock and the sleeper are fakes: nothing is paid for and
nothing waits (the fake sleeper only records and advances a fake clock). Only the last test runs the
real FFmpeg extraction, on a copy of a generated fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from clipforge.adapters.ffprobe import SourceUnreadableError, probe_source
from clipforge.adapters.media_tools import MediaTools, MediaToolsUnavailableError
from clipforge.adapters.openrouter_stt import MAI_MODEL, SttError, SttResult, SttUsage
from clipforge.adapters.stt_audio import NoAudioStreamError, SttAudioRequest, SttAudioResult
from clipforge.domain.chunking import estimate_payload
from clipforge.domain.interval import IntervalError
from clipforge.domain.transcript_merge import ChunkItem
from clipforge.services.errors import ErrorKind, ServiceError
from clipforge.services.export_engine import (
    DiskFullError,
    ExportCancelledError,
    SourceChangedError,
)
from clipforge.services.spend import SpendLedger
from clipforge.services.transcription_runs import (
    PRICE_BASIS,
    RunOutcome,
    SttRunOptions,
    TranscriptionRunService,
    backoff_seconds,
    cancellable_sleep,
    ffmpeg_extractor,
)
from clipforge.storage import (
    Database,
    ProjectRepository,
    SourceRepository,
    SpendEntry,
    SpendRepository,
    TranscriptChunk,
    TranscriptRepository,
    TranscriptRevision,
    TranscriptRun,
    TranscriptRunRepository,
    TranscriptSegment,
    TranscriptWord,
    open_database,
)

from .support import fixture_file

OPTIONS = SttRunOptions(audio_stream_index=1)
FIRST_UPLOAD_START = 0
SECOND_UPLOAD_START = 295_000  # the upload window of the second 300 s chunk starts 5 s early


# --- fakes -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Call:
    model: str
    language: str | None
    provider_options: Mapping[str, object] | None
    upload_start_ms: int
    upload_end_ms: int

    @property
    def upload_ms(self) -> int:
        return self.upload_end_ms - self.upload_start_ms


Script = Callable[[Call], SttResult]


def wav_for(start_ms: int, end_ms: int) -> bytes:
    """Stand-in for a WAV: the fake client reads the window back from it."""
    return b"RIFF" + f"{start_ms}:{end_ms}".encode("ascii")


def speech(
    call: Call, *, cost: Decimal | None = Decimal("0.00002"), speaker: str | None = None
) -> SttResult:
    """One word in the middle of the upload window (inside the core of every chunk that is at
    least 5 s long) and one segment over the whole window."""
    middle_s = call.upload_ms / 2000
    middle_ms = (call.upload_start_ms + call.upload_end_ms) // 2
    text = f"слово{middle_ms}"
    return SttResult(
        text=text,
        words=(ChunkItem(middle_s - 0.2, middle_s + 0.2, text, speaker=speaker),),
        segments=(ChunkItem(0.0, call.upload_ms / 1000, text, speaker=speaker),),
        has_word_timing=True,
        language="en",
        duration_s=None,
        usage=SttUsage(cost=cost),
        generation_id=f"gen-{middle_ms}",
    )


def scripted(*outcomes: SttError | None) -> Script:
    """The first calls get these outcomes in order (``None``: speech), later calls get speech."""
    queue = deque(outcomes)
    lock = threading.Lock()

    def script(call: Call) -> SttResult:
        with lock:
            outcome = queue.popleft() if queue else None
        if outcome is not None:
            raise outcome
        return speech(call)

    return script


def failing_at(upload_start_ms: int, *errors: SttError) -> Script:
    """Calls whose upload window starts at ``upload_start_ms`` fail with ``errors`` in order."""
    queue = deque(errors)
    lock = threading.Lock()

    def script(call: Call) -> SttResult:
        if call.upload_start_ms == upload_start_ms:
            with lock:
                error = queue.popleft() if queue else None
            if error is not None:
                raise error
        return speech(call)

    return script


class FakeStt:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.calls: list[Call] = []
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def transcribe(
        self,
        *,
        model: str,
        audio_wav: bytes,
        language: str | None = None,
        provider_options: Mapping[str, object] | None = None,
    ) -> SttResult:
        assert audio_wav.startswith(b"RIFF")
        start, end = audio_wav[4:].decode("ascii").split(":")
        call = Call(model, language, provider_options, int(start), int(end))
        with self._lock:
            self.calls.append(call)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            return self.script(call)
        finally:
            with self._lock:
                self.active -= 1


class FakeExtractor:
    def __init__(self) -> None:
        self.requests: list[SttAudioRequest] = []
        self.fail: Callable[[SttAudioRequest], None] | None = None
        self._lock = threading.Lock()

    def __call__(self, request: SttAudioRequest, *, cancel: threading.Event) -> SttAudioResult:
        with self._lock:
            self.requests.append(request)
        if self.fail is not None:
            self.fail(request)
        path = request.output_dir / f"{request.task_id}.wav"
        data = wav_for(request.upload_start_ms, request.upload_end_ms)
        path.write_bytes(data)
        return SttAudioResult(
            path=path,
            size_bytes=len(data),
            upload_start_ms=request.upload_start_ms,
            upload_end_ms=request.upload_end_ms,
            seek_mode="full_decode",
            ffmpeg_version="fake",
            ffprobe_version="fake",
        )


class FakeTime:
    """A clock that only moves when the sleeper is called. ``hook(index, seconds, halt)`` runs
    inside every sleep (``index`` counts from 1) and may block, like a real sleep does."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.hook: Callable[[int, float, threading.Event], None] | None = None
        self.cancel: threading.Event | None = None
        self._lock = threading.Lock()

    def clock(self) -> float:
        with self._lock:
            return self.now

    def sleeper(self, seconds: float, halt: threading.Event) -> bool:
        with self._lock:
            self.sleeps.append(seconds)
            index = len(self.sleeps)
        if self.hook is not None:
            self.hook(index, seconds, halt)
        with self._lock:
            self.now += seconds
        return halt.is_set() or (self.cancel is not None and self.cancel.is_set())


# --- harness -----------------------------------------------------------------------------------


@dataclass
class Harness:
    db: Database
    ledger: SpendLedger
    stt: FakeStt
    extractor: FakeExtractor
    time: FakeTime
    service: TranscriptionRunService
    source_id: str
    source_path: Path
    run_id: str
    options: SttRunOptions

    def run(
        self,
        *,
        ceiling: str = "10",
        cancel: threading.Event | None = None,
        retry_unknown: bool = False,
    ) -> RunOutcome:
        return self.service.run(
            self.run_id,
            options=self.options,
            ceiling_usd=Decimal(ceiling),
            cancel=cancel if cancel is not None else threading.Event(),
            retry_unknown=retry_unknown,
        )

    def chunks(self) -> list[TranscriptChunk]:
        """Every chunk row, a superseded parent included, in window order."""
        with self.db.session() as session:
            rows = TranscriptRunRepository(session).list_chunks(self.run_id)
        return sorted(rows, key=lambda chunk: (chunk.core_start_ms, -chunk.core_end_ms))

    def active_chunks(self) -> list[TranscriptChunk]:
        return [chunk for chunk in self.chunks() if chunk.state != "superseded"]

    def entries(self) -> list[SpendEntry]:
        with self.db.session() as session:
            return SpendRepository(session).list_for_run(self.run_id)

    def run_row(self) -> TranscriptRun:
        with self.db.session() as session:
            row = TranscriptRunRepository(session).get_run(self.run_id)
        assert row is not None
        return row

    def revisions(self) -> list[TranscriptRevision]:
        with self.db.session() as session:
            return TranscriptRepository(session).list_page_for_source(self.source_id).items

    def words(self, revision: TranscriptRevision) -> list[TranscriptWord]:
        with self.db.session() as session:
            return TranscriptRepository(session).list_words(revision.id)

    def segments(self, revision: TranscriptRevision) -> list[TranscriptSegment]:
        with self.db.session() as session:
            return TranscriptRepository(session).list_segments(revision.id)


MakeHarness = Callable[..., Harness]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "data")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def make(tmp_path: Path, db: Database) -> MakeHarness:
    def build(
        duration_ms: int = 620_000,
        *,
        script: Script | None = None,
        concurrency: int = 2,
        jitter: float = 0.0,
        options: SttRunOptions = OPTIONS,
    ) -> Harness:
        media = tmp_path / "media.bin"
        media.write_bytes(b"not really a video")
        state = media.stat()
        with db.write_session() as session:
            project = ProjectRepository(session).add("Проєкт")
            source = SourceRepository(session).add(
                project_id=project.id,
                path=str(media),
                display_name=media.name,
                size=state.st_size,
                mtime=state.st_mtime_ns,
                quick_fingerprint="quick-1",
                hash_status="pending",
                duration_ms=duration_ms,
                video_stream_index=0,
                audio_streams=[{"index": 1, "codec": "aac", "default": True}],
                canonical_origin={"origin_ms": 0, "time_base": "1/1000"},
                probe_json={},
                availability="available",
            )
            source_id = source.id
        stt = FakeStt(script if script is not None else speech)
        extractor = FakeExtractor()
        fake_time = FakeTime()
        service = TranscriptionRunService(
            db,
            SpendLedger(db),
            client=stt,
            extract_audio=extractor,
            clock=fake_time.clock,
            sleeper=fake_time.sleeper,
            jitter=lambda: jitter,
            concurrency=concurrency,
        )
        run_id = service.create_run(source_id=source_id, model=MAI_MODEL, options=options)
        return Harness(
            db=db,
            ledger=SpendLedger(db),
            stt=stt,
            extractor=extractor,
            time=fake_time,
            service=service,
            source_id=source_id,
            source_path=media,
            run_id=run_id,
            options=options,
        )

    return build


def windows(chunks: list[TranscriptChunk]) -> list[tuple[int, int, int, int]]:
    return [(c.core_start_ms, c.core_end_ms, c.upload_start_ms, c.upload_end_ms) for c in chunks]


def states(outcome: RunOutcome) -> list[tuple[int, str, str | None]]:
    return [(c.core_start_ms, c.state, c.error_code) for c in outcome.chunks]


def service_error(call: Callable[[], object]) -> ServiceError:
    with pytest.raises(ServiceError) as caught:
        call()
    return caught.value


# --- create_run --------------------------------------------------------------------------------


def test_create_run_stores_a_queued_run_with_a_pending_chunk_for_every_window(
    make: MakeHarness,
) -> None:
    h = make(620_000)

    run = h.run_row()
    assert (run.source_id, run.model, run.provider, run.status) == (
        h.source_id,
        MAI_MODEL,
        "openrouter",
        "queued",
    )
    assert run.options_hash == OPTIONS.options_hash()
    assert (run.coverage, run.usage) == (None, None)
    chunks = h.chunks()
    assert windows(chunks) == [
        (0, 300_000, 0, 305_000),
        (300_000, 600_000, 295_000, 605_000),
        (600_000, 620_000, 595_000, 620_000),
    ]
    assert [(c.state, c.attempts, c.reserved_micro_usd) for c in chunks] == [("pending", 0, 0)] * 3
    assert h.stt.calls == []


def test_create_run_refuses_what_it_cannot_run_and_stores_nothing(
    make: MakeHarness, db: Database
) -> None:
    h = make(60_000)

    unknown_source = service_error(
        lambda: h.service.create_run(source_id="nope", model=MAI_MODEL, options=OPTIONS)
    )
    assert (unknown_source.kind, unknown_source.code) == (ErrorKind.NOT_FOUND, "SOURCE_NOT_FOUND")
    bad_model = service_error(
        lambda: h.service.create_run(source_id=h.source_id, model="openai/whisper", options=OPTIONS)
    )
    assert (bad_model.kind, bad_model.code) == (ErrorKind.INVALID, "UNSUPPORTED_STT_MODEL")
    bad_stream = service_error(
        lambda: h.service.create_run(
            source_id=h.source_id, model=MAI_MODEL, options=SttRunOptions(audio_stream_index=7)
        )
    )
    assert (bad_stream.kind, bad_stream.code) == (ErrorKind.INVALID, "INVALID_AUDIO_STREAM")

    with db.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM transcript_runs").scalar() == 1


def test_options_hash_is_stable_distinguishes_options_and_refuses_what_is_not_json() -> None:
    plain = SttRunOptions(audio_stream_index=1)
    same = SttRunOptions(audio_stream_index=1, language=None, provider_options=None)
    assert plain.options_hash() == same.options_hash()
    variants = [
        SttRunOptions(audio_stream_index=2),
        SttRunOptions(audio_stream_index=1, language="es"),
        SttRunOptions(audio_stream_index=1, provider_options={"a": 1}),
    ]
    hashes = {plain.options_hash(), *(variant.options_hash() for variant in variants)}
    assert len(hashes) == 4
    ordered = SttRunOptions(audio_stream_index=1, provider_options={"a": 1, "b": 2})
    swapped = SttRunOptions(audio_stream_index=1, provider_options={"b": 2, "a": 1})
    assert ordered.options_hash() == swapped.options_hash()
    error = service_error(
        SttRunOptions(audio_stream_index=1, provider_options={"a": object()}).options_hash
    )
    assert error.code == "INVALID_STT_OPTIONS"


def test_run_refuses_an_unknown_run_other_options_and_a_bad_budget(make: MakeHarness) -> None:
    h = make(60_000)
    cancel = threading.Event()

    missing = service_error(
        lambda: h.service.run("nope", options=OPTIONS, ceiling_usd=Decimal("1"), cancel=cancel)
    )
    assert (missing.kind, missing.code) == (ErrorKind.NOT_FOUND, "TRANSCRIPTION_RUN_NOT_FOUND")
    other = service_error(
        lambda: h.service.run(
            h.run_id,
            options=SttRunOptions(audio_stream_index=1, language="de"),
            ceiling_usd=Decimal("1"),
            cancel=cancel,
        )
    )
    assert (other.kind, other.code) == (ErrorKind.CONFLICT, "RUN_OPTIONS_MISMATCH")
    for budget in ("-0.01", "NaN", "Infinity"):
        bad = service_error(
            lambda budget=budget: h.service.run(  # type: ignore[misc]
                h.run_id, options=OPTIONS, ceiling_usd=Decimal(budget), cancel=cancel
            )
        )
        assert (bad.kind, bad.code) == (ErrorKind.INVALID, "INVALID_BUDGET")
    assert h.stt.calls == []
    assert [c.state for c in h.chunks()] == ["pending"]
    assert h.run_row().status == "queued"


def test_the_concurrency_must_be_one_or_two(make: MakeHarness, db: Database) -> None:
    h = make(60_000)
    for bad in (0, 3):
        with pytest.raises(ValueError, match="Паралельність"):
            TranscriptionRunService(
                db,
                h.ledger,
                client=h.stt,
                extract_audio=h.extractor,
                concurrency=bad,
            )


# --- a whole run -------------------------------------------------------------------------------


def test_a_long_source_is_transcribed_chunk_by_chunk_into_one_stt_revision(
    make: MakeHarness, db: Database
) -> None:
    h = make(620_000)

    outcome = h.run()

    assert (outcome.status, outcome.error_code) == ("completed", None)
    assert [(c.core_start_ms, c.core_end_ms, c.state, c.attempts) for c in outcome.chunks] == [
        (0, 300_000, "succeeded", 1),
        (300_000, 600_000, "succeeded", 1),
        (600_000, 620_000, "succeeded", 1),
    ]
    requested = sorted((r.upload_start_ms, r.upload_end_ms) for r in h.extractor.requests)
    assert requested == [(0, 305_000), (295_000, 605_000), (595_000, 620_000)]
    assert {(r.source, r.audio_stream_index) for r in h.extractor.requests} == {(h.source_path, 1)}
    assert {r.output_dir for r in h.extractor.requests} == {db.data_dir / "stt" / h.run_id}

    (revision,) = h.revisions()
    assert outcome.revision_id == revision.id
    assert (revision.origin, revision.run_id, revision.revision) == ("stt", h.run_id, 1)
    assert (revision.timing_level, revision.language, revision.duration_ms) == (
        "word",
        "en",
        620_000,
    )
    assert (revision.word_count, revision.segment_count) == (3, 3)
    assert revision.coverage == [
        {"start_ms": 0, "end_ms": 300_000, "state": "succeeded"},
        {"start_ms": 300_000, "end_ms": 600_000, "state": "succeeded"},
        {"start_ms": 600_000, "end_ms": 620_000, "state": "succeeded"},
    ]
    assert revision.metadata_json == {
        "model": MAI_MODEL,
        "provider": "openrouter",
        "diagnostics": [],
    }
    words = h.words(revision)
    assert [w.text for w in words] == ["слово152500", "слово450000", "слово607500"]
    for word, middle in zip(words, (152_500, 450_000, 607_500), strict=True):
        assert abs(word.start_ms - (middle - 200)) <= 1
        assert abs(word.end_ms - (middle + 200)) <= 1
    chunks = h.chunks()
    assert [w.chunk_id for w in words] == [c.id for c in chunks]
    assert [(s.id, s.word_ids) for s in h.segments(revision)] == [
        ("s1", ["w1"]),
        ("s2", ["w2"]),
        ("s3", ["w3"]),
    ]

    # Money: one reservation per attempt, whole seconds of the upload window at the upper tariff.
    assert [(c.reserved_micro_usd, c.actual_micro_usd) for c in chunks] == [
        (30_500, 20),
        (31_000, 20),
        (2_500, 20),
    ]
    entries = h.entries()
    assert {e.state for e in entries} == {"completed"}
    assert sorted(e.request_id for e in entries) == sorted(c.request_id or "" for c in chunks)
    snapshot = next(e.price_snapshot for e in entries if e.reserved_micro_usd == 30_500)
    assert snapshot == {
        "usd_per_second_upper": "0.0001",
        "billed_seconds": 305,
        "upload_ms": 305_000,
        "basis": PRICE_BASIS,
    }
    assert outcome.committed_micro_usd == 60

    # Files: the response of every chunk stays, the audio does not.
    directory = db.data_dir / "stt" / h.run_id
    assert sorted(p.name for p in directory.iterdir()) == sorted(f"{c.id}.json" for c in chunks)
    assert [Path(c.response_path or "") for c in chunks] == [
        directory / f"{c.id}.json" for c in chunks
    ]
    run = h.run_row()
    assert run.status == "completed"
    assert run.coverage == revision.coverage
    assert run.usage == {"committed_micro_usd": 60, "error_code": None, "revision_id": revision.id}


def test_words_keep_their_chunk_and_the_chunk_local_speaker_label(make: MakeHarness) -> None:
    h = make(620_000, script=lambda call: speech(call, speaker="A"))

    outcome = h.run()

    assert outcome.status == "completed"
    (revision,) = h.revisions()
    chunks = h.chunks()
    words = h.words(revision)
    assert [(w.speaker_local, w.chunk_id) for w in words] == [("A", c.id) for c in chunks]


def test_language_and_provider_options_reach_the_client(make: MakeHarness) -> None:
    options = SttRunOptions(
        audio_stream_index=1, language="es", provider_options={"enhancedMode": {"on": True}}
    )
    h = make(60_000, options=options)

    assert h.run().status == "completed"
    (call,) = h.stt.calls
    assert (call.model, call.language, call.provider_options) == (
        MAI_MODEL,
        "es",
        {"enhancedMode": {"on": True}},
    )

    plain = make(60_000)
    assert plain.run().status == "completed"
    assert (plain.stt.calls[0].language, plain.stt.calls[0].provider_options) == (None, None)


# --- T-025: Retry-After, backoff, at most three attempts ---------------------------------------


def test_a_429_waits_for_retry_after_and_every_attempt_is_a_new_request(make: MakeHarness) -> None:
    limited = SttError("rate_limited", status=429, retry_after_s=7.0)
    h = make(60_000, script=scripted(limited, limited, None), concurrency=1)

    outcome = h.run()

    assert outcome.status == "completed"
    assert h.time.sleeps == [7.0, 7.0]
    assert len(h.stt.calls) == 3
    entries = h.entries()
    assert sorted(e.state for e in entries) == ["completed", "released", "released"]
    request_ids = {e.request_id for e in entries}
    assert len(request_ids) == 3
    assert {uuid.UUID(request_id).version for request_id in request_ids} == {4}
    (chunk,) = h.chunks()
    assert chunk.attempts == 3
    assert next(e for e in entries if e.state == "completed").request_id == chunk.request_id
    # What was released is not spent: only the paid attempt counts.
    assert outcome.committed_micro_usd == 20


def test_three_attempts_at_most_then_the_chunk_fails_with_the_last_code(make: MakeHarness) -> None:
    limited = SttError("rate_limited", status=429, retry_after_s=1.0)
    h = make(60_000, script=scripted(*[limited] * 5), concurrency=1)

    outcome = h.run()

    assert len(h.stt.calls) == 3
    assert h.time.sleeps == [1.0, 1.0]  # no wait after the last attempt
    assert states(outcome) == [(0, "failed", "STT_RATE_LIMITED")]
    assert outcome.chunks[0].attempts == 3
    assert (outcome.status, outcome.revision_id, outcome.committed_micro_usd) == ("failed", None, 0)
    assert {e.state for e in h.entries()} == {"released"}
    assert h.revisions() == []


@pytest.mark.parametrize("error", [SttError("upstream", status=503), SttError("not_sent")])
@pytest.mark.parametrize(("unit", "expected"), [(0.0, [1.0, 2.0]), (1.0, [2.0, 4.0])])
def test_without_retry_after_the_wait_is_an_exponential_backoff_with_jitter(
    make: MakeHarness, error: SttError, unit: float, expected: list[float]
) -> None:
    h = make(60_000, script=scripted(error, error, None), concurrency=1, jitter=unit)

    outcome = h.run()

    assert outcome.status == "completed"
    assert h.time.sleeps == expected
    assert sorted(e.state for e in h.entries()) == ["completed", "released", "released"]


@pytest.mark.parametrize("unit", [0.0, 0.3, 1.0])
def test_backoff_never_exceeds_the_cap_and_never_shrinks(unit: float) -> None:
    waits = [backoff_seconds(attempt, unit) for attempt in range(1, 40)]
    # A literal, not the constant: a change of the cap in the service must fail here.
    assert all(1.0 <= wait <= 60.0 for wait in waits)
    assert waits == sorted(waits)
    assert backoff_seconds(1, 0.0) == 1.0
    assert backoff_seconds(1, 1.0) == 2.0
    assert backoff_seconds(39, 1.0) == 60.0
    assert backoff_seconds(1, 5.0) == 2.0  # a jitter outside [0, 1] is clamped
    assert backoff_seconds(1, -5.0) == 1.0


def test_a_retry_after_longer_than_the_cap_is_cut_to_it(make: MakeHarness) -> None:
    long_wait = SttError("rate_limited", status=429, retry_after_s=100_000.0)
    h = make(60_000, script=scripted(long_wait, None), concurrency=1)

    assert h.run().status == "completed"
    assert h.time.sleeps == [300.0]  # a literal: a change of the cap must fail here (ADR 032)


def test_a_429_makes_the_other_worker_wait_for_the_same_cooldown(make: MakeHarness) -> None:
    c1_may_finish = threading.Event()
    c2_waited = threading.Event()
    limited_once = deque([SttError("rate_limited", status=429, retry_after_s=30.0)])

    def script(call: Call) -> SttResult:
        if call.upload_start_ms == FIRST_UPLOAD_START and limited_once:
            raise limited_once.popleft()
        if call.upload_start_ms == SECOND_UPLOAD_START:
            assert c1_may_finish.wait(5)
        return speech(call)

    def hook(index: int, seconds: float, halt: threading.Event) -> None:
        if index == 1:  # the chunk that was limited sleeps its Retry-After ...
            c1_may_finish.set()
            assert c2_waited.wait(5)  # ... and does not wake before the third chunk has waited
        else:
            c2_waited.set()

    h = make(900_000, script=script, concurrency=2)
    h.time.hook = hook

    outcome = h.run()

    # Chunk 1 finishes while chunk 0 sleeps, the free worker takes chunk 2 and must wait first.
    assert outcome.status == "completed"
    assert h.time.sleeps == [30.0, 30.0]
    assert sorted(e.state for e in h.entries()) == ["completed"] * 3 + ["released"]


# --- T-026: auth and funds stop the run --------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "status", "code"), [("auth", 401, "STT_AUTH"), ("funds", 402, "STT_FUNDS")]
)
def test_auth_and_funds_stop_the_run_and_cancel_the_rest(
    make: MakeHarness, kind: str, status: int, code: str
) -> None:
    error = SttError(kind, status=status)  # type: ignore[arg-type]
    h = make(900_000, script=scripted(error), concurrency=1)

    outcome = h.run()

    assert len(h.stt.calls) == 1
    assert h.time.sleeps == []
    assert states(outcome) == [
        (0, "failed", code),
        (300_000, "cancelled", code),
        (600_000, "cancelled", code),
    ]
    assert (outcome.status, outcome.error_code, outcome.revision_id) == ("failed", code, None)
    assert [e.state for e in h.entries()] == ["released"]
    assert outcome.committed_micro_usd == 0
    assert h.revisions() == []
    run = h.run_row()
    assert run.status == "failed"
    assert run.usage == {"committed_micro_usd": 0, "error_code": code, "revision_id": None}


def test_a_stop_after_some_chunks_succeeded_keeps_what_was_paid_for(make: MakeHarness) -> None:
    h = make(900_000, script=scripted(None, SttError("funds", status=402)), concurrency=1)

    outcome = h.run()

    assert states(outcome) == [
        (0, "succeeded", None),
        (300_000, "failed", "STT_FUNDS"),
        (600_000, "cancelled", "STT_FUNDS"),
    ]
    assert (outcome.status, outcome.error_code) == ("partial", "STT_FUNDS")
    (revision,) = h.revisions()
    assert revision.word_count == 1
    # A chunk that was cancelled is not transcribed yet: the document calls it pending.
    assert [c["state"] for c in revision.coverage] == ["succeeded", "failed", "pending"]


def test_a_stop_ends_the_wait_of_the_worker_that_is_still_retrying(make: MakeHarness) -> None:
    sibling_waiting = threading.Event()

    def script(call: Call) -> SttResult:
        if call.upload_start_ms == SECOND_UPLOAD_START:
            raise SttError("upstream", status=503)
        assert sibling_waiting.wait(5)
        raise SttError("auth", status=401)

    def hook(index: int, seconds: float, halt: threading.Event) -> None:
        sibling_waiting.set()
        assert halt.wait(5)  # like a real sleep: it ends when the run is halted

    h = make(600_000, script=script, concurrency=2)
    h.time.hook = hook

    outcome = h.run()

    assert states(outcome) == [(0, "failed", "STT_AUTH"), (300_000, "cancelled", "STT_AUTH")]
    assert (outcome.status, outcome.error_code) == ("failed", "STT_AUTH")
    assert len(h.stt.calls) == 2
    assert [e.state for e in h.entries()] == ["released", "released"]


class WorkerCrashed(Exception):
    """A bug in a worker: nothing in the service catches it."""


def test_a_crashed_worker_halts_its_sibling_in_a_retry_pause_so_that_it_sends_nothing_more(
    make: MakeHarness,
) -> None:
    sibling_pausing = threading.Event()

    def crash(request: SttAudioRequest) -> None:
        if request.upload_start_ms == FIRST_UPLOAD_START:
            assert sibling_pausing.wait(5)  # chunk 0 dies while chunk 1 waits out its backoff
            raise WorkerCrashed

    def hook(index: int, seconds: float, halt: threading.Event) -> None:
        sibling_pausing.set()
        # Like a real sleep it ends when the run is halted. The result is not asserted: a service
        # that never halts lets the pause run out and sends again, and that must show as a
        # request below, not as a failure inside this thread.
        halt.wait(3)

    h = make(
        600_000,
        script=failing_at(SECOND_UPLOAD_START, SttError("upstream", status=503)),
        concurrency=2,
    )
    h.extractor.fail = crash
    h.time.hook = hook

    with pytest.raises(WorkerCrashed):
        h.run()

    # Exactly one request was sent: chunk 1's, which got the 503 before the crash. Its retry
    # (a second request for 295 000) is the paid request a crash must not leave behind.
    assert [c.upload_start_ms for c in h.stt.calls] == [SECOND_UPLOAD_START]
    assert h.time.sleeps == [1.0]
    assert [e.state for e in h.entries()] == ["released"]
    dead, sibling = h.chunks()
    assert (dead.state, dead.attempts) == ("running", 1)
    assert (sibling.state, sibling.attempts, sibling.error_code) == ("cancelled", 1, None)


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("invalid_request", 400, "STT_INVALID_REQUEST"),
        ("model_unavailable", 404, "STT_MODEL_UNAVAILABLE"),
    ],
)
def test_an_error_a_retry_cannot_fix_fails_the_chunk_at_once(
    make: MakeHarness, kind: str, status: int | None, code: str
) -> None:
    h = make(60_000, script=scripted(SttError(kind, status=status)), concurrency=1)  # type: ignore[arg-type]

    outcome = h.run()

    assert len(h.stt.calls) == 1
    assert h.time.sleeps == []
    assert states(outcome) == [(0, "failed", code)]
    assert [e.state for e in h.entries()] == ["released"]
    assert outcome.status == "failed"


def test_an_unusable_answer_is_most_likely_paid_for_so_its_reservation_stays_and_is_not_retried(
    make: MakeHarness,
) -> None:
    h = make(
        60_000,
        script=scripted(SttError("invalid_response", status=200, generation_id="gen-1")),
        concurrency=1,
    )

    outcome = h.run()

    assert len(h.stt.calls) == 1  # no retry: the same request would get the same body
    assert h.time.sleeps == []
    assert states(outcome) == [(0, "failed", "STT_INVALID_RESPONSE")]
    assert (outcome.status, outcome.revision_id) == ("failed", None)
    # Not released: the answer may have been billed, and its cost is unknown (actual is None), so
    # the whole reservation (60 s at 0.0001 USD/s) counts as committed.
    (entry,) = h.entries()
    assert (entry.state, entry.reserved_micro_usd, entry.actual_micro_usd) == (
        "completed",
        6_000,
        None,
    )
    assert outcome.committed_micro_usd == 6_000
    (chunk,) = h.chunks()
    assert (chunk.attempts, chunk.reserved_micro_usd, chunk.actual_micro_usd) == (1, 6_000, None)
    assert h.revisions() == []


# --- T-027: 413 and MERGE_SEGMENT_TOO_LONG split the core --------------------------------------


def test_a_413_splits_the_core_in_two_and_the_coverage_stays_exact(make: MakeHarness) -> None:
    def script(call: Call) -> SttResult:
        if call.upload_ms > 160_000:
            raise SttError("payload_too_large", status=413)
        return speech(call)

    h = make(300_000, script=script)

    outcome = h.run()

    assert outcome.status == "completed"
    parent, first, second = h.chunks()
    assert (parent.state, parent.error_code, parent.attempts) == (
        "superseded",
        "STT_PAYLOAD_TOO_LARGE",
        1,
    )
    assert windows([parent]) == [(0, 300_000, 0, 300_000)]
    # The halves cover the parent exactly and their context is recomputed.
    assert windows([first, second]) == [
        (0, 150_000, 0, 155_000),
        (150_000, 300_000, 145_000, 300_000),
    ]
    assert [(c.state, c.attempts) for c in (first, second)] == [("succeeded", 1)] * 2
    assert [(c.core_start_ms, c.core_end_ms) for c in outcome.chunks] == [
        (0, 150_000),
        (150_000, 300_000),
    ]
    (revision,) = h.revisions()
    expected = [
        {"start_ms": 0, "end_ms": 150_000, "state": "succeeded"},
        {"start_ms": 150_000, "end_ms": 300_000, "state": "succeeded"},
    ]
    assert revision.coverage == expected
    assert h.run_row().coverage == expected
    assert [w.chunk_id for w in h.words(revision)] == [first.id, second.id]
    assert sorted(e.state for e in h.entries()) == ["completed", "completed", "released"]
    assert sorted((r.upload_start_ms, r.upload_end_ms) for r in h.extractor.requests) == [
        (0, 155_000),
        (0, 300_000),
        (145_000, 300_000),
    ]


def test_a_segment_too_long_to_split_by_words_splits_the_core_and_the_answer_is_paid(
    make: MakeHarness,
) -> None:
    def script(call: Call) -> SttResult:
        # Different text per window: equal text over the seam would be one repeated segment.
        text = "а" * 2_001 if call.upload_ms > 160_000 else f"слова{call.upload_start_ms}"
        return SttResult(
            text=text,
            words=(),
            segments=(ChunkItem(0.0, call.upload_ms / 1000, text),),
            has_word_timing=False,
            language="en",
            duration_s=None,
            usage=SttUsage(cost=Decimal("0.00002")),
            generation_id=None,
        )

    h = make(300_000, script=script)

    outcome = h.run()

    assert outcome.status == "completed"
    parent, first, second = h.chunks()
    assert (parent.state, parent.error_code, parent.actual_micro_usd) == (
        "superseded",
        "MERGE_SEGMENT_TOO_LONG",
        20,
    )
    assert [c.state for c in (first, second)] == ["succeeded", "succeeded"]
    (revision,) = h.revisions()
    assert (revision.timing_level, revision.word_count, revision.segment_count) == ("segment", 0, 2)
    # The long answer was paid for: complete, not release.
    assert [e.state for e in h.entries()] == ["completed"] * 3
    assert outcome.committed_micro_usd == 60


def test_a_chunk_below_the_minimum_core_fails_instead_of_splitting(make: MakeHarness) -> None:
    def too_large(call: Call) -> SttResult:
        raise SttError("payload_too_large", status=413)

    h = make(20_000, script=too_large)

    outcome = h.run()

    # 20 s -> 2 x 10 s -> 4 x 5 s; a 5 s core would leave halves below the 5 s minimum.
    rows = h.chunks()
    assert [c.state for c in rows].count("superseded") == 3
    assert windows(h.active_chunks()) == [
        (0, 5_000, 0, 10_000),
        (5_000, 10_000, 0, 15_000),
        (10_000, 15_000, 5_000, 20_000),
        (15_000, 20_000, 10_000, 20_000),
    ]
    assert [c.error_code for c in h.active_chunks()] == ["CHUNK_BELOW_MIN_CORE"] * 4
    assert states(outcome) == [
        (s, "failed", "CHUNK_BELOW_MIN_CORE") for s in (0, 5_000, 10_000, 15_000)
    ]
    assert len(h.stt.calls) == 7
    assert (outcome.status, outcome.revision_id, outcome.committed_micro_usd) == ("failed", None, 0)
    assert {e.state for e in h.entries()} == {"released"}
    assert [c["state"] for c in h.run_row().coverage or []] == ["failed"] * 4


def test_chunks_are_taken_in_core_order_and_the_halves_of_a_split_come_before_later_chunks(
    make: MakeHarness,
) -> None:
    def script(call: Call) -> SttResult:
        if call.upload_start_ms == 0 and call.upload_ms > 160_000:
            raise SttError("payload_too_large", status=413)
        return speech(call)

    h = make(900_000, script=script, concurrency=1)

    assert h.run().status == "completed"
    assert [c.upload_start_ms for c in h.stt.calls] == [0, 0, 145_000, 295_000, 595_000]


# --- T-028: unknown outcome ---------------------------------------------------------------------


def test_a_timeout_after_sending_is_unknown_and_is_not_retried_by_itself(
    make: MakeHarness,
) -> None:
    unknown = SttError("unknown_outcome")
    h = make(600_000, script=failing_at(SECOND_UPLOAD_START, unknown))

    outcome = h.run()

    assert len(h.stt.calls) == 2
    assert h.time.sleeps == []
    assert states(outcome) == [
        (0, "succeeded", None),
        (300_000, "unknown_outcome", "STT_UNKNOWN_OUTCOME"),
    ]
    assert (outcome.status, outcome.error_code) == ("partial", None)
    unknown_entry = next(e for e in h.entries() if e.state == "unknown_outcome")
    assert unknown_entry.reserved_micro_usd == 30_500
    # The money may be gone: the reservation keeps counting, next to the paid chunk.
    assert outcome.committed_micro_usd == 30_500 + 20
    (revision,) = h.revisions()
    assert [c["state"] for c in revision.coverage] == ["succeeded", "unknown_outcome"]

    # Running again without the explicit decision sends nothing and writes no new revision.
    again = h.run()
    assert len(h.stt.calls) == 2
    assert (again.status, again.revision_id) == ("partial", revision.id)
    assert len(h.revisions()) == 1


def test_an_unknown_chunk_is_sent_again_only_on_an_explicit_decision(make: MakeHarness) -> None:
    h = make(600_000, script=failing_at(SECOND_UPLOAD_START, SttError("unknown_outcome")))
    first = h.run()
    assert first.status == "partial"

    second = h.run(retry_unknown=True)

    assert len(h.stt.calls) == 3
    assert [c.upload_start_ms for c in h.stt.calls].count(SECOND_UPLOAD_START) == 2
    assert second.status == "completed"
    entries = h.entries()
    assert sorted(e.state for e in entries) == ["completed", "completed", "unknown_outcome"]
    assert len({e.request_id for e in entries}) == 3
    # The unknown attempt still counts: nothing makes it free.
    assert second.committed_micro_usd == 30_500 + 20 + 20
    assert [r.revision for r in h.revisions()] == [1, 2]
    assert second.revision_id == h.revisions()[1].id


# --- T-029: a partial transcript ----------------------------------------------------------------


def test_a_failed_chunk_gives_a_partial_transcript_with_a_visible_gap(make: MakeHarness) -> None:
    h = make(
        600_000, script=failing_at(SECOND_UPLOAD_START, SttError("invalid_request", status=400))
    )

    outcome = h.run()

    assert (outcome.status, outcome.error_code) == ("partial", None)
    assert states(outcome) == [(0, "succeeded", None), (300_000, "failed", "STT_INVALID_REQUEST")]
    assert len(h.stt.calls) == 2  # no retry of a request that is the same
    (revision,) = h.revisions()
    assert revision.coverage == [
        {"start_ms": 0, "end_ms": 300_000, "state": "succeeded"},
        {"start_ms": 300_000, "end_ms": 600_000, "state": "failed"},
    ]
    assert [w.text for w in h.words(revision)] == ["слово152500"]
    run = h.run_row()
    assert (run.status, run.coverage) == ("partial", revision.coverage)


# --- T-030: the cache ---------------------------------------------------------------------------


def test_a_repeated_run_sends_only_the_chunks_that_are_not_succeeded(make: MakeHarness) -> None:
    h = make(
        900_000, script=failing_at(SECOND_UPLOAD_START, SttError("invalid_request", status=400))
    )
    first = h.run()
    assert first.status == "partial"
    assert len(h.stt.calls) == 3

    second = h.run()

    assert second.status == "completed"
    assert [c.upload_start_ms for c in h.stt.calls[3:]] == [SECOND_UPLOAD_START]
    first_revision, second_revision = h.revisions()
    assert (first_revision.word_count, second_revision.word_count) == (2, 3)
    assert [c["state"] for c in second_revision.coverage] == ["succeeded"] * 3
    # The first revision is history and does not change.
    assert [c["state"] for c in first_revision.coverage] == ["succeeded", "failed", "succeeded"]
    assert second.revision_id == second_revision.id
    chunk = h.chunks()[1]
    assert (chunk.state, chunk.attempts, chunk.error_code) == ("succeeded", 2, None)

    third = h.run()
    assert len(h.stt.calls) == 4  # nothing left to send
    assert (third.status, third.revision_id) == ("completed", second_revision.id)
    assert len(h.revisions()) == 2


def test_a_call_that_has_no_succeeded_chunk_left_keeps_the_revision_of_the_earlier_call(
    make: MakeHarness,
) -> None:
    h = make(60_000, concurrency=1)
    first = h.run()
    assert (first.status, first.committed_micro_usd) == ("completed", 20)
    (revision,) = h.revisions()
    assert first.revision_id == revision.id

    # The cached response is gone, so the chunk must be sent again; the provider refuses the key.
    Path(h.chunks()[0].response_path or "").unlink()
    h.stt.script = scripted(SttError("auth", status=401))

    outcome = h.run()

    assert states(outcome) == [(0, "failed", "STT_AUTH")]
    assert (outcome.status, outcome.error_code) == ("failed", "STT_AUTH")
    assert outcome.revision_id == revision.id  # not None: the earlier revision still exists
    assert [r.id for r in h.revisions()] == [revision.id]
    run = h.run_row()
    assert run.status == "failed"
    assert run.usage == {
        "committed_micro_usd": 20,
        "error_code": "STT_AUTH",
        "revision_id": revision.id,
    }


@pytest.mark.parametrize("damage", ["missing", "not_json", "other_window", "bad_item", "elsewhere"])
def test_a_response_file_that_cannot_be_trusted_is_sent_again(
    make: MakeHarness, tmp_path: Path, damage: str
) -> None:
    h = make(600_000)
    assert h.run().status == "completed"
    assert len(h.stt.calls) == 2
    chunk = h.chunks()[0]
    path = Path(chunk.response_path or "")
    elsewhere = tmp_path / "elsewhere.json"
    if damage == "missing":
        path.unlink()
    elif damage == "not_json":
        path.write_text("{", encoding="ascii")
    elif damage == "elsewhere":
        shutil.copyfile(path, elsewhere)
        with h.db.write_session() as session:
            TranscriptRunRepository(session).update_chunk(chunk.id, response_path=str(elsewhere))
    else:
        document = json.loads(path.read_text(encoding="ascii"))
        if damage == "other_window":
            document["window"]["core_end_ms"] += 1
        else:
            document["words"][0]["start_s"] = 99_999.0
        path.write_text(json.dumps(document), encoding="ascii")

    outcome = h.run()

    assert outcome.status == "completed"
    assert [c.upload_start_ms for c in h.stt.calls[2:]] == [FIRST_UPLOAD_START]
    assert h.chunks()[0].attempts == 2
    assert len(h.revisions()) == 2
    if damage == "elsewhere":
        assert elsewhere.is_file()  # a file outside the run's folder is never touched


# --- budget -------------------------------------------------------------------------------------


def test_a_budget_that_does_not_fit_the_first_request_sends_nothing(make: MakeHarness) -> None:
    h = make(620_000)

    outcome = h.run(ceiling="0.01")  # the first request alone is reserved at 0.0305

    assert h.stt.calls == []
    assert states(outcome) == [(s, "failed", "BUDGET_EXCEEDED") for s in (0, 300_000, 600_000)]
    assert (outcome.status, outcome.error_code, outcome.revision_id) == (
        "failed",
        "BUDGET_EXCEEDED",
        None,
    )
    assert h.entries() == []
    assert h.run_row().usage == {
        "committed_micro_usd": 0,
        "error_code": "BUDGET_EXCEEDED",
        "revision_id": None,
    }


def test_the_budget_is_checked_after_the_audio_is_ready_and_still_sends_nothing(
    make: MakeHarness,
) -> None:
    h = make(620_000, concurrency=1)

    outcome = h.run(ceiling="0.01")  # the first request alone is reserved at 0.0305

    # The reservation is taken after the extraction, so the first chunk's audio was prepared, but
    # the budget refused the reservation: no request, no ledger entry, no audio left behind.
    assert len(h.extractor.requests) == 1
    assert h.stt.calls == []
    assert h.entries() == []
    assert states(outcome) == [(s, "failed", "BUDGET_EXCEEDED") for s in (0, 300_000, 600_000)]
    assert (outcome.status, outcome.error_code) == ("failed", "BUDGET_EXCEEDED")
    assert outcome.committed_micro_usd == 0
    assert list((h.db.data_dir / "stt" / h.run_id).iterdir()) == []


def test_the_run_stops_when_the_next_request_does_not_fit_and_a_reservation_keeps_counting(
    make: MakeHarness,
) -> None:
    h = make(620_000, script=lambda call: speech(call, cost=None), concurrency=1)

    outcome = h.run(ceiling="0.04")

    # The provider reported no cost, so the first reservation (30 500) stays in full: the second
    # request (31 000) would bring the run to 61 500 > 40 000.
    assert len(h.stt.calls) == 1
    assert states(outcome) == [
        (0, "succeeded", None),
        (300_000, "failed", "BUDGET_EXCEEDED"),
        (600_000, "failed", "BUDGET_EXCEEDED"),
    ]
    assert (outcome.status, outcome.error_code) == ("partial", "BUDGET_EXCEEDED")
    assert outcome.committed_micro_usd == 30_500
    (entry,) = h.entries()
    assert (entry.state, entry.reserved_micro_usd, entry.actual_micro_usd) == (
        "completed",
        30_500,
        None,
    )
    assert h.chunks()[0].actual_micro_usd is None


def test_a_reported_cost_replaces_the_reservation_and_frees_the_budget(make: MakeHarness) -> None:
    h = make(620_000, concurrency=1)

    outcome = h.run(ceiling="0.04")

    assert outcome.status == "completed"
    assert len(h.stt.calls) == 3
    assert outcome.committed_micro_usd == 60


def test_the_budget_covers_the_whole_run_across_calls(make: MakeHarness) -> None:
    h = make(
        600_000,
        script=failing_at(SECOND_UPLOAD_START, SttError("invalid_request", status=400)),
        concurrency=1,
    )
    assert h.run(ceiling="0.04").status == "partial"
    committed = h.ledger.committed_micro_usd(h.run_id)
    assert committed == 20  # the released attempt is free

    h.stt.script = lambda call: speech(call, cost=None)  # the retry reserves 30 500
    second = h.run(ceiling=str(Decimal(committed + 30_499) / 1_000_000))  # one micro-USD short

    assert h.chunks()[1].error_code == "BUDGET_EXCEEDED"
    assert (second.status, second.error_code) == ("partial", "BUDGET_EXCEEDED")
    third = h.run(ceiling="1")
    assert third.status == "completed"


# --- concurrency and order ----------------------------------------------------------------------


def test_at_most_two_requests_are_in_flight(make: MakeHarness) -> None:
    both_in_flight = threading.Barrier(2, timeout=5)
    release = threading.Event()

    def script(call: Call) -> SttResult:
        if call.upload_start_ms in (FIRST_UPLOAD_START, SECOND_UPLOAD_START):
            both_in_flight.wait()  # the first two chunks really run side by side
            assert release.wait(5)
        return speech(call)

    h = make(1_500_000, script=script)  # five chunks
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.run)
        for _ in range(500):
            if h.stt.active == 2:
                break
            threading.Event().wait(0.01)
        assert h.stt.active == 2
        threading.Event().wait(0.2)  # give a third request every chance to start
        assert (h.stt.active, len(h.stt.calls)) == (2, 2)
        release.set()
        outcome = future.result(timeout=30)

    assert outcome.status == "completed"
    assert h.stt.peak == 2
    assert len(h.stt.calls) == 5


def test_chunks_are_sent_in_the_order_of_their_cores(make: MakeHarness) -> None:
    h = make(1_500_000, concurrency=1)

    assert h.run().status == "completed"
    assert [c.upload_start_ms for c in h.stt.calls] == [0, 295_000, 595_000, 895_000, 1_195_000]


def test_a_second_run_call_for_the_same_run_is_refused_while_one_is_running(
    make: MakeHarness,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def script(call: Call) -> SttResult:
        entered.set()
        assert release.wait(5)
        return speech(call)

    h = make(60_000, script=script)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.run)
        assert entered.wait(5)
        refused = service_error(h.run)
        release.set()
        outcome = future.result(timeout=30)

    assert (refused.kind, refused.code) == (ErrorKind.CONFLICT, "TRANSCRIPTION_RUN_ACTIVE")
    assert outcome.status == "completed"
    assert h.run().status == "completed"  # the claim was released
    assert len(h.stt.calls) == 1


# --- cancellation -------------------------------------------------------------------------------


def test_a_run_that_is_cancelled_before_it_starts_sends_nothing(make: MakeHarness) -> None:
    h = make(620_000)
    cancel = threading.Event()
    cancel.set()

    outcome = h.run(cancel=cancel)

    assert h.stt.calls == []
    assert h.extractor.requests == []
    assert states(outcome) == [(s, "cancelled", None) for s in (0, 300_000, 600_000)]
    assert (outcome.status, outcome.revision_id) == ("cancelled", None)
    assert h.entries() == []
    assert h.run_row().status == "cancelled"


def test_cancelling_during_a_retry_wait_ends_the_wait_and_gives_the_reservation_back(
    make: MakeHarness,
) -> None:
    cancel = threading.Event()
    h = make(600_000, script=scripted(SttError("upstream", status=503)), concurrency=1)
    h.time.cancel = cancel
    h.time.hook = lambda index, seconds, halt: cancel.set()

    outcome = h.run(cancel=cancel)

    assert len(h.stt.calls) == 1
    assert states(outcome) == [(0, "cancelled", None), (300_000, "cancelled", None)]
    assert outcome.status == "cancelled"
    assert [e.state for e in h.entries()] == ["released"]


def test_cancelling_during_the_audio_extraction_ends_it_and_sends_nothing(
    make: MakeHarness,
) -> None:
    cancel = threading.Event()
    h = make(600_000, concurrency=1)

    def fail(request: SttAudioRequest) -> None:
        cancel.set()
        raise ExportCancelledError("Підготовку аудіо скасовано")

    h.extractor.fail = fail

    outcome = h.run(cancel=cancel)

    assert h.stt.calls == []
    assert states(outcome) == [(0, "cancelled", None), (300_000, "cancelled", None)]
    assert h.entries() == []  # nothing is reserved while FFmpeg runs
    assert list((h.db.data_dir / "stt" / h.run_id).iterdir()) == []


def test_a_cancel_that_arrives_during_the_extraction_is_honoured_before_anything_is_reserved(
    make: MakeHarness,
) -> None:
    cancel = threading.Event()
    h = make(600_000, concurrency=1)
    # The cancel comes while FFmpeg runs, but the extraction is not cut short and finishes.
    h.extractor.fail = lambda request: cancel.set()

    outcome = h.run(cancel=cancel)

    assert len(h.extractor.requests) == 1  # chunk 0 was extracted; chunk 1 never started
    assert h.stt.calls == []  # 0 requests
    assert h.entries() == []  # 0 reservations
    assert [c.reserved_micro_usd for c in h.chunks()] == [0, 0]
    assert states(outcome) == [(0, "cancelled", None), (300_000, "cancelled", None)]
    assert (outcome.status, outcome.committed_micro_usd) == ("cancelled", 0)
    assert list((h.db.data_dir / "stt" / h.run_id).iterdir()) == []


def test_cancelling_after_a_chunk_succeeded_keeps_it_and_marks_the_rest_pending(
    make: MakeHarness,
) -> None:
    cancel = threading.Event()

    def script(call: Call) -> SttResult:
        result = speech(call)
        cancel.set()  # the user cancels while the first request is on the wire
        return result

    h = make(900_000, script=script, concurrency=1)

    outcome = h.run(cancel=cancel)

    assert len(h.stt.calls) == 1  # the answer of a request that was sent is kept
    assert states(outcome) == [
        (0, "succeeded", None),
        (300_000, "cancelled", None),
        (600_000, "cancelled", None),
    ]
    assert outcome.status == "partial"
    (revision,) = h.revisions()
    assert [c["state"] for c in revision.coverage] == ["succeeded", "pending", "pending"]
    assert [e.state for e in h.entries()] == ["completed"]

    # A later run without the cancel sends only what is left.
    resumed = h.run()
    assert resumed.status == "completed"
    assert [c.upload_start_ms for c in h.stt.calls[1:]] == [SECOND_UPLOAD_START, 595_000]


def test_the_default_sleeper_ends_when_cancelled() -> None:
    cancelled = threading.Event()
    cancelled.set()
    assert cancellable_sleep(60.0, cancelled) is True
    assert cancellable_sleep(0.0, threading.Event()) is False


# --- audio, answers and the source --------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (SourceChangedError("x"), "SOURCE_CHANGED"),
        (NoAudioStreamError("x"), "SOURCE_NO_AUDIO"),
        (DiskFullError("x"), "DISK_FULL"),
        (SourceUnreadableError("x"), "SOURCE_UNREADABLE"),
        (IntervalError("INTERVAL_END_AFTER_DURATION", "x", "end_ms"), "INVALID_INTERVAL"),
        (MediaToolsUnavailableError("x"), "MEDIA_TOOLS_UNAVAILABLE"),
        (ValueError("x"), "AUDIO_FAILED"),
    ],
)
def test_an_audio_failure_fails_the_chunk_without_sending_and_reserves_nothing(
    make: MakeHarness, error: Exception, code: str
) -> None:
    h = make(60_000, concurrency=1)

    def fail(request: SttAudioRequest) -> None:
        raise error

    h.extractor.fail = fail

    outcome = h.run()

    assert h.stt.calls == []
    assert states(outcome) == [(0, "failed", code)]
    assert h.entries() == []
    assert outcome.committed_micro_usd == 0
    assert len(h.extractor.requests) == 1  # no retry of what cannot work
    assert outcome.status == "failed"


def test_an_answer_that_fails_validation_is_paid_for_and_not_retried(make: MakeHarness) -> None:
    def far_outside(call: Call) -> SttResult:
        late = call.upload_ms / 1000 + 1_000.0
        return SttResult(
            text="x",
            words=(ChunkItem(late, late + 1.0, "x"),),
            segments=(),
            has_word_timing=True,
            language="en",
            duration_s=None,
            usage=SttUsage(cost=Decimal("0.00002")),
            generation_id=None,
        )

    h = make(60_000, script=far_outside, concurrency=1)

    outcome = h.run()

    assert len(h.stt.calls) == 1
    assert states(outcome) == [(0, "failed", "MERGE_INVALID_PROVIDER_TIMING")]
    assert [e.state for e in h.entries()] == ["completed"]
    assert outcome.committed_micro_usd == 20
    assert h.revisions() == []
    directory = h.db.data_dir / "stt" / h.run_id
    assert list(directory.iterdir()) == []  # a response that was not accepted is not kept


def test_text_that_cannot_be_stored_is_refused_like_any_invalid_answer(make: MakeHarness) -> None:
    def with_nul(call: Call) -> SttResult:
        word = ChunkItem(1.0, 2.0, "a\x00b")
        return SttResult(
            text="a\x00b",
            words=(word,),
            segments=(),
            has_word_timing=True,
            language="en",
            duration_s=None,
            usage=SttUsage(cost=None),
            generation_id=None,
        )

    h = make(60_000, script=with_nul, concurrency=1)

    outcome = h.run()

    assert states(outcome) == [(0, "failed", "MERGE_INVALID_PROVIDER_TEXT")]
    assert [e.state for e in h.entries()] == ["completed"]
    assert h.revisions() == []


@pytest.mark.parametrize("damage", ["changed", "touched", "gone"])
def test_a_source_that_is_not_what_was_registered_is_not_transcribed(
    make: MakeHarness, damage: str
) -> None:
    h = make(60_000)
    media = h.source_path
    if damage == "changed":
        media.write_bytes(b"another, longer content")
    elif damage == "touched":
        state = media.stat()
        os.utime(media, ns=(state.st_atime_ns, state.st_mtime_ns + 10_000_000_000))
    else:
        media.unlink()

    refused = service_error(h.run)

    if damage == "gone":
        assert (refused.kind, refused.code) == (ErrorKind.UNAVAILABLE, "SOURCE_UNAVAILABLE")
    else:
        assert (refused.kind, refused.code) == (ErrorKind.CONFLICT, "SOURCE_CHANGED")
    assert h.stt.calls == []
    assert h.extractor.requests == []
    assert [c.state for c in h.chunks()] == ["pending"]
    assert h.entries() == []
    assert h.run_row().status == "queued"


# --- recovery of a call that died ---------------------------------------------------------------


def plant_running_chunk(h: Harness, entry_state: str | None) -> str:
    """A chunk left ``running`` by a dead call, with a reservation in ``entry_state`` (or none)."""
    (chunk,) = h.chunks()
    request_id = "dead-request"
    if entry_state is not None:
        entry = h.ledger.reserve(
            h.run_id, request_id, MAI_MODEL, Decimal("0.0305"), Decimal("10"), {}
        )
        if entry_state == "released":
            h.ledger.release(entry.id)
        elif entry_state == "completed":
            h.ledger.complete(entry.id, Decimal("0.00002"))
        elif entry_state == "unknown_outcome":
            h.ledger.mark_unknown(entry.id)
    with h.db.write_session() as session:
        TranscriptRunRepository(session).update_chunk(
            chunk.id, state="running", request_id=request_id
        )
    return chunk.id


@pytest.mark.parametrize(
    ("entry_state", "entry_state_after", "committed_micro_usd"),
    [
        # The reservation of a lost request is marked unknown and counts in full (30 500).
        ("reserved", "unknown_outcome", 30_500),
        # The ledger already knows the cost (20 micro-USD): the entry stays and counts that cost.
        ("completed", "completed", 20),
        ("unknown_outcome", "unknown_outcome", 30_500),
    ],
)
def test_a_chunk_left_running_with_a_reservation_is_unknown_and_not_sent(
    make: MakeHarness, entry_state: str, entry_state_after: str, committed_micro_usd: int
) -> None:
    h = make(60_000)
    plant_running_chunk(h, entry_state)

    outcome = h.run()

    assert h.stt.calls == []
    assert states(outcome) == [(0, "unknown_outcome", "STT_UNKNOWN_OUTCOME")]
    assert outcome.status == "failed"
    (entry,) = h.entries()
    assert entry.state == entry_state_after
    assert outcome.committed_micro_usd == committed_micro_usd


def test_an_unknown_chunk_of_a_dead_call_is_sent_again_on_an_explicit_decision(
    make: MakeHarness,
) -> None:
    h = make(60_000)
    plant_running_chunk(h, "reserved")

    outcome = h.run(retry_unknown=True)

    assert outcome.status == "completed"
    assert len(h.stt.calls) == 1
    assert sorted(e.state for e in h.entries()) == ["completed", "unknown_outcome"]
    assert outcome.committed_micro_usd == 30_500 + 20


@pytest.mark.parametrize("entry_state", ["released", None])
def test_a_chunk_left_running_that_never_reserved_anything_is_simply_sent(
    make: MakeHarness, entry_state: str | None
) -> None:
    h = make(60_000)
    plant_running_chunk(h, entry_state)

    outcome = h.run()

    assert outcome.status == "completed"
    assert len(h.stt.calls) == 1


class ProcessDied(Exception):
    """Stands in for the death of the process: nothing in the service catches it."""


def test_a_death_during_the_audio_extraction_leaves_no_reservation_and_the_chunk_pending(
    make: MakeHarness,
) -> None:
    h = make(60_000, concurrency=1)

    def die(request: SttAudioRequest) -> None:
        raise ProcessDied

    h.extractor.fail = die
    with pytest.raises(ProcessDied):
        h.run()

    # What the dead call left: a chunk that is `running` and not a single ledger entry.
    (dead,) = h.chunks()
    assert (dead.state, dead.attempts, dead.reserved_micro_usd) == ("running", 1, 0)
    assert dead.request_id is not None
    assert h.entries() == []
    assert h.stt.calls == []

    # Recovery with the run cancelled at once: the chunk is reset to pending and reaches the queue
    # (it would stay `unknown_outcome` and be skipped had anything been reserved), then it is
    # settled as cancelled. Still no ledger entry.
    cancel = threading.Event()
    cancel.set()
    recovered = h.run(cancel=cancel)
    assert states(recovered) == [(0, "cancelled", None)]
    assert h.run_row().coverage == [{"start_ms": 0, "end_ms": 60_000, "state": "pending"}]
    assert h.entries() == []

    # Recovery without a cancel sends the chunk on its own, without `retry_unknown`: it is a new
    # attempt with a new request and the only ledger entry of the run.
    h.extractor.fail = None
    outcome = h.run()
    assert outcome.status == "completed"
    assert len(h.stt.calls) == 1
    (entry,) = h.entries()
    assert entry.state == "completed"
    assert entry.request_id != dead.request_id
    (chunk,) = h.chunks()
    assert (chunk.state, chunk.attempts, chunk.request_id) == ("succeeded", 2, entry.request_id)
    assert outcome.committed_micro_usd == 20


# --- the real audio extraction ------------------------------------------------------------------


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_real_extraction_feeds_the_client_and_leaves_the_original_untouched(
    tmp_path: Path, db: Database, media_tools: MediaTools
) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "color_gop.mp4"
    shutil.copyfile(fixture_file("color_gop.mp4"), source)
    before = (sha256(source), source.stat().st_size, source.stat().st_mtime_ns)
    probe = probe_source(media_tools.ffprobe, source)
    audio_index = probe.audio_streams[0].index
    with db.write_session() as session:
        project = ProjectRepository(session).add("Проєкт")
        source_id = (
            SourceRepository(session)
            .add(
                project_id=project.id,
                path=str(source),
                display_name=source.name,
                size=before[1],
                mtime=before[2],
                quick_fingerprint="quick-1",
                hash_status="pending",
                duration_ms=probe.duration_ms,
                video_stream_index=probe.video_stream_index,
                audio_streams=[{"index": audio_index}],
                canonical_origin={"origin_ms": 0, "time_base": "1/1000"},
                probe_json={},
                availability="available",
            )
            .id
        )

    received: list[bytes] = []

    class RecordingStt:
        def transcribe(
            self,
            *,
            model: str,
            audio_wav: bytes,
            language: str | None = None,
            provider_options: Mapping[str, object] | None = None,
        ) -> SttResult:
            received.append(audio_wav)
            middle = probe.duration_ms / 2000
            return SttResult(
                text="hello",
                words=(ChunkItem(middle, middle + 0.5, "hello"),),
                segments=(),
                has_word_timing=True,
                language="en",
                duration_s=None,
                usage=SttUsage(cost=Decimal("0.00001")),
                generation_id=None,
            )

    fake_time = FakeTime()
    service = TranscriptionRunService(
        db,
        SpendLedger(db),
        client=RecordingStt(),
        extract_audio=ffmpeg_extractor(media_tools),
        clock=fake_time.clock,
        sleeper=fake_time.sleeper,
    )
    options = SttRunOptions(audio_stream_index=audio_index)
    run_id = service.create_run(source_id=source_id, model=MAI_MODEL, options=options)

    outcome = service.run(
        run_id, options=options, ceiling_usd=Decimal("1"), cancel=threading.Event()
    )

    assert outcome.status == "completed"
    (audio,) = received
    expected = estimate_payload(probe.duration_ms).audio_bytes  # 44 + 2 * 16000 Hz * duration
    assert len(audio) == expected
    assert audio[:4] == b"RIFF"
    assert audio[8:12] == b"WAVE"
    directory = db.data_dir / "stt" / run_id
    assert [p.suffix for p in directory.iterdir()] == [
        ".json"
    ]  # the audio is gone, the answer stays
    assert (sha256(source), source.stat().st_size, source.stat().st_mtime_ns) == before
    with db.session() as session:
        (revision,) = TranscriptRepository(session).list_page_for_source(source_id).items
        words = TranscriptRepository(session).list_words(revision.id)
    assert [w.text for w in words] == ["hello"]
