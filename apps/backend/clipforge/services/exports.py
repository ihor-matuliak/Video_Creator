"""Export batches (FR-036..FR-044, docs/04 sections 4 and 5, ADR 017, 018 and 020).

``create_batch`` validates the request and, in one write transaction, stores the batch, one item
with an immutable snapshot per clip, the ``export`` job and ``export_state = queued`` on every
clip. Nothing is started before every rule passed (a clip that is not approved is a 409 before
any subprocess). One worker thread then runs the jobs one after another, one render at a time
(FR-039): it calls ``export_engine.export_clip`` for each item from the snapshot, then writes the
manifest JSON and the CSV catalog (FR-042) into the output directory.

Item states: ``queued`` -> ``running`` -> ``verified`` / ``failed`` / ``cancelled``. The clip's
``export_state`` follows the item and becomes ``stale`` when the clip's start or end changed since
the snapshot (the rule of ``ClipService.edit``, FR-032); a new title or review status does not make
a render stale. Job progress is ``{stage, total, completed, failed}``: ``completed`` counts every
finished item (verified, failed or cancelled), ``failed`` the failed ones; both only grow. While an
item renders, ``stage`` is the engine stage (``render`` or ``verify``) and ``item_percent`` (int,
0..100) is the position of that stage of the current item; it is written at most once per
``PROGRESS_INTERVAL_S`` (the first report of a stage is always written). Batch status:
``cancelled`` when any item was cancelled (finished items are kept), otherwise ``succeeded``,
``partial_failed`` or ``failed`` from the items.

Job record conventions are those of ``services/jobs.py``. The checkpoint keeps ``batch_id`` (the
link from the batch to its job), and after the run ``manifest_path`` and ``catalog_path``;
``recover`` writes both files for the batches it closes, too (FR-042).

Messages are fixed Ukrainian text, never an exception text or a path. A failed item is logged at
WARNING with its codes and a bounded tail of the engine's stderr (docs/05 section 3) in which the
source, the output directory and the temp file are replaced by ``<source>``, ``<output>`` and
``<temp>``; that text never reaches the API, the database or a manifest.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy.orm import Session

from clipforge.adapters.ffprobe import ProbeError
from clipforge.adapters.media_tools import MediaTools, MediaToolsUnavailableError
from clipforge.adapters.process import FfmpegProgress
from clipforge.domain.catalog import CatalogRow, render_catalog_csv
from clipforge.domain.interval import IntervalError
from clipforge.services.errors import (
    ServiceError,
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
    ExportRequest,
    ExportResult,
    ExportStage,
    export_clip,
)
from clipforge.services.jobs import JobView, job_view
from clipforge.services.text import is_storable
from clipforge.storage import (
    Clip,
    ClipRepository,
    Database,
    ExportItem,
    ExportRepository,
    JobRepository,
    ProjectRepository,
    Source,
    SourceRepository,
)
from clipforge.storage.util import utc_now

EXPORT_JOB: Final = "export"
SUPPORTED_VIDEO_CODEC: Final = "h264"
SUPPORTED_QUALITY: Final = "high"
AUDIO_MODES: Final = ("selected", "none", "all")
MANIFEST_SCHEMA_VERSION: Final = 1
PROGRESS_INTERVAL_S: Final = 0.5

_LOGGER = logging.getLogger(__name__)
_DETAIL_LIMIT_BYTES: Final = 4096

_OPEN_ITEM_STATES: Final = ("queued", "running")
_OPEN_CLIP_STATES: Final = ("queued", "running")
_OPEN_BATCH_STATUSES: Final = ("queued", "running")
_ACTIVE_JOB_STATES: Final = ("running", "cancelling")

_ITEM_ERRORS: Final = {
    "SOURCE_UNAVAILABLE": "Файл джерела недоступний",
    "SOURCE_CHANGED": "Файл джерела змінився після реєстрації",
    "SOURCE_UNREADABLE": "Файл джерела пошкоджений або має непідтримуваний формат",
    "SOURCE_NO_VIDEO": "У файлі джерела немає відеодоріжки",
    "PROBE_TIMEOUT": "Перевірка файлу джерела перевищила ліміт часу",
    "HDR_UNSUPPORTED": "HDR-відео поки не підтримується для експорту",
    "INVALID_AUDIO_STREAM": "Вибрана аудіодоріжка відсутня у файлі",
    "INVALID_INTERVAL": "Межі кліпу виходять за межі джерела",
    "OUTPUT_DIR_UNAVAILABLE": "Тека експорту недоступна",
    "OUTPUT_NOT_WRITABLE": "Немає доступу на запис у теку експорту",
    "DISK_FULL": "Недостатньо місця на диску для експорту",
    "FFMPEG_FAILED": "FFmpeg не зміг створити кліп",
    "NO_VIDEO_FRAMES": "У вибраному інтервалі немає жодного кадру відео",
    "OUTPUT_INVALID": "Створений кліп не пройшов перевірку",
    "MEDIA_TOOLS_UNAVAILABLE": "FFmpeg або FFprobe недоступні",
    "INTERRUPTED": "Експорт перервано перезапуском застосунку",
    "EXPORT_FAILED": "Не вдалося експортувати кліп",
}
_JOB_ALL_FAILED: Final = {"code": "EXPORT_FAILED", "message": "Жоден кліп не вдалося експортувати"}
_JOB_CRASHED: Final = {"code": "EXPORT_FAILED", "message": "Експорт завершився помилкою"}
_JOB_CATALOG_FAILED: Final = {
    "code": "CATALOG_WRITE_FAILED",
    "message": "Не вдалося записати маніфест і каталог експорту",
}


# --- request and view types ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportProfile:
    """The user's export profile (docs/04 section 4); validated by ``create_batch``."""

    video_codec: str
    quality: str
    audio_mode: str
    audio_stream_index: int | None
    write_srt: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "video_codec": self.video_codec,
            "quality": self.quality,
            "audio_mode": self.audio_mode,
            "audio_stream_index": self.audio_stream_index,
            "write_srt": self.write_srt,
        }


