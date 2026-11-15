"""Preview proxies (FR-029, ADR 021): ``POST /v1/sources/{id}/previews``, ``GET /v1/previews/{id}``
and ``GET /v1/previews/{id}/media``.

``create`` validates the request and, in one write transaction, stores the ``previews`` row and its
``preview`` job (both ``queued``). If a ``ready`` preview for the same ``(source, audio stream,
profile version)`` exists, still matches the registered size and mtime of the source and its file
is on disk, that preview is returned and nothing is started; a preview of the same key that is
still ``queued`` or ``running`` is returned too, so one source is never rendered twice at once.
One worker thread then runs the jobs one after another, one render at a time:
``preview_engine.build_preview`` writes ``<data_dir>/cache/previews/<preview_id>.mp4`` through the
temp file ``.clipforge-<preview_id>.part``.

Preview statuses: ``queued`` -> ``running`` -> ``ready`` / ``failed`` / ``cancelled``; a restart
turns ``queued`` and ``running`` into ``interrupted`` (``recover``), and only the engine's exact
temp name of such a preview is deleted. The job of a preview follows its row: ``running`` ->
``succeeded`` / ``failed`` / ``cancelled``, with ``cancelling`` while a cancel is being carried
out. Job progress is ``{stage, percent}`` (``stage`` is ``render`` while FFmpeg works; ``percent``
is FFmpeg's ``out_time`` against the source duration, 0..100, written at most once per
``PROGRESS_INTERVAL_S``). Job record conventions are those of ``services/jobs.py``; the checkpoint
keeps ``preview_id``.

Messages are fixed Ukrainian text, never an exception text or a path.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from clipforge.adapters.ffprobe import ProbeError
from clipforge.adapters.media_tools import MediaTools, MediaToolsUnavailableError
from clipforge.adapters.process import FfmpegProgress
from clipforge.services.errors import (
    ServiceError,
    bad_request,
    conflict,
    invalid,
    media_tools_unavailable,
    not_found,
)
from clipforge.services.export_engine import (
    TEMP_PREFIX,
    TEMP_SUFFIX,
    ExportCancelledError,
    ExportError,
)
from clipforge.services.jobs import JobView, job_view
from clipforge.services.preview_engine import PROFILE_VERSION, PreviewRequest, build_preview
from clipforge.services.source_files import AVAILABLE, CHANGED, current_availability
from clipforge.services.sources import OpenedMedia
from clipforge.storage import (
    Database,
    JobRepository,
    Preview,
    PreviewRepository,
    Source,
    SourceRepository,
)

PREVIEW_JOB: Final = "preview"
PROGRESS_INTERVAL_S: Final = 0.5

_LOGGER = logging.getLogger(__name__)

_ACTIVE_JOB_STATES: Final = ("running", "cancelling")
_OPEN_PREVIEW_STATUSES: Final = ("queued", "running")
_GENERIC_ERROR: Final = "PREVIEW_FAILED"
_ERRORS: Final = {
    "SOURCE_UNAVAILABLE": "Файл джерела недоступний",
    "SOURCE_CHANGED": "Файл джерела змінився після реєстрації",
    "SOURCE_UNREADABLE": "Файл джерела пошкоджений або має непідтримуваний формат",
    "SOURCE_NO_VIDEO": "У файлі джерела немає відеодоріжки",
    "PROBE_TIMEOUT": "Перевірка файлу джерела перевищила ліміт часу",
    "HDR_UNSUPPORTED": "HDR-відео поки не підтримується для прев'ю",
    "INVALID_AUDIO_STREAM": "Вибрана аудіодоріжка відсутня у файлі",
    "OUTPUT_DIR_UNAVAILABLE": "Тека кешу прев'ю недоступна",
    "OUTPUT_NOT_WRITABLE": "Немає доступу на запис у кеш прев'ю",
    "DISK_FULL": "Недостатньо місця на диску для прев'ю",
    "FFMPEG_FAILED": "FFmpeg не зміг створити прев'ю",
    "OUTPUT_INVALID": "Створене прев'ю не пройшло перевірку",
    "MEDIA_TOOLS_UNAVAILABLE": "FFmpeg або FFprobe недоступні",
    "INTERRUPTED": "Створення прев'ю перервано перезапуском застосунку",
    _GENERIC_ERROR: "Не вдалося створити прев'ю",
}


@dataclass(frozen=True, slots=True)
class CreatedPreview:
    id: str
    job_id: str
    status: str
    """``ready`` when an existing preview was reused (nothing was started)."""


@dataclass(frozen=True, slots=True)
class PreviewView:
    id: str
    source_id: str
    audio_stream_index: int | None
    status: str
    job_id: str
    error: dict[str, str] | None


@dataclass(frozen=True, slots=True)
class _Work:
    preview_id: str
    source_path: Path | None
    size: int
    mtime_ns: int
    fingerprint: str
    duration_ms: int
    audio_index: int | None


@dataclass(frozen=True, slots=True)
class _Ready:
    size: int


@dataclass(frozen=True, slots=True)
class _Failed:
    code: str


@dataclass(frozen=True, slots=True)
class _Cancelled:
    pass


_Outcome = _Ready | _Failed | _Cancelled


class PreviewService:
    def __init__(self, database: Database, media_tools: MediaTools | None) -> None:
        self._database = database
        self._tools = media_tools
        self._directory = database.data_dir / "cache" / "previews"
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._stopping = threading.Event()
        # One worker: one FFmpeg render at a time.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview")

    # -- create / read -----------------------------------------------------------------------

    def create(self, source_id: str, audio_stream_index: int | None) -> CreatedPreview:
        if self._tools is None:
            raise media_tools_unavailable()
        with self._database.write_session() as session:
            source = SourceRepository(session).get(source_id)
            if source is None:
                raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
            _require_audio_stream(source, audio_stream_index)
            _require_source_file(source)

            previews = PreviewRepository(session)
            key = (source.id, audio_stream_index, PROFILE_VERSION)
            ready = previews.find_ready(*key)
            if ready is not None and self._is_reusable(ready, source):
                return CreatedPreview(id=ready.id, job_id=ready.job_id, status="ready")
            running = previews.find_open(*key)
            if running is not None:
                return CreatedPreview(id=running.id, job_id=running.job_id, status=running.status)

            jobs = JobRepository(session)
            job = jobs.add(type=PREVIEW_JOB, progress_json={"stage": "queued", "percent": 0})
            preview = previews.add(
                source_id=source.id,
                audio_stream_index=audio_stream_index,
                profile_version=PROFILE_VERSION,
                job_id=job.id,
                source_size=source.size,
                source_mtime=source.mtime,
            )
            jobs.set_checkpoint(job.id, {"preview_id": preview.id})
            created = CreatedPreview(id=preview.id, job_id=job.id, status=preview.status)

        event = threading.Event()
        with self._lock:
            self._events[created.job_id] = event
        try:
            self._executor.submit(self._run, created.job_id, created.id, event)
        except RuntimeError:
            # Shut down: the job stays queued and is recovered at the next start.
            _LOGGER.warning("Preview job was not started: the service is stopped")
        return created

    def get(self, preview_id: str) -> PreviewView:
        with self._database.session() as session:
            return _view(_require_preview(PreviewRepository(session), preview_id))

    def open_media(self, preview_id: str) -> OpenedMedia:
        """Open a ``ready`` preview read-only for playback. A preview that is not ready, or whose
        file is gone or has another size than the verified one, is ``PREVIEW_NOT_READY``."""
        with self._database.session() as session:
            preview = _require_preview(PreviewRepository(session), preview_id)
            status, expected_size = preview.status, preview.file_size
        if status != "ready" or expected_size is None:
            raise _not_ready(retryable=status in _OPEN_PREVIEW_STATUSES)
        path = self._final_path(preview_id)
        try:
            handle = path.open("rb")  # closed by the caller
        except OSError:
            raise _not_ready(retryable=False) from None
        try:
            if handle.seek(0, 2) != expected_size:
                raise _not_ready(retryable=False)
        except BaseException:
            handle.close()
            raise
        return OpenedMedia(file=handle, size=expected_size, extension=path.suffix)

    def _is_reusable(self, preview: Preview, source: Source) -> bool:
        if (preview.source_size, preview.source_mtime) != (source.size, source.mtime):
            return False
        try:
            return self._final_path(preview.id).stat().st_size == preview.file_size
        except OSError:
            return False

    # -- cancel / recovery / shutdown --------------------------------------------------------

    def cancel(self, job_id: str) -> JobView:
        """Cancel a preview job (``POST /v1/jobs/{id}/cancel``).

        A queued job is cancelled at once. A running job becomes ``cancelling``: FFmpeg is stopped,
        the temp file removed, and the worker then marks the job and the preview ``cancelled``.
        Cancelling a job that is already ``cancelling`` or ``cancelled`` changes nothing.
        """
        signal: threading.Event | None = None
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None:
                raise not_found("JOB_NOT_FOUND", "Завдання не знайдено")
            if job.state == "queued":
                preview_id = _preview_id_of(job.checkpoint_json)
                if (
                    jobs.transition(
                        job_id,
                        "cancelled",
                        from_states=("queued",),
                        progress_json={"stage": "cancelled", "percent": 0},
                    )
                    and preview_id is not None
                ):
                    PreviewRepository(session).set_state(preview_id, "cancelled")
            elif job.state == "running":
                if jobs.transition(job_id, "cancelling", from_states=("running",)):
                    with self._lock:
                        signal = self._events.get(job_id)
            elif job.state not in ("cancelling", "cancelled"):
                raise conflict("JOB_NOT_CANCELLABLE", "Завдання вже завершено")
            session.refresh(job)
            view = job_view(job)
        if signal is not None:
            signal.set()
        return view

    def recover(self) -> None:
        """Start-up recovery: a preview a dead process left ``queued`` or ``running`` becomes
        ``interrupted`` and the temp file of its render, only under the engine's exact name, is
        removed. Its job is already ``interrupted`` (``JobRunner.recover``)."""
        orphans: list[Path] = []
        with self._database.write_session() as session:
            previews = PreviewRepository(session)
            for preview in previews.list_with_status(_OPEN_PREVIEW_STATUSES):
                previews.set_state(preview.id, "interrupted", error_code="INTERRUPTED")
                orphan = self._temp_path(preview.id)
                if orphan is not None:
                    orphans.append(orphan)
        for path in orphans:
            _remove_file(path)

    def shutdown(self) -> None:
        """Stop the current render and wait for the worker. Unfinished rows are left as they are:
        the next start recovers them like after a crash."""
        self._stopping.set()
        with self._lock:
            for event in self._events.values():
                event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    # -- worker ------------------------------------------------------------------------------

    def _run(self, job_id: str, preview_id: str, cancel: threading.Event) -> None:
        try:
            try:
                self._execute(job_id, preview_id, cancel)
            except Exception as exc:
                # Never log the exception text: it may carry a path.
                _LOGGER.error("Preview job failed unexpectedly (%s)", type(exc).__name__)
                if not self._stopping.is_set():
                    self._finish(job_id, preview_id, _Failed(_GENERIC_ERROR))
        except Exception as inner:
            _LOGGER.error("Preview job could not be marked failed (%s)", type(inner).__name__)
        finally:
            with self._lock:
                self._events.pop(job_id, None)

    def _execute(self, job_id: str, preview_id: str, cancel: threading.Event) -> None:
        work = self._begin(job_id, preview_id)
        if work is None:
            return
        progress = _Progress(self._database, job_id, work.duration_ms)
        try:
            outcome = self._render(work, cancel, progress)
        finally:
            progress.close()
        if isinstance(outcome, _Cancelled) and self._stopping.is_set():
            return  # shutting down: the next start recovers the row
        if isinstance(outcome, _Failed):
            _LOGGER.warning("Preview failed: preview=%s code=%s", preview_id, outcome.code)
        self._finish(job_id, preview_id, outcome)

    def _begin(self, job_id: str, preview_id: str) -> _Work | None:
        """Move the job and its preview to ``running``; ``None`` when the job is no longer queued
        (it was cancelled while it waited)."""
        with self._database.write_session() as session:
            previews = PreviewRepository(session)
            preview = previews.get(preview_id)
            if preview is None:
                return None
            if not JobRepository(session).transition(
                job_id,
                "running",
                from_states=("queued",),
                progress_json={"stage": "render", "percent": 0},
            ):
                return None
            previews.set_state(preview_id, "running")
            source = SourceRepository(session).get(preview.source_id)
            return _Work(
                preview_id=preview_id,
                source_path=Path(source.path) if source is not None else None,
                size=source.size if source is not None else 0,
                mtime_ns=source.mtime if source is not None else 0,
                fingerprint=source.quick_fingerprint if source is not None else "",
                duration_ms=source.duration_ms if source is not None else 1,
                audio_index=preview.audio_stream_index,
            )

    def _render(self, work: _Work, cancel: threading.Event, progress: _Progress) -> _Outcome:
        try:
            if self._tools is None:
                raise MediaToolsUnavailableError("FFmpeg або FFprobe недоступні")
            if work.source_path is None:
                return _Failed("SOURCE_UNAVAILABLE")
            try:
                self._directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                return _Failed("OUTPUT_DIR_UNAVAILABLE")
            result = build_preview(
                self._tools,
                PreviewRequest(
                    task_id=work.preview_id,
                    source=work.source_path,
                    output_dir=self._directory,
                    audio_stream_index=work.audio_index,
                    expected_size=work.size,
                    expected_mtime_ns=work.mtime_ns,
                    expected_fingerprint=work.fingerprint,
                ),
                cancel=cancel,
                on_progress=progress,
            )
        except ExportCancelledError:
            return _Cancelled()
        except (ExportError, ProbeError) as error:
            return _Failed(error.code)
        except MediaToolsUnavailableError:
            return _Failed("MEDIA_TOOLS_UNAVAILABLE")
        except Exception as exc:
            _LOGGER.error("Preview render failed unexpectedly (%s)", type(exc).__name__)
            return _Failed(_GENERIC_ERROR)
        return _Ready(size=result.size)

    def _finish(self, job_id: str, preview_id: str, outcome: _Outcome) -> None:
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            previews = PreviewRepository(session)
            if isinstance(outcome, _Ready):
                if jobs.transition(
                    job_id,
                    "succeeded",
                    from_states=_ACTIVE_JOB_STATES,
                    progress_json={"stage": "done", "percent": 100},
                ):
                    previews.set_state(preview_id, "ready", file_size=outcome.size)
            elif isinstance(outcome, _Failed):
                code = _known_code(outcome.code)
                jobs.set_checkpoint(
                    job_id,
                    {
                        "preview_id": preview_id,
                        "error": {"code": code, "message": _ERRORS[code]},
                    },
                )
                if jobs.transition(
                    job_id,
                    "failed",
                    from_states=("queued", *_ACTIVE_JOB_STATES),
                    progress_json={"stage": "failed", "percent": 0},
                ):
                    previews.set_state(preview_id, "failed", error_code=code)
            elif jobs.transition(
                job_id,
                "cancelled",
                from_states=_ACTIVE_JOB_STATES,
                progress_json={"stage": "cancelled", "percent": 0},
            ):
                previews.set_state(preview_id, "cancelled")

    # -- files -------------------------------------------------------------------------------

    def _final_path(self, preview_id: str) -> Path:
        return self._directory / f"{preview_id.lower()}.mp4"

    def _temp_path(self, preview_id: str) -> Path | None:
        """The temp file of this preview's render under the exact name the engine gives it; a
        stored id that is not a canonical UUID never becomes a path."""
        try:
            canonical = str(uuid.UUID(preview_id))
        except ValueError:
            return None
        if canonical != preview_id.lower():
            return None
        return self._directory / f"{TEMP_PREFIX}{canonical}{TEMP_SUFFIX}"


class _Progress:
    """The ``on_progress`` callback of one render: ``percent`` is FFmpeg's ``out_time`` against the
    duration of the source (0..100). Written at most once per ``PROGRESS_INTERVAL_S``; runs on
    FFmpeg's reader thread, never raises and writes nothing after ``close``."""

    def __init__(self, database: Database, job_id: str, duration_ms: int) -> None:
        self._database = database
        self._job_id = job_id
        self._duration_us = max(1, duration_ms) * 1000
        self._lock = threading.Lock()
        self._written_at = 0.0
        self._last = 0
        self._closed = False

    def __call__(self, progress: FfmpegProgress) -> None:
        with self._lock:
            now = time.monotonic()
            if self._closed or now - self._written_at < PROGRESS_INTERVAL_S:
                return
            percent = self._percent(progress)
            if percent < self._last:  # never go backwards
                return
            self._written_at, self._last = now, percent
            try:
                with self._database.write_session() as session:
                    JobRepository(session).set_progress(
                        self._job_id, {"stage": "render", "percent": percent}
                    )
            except Exception as exc:
                # A lost progress report must not fail the render; no exception text in the log.
                _LOGGER.warning("Preview progress was not saved (%s)", type(exc).__name__)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _percent(self, progress: FfmpegProgress) -> int:
        if progress.ended:
            return 100
        if progress.out_time_us is None:
            return 0
        return max(0, min(100, progress.out_time_us * 100 // self._duration_us))


# --- helpers ---------------------------------------------------------------------------------


def _require_preview(previews: PreviewRepository, preview_id: str) -> Preview:
    preview = previews.get(preview_id)
    if preview is None:
        raise not_found("PREVIEW_NOT_FOUND", "Прев'ю не знайдено")
    return preview


def _require_audio_stream(source: Source, audio_stream_index: int | None) -> None:
    if audio_stream_index is None:
        return
    if audio_stream_index not in {int(stream["index"]) for stream in source.audio_streams}:
        raise invalid(
            "INVALID_AUDIO_STREAM",
            "Вибрана аудіодоріжка відсутня у джерелі",
            field="audio_stream_index",
        )


def _require_source_file(source: Source) -> None:
    """The file must still be what was registered, or there is nothing to render a proxy of."""
    availability = current_availability(source)
    if availability == AVAILABLE:
        return
    code = "SOURCE_CHANGED" if availability == CHANGED else "SOURCE_UNAVAILABLE"
    raise bad_request(code, _ERRORS[code])


def _not_ready(*, retryable: bool) -> ServiceError:
    return conflict("PREVIEW_NOT_READY", "Прев'ю ще не готове", retryable=retryable)


def _known_code(code: str) -> str:
    return code if code in _ERRORS else _GENERIC_ERROR


def _preview_id_of(checkpoint: dict[str, Any] | None) -> str | None:
    value = (checkpoint or {}).get("preview_id")
    return value if isinstance(value, str) else None


def _view(preview: Preview) -> PreviewView:
    error: dict[str, str] | None = None
    if preview.error_code:
        code = _known_code(preview.error_code)
        error = {"code": code, "message": _ERRORS[code]}
    return PreviewView(
        id=preview.id,
        source_id=preview.source_id,
        audio_stream_index=preview.audio_stream_index,
        status=preview.status,
        job_id=preview.job_id,
        error=error,
    )


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        _LOGGER.warning("A leftover preview temp file could not be removed")
