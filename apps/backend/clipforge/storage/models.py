"""SQLAlchemy models for docs/04_ARCHITECTURE_API.md section 3 (ADR 017).

The schema itself is created by the Alembic migrations, never by ``create_all``. The CHECK
constraints are repeated here so the models document the rules; a test keeps them identical to
the ones in the initial migration.

Times: ``*_ms`` columns are integer milliseconds, source-relative, intervals ``[start, end)``.
Timestamps (``created_at`` ...) are ISO 8601 UTC text from ``util.utc_now``.
"""

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import VersionedJson

REVIEW_STATUSES = ("suggested", "approved", "rejected")
EXPORT_STATES = ("none", "queued", "running", "verified", "failed", "cancelled", "stale")
JOB_STATES = (
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_projects_name_length"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    archived_at: Mapped[str | None] = mapped_column(String, nullable=True)


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("size >= 0", name="ck_sources_size"),
        CheckConstraint("duration_ms > 0", name="ck_sources_duration"),
        CheckConstraint("video_stream_index >= 0", name="ck_sources_video_stream_index"),
        CheckConstraint("sha256 IS NULL OR length(sha256) = 64", name="ck_sources_sha256_length"),
        Index("ix_sources_project_id", "project_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # st_mtime_ns of the file at registration.
    mtime: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quick_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    hash_status: Mapped[str] = mapped_column(String, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    video_stream_index: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_streams: Mapped[list[dict[str, Any]]] = mapped_column(VersionedJson, nullable=False)
    # Observed origin and time base (docs/05 section 1); the shape is owned by the media adapter.
    canonical_origin: Mapped[dict[str, Any]] = mapped_column(VersionedJson, nullable=False)
    probe_json: Mapped[dict[str, Any]] = mapped_column(VersionedJson, nullable=False)
    availability: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class Clip(Base):
    __tablename__ = "clips"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_clips_revision"),
        CheckConstraint("0 <= start_ms AND start_ms < end_ms", name="ck_clips_interval"),
        CheckConstraint(
            "(recommended_start_ms IS NULL AND recommended_end_ms IS NULL) "
            "OR (recommended_start_ms IS NOT NULL AND recommended_end_ms IS NOT NULL "
            "AND 0 <= recommended_start_ms AND recommended_start_ms < recommended_end_ms)",
            name="ck_clips_recommended_interval",
        ),
        CheckConstraint("visual_review_required IN (0, 1)", name="ck_clips_visual_review_required"),
        CheckConstraint(_in("review_status", REVIEW_STATUSES), name="ck_clips_review_status"),
        CheckConstraint(_in("export_state", EXPORT_STATES), name="ck_clips_export_state"),
        Index("ix_clips_source_id", "source_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    # No foreign key yet: the analysis_runs table arrives with the analysis milestone.
    analysis_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    recommended_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommended_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    categories: Mapped[list[str]] = mapped_column(VersionedJson, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(VersionedJson, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scores: Mapped[dict[str, int] | None] = mapped_column(VersionedJson, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    visual_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    review_status: Mapped[str] = mapped_column(String, nullable=False)
    # Independent of review_status; changing it does not bump `revision`.
    export_state: Mapped[str] = mapped_column(String, nullable=False, server_default="none")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ExportBatch(Base):
    __tablename__ = "export_batches"
    __table_args__ = (Index("ix_export_batches_project_id", "project_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    output_dir: Mapped[str] = mapped_column(Text, nullable=False)
    profile_json: Mapped[dict[str, Any]] = mapped_column(VersionedJson, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ExportItem(Base):
    __tablename__ = "export_items"
    __table_args__ = (
        CheckConstraint("clip_revision >= 1", name="ck_export_items_clip_revision"),
        UniqueConstraint("batch_id", "clip_id", name="uq_export_items_batch_clip"),
        Index("ix_export_items_batch_id", "batch_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("export_batches.id"), nullable=False)
    clip_id: Mapped[str] = mapped_column(ForeignKey("clips.id"), nullable=False)
    clip_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # Immutable snapshot of the clip and profile taken when the batch was created.
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(VersionedJson, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    temp_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_json: Mapped[dict[str, Any] | None] = mapped_column(VersionedJson, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("state", JOB_STATES), name="ck_jobs_state"),
        Index("ix_jobs_state", "state"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    progress_json: Mapped[dict[str, Any] | None] = mapped_column(VersionedJson, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    checkpoint_json: Mapped[dict[str, Any] | None] = mapped_column(VersionedJson, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
