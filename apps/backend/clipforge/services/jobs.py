"""Background jobs of the sidecar and ``GET /v1/jobs/{id}`` (ADR 017).

M1 jobs are rows in the ``jobs`` table plus one worker thread in this process; leases and resume
are M6, so a restart marks unfinished jobs ``interrupted`` (``JobRunner.recover``).

The only job type here is ``source_hash`` (FR-004): the full SHA-256 of a registered source.
Job record conventions (shared with later job types):

* ``progress_json`` is shown as ``progress``; it always holds a ``stage``;
* ``checkpoint_json["error"] = {"code", "message"}`` is shown as ``error`` once the job failed.
  The message is fixed Ukrainian text, never an exception text or a path.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from clipforge.adapters.fingerprint import HashCancelledError, sha256_file
from clipforge.services.errors import conflict, not_found
from clipforge.services.source_files import AVAILABLE, current_availability
from clipforge.storage import Database, Job, JobRepository, SourceRepository

SOURCE_HASH_JOB: Final = "source_hash"
HASH_PENDING: Final = "pending"
HASH_COMPUTED: Final = "computed"
HASH_FAILED_STATUS: Final = "failed"

_LOGGER = logging.getLogger(__name__)

_HASH_ERRORS: Final = {
    "SOURCE_UNAVAILABLE": "Файл джерела недоступний",
    "SOURCE_CHANGED": "Файл джерела змінився, тому хеш не можна довіряти",
    "HASH_FAILED": "Не вдалося обчислити контрольну суму файлу",
}
_GENERIC_JOB_ERROR: Final = {"code": "JOB_FAILED", "message": "Завдання завершилося помилкою"}


class _HashFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class JobView:
    id: str
    type: str
    state: str
    progress: dict[str, Any]
    error: dict[str, str] | None


JobCanceller = Callable[[str], JobView]


class JobService:
    """``cancellers`` maps a job type to the function that cancels jobs of that type; a type
    without one cannot be cancelled."""

    def __init__(
        self, database: Database, cancellers: Mapping[str, JobCanceller] | None = None
    ) -> None:
        self._database = database
        self._cancellers = dict(cancellers or {})

    def get(self, job_id: str) -> JobView:
        with self._database.session() as session:
            job = JobRepository(session).get(job_id)
            if job is None:
                raise not_found("JOB_NOT_FOUND", "Завдання не знайдено")
            return job_view(job)

    def cancel(self, job_id: str) -> JobView:
        """Ask the owner of the job's type to stop it (docs/04 section 5)."""
        with self._database.session() as session:
            job = JobRepository(session).get(job_id)
            if job is None:
                raise not_found("JOB_NOT_FOUND", "Завдання не знайдено")
            canceller = self._cancellers.get(job.type)
        if canceller is None:
            raise conflict("JOB_NOT_CANCELLABLE", "Це завдання не можна скасувати")
        return canceller(job_id)


def job_view(job: Job) -> JobView:
    return JobView(
        id=job.id,
        type=job.type,
        state=job.state,
        progress=dict(job.progress_json or {}),
        error=_error_of(job),
    )


def _error_of(job: Job) -> dict[str, str] | None:
    if job.state != "failed":
        return None
    raw = (job.checkpoint_json or {}).get("error")
    if isinstance(raw, dict):
        code, message = raw.get("code"), raw.get("message")
        if isinstance(code, str) and isinstance(message, str):
            return {"code": code, "message": message}
    return dict(_GENERIC_JOB_ERROR)


def add_source_hash_job(jobs: JobRepository, source_id: str) -> str:
    """Queued job row inside the caller's transaction; ``JobRunner.submit_source_hash`` runs it
    after that transaction committed."""
    job = jobs.add(
        type=SOURCE_HASH_JOB,
        progress_json={"stage": "queued"},
        checkpoint_json={"source_id": source_id},
    )
    return job.id


