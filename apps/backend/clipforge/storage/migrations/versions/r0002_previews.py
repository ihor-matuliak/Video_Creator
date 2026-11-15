"""Preview proxies: the ``previews`` table (ADR 021).

Frozen on purpose: it does not import the models. The CHECK texts must stay identical to the ones
in ``clipforge.storage.models``.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "previews",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("audio_stream_index", sa.Integer(), nullable=True),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source_size", sa.BigInteger(), nullable=False),
        sa.Column("source_mtime", sa.BigInteger(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "audio_stream_index IS NULL OR audio_stream_index >= 0",
            name="ck_previews_audio_stream_index",
        ),
        sa.CheckConstraint("profile_version >= 1", name="ck_previews_profile_version"),
        sa.CheckConstraint("source_size >= 0", name="ck_previews_source_size"),
        sa.CheckConstraint("file_size IS NULL OR file_size >= 0", name="ck_previews_file_size"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ready', 'failed', 'cancelled', 'interrupted')",
            name="ck_previews_status",
        ),
    )
    op.create_index("ix_previews_source_id", "previews", ["source_id"])