@dataclass(frozen=True, slots=True)
class CreatedBatch:
    id: str
    job_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ExportItemView:
    id: str
    clip_id: str
    clip_revision: int
    state: str
    final_path: str | None
    observed: dict[str, Any] | None
    error: dict[str, str] | None


@dataclass(frozen=True, slots=True)
class ExportBatchView:
    id: str
    project_id: str
    job_id: str | None
    status: str
    output_dir: str
    profile: dict[str, Any]
    items: list[ExportItemView]
    manifest_path: str | None
    catalog_path: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class _ItemRecord:
    """A detached copy of an item row, safe to use after its transaction ended."""

    id: str
    clip_id: str
    clip_revision: int
    state: str
    final_path: str | None
    observed: dict[str, Any] | None
    error_code: str | None
    snapshot: dict[str, Any]

    @classmethod
    def of(cls, item: ExportItem) -> _ItemRecord:
        return cls(
            id=item.id,
            clip_id=item.clip_id,
            clip_revision=item.clip_revision,
            state=item.state,
            final_path=item.final_path,
            observed=dict(item.observed_json) if item.observed_json is not None else None,
            error_code=item.error_code,
            snapshot=dict(item.snapshot_json),
        )

    @property
    def clip(self) -> dict[str, Any]:
        value = self.snapshot.get("clip")
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class _BatchRecord:
    """A detached copy of a batch with its items, enough to write its manifest and catalog."""

    id: str
    project_id: str
    status: str
    output_dir: Path
    profile: dict[str, Any]
    created_at: str
    items: list[_ItemRecord]


@dataclass(frozen=True, slots=True)
class _Work:
    batch_id: str
    states: tuple[str, ...]
    """The item states of the whole batch when this item started (this one is ``running``)."""
    item_id: str
    clip_id: str
    revision: int
    start_ms: int
    end_ms: int
    audio_index: int | None
    audio_indexes: tuple[int, ...]
    output_dir: Path
    source_path: Path | None
    fingerprint: str
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _Verified:
    path: Path
    observed: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Failed:
    code: str


@dataclass(frozen=True, slots=True)
class _Cancelled:
    pass


_Outcome = _Verified | _Failed | _Cancelled