class JobRunner:
    """One worker thread for ``source_hash`` jobs."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source-hash")

    def submit_source_hash(self, job_id: str, source_id: str) -> None:
        try:
            self._executor.submit(self._run, job_id, source_id)
        except RuntimeError:
            # Shut down: the job stays queued and is recovered at the next start.
            _LOGGER.warning("Source hash job was not started: the runner is stopped")

    def recover(self) -> None:
        """Start-up recovery: jobs left unfinished by a dead process become ``interrupted``, and
        every source whose hash was never computed gets a fresh hash job. A source whose hash
        failed is retried too, but only when its file is available again (regular file with the
        stored size and mtime_ns); a missing or changed file stays failed."""
        with self._database.write_session() as session:
            JobRepository(session).interrupt_unfinished()
            jobs = JobRepository(session)
            sources = SourceRepository(session)
            for failed in sources.list_by_hash_status(HASH_FAILED_STATUS):
                if current_availability(failed) == AVAILABLE:
                    sources.set_hash(failed.id, sha256=None, hash_status=HASH_PENDING)
            queued = [
                (add_source_hash_job(jobs, source.id), source.id)
                for source in sources.list_by_hash_status(HASH_PENDING)
            ]
        for job_id, source_id in queued:
            self.submit_source_hash(job_id, source_id)

    def shutdown(self) -> None:
        """Stop hashing at the next chunk and wait for the worker. The interrupted job is
        recovered at the next start."""
        self._stop.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _run(self, job_id: str, source_id: str) -> None:
        try:
            self._hash_source(job_id, source_id)
        except Exception as exc:
            # Never log the exception text: it may carry a path.
            _LOGGER.error("Source hash job failed unexpectedly (%s)", type(exc).__name__)
            try:
                self._finish_failed(job_id, source_id, "HASH_FAILED")
            except Exception as inner:
                _LOGGER.error("Hash job could not be marked failed (%s)", type(inner).__name__)

    def _hash_source(self, job_id: str, source_id: str) -> None:
        with self._database.write_session() as session:
            started = JobRepository(session).transition(
                job_id, "running", from_states=("queued",), progress_json={"stage": "hashing"}
            )
            source = SourceRepository(session).get(source_id) if started else None
            snapshot = (Path(source.path), source.size, source.mtime) if source else None
        if not started:
            return
        if snapshot is None:
            self._finish_failed(job_id, source_id, "HASH_FAILED")
            return
        path, size, mtime = snapshot
        try:
            _require_unchanged(path, size, mtime)
            digest = sha256_file(path, should_cancel=self._stop.is_set)
            _require_unchanged(path, size, mtime)
        except HashCancelledError:
            self._finish_interrupted(job_id)
        except _HashFailure as failure:
            self._finish_failed(job_id, source_id, failure.code)
        except OSError:
            self._finish_failed(job_id, source_id, "SOURCE_UNAVAILABLE")
        else:
            self._finish_succeeded(job_id, source_id, digest)

    def _finish_succeeded(self, job_id: str, source_id: str, digest: str) -> None:
        with self._database.write_session() as session:
            SourceRepository(session).set_hash(source_id, sha256=digest, hash_status=HASH_COMPUTED)
            JobRepository(session).transition(
                job_id, "succeeded", from_states=("running",), progress_json={"stage": "done"}
            )

    def _finish_failed(self, job_id: str, source_id: str, code: str) -> None:
        with self._database.write_session() as session:
            SourceRepository(session).set_hash(
                source_id, sha256=None, hash_status=HASH_FAILED_STATUS
            )
            jobs = JobRepository(session)
            jobs.set_checkpoint(
                job_id,
                {
                    "source_id": source_id,
                    "error": {"code": code, "message": _HASH_ERRORS[code]},
                },
            )
            jobs.transition(
                job_id,
                "failed",
                from_states=("queued", "running"),
                progress_json={"stage": "failed"},
            )

    def _finish_interrupted(self, job_id: str) -> None:
        with self._database.write_session() as session:
            JobRepository(session).transition(job_id, "interrupted", from_states=("running",))


def _require_unchanged(path: Path, size: int, mtime_ns: int) -> None:
    """The file must still be the one that was probed: size and mtime as registered."""
    stat = path.stat()
    if stat.st_size != size or stat.st_mtime_ns != mtime_ns:
        raise _HashFailure("SOURCE_CHANGED")
