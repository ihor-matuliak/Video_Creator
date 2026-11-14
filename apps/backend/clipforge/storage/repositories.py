"""Repositories over one SQLAlchemy ``Session`` (ADR 017).

A repository never commits: the caller owns the transaction (``Database.session()`` or
``Database.write_session()``). Methods ``flush`` so constraint violations (``IntegrityError``)
surface at the call that caused them. Input validation beyond the DB constraints belongs to the
services layer.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from sqlalchemy import CursorResult, Result, Select, and_, or_, select, text, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from .errors import RecordNotFound, StaleRevision
from .models import Clip, ExportBatch, ExportItem, Job, Project, Source
from .util import new_id, utc_now

MAX_PAGE_SIZE: Final = 1000

# Clip fields a user may change. Revision, times of analysis output, evidence and scores are not
# user-editable; export_state has its own method because it must not bump the revision.
CLIP_EDITABLE_FIELDS: Final = frozenset(
    {"title", "start_ms", "end_ms", "categories", "review_status", "visual_review_required"}
)
EXPORT_ITEM_MUTABLE_FIELDS: Final = frozenset(
    {"state", "temp_path", "final_path", "observed_json", "error_code"}
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
        self, *, limit: int = 50, after: str | None = None, include_archived: bool = False
    ) -> Page[Project]:
        statement = select(Project)
        if not include_archived:
            statement = statement.where(Project.archived_at.is_(None))
        return _page(
            self._session, statement, Project.created_at, Project.id, limit=limit, after=after
        )

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