class _SourceProblem(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- service ---------------------------------------------------------------------------------


class ExportService:
    def __init__(self, database: Database, media_tools: MediaTools | None) -> None:
        self._database = database
        self._tools = media_tools
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._stopping = threading.Event()
        # One worker: one FFmpeg render at a time (FR-039).
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="export")

    # -- create / read -----------------------------------------------------------------------

    def create_batch(
        self,
        *,
        project_id: str,
        clip_ids: Sequence[str],
        output_dir: str,
        profile: ExportProfile,
    ) -> CreatedBatch:
        if self._tools is None:
            raise media_tools_unavailable()
        ids = list(dict.fromkeys(clip_ids))
        if not ids:
            raise invalid(
                "EXPORT_NO_CLIPS", "Виберіть хоча б один кліп для експорту", field="clip_ids"
            )
        _validate_profile(profile)
        directory = _parse_output_dir(output_dir)

        with self._database.write_session() as session:
            if ProjectRepository(session).get(project_id) is None:
                raise not_found("PROJECT_NOT_FOUND", "Проєкт не знайдено")
            loaded = _load_clips(session, ids)
            mismatched = [clip.id for clip, source in loaded if source.project_id != project_id]
            if mismatched:
                raise _invalid_with(
                    "CLIP_PROJECT_MISMATCH",
                    "Кліп належить іншому проєкту",
                    field="clip_ids",
                    clip_ids=mismatched,
                )
            audio = [_audio_plan(profile, source) for _, source in loaded]
            unapproved = [clip.id for clip, _ in loaded if clip.review_status != "approved"]
            if unapproved:
                raise conflict(
                    "CLIP_NOT_APPROVED",
                    "Експортувати можна лише підтверджені кліпи",
                    details={"clip_ids": unapproved},
                )

            exports = ExportRepository(session)
            clips = ClipRepository(session)
            batch = exports.add_batch(
                project_id=project_id,
                output_dir=str(directory),
                profile_json=profile.as_json(),
                status="queued",
            )
            job = JobRepository(session).add(
                type=EXPORT_JOB,
                progress_json=_progress("queued", ["queued"] * len(loaded)),
                checkpoint_json={"batch_id": batch.id},
            )
            for position, ((clip, source), plan) in enumerate(zip(loaded, audio, strict=True)):
                exports.add_item(
                    batch_id=batch.id,
                    clip_id=clip.id,
                    clip_revision=clip.revision,
                    snapshot_json=_snapshot(position, clip, source, profile, plan),
                    state="queued",
                )
                clips.set_export_state(clip.id, "queued")
            created = CreatedBatch(id=batch.id, job_id=job.id, status=batch.status)

        event = threading.Event()
        with self._lock:
            self._events[created.job_id] = event
        try:
            self._executor.submit(self._run, created.job_id, event)
        except RuntimeError:
            # Shut down: the job stays queued and is recovered at the next start.
            _LOGGER.warning("Export job was not started: the service is stopped")
        return created

    def get_batch(self, batch_id: str) -> ExportBatchView:
        with self._database.session() as session:
            exports = ExportRepository(session)
            batch = exports.get_batch(batch_id)
            if batch is None:
                raise not_found("EXPORT_BATCH_NOT_FOUND", "Пакет експорту не знайдено")
            records = [_ItemRecord.of(item) for item in _ordered(exports.list_items(batch_id))]
            job = JobRepository(session).find_by_checkpoint(
                job_type=EXPORT_JOB, key="batch_id", value=batch_id
            )
            checkpoint = dict(job.checkpoint_json or {}) if job is not None else {}
            return ExportBatchView(
                id=batch.id,
                project_id=batch.project_id,
                job_id=job.id if job is not None else None,
                status=batch.status,
                output_dir=batch.output_dir,
                profile=dict(batch.profile_json),
                items=[_item_view(record) for record in records],
                manifest_path=_optional_text(checkpoint.get("manifest_path")),
                catalog_path=_optional_text(checkpoint.get("catalog_path")),
                created_at=batch.created_at,
            )

    # -- cancel / recovery / shutdown --------------------------------------------------------

    def cancel(self, job_id: str) -> JobView:
        """Cancel an export job (``POST /v1/jobs/{id}/cancel``).

        A queued job is cancelled at once with its whole batch. A running job becomes
        ``cancelling``: the current render is stopped (its temp file removed), the items not
        rendered yet are cancelled and the finished items are kept. Cancelling a job that is
        already ``cancelling`` or ``cancelled`` changes nothing.
        """
        signal: threading.Event | None = None
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            if job is None:
                raise not_found("JOB_NOT_FOUND", "Завдання не знайдено")
            if job.state == "queued":
                batch_id = _batch_id_of(job.checkpoint_json)
                if (
                    jobs.transition(
                        job_id,
                        "cancelled",
                        from_states=("queued",),
                        progress_json={"stage": "cancelled"},
                    )
                    and batch_id is not None
                ):
                    _cancel_unstarted_batch(session, batch_id)
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
        """Start-up recovery (ADR 017): batches a dead process left open end ``failed`` or
        ``partial_failed``; their unfinished items fail with ``INTERRUPTED`` and the temp file
        the dead render may have left is removed. The jobs themselves are already
        ``interrupted`` (``JobRunner.recover``)."""
        orphans: list[Path] = []
        closed: list[_BatchRecord] = []
        with self._database.write_session() as session:
            exports = ExportRepository(session)
            clips = ClipRepository(session)
            for batch in exports.list_batches_with_status(_OPEN_BATCH_STATUSES):
                items = exports.list_items(batch.id)
                for item in items:
                    if item.state not in _OPEN_ITEM_STATES:
                        continue
                    orphan = _own_temp(item, Path(batch.output_dir))
                    if orphan is not None:
                        orphans.append(orphan)
                    exports.update_item(
                        item.id, state="failed", temp_path=None, error_code="INTERRUPTED"
                    )
                    _settle_clip(clips, item.clip_id, "failed")
                status = _batch_status([item.state for item in items])
                exports.set_batch_status(batch.id, status)
                closed.append(
                    _BatchRecord(
                        id=batch.id,
                        project_id=batch.project_id,
                        status=status,
                        output_dir=Path(batch.output_dir),
                        profile=dict(batch.profile_json),
                        created_at=batch.created_at,
                        items=[_ItemRecord.of(item) for item in _ordered(items)],
                    )
                )
        for path in orphans:
            _remove_file(path)
        for record in closed:
            self._publish_closed(record)

    def _publish_closed(self, batch: _BatchRecord) -> None:
        """Write the manifest and the catalog of a batch ``recover`` closed and link them from its
        job. Best effort: a missing output directory or a bad row never stops the start-up."""
        try:
            manifest_path, catalog_path = _write_batch_files(self._tools, batch)
            with self._database.write_session() as session:
                jobs = JobRepository(session)
                job = jobs.find_by_checkpoint(job_type=EXPORT_JOB, key="batch_id", value=batch.id)
                if job is None:
                    return
                checkpoint: dict[str, Any] = dict(job.checkpoint_json or {})
                checkpoint["manifest_path"] = _text_or_none(manifest_path)
                checkpoint["catalog_path"] = _text_or_none(catalog_path)
                jobs.set_checkpoint(job.id, checkpoint)
        except Exception as exc:
            # Never log the exception text: it may carry a path.
            _LOGGER.error(
                "Manifest of an interrupted export was not written (%s)", type(exc).__name__
            )

    def shutdown(self) -> None:
        """Stop the current render and wait for the worker. Unfinished rows are left as they are:
        the next start recovers them like after a crash."""
        self._stopping.set()
        with self._lock:
            for event in self._events.values():
                event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    # -- worker ------------------------------------------------------------------------------

    def _run(self, job_id: str, cancel: threading.Event) -> None:
        try:
            try:
                self._execute(job_id, cancel)
            except Exception as exc:
                # Never log the exception text: it may carry a path.
                _LOGGER.error("Export job failed unexpectedly (%s)", type(exc).__name__)
                self._abort(job_id)
        except Exception as inner:
            _LOGGER.error("Export job could not be marked failed (%s)", type(inner).__name__)
        finally:
            with self._lock:
                self._events.pop(job_id, None)

    def _execute(self, job_id: str, cancel: threading.Event) -> None:
        started = self._begin(job_id)
        if started is None:
            return
        batch_id, item_ids = started
        for item_id in item_ids:
            if cancel.is_set():
                break
            self._render_item(job_id, item_id, cancel)
        if self._stopping.is_set():
            return
        self._complete(job_id, batch_id, crashed=False)

    def _abort(self, job_id: str) -> None:
        with self._database.session() as session:
            job = JobRepository(session).get(job_id)
            batch_id = _batch_id_of(job.checkpoint_json) if job is not None else None
        if batch_id is not None and not self._stopping.is_set():
            self._complete(job_id, batch_id, crashed=True)

    def _begin(self, job_id: str) -> tuple[str, list[str]] | None:
        """Move the job and its batch to ``running``; ``None`` when the job is no longer queued
        (it was cancelled while it waited)."""
        with self._database.write_session() as session:
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            batch_id = _batch_id_of(job.checkpoint_json) if job is not None else None
            if batch_id is None:
                return None
            exports = ExportRepository(session)
            items = _ordered(exports.list_items(batch_id))
            progress = _progress("render", [item.state for item in items])
            if not jobs.transition(
                job_id, "running", from_states=("queued",), progress_json=progress
            ):
                return None
            exports.set_batch_status(batch_id, "running")
            return batch_id, [item.id for item in items]

    def _render_item(self, job_id: str, item_id: str, cancel: threading.Event) -> None:
        work = self._start_item(item_id)
        if work is None:
            return
        outcome = self._render(job_id, work, cancel)
        if isinstance(outcome, _Cancelled) and self._stopping.is_set():
            return  # shutting down: the next start recovers the row
        self._finish_item(job_id, work, outcome)

    def _start_item(self, item_id: str) -> _Work | None:
        with self._database.write_session() as session:
            exports = ExportRepository(session)
            item = exports.get_item(item_id)
            if item is None or item.state != "queued":
                return None
            batch = exports.get_batch(item.batch_id)
            if batch is None:
                return None
            snapshot = item.snapshot_json
            clip = snapshot["clip"]
            audio = snapshot["audio"]
            source = SourceRepository(session).get(str(snapshot["source_id"]))
            output_dir = Path(batch.output_dir)
            exports.update_item(
                item_id,
                state="running",
                temp_path=str(output_dir / f"{TEMP_PREFIX}{item_id}{TEMP_SUFFIX}"),
            )
            ClipRepository(session).set_export_state(item.clip_id, "running")
            states = tuple(
                "running" if other.id == item_id else other.state
                for other in _ordered(exports.list_items(item.batch_id))
            )
            single = audio.get("stream_index")
            return _Work(
                batch_id=item.batch_id,
                states=states,
                item_id=item_id,
                clip_id=item.clip_id,
                revision=item.clip_revision,
                start_ms=int(clip["start_ms"]),
                end_ms=int(clip["end_ms"]),
                audio_index=int(single) if single is not None else None,
                audio_indexes=tuple(int(index) for index in audio.get("stream_indexes", ())),
                output_dir=output_dir,
                source_path=Path(source.path) if source is not None else None,
                fingerprint=source.quick_fingerprint if source is not None else "",
                size=source.size if source is not None else 0,
                mtime_ns=source.mtime if source is not None else 0,
            )

    def _render(self, job_id: str, work: _Work, cancel: threading.Event) -> _Outcome:
        progress = _ItemProgress(self._database, job_id, work)
        try:
            if self._tools is None:
                raise MediaToolsUnavailableError("FFmpeg або FFprobe недоступні")
            source = _require_registered_source(work)
            result = export_clip(
                self._tools,
                ExportRequest(
                    task_id=work.item_id,
                    source=source,
                    output_dir=work.output_dir,
                    start_ms=work.start_ms,
                    end_ms=work.end_ms,
                    clip_id=work.clip_id,
                    revision=work.revision,
                    audio_stream_index=work.audio_index,
                    expected_fingerprint=work.fingerprint,
                    audio_stream_indexes=work.audio_indexes,
                ),
                cancel=cancel,
                on_progress=progress,
            )
        except ExportCancelledError:
            return _Cancelled()
        except ExportError as error:
            _log_item_failure(work, error.code, error.detail)
            return _Failed(error.code)
        except ProbeError as error:
            _log_item_failure(work, error.code, error.detail)
            return _Failed(error.code)
        except _SourceProblem as problem:
            _log_item_failure(work, problem.code)
            return _Failed(problem.code)
        except IntervalError:
            _log_item_failure(work, "INVALID_INTERVAL")
            return _Failed("INVALID_INTERVAL")
        except MediaToolsUnavailableError:
            _log_item_failure(work, "MEDIA_TOOLS_UNAVAILABLE")
            return _Failed("MEDIA_TOOLS_UNAVAILABLE")
        except Exception as exc:
            _LOGGER.error("Export of one clip failed unexpectedly (%s)", type(exc).__name__)
            return _Failed("EXPORT_FAILED")
        finally:
            progress.close()
        return _Verified(path=result.path, observed=_observed(work, result))

    def _finish_item(self, job_id: str, work: _Work, outcome: _Outcome) -> None:
        with self._database.write_session() as session:
            exports = ExportRepository(session)
            clips = ClipRepository(session)
            if isinstance(outcome, _Verified):
                exports.update_item(
                    work.item_id,
                    state="verified",
                    final_path=str(outcome.path),
                    observed_json=outcome.observed,
                    temp_path=None,
                    error_code=None,
                )
                # The render used the snapshot bounds; it is stale only if the clip's bounds differ
                # now, like in ClipService.edit. A title, category or review change is not enough.
                current = clips.get(work.clip_id)
                changed = current is not None and (
                    (current.start_ms, current.end_ms) != (work.start_ms, work.end_ms)
                )
                clips.set_export_state(work.clip_id, "stale" if changed else "verified")
            elif isinstance(outcome, _Failed):
                exports.update_item(
                    work.item_id, state="failed", temp_path=None, error_code=outcome.code
                )
                clips.set_export_state(work.clip_id, "failed")
            else:
                exports.update_item(work.item_id, state="cancelled", temp_path=None)
                clips.set_export_state(work.clip_id, "cancelled")
            item = exports.get_item(work.item_id)
            if item is not None:
                states = [other.state for other in exports.list_items(item.batch_id)]
                JobRepository(session).set_progress(job_id, _progress("render", states))

    def _complete(self, job_id: str, batch_id: str, *, crashed: bool) -> None:
        """Settle the items nobody rendered, write the manifest and the catalog, then publish the
        final batch status and job state together."""
        leftover_state = "failed" if crashed else "cancelled"
        leftover_code = "EXPORT_FAILED" if crashed else None
        with self._database.write_session() as session:
            exports = ExportRepository(session)
            clips = ClipRepository(session)
            batch = exports.get_batch(batch_id)
            if batch is None:
                return
            items = _ordered(exports.list_items(batch_id))
            for item in items:
                if item.state in _OPEN_ITEM_STATES:
                    exports.update_item(
                        item.id, state=leftover_state, temp_path=None, error_code=leftover_code
                    )
                    _settle_clip(clips, item.clip_id, leftover_state)
            records = [_ItemRecord.of(item) for item in items]
            states = [record.state for record in records]
            finished = _BatchRecord(
                id=batch_id,
                project_id=batch.project_id,
                status=_batch_status(states),
                output_dir=Path(batch.output_dir),
                profile=dict(batch.profile_json),
                created_at=batch.created_at,
                items=records,
            )

        status = finished.status
        manifest_path, catalog_path = _write_batch_files(self._tools, finished)
        error: dict[str, str] | None = None
        if crashed:
            job_state, stage, error = "failed", "failed", dict(_JOB_CRASHED)
        elif manifest_path is None or catalog_path is None:
            job_state, stage, error = "failed", "failed", dict(_JOB_CATALOG_FAILED)
        elif status == "cancelled":
            job_state, stage = "cancelled", "cancelled"
        elif status == "failed":
            job_state, stage, error = "failed", "failed", dict(_JOB_ALL_FAILED)
        else:
            job_state, stage = "succeeded", "done"

        with self._database.write_session() as session:
            ExportRepository(session).set_batch_status(batch_id, status)
            jobs = JobRepository(session)
            job = jobs.get(job_id)
            checkpoint: dict[str, Any] = dict(job.checkpoint_json or {}) if job is not None else {}
            checkpoint["manifest_path"] = _text_or_none(manifest_path)
            checkpoint["catalog_path"] = _text_or_none(catalog_path)
            if error is not None:
                checkpoint["error"] = error
            jobs.set_checkpoint(job_id, checkpoint)
            jobs.transition(
                job_id,
                job_state,
                from_states=_ACTIVE_JOB_STATES,
                progress_json=_progress(stage, states),
            )


