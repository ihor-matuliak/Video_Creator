"""SQLAlchemy models for docs/04_ARCHITECTURE_API.md section 3 (ADR 017).

The schema itself is created by the Alembic migrations, never by ``create_all``. The CHECK
constraints are repeated here so the models document the rules; a test keeps them identical to
the ones in the initial migration.

Times: ``*_ms`` columns are integer milliseconds, source-relative, intervals ``[start, end)``.
Timestamps (``created_at`` ...) are ISO 8601 UTC text from ``util.utc_now``.
"""

from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
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
PREVIEW_STATUSES = ("queued", "running", "ready", "failed", "cancelled", "interrupted")
TRANSCRIPT_ORIGINS = ("import", "stt", "correction")
TIMING_LEVELS = ("word", "segment")
# FR-018: the state of one STT chunk. ``superseded`` (docs/05 section 4, ADR 032) marks a parent
# that was split into two smaller chunks; it is never sent and never part of the coverage.
CHUNK_STATES = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "unknown_outcome",
    "superseded",
)
# FR-049: the state of one paid-request reservation in the spend ledger. This is the only place
# the state names are written; the CHECK, the repository and the ledger service all use them.
SPEND_RESERVED: Final = "reserved"
SPEND_COMPLETED: Final = "completed"
SPEND_UNKNOWN_OUTCOME: Final = "unknown_outcome"
SPEND_RELEASED: Final = "released"
SPEND_STATES: Final = (SPEND_RESERVED, SPEND_COMPLETED, SPEND_UNKNOWN_OUTCOME, SPEND_RELEASED)
# What an entry in each state adds to what its run has committed. Every state is in exactly one of
# the three groups (a test keeps it so): a new state must be placed on purpose, not fall through.
SPEND_COUNTS_ACTUAL: Final = (SPEND_COMPLETED,)  # the actual cost, else the reservation
SPEND_COUNTS_RESERVED: Final = (SPEND_RESERVED, SPEND_UNKNOWN_OUTCOME)  # the reservation
SPEND_COUNTS_NOTHING: Final = (SPEND_RELEASED,)
# ADR 041 §8: the status of an analysis run, the state of one window, the kind of one request
# (attempt). The ADR does not name the states of one attempt: they are the window states.
ANALYSIS_RUN_STATUSES: Final = (
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
)
ANALYSIS_WINDOW_STATES: Final = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "unknown_outcome",
    "cancelled",
)
ANALYSIS_ATTEMPT_STATES: Final = ANALYSIS_WINDOW_STATES
ANALYSIS_ATTEMPT_KINDS: Final = ("first", "repair")


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
        CheckConstraint(
            "(evidence_start_ms IS NULL AND evidence_end_ms IS NULL) "
            "OR (evidence_start_ms IS NOT NULL AND evidence_end_ms IS NOT NULL "
            "AND 0 <= evidence_start_ms AND evidence_start_ms < evidence_end_ms)",
            name="ck_clips_evidence_interval",
        ),
        CheckConstraint("visual_review_required IN (0, 1)", name="ck_clips_visual_review_required"),
        CheckConstraint(_in("review_status", REVIEW_STATUSES), name="ck_clips_review_status"),
        CheckConstraint(_in("export_state", EXPORT_STATES), name="ck_clips_export_state"),
        Index("ix_clips_source_id", "source_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    # The analysis run that suggested the clip; NULL for a manual clip (ADR 041 §8).
    analysis_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("analysis_runs.id"), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    recommended_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommended_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Transcript span of the suggestion, [start, end) (ADR 041 §6); NULL for a manual clip.
    evidence_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


class AnalysisRun(Base):
    """One AI selection run over a source (ADR 041 §8). It reads one transcript revision; its
    windows and their attempts are rows of their own. ``status`` is ``ANALYSIS_RUN_STATUSES``."""

    __tablename__ = "analysis_runs"
    __table_args__ = (
        CheckConstraint(_in("status", ANALYSIS_RUN_STATUSES), name="ck_analysis_runs_status"),
        Index("ix_analysis_runs_source_id", "source_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    transcript_id: Mapped[str] = mapped_column(
        ForeignKey("transcript_revisions.id"), nullable=False
    )
    # The number of the transcript revision that ``transcript_id`` names.
    transcript_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_json: Mapped[dict[str, Any]] = mapped_column(VersionedJson, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_level: Mapped[str] = mapped_column(String, nullable=False)
    price_snapshot_json: Mapped[dict[str, Any]] = mapped_column(VersionedJson, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    # Diagnostics of the run (ADR 041 §7); NULL until the run has them.
    diagnostics_json: Mapped[dict[str, Any] | None] = mapped_column(VersionedJson, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class AnalysisWindow(Base):
    """One analysis window of a run (ADR 041 §3): the units ``first..last`` by ordinal, of which
    the core ``core_first..core_last`` belongs to this window. The states are
    ``ANALYSIS_WINDOW_STATES``."""

    __tablename__ = "analysis_windows"
    __table_args__ = (
        CheckConstraint(
            "first <= core_first AND core_first <= core_last AND core_last <= last",
            name="ck_analysis_windows_span",
        ),
        CheckConstraint(_in("state", ANALYSIS_WINDOW_STATES), name="ck_analysis_windows_state"),
        CheckConstraint("attempts >= 0", name="ck_analysis_windows_attempts"),
        CheckConstraint("repair_used IN (0, 1)", name="ck_analysis_windows_repair_used"),
        UniqueConstraint("run_id", "idx", name="uq_analysis_windows_run_idx"),
        Index("ix_analysis_windows_run_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), nullable=False)
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    # Ordinals of the transcript units (words or segments) of the run's revision.
    first: Mapped[int] = mapped_column(Integer, nullable=False)
    core_first: Mapped[int] = mapped_column(Integer, nullable=False)
    core_last: Mapped[int] = mapped_column(Integer, nullable=False)
    last: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    repair_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    # The candidates of the window that passed validation (ADR 041 §8); NULL until it has them.
    result_json: Mapped[list[dict[str, Any]] | None] = mapped_column(VersionedJson, nullable=True)


class AnalysisAttempt(Base):
    """One request for a window: its first answer or its repair (ADR 041 §4). ``raw_output`` is
    the answer kept for local diagnostics and never holds the key. ``state`` is
    ``ANALYSIS_ATTEMPT_STATES``."""

    __tablename__ = "analysis_attempts"
    __table_args__ = (
        CheckConstraint(_in("kind", ANALYSIS_ATTEMPT_KINDS), name="ck_analysis_attempts_kind"),
        CheckConstraint(_in("state", ANALYSIS_ATTEMPT_STATES), name="ck_analysis_attempts_state"),
        # The cap counts UTF-8 bytes (ADR 041 §14): length() of TEXT counts characters, so the
        # value is cast to BLOB first.
        CheckConstraint(
            "length(CAST(raw_output AS BLOB)) <= 1048576", name="ck_analysis_attempts_raw_output"
        ),
        Index("ix_analysis_attempts_window_id", "window_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    window_id: Mapped[str] = mapped_column(ForeignKey("analysis_windows.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The spend-ledger entry that reserved the money of this request; no foreign key, as in
    # SpendEntry.
    spend_entry_id: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_json: Mapped[dict[str, Any] | None] = mapped_column(VersionedJson, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


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


class Preview(Base):
    """A preview proxy of a source (ADR 021); its file is ``<data_dir>/cache/previews/<id>.mp4``."""

    __tablename__ = "previews"
    __table_args__ = (
        CheckConstraint(
            "audio_stream_index IS NULL OR audio_stream_index >= 0",
            name="ck_previews_audio_stream_index",
        ),
        CheckConstraint("profile_version >= 1", name="ck_previews_profile_version"),
        CheckConstraint("source_size >= 0", name="ck_previews_source_size"),
        CheckConstraint("file_size IS NULL OR file_size >= 0", name="ck_previews_file_size"),
        CheckConstraint(_in("status", PREVIEW_STATUSES), name="ck_previews_status"),
        Index("ix_previews_source_id", "source_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    # Global source stream index of the kept audio; NULL is a proxy without sound.
    audio_stream_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Version of the render recipe: a preview of another version is never reused.
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    # Size and st_mtime_ns of the source this proxy was made from.
    source_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_mtime: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Size of the verified file; set when the preview is ready.
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


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


class TranscriptRun(Base):
    """One STT run over a source (docs/04 section 3). Its result is a ``TranscriptRevision`` whose
    ``run_id`` points back here; the revision number lives on the revision, not on the run. The
    ``status`` vocabulary belongs to the transcription service (M3), the DB does not fix it."""

    __tablename__ = "transcript_runs"
    __table_args__ = (Index("ix_transcript_runs_source_id", "source_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    options_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    # Spans of {"start_ms", "end_ms", "state"}; NULL until the run has something to report.
    coverage: Mapped[list[dict[str, Any]] | None] = mapped_column(VersionedJson, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(VersionedJson, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class TranscriptChunk(Base):
    """One STT request window: the core ``[core_start, core_end)`` plus upload context around it."""

    __tablename__ = "transcript_chunks"
    __table_args__ = (
        CheckConstraint(
            "0 <= core_start_ms AND core_start_ms < core_end_ms", name="ck_transcript_chunks_core"
        ),
        CheckConstraint(
            "0 <= upload_start_ms AND upload_start_ms <= core_start_ms "
            "AND core_end_ms <= upload_end_ms",
            name="ck_transcript_chunks_upload",
        ),
        CheckConstraint(_in("state", CHUNK_STATES), name="ck_transcript_chunks_state"),
        CheckConstraint("attempts >= 0", name="ck_transcript_chunks_attempts"),
        CheckConstraint(
            "reserved_micro_usd >= 0 AND (actual_micro_usd IS NULL OR actual_micro_usd >= 0)",
            name="ck_transcript_chunks_costs",
        ),
        Index("ix_transcript_chunks_run_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("transcript_runs.id"), nullable=False)
    core_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    core_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    upload_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    upload_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # App-owned file with the raw provider response (docs/04 section 3); never a source file.
    response_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    # Whole micro-USD summed over every attempt of the chunk (ADR 032); the spend ledger is the
    # source of truth. ``actual`` stays NULL until an attempt reports a cost.
    reserved_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class TranscriptRevision(Base):
    """An immutable version of the transcript of a source.

    Every import, STT run and text correction writes a new row (``revision`` counts per source);
    nothing updates or deletes it afterwards, and the words and segments belong to exactly one
    revision. The migration enforces this with triggers. ``id`` is what the API calls the
    transcript id: an analysis run that refers to it refers to this exact text, so evidence ids
    (``TranscriptWord.id``, ``TranscriptSegment.id``) are scoped to one revision.
    """

    __tablename__ = "transcript_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_transcript_revisions_revision"),
        CheckConstraint(_in("origin", TRANSCRIPT_ORIGINS), name="ck_transcript_revisions_origin"),
        CheckConstraint(
            _in("timing_level", TIMING_LEVELS), name="ck_transcript_revisions_timing_level"
        ),
        CheckConstraint(
            "length(language) BETWEEN 2 AND 32", name="ck_transcript_revisions_language"
        ),
        CheckConstraint("duration_ms > 0", name="ck_transcript_revisions_duration"),
        CheckConstraint(
            "word_count >= 0 AND segment_count >= 0", name="ck_transcript_revisions_counts"
        ),
        CheckConstraint(
            "timing_level = 'word' OR word_count = 0", name="ck_transcript_revisions_word_level"
        ),
        # An STT revision comes from a run, a correction from its parent revision; an import
        # from neither.
        CheckConstraint(
            "(origin = 'stt') = (run_id IS NOT NULL) "
            "AND (origin = 'correction') = (parent_id IS NOT NULL)",
            name="ck_transcript_revisions_provenance",
        ),
        UniqueConstraint("source_id", "revision", name="uq_transcript_revisions_source_revision"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("transcript_runs.id"), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("transcript_revisions.id"), nullable=True
    )
    timing_level: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False)
    # Duration of the source the times are checked against: 0 <= start < end <= duration_ms.
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Spans of {"start_ms", "end_ms", "state"}: which part of the source has text.
    coverage: Mapped[list[dict[str, Any]]] = mapped_column(VersionedJson, nullable=False)
    # How the revision came to be, e.g. {"format": "srt", "offset_ms": 0} (docs/05 section 7).
    metadata_json: Mapped[dict[str, Any]] = mapped_column(VersionedJson, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class TranscriptWord(Base):
    """A word with source-relative time. ``id`` is unique within its revision only."""

    __tablename__ = "transcript_words"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_transcript_words_ordinal"),
        CheckConstraint("0 <= start_ms AND start_ms < end_ms", name="ck_transcript_words_interval"),
        CheckConstraint("length(text) BETWEEN 1 AND 2000", name="ck_transcript_words_text"),
        UniqueConstraint("revision_id", "ordinal", name="uq_transcript_words_revision_ordinal"),
    )

    revision_id: Mapped[str] = mapped_column(
        ForeignKey("transcript_revisions.id"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # The STT chunk the word came from; NULL for imported words.
    chunk_id: Mapped[str | None] = mapped_column(ForeignKey("transcript_chunks.id"), nullable=True)
    # Speaker label local to one chunk: never the same speaker across chunks (FR-017).
    speaker_local: Mapped[str | None] = mapped_column(String, nullable=True)
    # As the provider reported it; not a calibrated probability.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class TranscriptSegment(Base):
    """A segment (cue, sentence) with source-relative time. ``id`` is unique within its revision."""

    __tablename__ = "transcript_segments"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_transcript_segments_ordinal"),
        CheckConstraint(
            "0 <= start_ms AND start_ms < end_ms", name="ck_transcript_segments_interval"
        ),
        CheckConstraint("length(text) BETWEEN 1 AND 2000", name="ck_transcript_segments_text"),
        CheckConstraint(
            _in("timing_level", TIMING_LEVELS), name="ck_transcript_segments_timing_level"
        ),
        UniqueConstraint("revision_id", "ordinal", name="uq_transcript_segments_revision_ordinal"),
    )

    revision_id: Mapped[str] = mapped_column(
        ForeignKey("transcript_revisions.id"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Ids of the words of the same revision that belong to the segment; empty without words.
    word_ids: Mapped[list[str]] = mapped_column(VersionedJson, nullable=False)
    # Timing level of the revision the segment belongs to.
    timing_level: Mapped[str] = mapped_column(String, nullable=False)


class AppSetting(Base):
    """A non-secret application setting that differs from its default (FR-048).

    ``value_json`` is never SQL NULL: a setting that goes back to its default loses its row. The
    allowed keys and values belong to ``clipforge.services.settings``. Secrets are never stored.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value_json: Mapped[Any] = mapped_column(VersionedJson, nullable=False)


class SpendEntry(Base):
    """A reservation and settlement of paid-request money (FR-049).

    ``run_id`` has no foreign key: it names a transcription run now and an analysis run once that
    table exists, and both spend against one ledger. ``state`` is one of ``SPEND_STATES``; the
    transitions between them belong to ``clipforge.services.spend``.

    Money is a whole number of micro-USD (1 USD = 1 000 000), never a float: the sums that are
    compared with the budget ceiling must be exact. ``actual_micro_usd`` is NULL until the provider
    reports a cost, and stays NULL when it never does.
    """

    __tablename__ = "spend_entries"
    __table_args__ = (
        CheckConstraint(_in("state", SPEND_STATES), name="ck_spend_entries_state"),
        CheckConstraint("reserved_micro_usd >= 0", name="ck_spend_entries_reserved"),
        CheckConstraint(
            "actual_micro_usd IS NULL OR actual_micro_usd >= 0", name="ck_spend_entries_actual"
        ),
        UniqueConstraint("run_id", "request_id", name="uq_spend_entries_run_request"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    reserved_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    model: Mapped[str] = mapped_column(String, nullable=False)
    # Prices the reservation was computed from; a later price change must not rewrite history.
    price_snapshot: Mapped[dict[str, Any]] = mapped_column(VersionedJson, nullable=False)
