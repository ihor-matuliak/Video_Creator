"""STT chunks: cost columns as whole micro-USD and the ``superseded`` state (ADR 032).

``transcript_chunks.reserved_cost`` / ``actual_cost`` (Float USD) become ``reserved_micro_usd`` /
``actual_micro_usd`` (whole micro-USD, summed over the attempts of the chunk), and the state
vocabulary gets ``superseded`` for a chunk that was split in two (docs/05 section 4).

SQLite cannot drop a column that a CHECK constraint names, so the table is rebuilt. It is
referenced by ``transcript_words.chunk_id`` and ``foreign_keys`` is ON inside this transaction (the
pragma cannot be switched in a transaction), so the usual rename-and-copy would leave the words
pointing at a table that is gone. Instead the rows are read, the table is dropped and created again
under the same name and the rows are inserted into it: with ``defer_foreign_keys`` the check waits
for the commit, and inserting a parent row again resolves the words that name it. A row that is
lost on the way would fail the commit, so the migration cannot silently orphan a word.

Upgrade: a Float amount is read as its shortest decimal text and rounded UP to a micro-USD (the
safe direction for money that is spent, as in 0004).
Downgrade: micro-USD / 1 000 000 as REAL. A ``superseded`` parent has no place in the old
vocabulary, so its row is dropped: its two children cover its core exactly, and a parent never
has words of its own (words come from chunks that succeeded; a split chunk never did).

Frozen on purpose: it does not import the models. The CHECK texts must stay identical to the ones
in ``clipforge.storage.models``.
"""

from decimal import ROUND_CEILING, Context, Decimal
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None

_MICRO = Decimal(1_000_000)
_ROUND_UP = Context(prec=100, rounding=ROUND_CEILING)
_COMMON_KEYS = (
    "id",
    "run_id",
    "core_start_ms",
    "core_end_ms",
    "upload_start_ms",
    "upload_end_ms",
    "state",
    "attempts",
    "request_id",
    "response_path",
    "error_code",
)
_STATES_OLD = "'pending', 'running', 'succeeded', 'failed', 'cancelled', 'unknown_outcome'"
_STATES_NEW = _STATES_OLD + ", 'superseded'"

_SELECT_OLD = (
    "SELECT id, run_id, core_start_ms, core_end_ms, upload_start_ms, upload_end_ms, state, "
    "attempts, request_id, response_path, error_code, reserved_cost AS reserved, "
    "actual_cost AS actual FROM transcript_chunks"
)
_INSERT_NEW = (
    "INSERT INTO transcript_chunks (id, run_id, core_start_ms, core_end_ms, upload_start_ms, "
    "upload_end_ms, state, attempts, request_id, response_path, error_code, "
    "reserved_micro_usd, actual_micro_usd) VALUES (:id, :run_id, :core_start_ms, :core_end_ms, "
    ":upload_start_ms, :upload_end_ms, :state, :attempts, :request_id, :response_path, "
    ":error_code, :reserved, :actual)"
)
_SELECT_NEW = (
    "SELECT id, run_id, core_start_ms, core_end_ms, upload_start_ms, upload_end_ms, state, "
    "attempts, request_id, response_path, error_code, reserved_micro_usd AS reserved, "
    "actual_micro_usd AS actual FROM transcript_chunks WHERE state <> 'superseded'"
)
_INSERT_OLD = (
    "INSERT INTO transcript_chunks (id, run_id, core_start_ms, core_end_ms, upload_start_ms, "
    "upload_end_ms, state, attempts, request_id, response_path, error_code, "
    "reserved_cost, actual_cost) VALUES (:id, :run_id, :core_start_ms, :core_end_ms, "
    ":upload_start_ms, :upload_end_ms, :state, :attempts, :request_id, :response_path, "
    ":error_code, :reserved, :actual)"
)


def _micro_usd(value: float | None) -> int | None:
    if value is None:
        return None
    scaled = _ROUND_UP.multiply(Decimal(repr(float(value))), _MICRO)
    return int(scaled.to_integral_value(rounding=ROUND_CEILING))


def _usd(value: int | None) -> float | None:
    return value / 1_000_000 if value is not None else None


def _create_chunks(
    reserved: sa.Column[Any], actual: sa.Column[Any], states: str, costs_check: str
) -> None:
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
        reserved,
        actual,
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
        sa.CheckConstraint(f"state IN ({states})", name="ck_transcript_chunks_state"),
        sa.CheckConstraint("attempts >= 0", name="ck_transcript_chunks_attempts"),
        sa.CheckConstraint(costs_check, name="ck_transcript_chunks_costs"),
    )
    op.create_index("ix_transcript_chunks_run_id", "transcript_chunks", ["run_id"])


def upgrade() -> None:
    connection = op.get_bind()
    op.execute("PRAGMA defer_foreign_keys = ON")
    rows = connection.execute(sa.text(_SELECT_OLD)).mappings().all()
    op.drop_table("transcript_chunks")
    _create_chunks(
        sa.Column("reserved_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("actual_micro_usd", sa.BigInteger(), nullable=True),
        _STATES_NEW,
        "reserved_micro_usd >= 0 AND (actual_micro_usd IS NULL OR actual_micro_usd >= 0)",
    )
    if rows:
        connection.execute(
            sa.text(_INSERT_NEW),
            [
                {
                    **{key: row[key] for key in _COMMON_KEYS},
                    "reserved": _micro_usd(row["reserved"]),
                    "actual": _micro_usd(row["actual"]),
                }
                for row in rows
            ],
        )


def downgrade() -> None:
    connection = op.get_bind()
    op.execute("PRAGMA defer_foreign_keys = ON")
    rows = connection.execute(sa.text(_SELECT_NEW)).mappings().all()
    op.drop_table("transcript_chunks")
    _create_chunks(
        sa.Column("reserved_cost", sa.Float(), nullable=False),
        sa.Column("actual_cost", sa.Float(), nullable=True),
        _STATES_OLD,
        "reserved_cost >= 0 AND (actual_cost IS NULL OR actual_cost >= 0)",
    )
    if rows:
        connection.execute(
            sa.text(_INSERT_OLD),
            [
                {
                    **{key: row[key] for key in _COMMON_KEYS},
                    "reserved": _usd(row["reserved"]),
                    "actual": _usd(row["actual"]),
                }
                for row in rows
            ],
        )
