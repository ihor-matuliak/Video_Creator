"""Spend ledger money as integer micro-USD (FR-049).

``spend_entries.reserved_usd`` / ``actual_usd`` (Float) become ``reserved_micro_usd`` /
``actual_micro_usd`` (whole micro-USD): the sums compared with the budget ceiling must be exact.
SQLite cannot drop a column that a CHECK constraint names, so the table is rebuilt: the old one is
renamed, the new one created, the rows copied, the old one dropped. Nothing references the table.

Upgrade: a Float amount is read as its shortest decimal text and rounded UP to a micro-USD (the
safe direction for money that is spent). A state outside the new vocabulary becomes
``unknown_outcome``, and an ``unknown_outcome`` entry counts its reservation, so that reservation
becomes ``max(reserved, actual)``: such a row still counts against the budget, and never for less
than the cost it reported, instead of vanishing or being under-counted. ``actual`` is kept as it
was. Downgrade: micro-USD / 1 000 000 as REAL; the new CHECK on ``state`` disappears with the table.

Frozen on purpose: it does not import the models. The CHECK texts must stay identical to the ones
in ``clipforge.storage.models``.
"""

from decimal import ROUND_CEILING, Context, Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

_STATES = ("reserved", "completed", "unknown_outcome", "released")
_MICRO = Decimal(1_000_000)
_ROUND_UP = Context(prec=100, rounding=ROUND_CEILING)


def _micro_usd(value: float | None) -> int | None:
    if value is None:
        return None
    scaled = _ROUND_UP.multiply(Decimal(repr(float(value))), _MICRO)
    return int(scaled.to_integral_value(rounding=ROUND_CEILING))


def _entry_values(
    state: str, reserved_usd: float | None, actual_usd: float | None
) -> tuple[str, int | None, int | None]:
    """``(state, reserved_micro_usd, actual_micro_usd)`` of a Float-era row."""
    reserved, actual = _micro_usd(reserved_usd), _micro_usd(actual_usd)
    if state in _STATES:
        return state, reserved, actual
    return "unknown_outcome", max(reserved or 0, actual or 0), actual


def upgrade() -> None:
    op.rename_table("spend_entries", "spend_entries_r0003")
    op.create_table(
        "spend_entries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reserved_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("actual_micro_usd", sa.BigInteger(), nullable=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("price_snapshot", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('reserved', 'completed', 'unknown_outcome', 'released')",
            name="ck_spend_entries_state",
        ),
        sa.CheckConstraint("reserved_micro_usd >= 0", name="ck_spend_entries_reserved"),
        sa.CheckConstraint(
            "actual_micro_usd IS NULL OR actual_micro_usd >= 0", name="ck_spend_entries_actual"
        ),
        sa.UniqueConstraint("run_id", "request_id", name="uq_spend_entries_run_request"),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, run_id, request_id, state, reserved_usd, actual_usd, model, price_snapshot "
            "FROM spend_entries_r0003"
        )
    ).all()
    converted: list[dict[str, object]] = []
    for row_id, run_id, request_id, state, reserved_usd, actual_usd, model, snapshot in rows:
        new_state, reserved, actual = _entry_values(state, reserved_usd, actual_usd)
        converted.append(
            {
                "id": row_id,
                "run_id": run_id,
                "request_id": request_id,
                "state": new_state,
                "reserved": reserved,
                "actual": actual,
                "model": model,
                "snapshot": snapshot,
            }
        )
    if converted:
        connection.execute(
            sa.text(
                "INSERT INTO spend_entries (id, run_id, request_id, state, reserved_micro_usd, "
                "actual_micro_usd, model, price_snapshot) VALUES "
                "(:id, :run_id, :request_id, :state, :reserved, :actual, :model, :snapshot)"
            ),
            converted,
        )
    op.drop_table("spend_entries_r0003")


def downgrade() -> None:
    op.rename_table("spend_entries", "spend_entries_r0004")
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
    op.execute(
        "INSERT INTO spend_entries (id, run_id, request_id, state, reserved_usd, actual_usd, "
        "model, price_snapshot) "
        "SELECT id, run_id, request_id, state, CAST(reserved_micro_usd AS REAL) / 1000000.0, "
        "CAST(actual_micro_usd AS REAL) / 1000000.0, model, price_snapshot "
        "FROM spend_entries_r0004"
    )
    op.drop_table("spend_entries_r0004")
