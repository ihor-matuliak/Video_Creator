"""Repositories over one SQLAlchemy ``Session`` (ADR 017).

A repository never commits: the caller owns the transaction (``Database.session()`` or
``Database.write_session()``). Methods ``flush`` so constraint violations (``IntegrityError``)
surface at the call that caused them. Input validation beyond the DB constraints belongs to the
services layer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from sqlalchemy import (
    CursorResult,
    Result,
    Select,
    Text,
    and_,
    case,
    delete,
    func,
    insert,
    or_,
    select,
    text,
    type_coerce,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import InstrumentedAttribute, Session

from .database import CASEFOLD_FUNCTION
from .errors import RecordNotFound, StaleRevision, StorageError
from .models import (
    SPEND_COUNTS_ACTUAL,
    SPEND_COUNTS_NOTHING,
    SPEND_COUNTS_RESERVED,
    AppSetting,
    Clip,
    ExportBatch,
    ExportItem,
    Job,
    Preview,
    Project,
    Source,
    SpendEntry,
    TranscriptChunk,
    TranscriptRevision,
    TranscriptRun,
    TranscriptSegment,
    TranscriptWord,
)
from .types import decode_versioned_json
from .util import new_id, utc_now

_LOGGER = logging.getLogger(__name__)

MAX_PAGE_SIZE: Final = 1000

# Clip fields a user may change. Revision, times of analysis output, evidence and scores are not
# user-editable; export_state has its own method because it must not bump the revision.
CLIP_EDITABLE_FIELDS: Final = frozenset(
    {"title", "start_ms", "end_ms", "categories", "review_status", "visual_review_required"}
)
EXPORT_ITEM_MUTABLE_FIELDS: Final = frozenset(
    {"state", "temp_path", "final_path", "observed_json", "error_code"}
)
RUN_MUTABLE_FIELDS: Final = frozenset({"status", "coverage", "usage"})
CHUNK_MUTABLE_FIELDS: Final = frozenset(
    {
        "state",
        "attempts",
        "request_id",
        "response_path",
        "error_code",
        "reserved_micro_usd",
        "actual_micro_usd",
    }
)

Clock = Callable[[], str]

_CHECKPOINT_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)


class _HasId(Protocol):
    @property
    def id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of a keyset-paginated list. ``next_cursor`` is the id to pass as ``after``."""

    items: list[T]
    next_cursor: str | None


def _page[T: _HasId](
    session: Session,
    statement: Select[T],
    created_at: InstrumentedAttribute[str],
    ident: InstrumentedAttribute[str],
    *,
    limit: int,
    after: str | None,
) -> Page[T]:
    """Order by (created_at, id); ``after`` is the id of the last item of the previous page."""
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit має бути від 1 до {MAX_PAGE_SIZE}")
    if after is not None:
        marker = session.scalar(select(created_at).where(ident == after))
        if marker is None:
            raise ValueError("Невідомий курсор")
        statement = statement.where(
            or_(created_at > marker, and_(created_at == marker, ident > after))
        )
    rows = list(session.scalars(statement.order_by(created_at, ident).limit(limit + 1)))
    if len(rows) > limit:
        rows = rows[:limit]
        return Page(items=rows, next_cursor=rows[-1].id)
    return Page(items=rows, next_cursor=None)


def _rowcount(result: Result[Any]) -> int:
    if not isinstance(result, CursorResult):
        raise TypeError("Очікувався результат DML-оператора")
    return int(result.rowcount)


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    """A project with the figures of the project list (docs/03 section 4)."""

    project: Project
    source_count: int
    clip_count: int
    # The latest of the project's own update and the creation or update of its sources, clips
    # and export batches; same ISO 8601 UTC format as the stored timestamps.
    last_activity_at: str


