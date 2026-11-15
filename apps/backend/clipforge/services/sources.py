"""Sources (FR-002..FR-005): register by path, read, relink.

A source is a reference to a local file; the file is only read. Registration probes the file
with FFprobe (decode check included), takes the quick fingerprint and queues a full SHA-256 job.
Relink needs the full hash to match (FR-004): a different file is imported as a new source.

Error messages are fixed Ukrainian text. They never contain a path or an exception text.
"""

from __future__ import annotations

import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Final

from clipforge.adapters.ffprobe import ProbeError, SourceProbe, probe_source
from clipforge.adapters.fingerprint import quick_fingerprint, sha256_file
from clipforge.adapters.media_tools import MediaTools, MediaToolsUnavailableError
from clipforge.services.errors import (
    ServiceError,
    bad_request,
    conflict,
    invalid,
    invalid_cursor,
    media_tools_unavailable,
    not_found,
)
from clipforge.services.jobs import HASH_PENDING, JobRunner, add_source_hash_job
from clipforge.services.source_files import AVAILABLE, current_availability
from clipforge.services.text import is_storable
from clipforge.storage import (
    Database,
    JobRepository,
    Page,
    ProjectRepository,
    Source,
    SourceRepository,
)

_FILE_ERRORS: Final = {
    "SOURCE_UNAVAILABLE": ("Файл джерела недоступний: перевірте шлях і права доступу", False),
    "SOURCE_UNREADABLE": ("Файл пошкоджений або має непідтримуваний формат", False),
    "SOURCE_NO_VIDEO": ("У файлі немає відеодоріжки", False),
    "PROBE_TIMEOUT": ("Перевірка файлу перевищила ліміт часу; спробуйте ще раз", True),
    "SOURCE_CHANGED": ("Файл змінювався під час перевірки; спробуйте ще раз", True),
}


@dataclass(frozen=True, slots=True)
class RegisteredSource:
    source: Source
    hash_job_id: str


@dataclass(frozen=True, slots=True)
class OpenedMedia:
    """A registered source file opened read-only. The caller must close ``file``."""

    file: BinaryIO
    size: int
    extension: str


class SourceService:
    def __init__(
        self, database: Database, media_tools: MediaTools | None, runner: JobRunner
    ) -> None:
        self._database = database
        self._media_tools = media_tools
        self._runner = runner

    def register(self, project_id: str, raw_path: str) -> RegisteredSource:
        if self._media_tools is None:
            raise media_tools_unavailable()
        path = _parse_path(raw_path)
        with self._database.session() as session:
            if ProjectRepository(session).get(project_id) is None:
                raise not_found("PROJECT_NOT_FOUND", "Проєкт не знайдено")

        before = _stat_regular(path)
        probe = self._probe(self._media_tools, path)
        try:
            fingerprint = quick_fingerprint(path)
        except OSError:
            raise _file_error("SOURCE_UNAVAILABLE") from None
        if not _same_file_state(before, _stat_regular(path)):
            raise _file_error("SOURCE_CHANGED")

        with self._database.write_session() as session:
            source = SourceRepository(session).add(
                project_id=project_id,
                path=str(path),
                display_name=path.name,
                size=before.st_size,
                mtime=before.st_mtime_ns,
                quick_fingerprint=fingerprint,
                hash_status=HASH_PENDING,
                duration_ms=probe.duration_ms,
                video_stream_index=probe.video_stream_index,
                audio_streams=[asdict(audio) for audio in probe.audio_streams],
                canonical_origin=_canonical_origin(probe),
                probe_json=_probe_summary(probe),
                availability=AVAILABLE,
            )
            job_id = add_source_hash_job(JobRepository(session), source.id)
        self._runner.submit_source_hash(job_id, source.id)
        return RegisteredSource(source=source, hash_job_id=job_id)

    def get(self, source_id: str) -> Source:
        """The source with ``availability`` refreshed from the file system (FR-004)."""
        with self._database.session() as session:
            source = _require_source(SourceRepository(session), source_id)
        availability = current_availability(source)
        if availability == source.availability:
            return source
        with self._database.write_session() as session:
            return SourceRepository(session).set_availability(source_id, availability)

    def list_page(self, project_id: str, *, limit: int, cursor: str | None) -> Page[Source]:
        """One page of the project's sources, each with ``availability`` refreshed like ``get``."""
        with self._database.session() as session:
            if ProjectRepository(session).get(project_id) is None:
                raise not_found("PROJECT_NOT_FOUND", "Проєкт не знайдено")
            try:
                page = SourceRepository(session).list_page_for_project(
                    project_id, limit=limit, after=cursor
                )
            except ValueError:
                raise invalid_cursor() from None
        changed = {
            source.id: availability
            for source in page.items
            if (availability := current_availability(source)) != source.availability
        }
        if not changed:
            return page
        with self._database.write_session() as session:
            repo = SourceRepository(session)
            items = [
                repo.set_availability(source.id, changed[source.id])
                if source.id in changed
                else source
                for source in page.items
            ]
        return Page(items=items, next_cursor=page.next_cursor)

    def open_media(self, source_id: str) -> OpenedMedia:
        """Open the registered file read-only for playback.

        The size and mtime are checked on the open handle, so the bytes served are those of the
        file that was registered, even if the path is replaced right after the check.
        """
        with self._database.session() as session:
            source = _require_source(SourceRepository(session), source_id)
        try:
            handle = Path(source.path).open("rb")  # noqa: SIM115 - closed by the caller
        except OSError:
            raise _file_error("SOURCE_UNAVAILABLE") from None
        try:
            state = os.fstat(handle.fileno())
            if not stat.S_ISREG(state.st_mode):
                raise _file_error("SOURCE_UNAVAILABLE")
            if state.st_size != source.size or state.st_mtime_ns != source.mtime:
                raise _file_error("SOURCE_CHANGED")
        except BaseException:
            handle.close()
            raise
        return OpenedMedia(file=handle, size=state.st_size, extension=Path(source.path).suffix)

    def relink(self, source_id: str, raw_path: str) -> Source:
        """Point the source at ``raw_path`` when that file has the same full SHA-256."""
        path = _parse_path(raw_path)
        with self._database.session() as session:
            source = _require_source(SourceRepository(session), source_id)
            expected_sha256, hash_status, expected_size = (
                source.sha256,
                source.hash_status,
                source.size,
            )
        if expected_sha256 is None:
            raise _hash_not_ready(hash_status)

        before = _stat_regular(path)
        if before.st_size != expected_size:
            raise _mismatch()
        try:
            digest = sha256_file(path)
            fingerprint = quick_fingerprint(path)
        except OSError:
            raise _file_error("SOURCE_UNAVAILABLE") from None
        if not _same_file_state(before, _stat_regular(path)):
            raise _file_error("SOURCE_CHANGED")
        if digest != expected_sha256:
            raise _mismatch()

        with self._database.write_session() as session:
            return SourceRepository(session).relink(
                source_id,
                path=str(path),
                display_name=path.name,
                size=before.st_size,
                mtime=before.st_mtime_ns,
                quick_fingerprint=fingerprint,
                availability=AVAILABLE,
            )

    @staticmethod
    def _probe(tools: MediaTools, path: Path) -> SourceProbe:
        try:
            return probe_source(tools.ffprobe, path)
        except MediaToolsUnavailableError:
            raise media_tools_unavailable() from None
        except ProbeError as error:
            raise _file_error(error.code) from None


