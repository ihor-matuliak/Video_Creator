"""Initial schema: projects, sources, clips, export batches/items, jobs.

Frozen on purpose: it does not import the models. Change the schema with a new revision.
The CHECK texts must stay identical to the ones in ``clipforge.storage.models``.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("archived_at", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_projects_name_length"),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime", sa.BigInteger(), nullable=False),
        sa.Column("quick_fingerprint", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column("hash_status", sa.String(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("video_stream_index", sa.Integer(), nullable=False),
        sa.Column("audio_streams", sa.Text(), nullable=False),
        sa.Column("canonical_origin", sa.Text(), nullable=False),
        sa.Column("probe_json", sa.Text(), nullable=False),
        sa.Column("availability", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("size >= 0", name="ck_sources_size"),
        sa.CheckConstraint("duration_ms > 0", name="ck_sources_duration"),
        sa.CheckConstraint("video_stream_index >= 0", name="ck_sources_video_stream_index"),
        sa.CheckConstraint(
            "sha256 IS NULL OR length(sha256) = 64", name="ck_sources_sha256_length"
        ),
    )
    op.create_index("ix_sources_project_id", "sources", ["project_id"])

    op.create_table(
        "clips",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("analysis_run_id", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("recommended_start_ms", sa.Integer(), nullable=True),
        sa.Column("recommended_end_ms", sa.Integer(), nullable=True),
        sa.Column("categories", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.String(), nullable=False),
        sa.Column("evidence_ids", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("scores", sa.Text(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("visual_review_required", sa.Boolean(), nullable=False),
        sa.Column("review_status", sa.String(), nullable=False),
        sa.Column("export_state", sa.String(), server_default="none", nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("revision >= 1", name="ck_clips_revision"),
        sa.CheckConstraint("0 <= start_ms AND start_ms < end_ms", name="ck_clips_interval"),
        sa.CheckConstraint(
            "(recommended_start_ms IS NULL AND recommended_end_ms IS NULL) "
            "OR (recommended_start_ms IS NOT NULL AND recommended_end_ms IS NOT NULL "
            "AND 0 <= recommended_start_ms AND recommended_start_ms < recommended_end_ms)",
            name="ck_clips_recommended_interval",
        ),
        sa.CheckConstraint(
            "visual_review_required IN (0, 1)", name="ck_clips_visual_review_required"
        ),
        sa.CheckConstraint(
            "review_status IN ('suggested', 'approved', 'rejected')",
            name="ck_clips_review_status",
        ),
        sa.CheckConstraint(
            "export_state IN ('none', 'queued', 'running', 'verified', 'failed', "
            "'cancelled', 'stale')",
            name="ck_clips_export_state",
        ),
    )
    op.create_index("ix_clips_source_id", "clips", ["source_id"])

    op.create_table(
        "export_batches",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("output_dir", sa.Text(), nullable=False),
        sa.Column("profile_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_export_batches_project_id", "export_batches", ["project_id"])

    op.create_table(
        "export_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("clip_id", sa.String(), nullable=False),
        sa.Column("clip_revision", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("temp_path", sa.Text(), nullable=True),
        sa.Column("final_path", sa.Text(), nullable=True),
        sa.Column("observed_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["batch_id"], ["export_batches.id"]),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("clip_revision >= 1", name="ck_export_items_clip_revision"),
        sa.UniqueConstraint("batch_id", "clip_id", name="uq_export_items_batch_clip"),
    )
    op.create_index("ix_export_items_batch_id", "export_items", ["batch_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("progress_json", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.String(), nullable=True),
        sa.Column("checkpoint_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'cancelling', 'succeeded', 'failed', "
            "'cancelled', 'interrupted')",
            name="ck_jobs_state",
        ),
    )
    op.create_index("ix_jobs_state", "jobs", ["state"])