class _ItemProgress:
    """The ``on_progress`` callback of one render.

    It writes the engine stage and ``item_percent`` into the job's progress, next to the counts
    of the batch. ``item_percent`` is FFmpeg's ``out_time`` against the clip duration (0..100) and
    belongs to the current stage, so it starts again from 0 when ``verify`` follows ``render``.
    A report is written when the stage changed or ``PROGRESS_INTERVAL_S`` passed since the last
    write. It runs on FFmpeg's reader thread, never raises, and writes nothing after ``close``.
    """

    def __init__(self, database: Database, job_id: str, work: _Work) -> None:
        self._database = database
        self._job_id = job_id
        self._states = work.states
        self._duration_us = max(1, work.end_ms - work.start_ms) * 1000
        self._lock = threading.Lock()
        self._stage: ExportStage | None = None
        self._written_at = 0.0
        self._closed = False

    def __call__(self, stage: ExportStage, progress: FfmpegProgress) -> None:
        with self._lock:
            now = time.monotonic()
            if self._closed:
                return
            if stage == self._stage and now - self._written_at < PROGRESS_INTERVAL_S:
                return
            self._stage, self._written_at = stage, now
            value = {**_progress(stage, self._states), "item_percent": self._percent(progress)}
            try:
                with self._database.write_session() as session:
                    JobRepository(session).set_progress(self._job_id, value)
            except Exception as exc:
                # A lost progress report must not fail the render; no exception text in the log.
                _LOGGER.warning("Export progress was not saved (%s)", type(exc).__name__)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _percent(self, progress: FfmpegProgress) -> int:
        if progress.ended:
            return 100
        if progress.out_time_us is None:
            return 0
        return max(0, min(100, progress.out_time_us * 100 // self._duration_us))


# --- validation ------------------------------------------------------------------------------


def _validate_profile(profile: ExportProfile) -> None:
    """Unsupported values are 422, never a silent fallback (docs/04 section 4)."""
    if profile.video_codec != SUPPORTED_VIDEO_CODEC:
        raise invalid(
            "UNSUPPORTED_VIDEO_CODEC",
            "Підтримується лише відеокодек h264",
            field="profile.video_codec",
        )
    if profile.quality != SUPPORTED_QUALITY:
        raise invalid(
            "UNSUPPORTED_QUALITY",
            "Підтримується лише висока якість (high)",
            field="profile.quality",
        )
    if profile.audio_mode not in AUDIO_MODES:
        raise invalid(
            "UNSUPPORTED_AUDIO_MODE",
            "Режим аудіо має бути selected, none або all",
            field="profile.audio_mode",
        )
    index = profile.audio_stream_index
    if profile.audio_mode == "selected":
        if index is None or index < 0:
            raise invalid(
                "INVALID_AUDIO_STREAM",
                "Для режиму selected потрібна коректна аудіодоріжка",
                field="profile.audio_stream_index",
            )
    elif index is not None:
        raise invalid(
            "INVALID_AUDIO_STREAM",
            "Для режимів none і all аудіодоріжка має бути null",
            field="profile.audio_stream_index",
        )
    if profile.write_srt:
        raise invalid(
            "SRT_REQUIRES_TRANSCRIPT",
            "Для субтитрів SRT потрібен транскрипт, а його ще немає",
            field="profile.write_srt",
        )


def _parse_output_dir(raw: str) -> Path:
    """An absolute, existing directory in its canonical (resolved) form."""
    if not raw or not is_storable(raw):
        raise invalid("INVALID_OUTPUT_DIR", "Шлях до теки експорту некоректний", field="output_dir")
    path = Path(raw)
    if not path.is_absolute():
        raise invalid(
            "INVALID_OUTPUT_DIR", "Шлях до теки експорту має бути повним", field="output_dir"
        )
    try:
        resolved = path.resolve()
        usable = resolved.is_dir()
    except (OSError, RuntimeError, ValueError):
        usable = False
    if not usable:
        raise invalid("OUTPUT_DIR_UNAVAILABLE", "Тека експорту недоступна", field="output_dir")
    return resolved


def _load_clips(session: Session, clip_ids: Sequence[str]) -> list[tuple[Clip, Source]]:
    clips = ClipRepository(session)
    sources = SourceRepository(session)
    loaded: list[tuple[Clip, Source]] = []
    for clip_id in clip_ids:
        clip = clips.get(clip_id)
        if clip is None:
            raise not_found("CLIP_NOT_FOUND", "Кліп не знайдено")
        source = sources.get(clip.source_id)
        if source is None:
            raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
        loaded.append((clip, source))
    return loaded


def _audio_plan(profile: ExportProfile, source: Source) -> dict[str, Any]:
    """The audio of one clip's render: ``selected`` must be an audio stream of that source, ``all``
    is every audio stream of it in index order (ADR 020)."""
    available = sorted(int(stream["index"]) for stream in source.audio_streams)
    if profile.audio_mode == "selected":
        if profile.audio_stream_index not in available:
            raise _invalid_with(
                "INVALID_AUDIO_STREAM",
                "Вибрана аудіодоріжка відсутня у джерелі",
                field="profile.audio_stream_index",
                source_id=source.id,
            )
        return {
            "mode": "selected",
            "stream_index": profile.audio_stream_index,
            "stream_indexes": [],
        }
    if profile.audio_mode == "all":
        return {"mode": "all", "stream_index": None, "stream_indexes": available}
    return {"mode": "none", "stream_index": None, "stream_indexes": []}


def _invalid_with(code: str, message: str, *, field: str, **details: Any) -> ServiceError:
    error = invalid(code, message, field=field)
    error.details.update(details)
    return error


def _snapshot(
    position: int, clip: Clip, source: Source, profile: ExportProfile, audio: dict[str, Any]
) -> dict[str, Any]:
    """What the render uses: it never changes after the batch was created (docs/04 section 5)."""
    return {
        "position": position,
        "clip": {
            "id": clip.id,
            "revision": clip.revision,
            "title": clip.title,
            "start_ms": clip.start_ms,
            "end_ms": clip.end_ms,
            "categories": list(clip.categories),
            "review_status": clip.review_status,
            "evidence_type": clip.evidence_type,
        },
        "source_id": source.id,
        "audio": audio,
        "profile": profile.as_json(),
    }


# --- worker helpers --------------------------------------------------------------------------


def _require_registered_source(work: _Work) -> Path:
    """The file must still be what was registered (FR-038): present, a regular file, same size
    and modification time. The engine adds the quick fingerprint and its own before/after check."""
    path = work.source_path
    if path is None:
        raise _SourceProblem("SOURCE_UNAVAILABLE")
    try:
        state = path.stat()
    except OSError:
        raise _SourceProblem("SOURCE_UNAVAILABLE") from None
    if not stat.S_ISREG(state.st_mode):
        raise _SourceProblem("SOURCE_UNAVAILABLE")
    if state.st_size != work.size or state.st_mtime_ns != work.mtime_ns:
        raise _SourceProblem("SOURCE_CHANGED")
    return path


def _log_item_failure(work: _Work, code: str, detail: str = "") -> None:
    """One WARNING per failed item: ids, the code and a bounded, redacted stderr tail. The
    detail is for the log only (docs/05 section 3); the API and the database never get it."""
    _LOGGER.warning(
        "Export item failed: batch=%s item=%s code=%s detail=%s",
        work.batch_id,
        work.item_id,
        code,
        _log_detail(work, detail),
    )


def _log_detail(work: _Work, detail: str) -> str:
    """``detail`` without the source, the output directory and the temp file (their names too),
    on one line, cut to its last ``_DETAIL_LIMIT_BYTES`` bytes. Redacting comes first, so a path
    cut by the limit cannot survive in pieces."""
    temp = work.output_dir / f"{TEMP_PREFIX}{work.item_id.lower()}{TEMP_SUFFIX}"
    secrets: list[tuple[str, str]] = [
        (str(temp), "<temp>"),
        (temp.name, "<temp>"),
        (str(work.output_dir), "<output>"),
    ]
    if work.source_path is not None:
        secrets += [(str(work.source_path), "<source>"), (work.source_path.name, "<source>")]
    replacements: dict[str, str] = {}
    for value, token in secrets:
        for variant in (value, value.replace("\\", "/"), value.replace("/", "\\")):
            for form in (variant, variant.replace("\\", "\\\\")):  # also as JSON-escaped text
                if form:
                    replacements.setdefault(form.lower(), token)
    if replacements:
        # The longest first: a temp path contains the output directory, and so may the source.
        names = sorted(replacements, key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(name) for name in names), re.IGNORECASE)
        detail = pattern.sub(
            lambda found: replacements.get(found.group(0).lower(), "<redacted>"), detail
        )
    one_line = " | ".join(line.strip() for line in detail.splitlines() if line.strip())
    return one_line.encode("utf-8", errors="replace")[-_DETAIL_LIMIT_BYTES:].decode(
        "utf-8", errors="ignore"
    )


def _observed(work: _Work, result: ExportResult) -> dict[str, Any]:
    return {
        "seek_mode": result.seek_mode,
        "ffmpeg_version": result.ffmpeg_version,
        "ffprobe_version": result.ffprobe_version,
        "profile": dict(result.profile),
        "interval": {"start_ms": work.start_ms, "end_ms": work.end_ms},
        "first_video_pts_ms": result.first_video_pts_ms,
        "last_video_pts_ms": result.last_video_pts_ms,
        "observed_duration_ms": result.observed_duration_ms,
        "video_frame_count": result.video_frame_count,
        "audio_stream_indexes": _audio_indexes(work),
    }


def _audio_indexes(work: _Work) -> list[int]:
    if work.audio_indexes:
        return list(work.audio_indexes)
    return [work.audio_index] if work.audio_index is not None else []


def _batch_status(states: Sequence[str]) -> str:
    verified = states.count("verified")
    failed = states.count("failed")
    if states.count("cancelled"):
        return "cancelled"  # the user's cancel keeps the finished items (docs/04 section 5)
    if not failed:
        return "succeeded"
    return "failed" if not verified else "partial_failed"


def _progress(stage: str, states: Sequence[str]) -> dict[str, Any]:
    """Job progress from the item states. An item never leaves a finished state, so both counts
    only grow; every call site passes the states of the whole batch."""
    return {
        "stage": stage,
        "total": len(states),
        "completed": sum(1 for state in states if state not in _OPEN_ITEM_STATES),
        "failed": states.count("failed"),
    }


def _position(item: ExportItem) -> int:
    value = item.snapshot_json.get("position", 0)
    return value if isinstance(value, int) else 0


def _ordered(items: Sequence[ExportItem]) -> list[ExportItem]:
    return sorted(items, key=_position)


def _batch_id_of(checkpoint: dict[str, Any] | None) -> str | None:
    value = (checkpoint or {}).get("batch_id")
    return value if isinstance(value, str) else None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _text_or_none(path: Path | None) -> str | None:
    return str(path) if path is not None else None


def _settle_clip(clips: ClipRepository, clip_id: str, state: str) -> None:
    """Set the export state of a clip whose item never rendered, unless a newer result stands."""
    current = clips.get(clip_id)
    if current is not None and current.export_state in _OPEN_CLIP_STATES:
        clips.set_export_state(clip_id, state)


def _cancel_unstarted_batch(session: Session, batch_id: str) -> None:
    exports = ExportRepository(session)
    clips = ClipRepository(session)
    for item in exports.list_items(batch_id):
        if item.state == "queued":
            exports.update_item(item.id, state="cancelled")
            _settle_clip(clips, item.clip_id, "cancelled")
    exports.set_batch_status(batch_id, "cancelled")


def _own_temp(item: ExportItem, output_dir: Path) -> Path | None:
    """The temp file of this item's render, only if it has the exact name the engine gives it."""
    if item.temp_path is None:
        return None
    path = Path(item.temp_path)
    if path.parent != output_dir or path.name != f"{TEMP_PREFIX}{item.id}{TEMP_SUFFIX}":
        return None
    return path


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        _LOGGER.warning("A leftover export temp file could not be removed")


def _item_error(code: str | None) -> dict[str, str] | None:
    if code is None:
        return None
    known = code if code in _ITEM_ERRORS else "EXPORT_FAILED"
    return {"code": known, "message": _ITEM_ERRORS[known]}


def _item_view(record: _ItemRecord) -> ExportItemView:
    return ExportItemView(
        id=record.id,
        clip_id=record.clip_id,
        clip_revision=record.clip_revision,
        state=record.state,
        final_path=record.final_path,
        observed=record.observed,
        error=_item_error(record.error_code),
    )


# --- manifest and catalog --------------------------------------------------------------------


def _manifest(
    *,
    batch_id: str,
    project_id: str,
    status: str,
    requested_profile: dict[str, Any],
    created_at: str,
    records: Sequence[_ItemRecord],
    tools: MediaTools | None,
) -> bytes:
    """Manifest JSON (FR-036, FR-042, docs/05 section 1). Nothing secret is ever in it: it is
    built from the batch, the item snapshots and what the render observed."""
    document: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "batch_id": batch_id,
        "project_id": project_id,
        "status": status,
        "created_at": created_at,
        "finished_at": utc_now(),
        "requested_profile": requested_profile,
        "tools": {
            "ffmpeg_version": tools.ffmpeg_version if tools is not None else None,
            "ffprobe_version": tools.ffprobe_version if tools is not None else None,
        },
        "items": [_manifest_item(record) for record in records],
    }
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")