class ProjectRepository:
    def __init__(self, session: Session, *, now: Clock = utc_now) -> None:
        self._session = session
        self._now = now

    def add(self, name: str) -> Project:
        """``name`` is trimmed; the 1-120 length rule is a DB CHECK (FR-001)."""
        now = self._now()
        project = Project(
            id=new_id(), name=name.strip(), created_at=now, updated_at=now, archived_at=None
        )
        self._session.add(project)
        self._session.flush()
        return project

    def get(self, project_id: str) -> Project | None:
        return self._session.get(Project, project_id)

    def list_page(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        include_archived: bool = False,
        query: str | None = None,
    ) -> Page[Project]:
        """``query`` is trimmed; a project matches when its name contains it as a substring after
        ``str.casefold`` on both sides. An empty ``query`` does not filter. ``after`` may be any
        project, listed or not."""
        statement = select(Project)
        if not include_archived:
            statement = statement.where(Project.archived_at.is_(None))
        needle = (query or "").strip().casefold()
        if needle:
            folded = func.instr(getattr(func, CASEFOLD_FUNCTION)(Project.name), needle)
            statement = statement.where(folded > 0)
        return _page(
            self._session, statement, Project.created_at, Project.id, limit=limit, after=after
        )

    def summarize(self, projects: Sequence[Project]) -> list[ProjectSummary]:
        """Figures for ``projects`` in the same order: two aggregate queries, whatever the count.

        A clip belongs to a project through its source. ``last_activity_at`` compares strings,
        which is chronological because the timestamps have a fixed width (``util.utc_now``).
        """
        if not projects:
            return []
        ids = [project.id for project in projects]
        stats: dict[str, tuple[int, int, list[str]]] = {}
        in_sources = self._session.execute(
            select(
                Source.project_id,
                func.count(func.distinct(Source.id)),
                func.count(Clip.id),
                func.max(Source.created_at),
                func.max(Clip.updated_at),
            )
            .select_from(Source)
            .outerjoin(Clip, Clip.source_id == Source.id)
            .where(Source.project_id.in_(ids))
            .group_by(Source.project_id)
        )
        for project_id, sources, clips, last_source, last_clip in in_sources:
            stamps = [stamp for stamp in (last_source, last_clip) if stamp is not None]
            stats[project_id] = (sources, clips, stamps)
        in_batches = self._session.execute(
            select(ExportBatch.project_id, func.max(ExportBatch.created_at))
            .where(ExportBatch.project_id.in_(ids))
            .group_by(ExportBatch.project_id)
        )
        last_batches = {project_id: stamp for project_id, stamp in in_batches}
        summaries: list[ProjectSummary] = []
        for project in projects:
            sources, clips, stamps = stats.get(project.id, (0, 0, []))
            latest = [project.updated_at, *stamps]
            if project.id in last_batches:
                latest.append(last_batches[project.id])
            summaries.append(
                ProjectSummary(
                    project=project,
                    source_count=sources,
                    clip_count=clips,
                    last_activity_at=max(latest),
                )
            )
        return summaries

    def rename(self, project_id: str, name: str) -> Project:
        project = self._require(project_id)
        project.name = name.strip()
        project.updated_at = self._now()
        self._session.flush()
        return project

    def archive(self, project_id: str) -> Project:
        """Hides the project from the default list and keeps all its data (FR-001)."""
        project = self._require(project_id)
        now = self._now()
        project.archived_at = now
        project.updated_at = now
        self._session.flush()
        return project

    def unarchive(self, project_id: str) -> Project:
        """Returns an archived project to the default list (FR-001); its data is untouched."""
        project = self._require(project_id)
        project.archived_at = None
        project.updated_at = self._now()
        self._session.flush()
        return project

    def _require(self, project_id: str) -> Project:
        project = self._session.get(Project, project_id)
        if project is None:
            raise RecordNotFound("project", project_id)
        return project


class SourceRepository:
    def __init__(self, session: Session, *, now: Clock = utc_now) -> None:
        self._session = session
        self._now = now

    def add(
        self,
        *,
        project_id: str,
        path: str,
        display_name: str,
        size: int,
        mtime: int,
        quick_fingerprint: str,
        hash_status: str,
        duration_ms: int,
        video_stream_index: int,
        audio_streams: Sequence[Mapping[str, Any]],
        canonical_origin: Mapping[str, Any],
        probe_json: Mapping[str, Any],
        availability: str,
        sha256: str | None = None,
    ) -> Source:
        """``mtime`` is ``st_mtime_ns``. Vocabularies of hash_status/availability are the
        services layer's; the DB only checks sizes and ranges."""
        source = Source(
            id=new_id(),
            project_id=project_id,
            path=path,
            display_name=display_name,
            size=size,
            mtime=mtime,
            quick_fingerprint=quick_fingerprint,
            sha256=sha256,
            hash_status=hash_status,
            duration_ms=duration_ms,
            video_stream_index=video_stream_index,
            audio_streams=[dict(stream) for stream in audio_streams],
            canonical_origin=dict(canonical_origin),
            probe_json=dict(probe_json),
            availability=availability,
            created_at=self._now(),
        )
        self._session.add(source)
        self._session.flush()
        return source

    def get(self, source_id: str) -> Source | None:
        return self._session.get(Source, source_id)

    def list_for_project(self, project_id: str) -> list[Source]:
        statement = (
            select(Source)
            .where(Source.project_id == project_id)
            .order_by(Source.created_at, Source.id)
        )
        return list(self._session.scalars(statement))

    def list_page_for_project(
        self, project_id: str, *, limit: int = 50, after: str | None = None
    ) -> Page[Source]:
        """Sources of one project in (created_at, id) order; ``after`` as in ``list_page``."""
        statement = select(Source).where(Source.project_id == project_id)
        return _page(
            self._session, statement, Source.created_at, Source.id, limit=limit, after=after
        )

    def list_by_hash_status(self, hash_status: str) -> list[Source]:
        statement = (
            select(Source)
            .where(Source.hash_status == hash_status)
            .order_by(Source.created_at, Source.id)
        )
        return list(self._session.scalars(statement))

    def set_hash(self, source_id: str, *, sha256: str | None, hash_status: str) -> Source:
        source = self._require(source_id)
        source.sha256 = sha256
        source.hash_status = hash_status
        self._session.flush()
        return source

    def set_availability(self, source_id: str, availability: str) -> Source:
        source = self._require(source_id)
        source.availability = availability
        self._session.flush()
        return source

    def relink(
        self,
        source_id: str,
        *,
        path: str,
        display_name: str,
        size: int,
        mtime: int,
        quick_fingerprint: str,
        availability: str,
    ) -> Source:
        """Point the source at another copy of the same bytes (FR-004). Identity fields
        (``sha256``, ``duration_ms``, streams, origin) stay: the caller has proven that the new
        file is byte-identical."""
        source = self._require(source_id)
        source.path = path
        source.display_name = display_name
        source.size = size
        source.mtime = mtime
        source.quick_fingerprint = quick_fingerprint
        source.availability = availability
        self._session.flush()
        return source

    def _require(self, source_id: str) -> Source:
        source = self._session.get(Source, source_id)
        if source is None:
            raise RecordNotFound("source", source_id)
        return source