def _require_source(sources: SourceRepository, source_id: str) -> Source:
    source = sources.get(source_id)
    if source is None:
        raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
    return source


def _parse_path(raw: str) -> Path:
    """An absolute, storable path in its canonical (resolved) form."""
    if not raw or not is_storable(raw):
        raise invalid("INVALID_SOURCE_PATH", "Шлях до файлу некоректний", field="path")
    path = Path(raw)
    if not path.is_absolute():
        raise invalid("INVALID_SOURCE_PATH", "Шлях до файлу має бути повним", field="path")
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        raise _file_error("SOURCE_UNAVAILABLE") from None


def _stat_regular(path: Path) -> os.stat_result:
    try:
        result = path.stat()
    except OSError:
        raise _file_error("SOURCE_UNAVAILABLE") from None
    if not stat.S_ISREG(result.st_mode):
        raise _file_error("SOURCE_UNAVAILABLE")
    return result


def _same_file_state(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_size == second.st_size and first.st_mtime_ns == second.st_mtime_ns


def _file_error(code: str) -> ServiceError:
    if code not in _FILE_ERRORS:
        code = "SOURCE_UNREADABLE"
    message, retryable = _FILE_ERRORS[code]
    return bad_request(code, message, retryable=retryable)


def _mismatch() -> ServiceError:
    return conflict(
        "SOURCE_MISMATCH",
        "Це інший файл, а не копія оригіналу. Додайте його як нове джерело",
    )


def _hash_not_ready(hash_status: str) -> ServiceError:
    if hash_status == HASH_PENDING:
        return conflict(
            "SOURCE_HASH_PENDING",
            "Контрольну суму оригіналу ще обчислюють. Повторіть пізніше",
            retryable=True,
        )
    return conflict(
        "SOURCE_HASH_UNAVAILABLE",
        "Контрольну суму оригіналу не обчислено, тому перевірити копію неможливо",
    )


def _canonical_origin(probe: SourceProbe) -> dict[str, Any]:
    return {
        "origin_ms": probe.origin_ms,
        "origin_pts": probe.origin_pts,
        "time_base": f"{probe.time_base.numerator}/{probe.time_base.denominator}",
    }


def _probe_summary(probe: SourceProbe) -> dict[str, Any]:
    """The probe fields stored as JSON on the source and served as ``SourceOut.probe``.

    ``frame_rate`` is the average rate of the main video stream as a fraction string
    (``"30000/1001"``), ``null`` when unknown. ``vfr`` is ``true`` for variable frame rate,
    ``false`` for constant, ``null`` when not determined. Both come from the timestamps of
    the video packets (see ``analyze_frame_timing``). Sources registered before these keys
    existed do not have them at all.
    """
    rate = probe.frame_rate
    return {
        "format_name": probe.format_name,
        "video_codec": probe.video_codec,
        "width": probe.width,
        "height": probe.height,
        "rotation": probe.rotation,
        "hdr": probe.hdr,
        "unsupported_reason": probe.unsupported_reason,
        "frame_rate": None if rate is None else f"{rate.numerator}/{rate.denominator}",
        "vfr": probe.vfr,
    }
