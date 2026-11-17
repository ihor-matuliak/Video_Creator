"""Analysis runs, windows and attempts; a clip gets its evidence interval and analysis run.

``analysis_runs``, ``analysis_windows`` and ``analysis_attempts`` are new tables (ADR 041 §8).
``clips`` gets ``evidence_start_ms`` and ``evidence_end_ms`` (both NULL, or
``0 <= start < end``) and a real foreign key ``analysis_run_id -> analysis_runs.id``.
SQLite cannot add a foreign key to an existing table, so ``clips`` is rebuilt the way 0006
rebuilds ``transcript_chunks``: the rows are read, the table is dropped and created again, and
the rows are inserted under ``PRAGMA defer_foreign_keys``. The table ``export_items`` refers to
is not changed; a row that cannot be put back fails the commit, so nothing is orphaned silently.

Downgrade restores the 0006 schema. The three analysis tables go with their rows. The clips keep
their own columns but lose the evidence interval and the foreign key, and ``analysis_run_id``
becomes NULL: a link to a run that no longer exists would make the next upgrade fail its
foreign-key check.

Frozen on purpose: it does not import the models. The CHECK texts must stay identical to the ones
in ``clipforge.storage.models``.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

_SELECT_CLIPS = (
    "SELECT id, source_id, analysis_run_id, revision, title, start_ms, end_ms, "
    "recommended_start_ms, recommended_end_ms, categories, evidence_type, evidence_ids, reason, "
    "scores, rank, visual_review_required, review_status, export_state, created_at, updated_at "
    "FROM clips"
)
_INSERT_CLIPS_0007 = (
    "INSERT INTO clips (id, source_id, analysis_run_id, revision, title, start_ms, end_ms, "
    "recommended_start_ms, recommended_end_ms, evidence_start_ms, evidence_end_ms, categories, "
    "evidence_type, evidence_ids, reason, scores, rank, visual_review_required, review_status, "
    "export_state, created_at, updated_at) VALUES (:id, :source_id, :analysis_run_id, :revision, "
    ":title, :start_ms, :end_ms, :recommended_start_ms, :recommended_end_ms, NULL, NULL, "
    ":categories, :evidence_type, :evidence_ids, :reason, :scores, :rank, "
    ":visual_review_required, :review_status, :export_state, :created_at, :updated_at)"
)
_INSERT_CLIPS_0006 = (
    "INSERT INTO clips (id, source_id, analysis_run_id, revision, title, start_ms, end_ms, "
    "recommended_start_ms, recommended_end_ms, categories, evidence_type, evidence_ids, reason, "
    "scores, rank, visual_review_required, review_status, export_state, created_at, updated_at) "
    "VALUES (:id, :source_id, NULL, :revision, :title, :start_ms, :end_ms, :recommended_start_ms, "
    ":recommended_end_ms, :categories, :evidence_type, :evidence_ids, :reason, :scores, :rank, "
    ":visual_review_required, :review_status, :export_state, :created_at, :updated_at)"
)
_KEYS_0007 = (
    "id",
    "source_id",
    "analysis_run_id",
    "revision",
    "title",
    "start_ms",
    "end_ms",
    "recommended_start_ms",
    "recommended_end_ms",
    "categories",
    "evidence_type",
    "evidence_ids",
    "reason",
    "scores",
    "rank",
    "visual_review_required",
    "review_status",
    "export_state",
    "created_at",
    "updated_at",
)
_KEYS_0006 = (
    "id",
    "source_id",
    "revision",
    "title",
    "start_ms",
    "end_ms",
    "recommended_start_ms",
    "recommended_end_ms",
    "categories",
    "evidence_type",
    "evidence_ids",
    "reason",
    "scores",
    "rank",
    "visual_review_required",
    "review_status",
    "export_state",
    "created_at",
    "updated_at",
)


def _create_analysis_tables() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("transcript_id", sa.String(), nullable=False),
        sa.Column("transcript_revision", sa.Integer(), nullable=False),
        sa.Column("profile_json", sa.Text(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("unit_level", sa.String(), nullable=False),
        sa.Column("price_snapshot_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("diagnostics_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["transcript_id"], ["transcript_revisions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted')",
            name="ck_analysis_runs_status",
        ),
    )
    op.create_index("ix_analysis_runs_source_id", "analysis_runs", ["source_id"])

    op.create_table(
        "analysis_windows",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("first", sa.Integer(), nullable=False),
        sa.Column("core_first", sa.Integer(), nullable=False),
        sa.Column("core_last", sa.Integer(), nullable=False),
        sa.Column("last", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("repair_used", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["analysis_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "first <= core_first AND core_first <= core_last AND core_last <= last",
            name="ck_analysis_windows_span",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'unknown_outcome', "
            "'cancelled')",
            name="ck_analysis_windows_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_analysis_windows_attempts"),
        sa.CheckConstraint("repair_used IN (0, 1)", name="ck_analysis_windows_repair_used"),
        sa.UniqueConstraint("run_id", "idx", name="uq_analysis_windows_run_idx"),
    )
    op.create_index("ix_analysis_windows_run_id", "analysis_windows", ["run_id"])

    op.create_table(
        "analysis_attempts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("window_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("spend_entry_id", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("raw_output", sa.Text(), nullable=True),
        sa.Column("usage_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["window_id"], ["analysis_windows.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("kind IN ('first', 'repair')", name="ck_analysis_attempts_kind"),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'unknown_outcome', "
            "'cancelled')",
            name="ck_analysis_attempts_state",
        ),
        sa.CheckConstraint(
            "length(CAST(raw_output AS BLOB)) <= 1048576", name="ck_analysis_attempts_raw_output"
        ),
    )
    op.create_index("ix_analysis_attempts_window_id", "analysis_attempts", ["window_id"])


def _create_clips_0007() -> None:
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
        sa.Column("evidence_start_ms", sa.Integer(), nullable=True),
        sa.Column("evidence_end_ms", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"]),
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
            "(evidence_start_ms IS NULL AND evidence_end_ms IS NULL) "
            "OR (evidence_start_ms IS NOT NULL AND evidence_end_ms IS NOT NULL "
            "AND 0 <= evidence_start_ms AND evidence_start_ms < evidence_end_ms)",
            name="ck_clips_evidence_interval",
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


def _create_clips_0006() -> None:
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


def upgrade() -> None:
    connection = op.get_bind()
    op.execute("PRAGMA defer_foreign_keys = ON")
    rows = connection.execute(sa.text(_SELECT_CLIPS)).mappings().all()
    _create_analysis_tables()
    op.drop_table("clips")
    _create_clips_0007()
    if rows:
        connection.execute(
            sa.text(_INSERT_CLIPS_0007),
            [{key: row[key] for key in _KEYS_0007} for row in rows],
        )


def downgrade() -> None:
    connection = op.get_bind()
    op.execute("PRAGMA defer_foreign_keys = ON")
    rows = connection.execute(sa.text(_SELECT_CLIPS)).mappings().all()
    op.drop_table("clips")
    _create_clips_0006()
    if rows:
        connection.execute(
            sa.text(_INSERT_CLIPS_0006),
            [{key: row[key] for key in _KEYS_0006} for row in rows],
        )
    op.drop_table("analysis_attempts")
    op.drop_table("analysis_windows")
    op.drop_table("analysis_runs")
