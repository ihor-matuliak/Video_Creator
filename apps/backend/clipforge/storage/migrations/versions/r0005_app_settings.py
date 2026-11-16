"""Application settings: ``app_settings(key, value_json)`` (FR-048, docs/04 section 4).

One row per non-secret setting that differs from its default. A setting that is reset to its
default has no row, so ``value_json`` is never SQL NULL; its text is the versioned JSON of
``clipforge.storage.types.VersionedJson``. Secrets (the API key) never go into this table.

Frozen on purpose: it does not import the models.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
