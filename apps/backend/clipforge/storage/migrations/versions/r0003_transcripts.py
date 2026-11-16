"""Transcripts: runs, chunks, immutable revisions with their words and segments, spend ledger.

Frozen on purpose: it does not import the models. The CHECK texts must stay identical to the ones
in ``clipforge.storage.models``.

A revision, its words and its segments are written once: SQLite triggers abort every UPDATE and
DELETE of those three tables (the repositories never issue one either).
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

_IMMUTABLE_TABLES = ("transcript_revisions", "transcript_words", "transcript_segments")


def upgrade() -> None:
    op.create_table(
        "transcript_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("options_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("coverage", sa.Text(), nullable=True),
        sa.Column("usage", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transcript_runs_source_id", "transcript_runs", ["source_id"])

    op.create_table(
        "transcript_chunks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("core_start_ms", sa.Integer(), nullable=False),
        sa.Column("core_end_ms", sa.Integer(), nullable=False),
        sa.Column("upload_start_ms", sa.Integer(), nullable=False),
        sa.Column("upload_end_ms", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("response_path", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("reserved_cost", sa.Float(), nullable=False),
        sa.Column("actual_cost", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["transcript_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "0 <= core_start_ms AND core_start_ms < core_end_ms", name="ck_transcript_chunks_core"
        ),
        sa.CheckConstraint(
            "0 <= upload_start_ms AND upload_start_ms <= core_start_ms "
            "AND core_end_ms <= upload_end_ms",
            name="ck_transcript_chunks_upload",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', "
            "'unknown_outcome')",
            name="ck_transcript_chunks_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_transcript_chunks_attempts"),
        sa.CheckConstraint(
            "reserved_cost >= 0 AND (actual_cost IS NULL OR actual_cost >= 0)",
            name="ck_transcript_chunks_costs",
        ),
    )
    op.create_index("ix_transcript_chunks_run_id", "transcript_chunks", ["run_id"])

    op.create_table(
        "transcript_revisions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("timing_level", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("segment_count", sa.Integer(), nullable=False),
        sa.Column("coverage", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["transcript_runs.id"]),
        sa.ForeignKeyConstraint(["parent_id"], ["transcript_revisions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("revision >= 1", name="ck_transcript_revisions_revision"),
        sa.CheckConstraint(
            "origin IN ('import', 'stt', 'correction')", name="ck_transcript_revisions_origin"
        ),
        sa.CheckConstraint(
            "timing_level IN ('word', 'segment')", name="ck_transcript_revisions_timing_level"
        ),
        sa.CheckConstraint(
            "length(language) BETWEEN 2 AND 32", name="ck_transcript_revisions_language"
        ),
        sa.CheckConstraint("duration_ms > 0", name="ck_transcript_revisions_duration"),
        sa.CheckConstraint(
            "word_count >= 0 AND segment_count >= 0", name="ck_transcript_revisions_counts"
        ),
        sa.CheckConstraint(
            "timing_level = 'word' OR word_count = 0", name="ck_transcript_revisions_word_level"
        ),
        sa.CheckConstraint(
            "(origin = 'stt') = (run_id IS NOT NULL) "
            "AND (origin = 'correction') = (parent_id IS NOT NULL)",
            name="ck_transcript_revisions_provenance",
        ),
        sa.UniqueConstraint(
            "source_id", "revision", name="uq_transcript_revisions_source_revision"
        ),
    )

    op.create_table(
        "transcript_words",
        sa.Column("revision_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("chunk_id", sa.String(), nullable=True),
        sa.Column("speaker_local", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["revision_id"], ["transcript_revisions.id"]),
        sa.ForeignKeyConstraint(["chunk_id"], ["transcript_chunks.id"]),
        sa.PrimaryKeyConstraint("revision_id", "id"),
        sa.CheckConstraint("ordinal >= 0", name="ck_transcript_words_ordinal"),
        sa.CheckConstraint(
            "0 <= start_ms AND start_ms < end_ms", name="ck_transcript_words_interval"
        ),
        sa.CheckConstraint("length(text) BETWEEN 1 AND 2000", name="ck_transcript_words_text"),
        sa.UniqueConstraint("revision_id", "ordinal", name="uq_transcript_words_revision_ordinal"),
    )

    op.create_table(
        "transcript_segments",
        sa.Column("revision_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("word_ids", sa.Text(), nullable=False),
        sa.Column("timing_level", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["revision_id"], ["transcript_revisions.id"]),
        sa.PrimaryKeyConstraint("revision_id", "id"),
        sa.CheckConstraint("ordinal >= 0", name="ck_transcript_segments_ordinal"),
        sa.CheckConstraint(
            "0 <= start_ms AND start_ms < end_ms", name="ck_transcript_segments_interval"
        ),
        sa.CheckConstraint("length(text) BETWEEN 1 AND 2000", name="ck_transcript_segments_text"),
        sa.CheckConstraint(
            "timing_level IN ('word', 'segment')", name="ck_transcript_segments_timing_level"
        ),
        sa.UniqueConstraint(
            "revision_id", "ordinal", name="uq_transcript_segments_revision_ordinal"
        ),
    )

    op.create_table(
        "spend_entries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reserved_usd", sa.Float(), nullable=False),
        sa.Column("actual_usd", sa.Float(), nullable=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("price_snapshot", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("reserved_usd >= 0", name="ck_spend_entries_reserved"),
        sa.CheckConstraint("actual_usd IS NULL OR actual_usd >= 0", name="ck_spend_entries_actual"),
        sa.UniqueConstraint("run_id", "request_id", name="uq_spend_entries_run_request"),
    )

    for table in _IMMUTABLE_TABLES:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER tr_{table}_no_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'TRANSCRIPT_IMMUTABLE'); END"
            )