def _manifest_item(record: _ItemRecord) -> dict[str, Any]:
    clip = record.clip
    observed = record.observed or {}
    start_ms, end_ms = int(clip.get("start_ms", 0)), int(clip.get("end_ms", 0))
    return {
        "item_id": record.id,
        "clip_id": record.clip_id,
        "revision": record.clip_revision,
        "source_id": record.snapshot.get("source_id"),
        "title": clip.get("title"),
        "categories": clip.get("categories", []),
        "review_status": clip.get("review_status"),
        "interval": {"start_ms": start_ms, "end_ms": end_ms, "duration_ms": end_ms - start_ms},
        "state": record.state,
        "path": record.final_path,
        "error_code": record.error_code,
        "seek_mode": observed.get("seek_mode"),
        "ffmpeg_version": observed.get("ffmpeg_version"),
        "ffprobe_version": observed.get("ffprobe_version"),
        "encoding_profile": observed.get("profile"),
        "audio_stream_indexes": observed.get("audio_stream_indexes"),
        "first_video_pts_ms": observed.get("first_video_pts_ms"),
        "last_video_pts_ms": observed.get("last_video_pts_ms"),
        "observed_duration_ms": observed.get("observed_duration_ms"),
        "video_frame_count": observed.get("video_frame_count"),
    }