class ClipRepository:
    def __init__(self, session: Session, *, now: Clock = utc_now) -> None:
        self._session = session
        self._now = now

    def add(
        self,
        *,
        source_id: str,
        title: str,
        start_ms: int,
        end_ms: int,
        categories: Sequence[str],
        evidence_type: str,
        evidence_ids: Sequence[str] = (),
        reason: str | None = None,
        scores: Mapping[str, int] | None = None,
        rank: int | None = None,
        visual_review_required: bool = False,
        recommended_start_ms: int | None = None,
        recommended_end_ms: int | None = None,
        analysis_run_id: str | None = None,
        review_status: str = "suggested",
    ) -> Clip:
        """New clip at revision 1 with export_state "none"."""
        now = self._now()
        clip = Clip(
            id=new_id(),
            source_id=source_id,
            analysis_run_id=analysis_run_id,
            revision=1,
            title=title,
            start_ms=start_ms,
            end_ms=end_ms,
            recommended_start_ms=recommended_start_ms,
            recommended_end_ms=recommended_end_ms,
            categories=list(categories),
            evidence_type=evidence_type,
            evidence_ids=list(evidence_ids),
            reason=reason,
            scores=dict(scores) if scores is not None else None,
            rank=rank,
            visual_review_required=visual_review_required,
            review_status=review_status,
            export_state="none",
            created_at=now,
            updated_at=now,
        )
        self._session.add(clip)
        self._session.flush()
        return clip

    def get(self, clip_id: str) -> Clip | None:
        return self._session.get(Clip, clip_id)

    def list_page(
        self,
        *,
        source_id: str | None = None,
        review_status: str | None = None,
        category: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> Page[Clip]:
        statement = select(Clip)
        if source_id is not None:
            statement = statement.where(Clip.source_id == source_id)
        if review_status is not None:
            statement = statement.where(Clip.review_status == review_status)
        if category is not None:
            # `categories` is {"schema_version": 1, "value": [...]} (see types.VersionedJson).
            statement = statement.where(
                text(
                    "EXISTS (SELECT 1 FROM json_each(clips.categories, '$.value') AS c "
                    "WHERE c.value = :category)"
                ).bindparams(category=category)
            )
        return _page(self._session, statement, Clip.created_at, Clip.id, limit=limit, after=after)

    def update(self, clip_id: str, *, expected_revision: int, **changes: Any) -> Clip:
        """Optimistic update: write ``changes`` and bump the revision only if the stored
        revision still equals ``expected_revision`` (NFR-004).

        The compare-and-set is a single ``UPDATE ... WHERE revision = :expected``, so it is
        atomic even without a surrounding lock. On a mismatch nothing is written and
        ``StaleRevision`` carries the current revision. Run it as the first statement of the
        transaction or inside ``Database.write_session()``: after an earlier read in a deferred
        transaction SQLite may refuse the write with "database is locked" instead.

        Only ``CLIP_EDITABLE_FIELDS`` may be passed; a review change is an edit and bumps the
        revision too (docs/04 section 5).
        """
        unknown = sorted(set(changes) - CLIP_EDITABLE_FIELDS)
        if unknown:
            raise ValueError(f"Непідтримувані поля кліпу: {', '.join(unknown)}")
        if not changes:
            raise ValueError("Немає змін для запису")
        statement = (
            update(Clip)
            .where(Clip.id == clip_id, Clip.revision == expected_revision)
            .values(**changes, revision=Clip.revision + 1, updated_at=self._now())
            .execution_options(synchronize_session=False)
        )
        if _rowcount(self._session.execute(statement)) == 0:
            current = self._session.scalar(select(Clip.revision).where(Clip.id == clip_id))
            if current is None:
                raise RecordNotFound("clip", clip_id)
            raise StaleRevision(clip_id, expected_revision, current)
        return self._reload(clip_id)

    def set_export_state(self, clip_id: str, export_state: str) -> Clip:
        """Export state is independent of the user-visible revision (docs/04 section 5): an
        export finishing must not make the user's pending edit stale."""
        statement = (
            update(Clip)
            .where(Clip.id == clip_id)
            .values(export_state=export_state)
            .execution_options(synchronize_session=False)
        )
        if _rowcount(self._session.execute(statement)) == 0:
            raise RecordNotFound("clip", clip_id)
        return self._reload(clip_id)

    def _reload(self, clip_id: str) -> Clip:
        statement = select(Clip).where(Clip.id == clip_id).execution_options(populate_existing=True)
        return self._session.scalars(statement).one()


class ExportRepository:
    def __init__(self, session: Session, *, now: Clock = utc_now) -> None:
        self._session = session
        self._now = now

    def add_batch(
        self, *, project_id: str, output_dir: str, profile_json: Mapping[str, Any], status: str
    ) -> ExportBatch:
        batch = ExportBatch(
            id=new_id(),
            project_id=project_id,
            output_dir=output_dir,
            profile_json=dict(profile_json),
            status=status,
            created_at=self._now(),
        )
        self._session.add(batch)
        self._session.flush()
        return batch

    def get_batch(self, batch_id: str) -> ExportBatch | None:
        return self._session.get(ExportBatch, batch_id)

    def list_batches_with_status(self, statuses: Collection[str]) -> list[ExportBatch]:
        statement = (
            select(ExportBatch)
            .where(ExportBatch.status.in_(tuple(statuses)))
            .order_by(ExportBatch.created_at, ExportBatch.id)
        )
        return list(self._session.scalars(statement))

    def get_item(self, item_id: str) -> ExportItem | None:
        return self._session.get(ExportItem, item_id)

    def set_batch_status(self, batch_id: str, status: str) -> ExportBatch:
        batch = self._session.get(ExportBatch, batch_id)
        if batch is None:
            raise RecordNotFound("export_batch", batch_id)
        batch.status = status
        self._session.flush()
        return batch

    def add_item(
        self,
        *,
        batch_id: str,
        clip_id: str,
        clip_revision: int,
        snapshot_json: Mapping[str, Any],
        state: str,
    ) -> ExportItem:
        """The snapshot is written once and has no update method (docs/04 section 5)."""
        item = ExportItem(
            id=new_id(),
            batch_id=batch_id,
            clip_id=clip_id,
            clip_revision=clip_revision,
            snapshot_json=dict(snapshot_json),
            state=state,
            temp_path=None,
            final_path=None,
            observed_json=None,
            error_code=None,
        )
        self._session.add(item)
        self._session.flush()
        return item

    def list_items(self, batch_id: str) -> list[ExportItem]:
        statement = (
            select(ExportItem).where(ExportItem.batch_id == batch_id).order_by(ExportItem.id)
        )
        return list(self._session.scalars(statement))

    def update_item(self, item_id: str, **changes: Any) -> ExportItem:
        """Only ``EXPORT_ITEM_MUTABLE_FIELDS``; batch, clip, revision and snapshot are fixed."""
        unknown = sorted(set(changes) - EXPORT_ITEM_MUTABLE_FIELDS)
        if unknown:
            raise ValueError(f"Непідтримувані поля елемента експорту: {', '.join(unknown)}")
        item = self._session.get(ExportItem, item_id)
        if item is None:
            raise RecordNotFound("export_item", item_id)
        for name, value in changes.items():
            setattr(item, name, value)
        self._session.flush()
        return item


class PreviewRepository:
    """Preview proxies (ADR 021). The row says what a file in the preview cache is for; the
    status vocabulary is ``models.PREVIEW_STATUSES`` and the state machine is the service's."""

    def __init__(self, session: Session, *, now: Clock = utc_now) -> None:
        self._session = session
        self._now = now

    def add(
        self,
        *,
        source_id: str,
        audio_stream_index: int | None,
        profile_version: int,
        job_id: str,
        source_size: int,
        source_mtime: int,
    ) -> Preview:
        """New ``queued`` preview. ``source_size`` and ``source_mtime`` (``st_mtime_ns``) are the
        facts of the source the proxy is made from; they decide later whether it is still valid."""
        now = self._now()
        preview = Preview(
            id=new_id(),
            source_id=source_id,
            audio_stream_index=audio_stream_index,
            profile_version=profile_version,
            job_id=job_id,
            status="queued",
            source_size=source_size,
            source_mtime=source_mtime,
            file_size=None,
            error_code=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(preview)
        self._session.flush()
        return preview

    def get(self, preview_id: str) -> Preview | None:
        return self._session.get(Preview, preview_id)

    def find_ready(
        self, source_id: str, audio_stream_index: int | None, profile_version: int
    ) -> Preview | None:
        """The newest ``ready`` preview of the key ``(source, audio stream, profile version)``;
        a ``NULL`` audio stream (no sound) is a key of its own."""
        return self._newest(source_id, audio_stream_index, profile_version, ("ready",))

    def find_open(
        self, source_id: str, audio_stream_index: int | None, profile_version: int
    ) -> Preview | None:
        """The newest ``queued`` or ``running`` preview of the key."""
        return self._newest(source_id, audio_stream_index, profile_version, ("queued", "running"))

    def list_with_status(self, statuses: Collection[str]) -> list[Preview]:
        statement = (
            select(Preview)
            .where(Preview.status.in_(tuple(statuses)))
            .order_by(Preview.created_at, Preview.id)
        )
        return list(self._session.scalars(statement))

    def set_state(
        self,
        preview_id: str,
        status: str,
        *,
        file_size: int | None = None,
        error_code: str | None = None,
    ) -> Preview:
        """Set the status together with ``file_size`` and ``error_code`` (both replaced, so a
        state that has none clears them)."""
        preview = self._session.get(Preview, preview_id)
        if preview is None:
            raise RecordNotFound("preview", preview_id)
        preview.status = status
        preview.file_size = file_size
        preview.error_code = error_code
        preview.updated_at = self._now()
        self._session.flush()
        return preview

    def _newest(
        self,
        source_id: str,
        audio_stream_index: int | None,
        profile_version: int,
        statuses: Collection[str],
    ) -> Preview | None:
        audio = (
            Preview.audio_stream_index.is_(None)
            if audio_stream_index is None
            else Preview.audio_stream_index == audio_stream_index
        )
        statement = (
            select(Preview)
            .where(
                Preview.source_id == source_id,
                audio,
                Preview.profile_version == profile_version,
                Preview.status.in_(tuple(statuses)),
            )
            .order_by(Preview.created_at.desc(), Preview.id.desc())
            .limit(1)
        )
        return self._session.scalars(statement).first()


class JobRepository:
    def __init__(self, session: Session, *, now: Clock = utc_now) -> None:
        self._session = session
        self._now = now

    def add(
        self,
        *,
        type: str,
        state: str = "queued",
        progress_json: Mapping[str, Any] | None = None,
        checkpoint_json: Mapping[str, Any] | None = None,
    ) -> Job:
        now = self._now()
        job = Job(
            id=new_id(),
            type=type,
            state=state,
            progress_json=dict(progress_json) if progress_json is not None else None,
            lease_owner=None,
            lease_expires_at=None,
            checkpoint_json=dict(checkpoint_json) if checkpoint_json is not None else None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(job)
        self._session.flush()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._session.get(Job, job_id)

    def find_by_checkpoint(self, *, job_type: str, key: str, value: str) -> Job | None:
        """The job of ``job_type`` whose checkpoint has ``key`` equal to ``value`` (a link a job
        keeps to the entity it works on, for example ``batch_id``). ``key`` is a plain
        identifier: it becomes part of a JSON path."""
        if _CHECKPOINT_KEY.fullmatch(key) is None:
            raise ValueError("Некоректний ключ checkpoint")
        statement = (
            select(Job)
            .where(
                Job.type == job_type,
                # `checkpoint_json` is {"schema_version": 1, "value": {...}} (types.VersionedJson).
                text("json_extract(jobs.checkpoint_json, :path) = :value").bindparams(
                    path=f"$.value.{key}", value=value
                ),
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
        )
        return self._session.scalars(statement).first()

    def list_by_type(self, job_type: str, states: Collection[str]) -> list[Job]:
        """Jobs of ``job_type`` in any of ``states``, oldest first."""
        statement = (
            select(Job)
            .where(Job.type == job_type, Job.state.in_(tuple(states)))
            .order_by(Job.created_at, Job.id)
        )
        return list(self._session.scalars(statement))

    def set_state(self, job_id: str, state: str) -> Job:
        job = self._require(job_id)
        job.state = state
        job.updated_at = self._now()
        self._session.flush()
        return job

    def set_progress(self, job_id: str, progress_json: Mapping[str, Any]) -> Job:
        job = self._require(job_id)
        job.progress_json = dict(progress_json)
        job.updated_at = self._now()
        self._session.flush()
        return job

    def set_checkpoint(self, job_id: str, checkpoint_json: Mapping[str, Any]) -> Job:
        job = self._require(job_id)
        job.checkpoint_json = dict(checkpoint_json)
        job.updated_at = self._now()
        self._session.flush()
        return job

    def transition(
        self,
        job_id: str,
        to_state: str,
        *,
        from_states: Collection[str],
        progress_json: Mapping[str, Any] | None = None,
    ) -> bool:
        """Compare-and-set of the state: one ``UPDATE ... WHERE state IN (...)``, so two
        threads racing for the same job (a worker finishing, a user cancelling) cannot both
        win. Returns whether this call changed the row. ``progress_json`` replaces the stored
        progress in the same statement when given."""
        values: dict[str, Any] = {"state": to_state, "updated_at": self._now()}
        if progress_json is not None:
            values["progress_json"] = dict(progress_json)
        statement = (
            update(Job)
            .where(Job.id == job_id, Job.state.in_(tuple(from_states)))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(self._session.execute(statement)) == 1

    def interrupt_unfinished(self) -> int:
        """Start-up recovery for M1, where jobs live in the memory of one process: after a
        restart a ``queued``, ``running`` or ``cancelling`` job has no worker and never will
        (lease based resume is M6), so it is ``interrupted``. Returns how many were changed."""
        statement = (
            update(Job)
            .where(Job.state.in_(("queued", "running", "cancelling")))
            .values(state="interrupted", updated_at=self._now())
            .execution_options(synchronize_session=False)
        )
        return _rowcount(self._session.execute(statement))

    def _require(self, job_id: str) -> Job:
        job = self._session.get(Job, job_id)
        if job is None:
            raise RecordNotFound("job", job_id)
        return job


@dataclass(frozen=True, slots=True)
class NewWord:
    """A word to store: ``start_ms``/``end_ms`` are source milliseconds, ``[start, end)``."""

    id: str
    ordinal: int
    start_ms: int
    end_ms: int
    text: str
    chunk_id: str | None = None
    speaker_local: str | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class NewSegment:
    id: str
    ordinal: int
    start_ms: int
    end_ms: int
    text: str
    word_ids: Sequence[str] = ()


class TranscriptRepository:
    """Transcript revisions with their words and segments. Everything is write-once: there is no
    update or delete method, and the database refuses both (migration 0003)."""

    def __init__(self, session: Session, *, now: Clock = utc_now) -> None:
        self._session = session
        self._now = now

    def add_revision(
        self,
        *,
        source_id: str,
        origin: str,
        timing_level: str,
        language: str,
        duration_ms: int,
        coverage: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
        words: Sequence[NewWord] = (),
        segments: Sequence[NewSegment] = (),
        run_id: str | None = None,
        parent_id: str | None = None,
    ) -> TranscriptRevision:
        """Write a new revision with all its words and segments; the revision number is the
        source's highest plus one. Call it inside ``Database.write_session()`` so two writers
        cannot take the same number (the unique key would reject the second one anyway).

        Segments get the revision's ``timing_level``. Whether ``origin`` fits ``run_id`` and
        ``parent_id`` is a DB CHECK.
        """
        highest = self._session.scalar(
            select(func.max(TranscriptRevision.revision)).where(
                TranscriptRevision.source_id == source_id
            )
        )
        revision = TranscriptRevision(
            id=new_id(),
            source_id=source_id,
            revision=(highest or 0) + 1,
            origin=origin,
            run_id=run_id,
            parent_id=parent_id,
            timing_level=timing_level,
            language=language,
            duration_ms=duration_ms,
            word_count=len(words),
            segment_count=len(segments),
            coverage=[dict(span) for span in coverage],
            metadata_json=dict(metadata),
            created_at=self._now(),
        )
        self._session.add(revision)
        self._session.flush()
        if words:
            self._session.execute(
                insert(TranscriptWord),
                [
                    {
                        "revision_id": revision.id,
                        "id": word.id,
                        "ordinal": word.ordinal,
                        "start_ms": word.start_ms,
                        "end_ms": word.end_ms,
                        "text": word.text,
                        "chunk_id": word.chunk_id,
                        "speaker_local": word.speaker_local,
                        "confidence": word.confidence,
                    }
                    for word in words
                ],
            )
        if segments:
            self._session.execute(
                insert(TranscriptSegment),
                [
                    {
                        "revision_id": revision.id,
                        "id": segment.id,
                        "ordinal": segment.ordinal,
                        "start_ms": segment.start_ms,
                        "end_ms": segment.end_ms,
                        "text": segment.text,
                        "word_ids": list(segment.word_ids),
                        "timing_level": timing_level,
                    }
                    for segment in segments
                ],
            )
        return revision

    def get(self, revision_id: str) -> TranscriptRevision | None:
        return self._session.get(TranscriptRevision, revision_id)

    def list_page_for_source(
        self, source_id: str, *, limit: int = 50, after: str | None = None
    ) -> Page[TranscriptRevision]:
        """Revisions of one source in revision order; ``after`` is the id of the last revision of
        the previous page (it must belong to the same source)."""
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit має бути від 1 до {MAX_PAGE_SIZE}")
        statement = select(TranscriptRevision).where(TranscriptRevision.source_id == source_id)
        if after is not None:
            marker = self._session.scalar(
                select(TranscriptRevision.revision).where(
                    TranscriptRevision.id == after, TranscriptRevision.source_id == source_id
                )
            )
            if marker is None:
                raise ValueError("Невідомий курсор")
            statement = statement.where(TranscriptRevision.revision > marker)
        rows = list(
            self._session.scalars(statement.order_by(TranscriptRevision.revision).limit(limit + 1))
        )
        if len(rows) > limit:
            rows = rows[:limit]
            return Page(items=rows, next_cursor=rows[-1].id)
        return Page(items=rows, next_cursor=None)

    def list_words(
        self, revision_id: str, *, offset: int = 0, limit: int = MAX_PAGE_SIZE
    ) -> list[TranscriptWord]:
        """Words of the revision in ordinal order: the ``limit`` words after skipping ``offset``."""
        _check_window(offset, limit)
        statement = (
            select(TranscriptWord)
            .where(TranscriptWord.revision_id == revision_id)
            .order_by(TranscriptWord.ordinal)
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(statement))

    def list_segments(
        self, revision_id: str, *, offset: int = 0, limit: int = MAX_PAGE_SIZE
    ) -> list[TranscriptSegment]:
        """Segments of the revision in ordinal order, windowed like ``list_words``."""
        _check_window(offset, limit)
        statement = (
            select(TranscriptSegment)
            .where(TranscriptSegment.revision_id == revision_id)
            .order_by(TranscriptSegment.ordinal)
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(statement))


def _check_window(offset: int, limit: int) -> None:
    if offset < 0:
        raise ValueError("offset не може бути від'ємним")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit має бути від 1 до {MAX_PAGE_SIZE}")


class TranscriptRunRepository:
    """STT runs and their chunks. Which states a run or a chunk goes through is the business of
    ``clipforge.services.transcription_runs``: this class stores what it is given."""

    def __init__(self, session: Session, *, now: Clock = utc_now) -> None:
        self._session = session
        self._now = now

    def add_run(
        self,
        *,
        source_id: str,
        model: str,
        provider: str,
        options_hash: str,
        status: str,
        coverage: Sequence[Mapping[str, Any]] | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> TranscriptRun:
        run = TranscriptRun(
            id=new_id(),
            source_id=source_id,
            model=model,
            provider=provider,
            options_hash=options_hash,
            status=status,
            coverage=[dict(span) for span in coverage] if coverage is not None else None,
            usage=dict(usage) if usage is not None else None,
            created_at=self._now(),
        )
        self._session.add(run)
        self._session.flush()
        return run

    def get_run(self, run_id: str) -> TranscriptRun | None:
        return self._session.get(TranscriptRun, run_id)

    def list_runs_for_source(self, source_id: str) -> list[TranscriptRun]:
        """Every run of the source, the newest first: by ``created_at`` (fixed-width UTC text, so
        text order is time order), ties by id."""
        statement = (
            select(TranscriptRun)
            .where(TranscriptRun.source_id == source_id)
            .order_by(TranscriptRun.created_at.desc(), TranscriptRun.id.desc())
        )
        return list(self._session.scalars(statement))

    def update_run(self, run_id: str, **changes: Any) -> TranscriptRun:
        """Only ``RUN_MUTABLE_FIELDS``; the source, model, provider and options are fixed."""
        unknown = sorted(set(changes) - RUN_MUTABLE_FIELDS)
        if unknown:
            raise ValueError(f"Непідтримувані поля запуску розпізнавання: {', '.join(unknown)}")
        run = self._session.get(TranscriptRun, run_id)
        if run is None:
            raise RecordNotFound("transcript_run", run_id)
        for name, value in changes.items():
            setattr(run, name, value)
        self._session.flush()
        return run

    def add_chunk(
        self,
        *,
        run_id: str,
        core_start_ms: int,
        core_end_ms: int,
        upload_start_ms: int,
        upload_end_ms: int,
        state: str = "pending",
        reserved_micro_usd: int = 0,
    ) -> TranscriptChunk:
        """A chunk window of the run: the core ``[core_start, core_end)`` lies inside the upload
        window. ``state`` is one of ``models.CHUNK_STATES`` (a DB CHECK)."""
        chunk = TranscriptChunk(
            id=new_id(),
            run_id=run_id,
            core_start_ms=core_start_ms,
            core_end_ms=core_end_ms,
            upload_start_ms=upload_start_ms,
            upload_end_ms=upload_end_ms,
            state=state,
            attempts=0,
            request_id=None,
            response_path=None,
            error_code=None,
            reserved_micro_usd=reserved_micro_usd,
            actual_micro_usd=None,
        )
        self._session.add(chunk)
        self._session.flush()
        return chunk

    def get_chunk(self, chunk_id: str) -> TranscriptChunk | None:
        return self._session.get(TranscriptChunk, chunk_id)

    def update_chunk(self, chunk_id: str, **changes: Any) -> TranscriptChunk:
        """Only ``CHUNK_MUTABLE_FIELDS``; the run and the four window numbers are fixed."""
        unknown = sorted(set(changes) - CHUNK_MUTABLE_FIELDS)
        if unknown:
            raise ValueError(f"Непідтримувані поля частини розпізнавання: {', '.join(unknown)}")
        chunk = self._session.get(TranscriptChunk, chunk_id)
        if chunk is None:
            raise RecordNotFound("transcript_chunk", chunk_id)
        for name, value in changes.items():
            setattr(chunk, name, value)
        self._session.flush()
        return chunk

    def list_chunks(self, run_id: str) -> list[TranscriptChunk]:
        statement = (
            select(TranscriptChunk)
            .where(TranscriptChunk.run_id == run_id)
            .order_by(TranscriptChunk.core_start_ms, TranscriptChunk.id)
        )
        return list(self._session.scalars(statement))


class SpendRepository:
    """The rows of the paid-request ledger. Money is whole micro-USD; the Decimal conversion, the
    budget check and the allowed state changes are ``clipforge.services.spend``'s."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        run_id: str,
        request_id: str,
        state: str,
        reserved_micro_usd: int,
        model: str,
        price_snapshot: Mapping[str, Any],
        actual_micro_usd: int | None = None,
    ) -> SpendEntry:
        """One entry per ``(run_id, request_id)``: recording a request twice is a DB error."""
        entry = SpendEntry(
            id=new_id(),
            run_id=run_id,
            request_id=request_id,
            state=state,
            reserved_micro_usd=reserved_micro_usd,
            actual_micro_usd=actual_micro_usd,
            model=model,
            price_snapshot=dict(price_snapshot),
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    def get(self, entry_id: str) -> SpendEntry | None:
        """Always the stored row, also after ``transition`` changed it behind the session."""
        statement = (
            select(SpendEntry)
            .where(SpendEntry.id == entry_id)
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(statement).first()

    def find(self, run_id: str, request_id: str) -> SpendEntry | None:
        statement = select(SpendEntry).where(
            SpendEntry.run_id == run_id, SpendEntry.request_id == request_id
        )
        return self._session.scalars(statement).first()

    def list_for_run(self, run_id: str) -> list[SpendEntry]:
        statement = (
            select(SpendEntry).where(SpendEntry.run_id == run_id).order_by(SpendEntry.request_id)
        )
        return list(self._session.scalars(statement))

    def committed_micro_usd(self, run_id: str) -> int:
        """What the run has spent or may still spend: a ``completed`` entry counts its actual
        cost (its reservation when the cost is unknown), a ``reserved`` or ``unknown_outcome``
        entry its reservation, a ``released`` entry nothing (the groups are ``SPEND_COUNTS_*``).
        One aggregate query, so the figure is exact and consistent inside one transaction."""
        counted = case(
            (
                SpendEntry.state.in_(SPEND_COUNTS_ACTUAL),
                func.coalesce(SpendEntry.actual_micro_usd, SpendEntry.reserved_micro_usd),
            ),
            (SpendEntry.state.in_(SPEND_COUNTS_RESERVED), SpendEntry.reserved_micro_usd),
            (SpendEntry.state.in_(SPEND_COUNTS_NOTHING), 0),
            else_=0,
        )
        total = self._session.scalar(
            select(func.coalesce(func.sum(counted), 0)).where(SpendEntry.run_id == run_id)
        )
        return int(total or 0)

    def transition(
        self,
        entry_id: str,
        to_state: str,
        *,
        from_states: Collection[str],
        actual_micro_usd: int | None = None,
    ) -> bool:
        """Compare-and-set of the state: one ``UPDATE ... WHERE state IN (...)``. Returns whether
        this call changed the row; ``actual_micro_usd`` replaces the stored value. A bulk UPDATE
        does not touch entries already loaded in the session: read the row back with ``get``."""
        statement = (
            update(SpendEntry)
            .where(SpendEntry.id == entry_id, SpendEntry.state.in_(tuple(from_states)))
            .values(state=to_state, actual_micro_usd=actual_micro_usd)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(self._session.execute(statement)) == 1


class SettingsRepository:
    """The rows of ``app_settings``. Which keys exist and which values are allowed is
    ``clipforge.services.settings``'s: this class stores whatever JSON value it is given."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_many(self, keys: Collection[str]) -> dict[str, Any]:
        """The stored value of each of ``keys`` that has a readable row; the others are absent.

        A row whose text is not versioned JSON (the file was edited by hand or damaged) counts as
        absent: one bad row must not break the others. Only its key is logged, never its text.
        ``set`` and ``delete`` still work on such a row, so it can be replaced or removed.
        """
        # The column is read as plain text: the versioned-JSON decoding of the model would raise
        # for the whole result set on the first bad row.
        statement = select(AppSetting.key, type_coerce(AppSetting.value_json, Text)).where(
            AppSetting.key.in_(tuple(keys))
        )
        values: dict[str, Any] = {}
        for key, stored in self._session.execute(statement):
            try:
                values[key] = decode_versioned_json(stored)
            except StorageError:
                _LOGGER.warning("The stored value of setting %s is unreadable: ignored", key)
        return values

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` (never ``None``: use ``delete``) under ``key``, replacing a row, also
        one whose text cannot be read."""
        if value is None:
            raise ValueError("Значення налаштування не може бути null: використайте delete")
        # One upsert, no load of the old row: loading would decode (and fail on) a damaged text.
        insertion = sqlite_insert(AppSetting).values(key=key, value_json=value)
        self._session.execute(
            insertion.on_conflict_do_update(
                index_elements=[AppSetting.key], set_={"value_json": insertion.excluded.value_json}
            )
        )

    def delete(self, key: str) -> None:
        """Remove the row of ``key``, also one whose text cannot be read; nothing happens when
        there is none."""
        self._session.execute(delete(AppSetting).where(AppSetting.key == key))