def _catalog_rows(records: Sequence[_ItemRecord]) -> list[CatalogRow]:
    rows: list[CatalogRow] = []
    for record in records:
        clip = record.clip
        rows.append(
            CatalogRow(
                source_id=str(record.snapshot.get("source_id", "")),
                clip_id=record.clip_id,
                revision=record.clip_revision,
                title=str(clip.get("title", "")),
                categories=tuple(str(category) for category in clip.get("categories", [])),
                start_ms=int(clip.get("start_ms", 0)),
                end_ms=int(clip.get("end_ms", 1)),
                review_status=str(clip.get("review_status", "")),
                path=record.final_path or "",
                export_status=record.state,
            )
        )
    return rows


def _write_batch_files(
    tools: MediaTools | None, batch: _BatchRecord
) -> tuple[Path | None, Path | None]:
    """Write ``manifest_<batch_id>.json`` and ``catalog_<batch_id>.csv`` (FR-042); a path is
    ``None`` when that file could not be written. Used by the normal end of a batch and by
    ``ExportService.recover``."""
    manifest = _manifest(
        batch_id=batch.id,
        project_id=batch.project_id,
        status=batch.status,
        requested_profile=batch.profile,
        created_at=batch.created_at,
        records=batch.items,
        tools=tools,
    )
    catalog = render_catalog_csv(_catalog_rows(batch.items))
    manifest_path = _write_output(
        batch.output_dir, f"manifest_{batch.id}.json", manifest, f"{batch.id}-manifest"
    )
    catalog_path = _write_output(
        batch.output_dir, f"catalog_{batch.id}.csv", catalog, f"{batch.id}-catalog"
    )
    return manifest_path, catalog_path


def _write_output(directory: Path, name: str, data: bytes, token: str) -> Path | None:
    """Write ``name`` through a temp file in the same directory; ``None`` when it cannot be
    written. The name carries the batch ID, so it never replaces another batch's file."""
    temp = directory / f"{TEMP_PREFIX}{token}{TEMP_SUFFIX}"
    target = directory / name
    try:
        with temp.open("xb") as handle:
            handle.write(data)
        os.replace(temp, target)
    except OSError:
        _remove_file(temp)
        return None
    return target
