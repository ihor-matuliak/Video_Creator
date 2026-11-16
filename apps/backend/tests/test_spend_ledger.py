"""Spend ledger (FR-049, T-048): micro-USD money, reservations under a ceiling, state changes,
and migration 0004 (Float USD -> whole micro-USD)."""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import ROUND_DOWN, Context, Decimal, localcontext
from functools import partial
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from clipforge.services.errors import ErrorKind, ServiceError
from clipforge.services.spend import (
    BudgetExceededError,
    SpendLedger,
    ceiling_to_micro_usd,
    to_micro_usd,
)
from clipforge.storage import Base, Database, SpendEntry, SpendRepository, open_database
from clipforge.storage.database import BACKUP_DIRNAME, DB_FILENAME
from clipforge.storage.migrate import MIGRATIONS_DIR
from clipforge.storage.models import (
    SPEND_COUNTS_ACTUAL,
    SPEND_COUNTS_NOTHING,
    SPEND_COUNTS_RESERVED,
    SPEND_STATES,
)

from .support import head_revision

RUN = "run-1"
MODEL = "microsoft/mai-transcribe-2"
SNAPSHOT = {"usd_per_minute": Decimal("0.0030"), "captured_at": "2026-11-15"}
THREADS = 20


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "data")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def ledger(db: Database) -> SpendLedger:
    return SpendLedger(db)


def reserve(
    ledger: SpendLedger,
    request_id: str,
    upper: str,
    *,
    ceiling: str = "1.00",
    run_id: str = RUN,
) -> SpendEntry:
    return ledger.reserve(run_id, request_id, MODEL, Decimal(upper), Decimal(ceiling), SNAPSHOT)


def stored(db: Database, run_id: str = RUN) -> list[SpendEntry]:
    with db.session() as session:
        return SpendRepository(session).list_for_run(run_id)


def service_error(call: Callable[[], object]) -> ServiceError:
    with pytest.raises(ServiceError) as caught:
        call()
    return caught.value


# --- Decimal -> micro-USD ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "micro"),
    [
        ("0", 0),
        ("0.000001", 1),
        ("0.0000001", 1),  # a tenth of a micro-USD still costs one
        ("0.0000010000001", 2),
        ("0.0013333333", 1334),  # 1333.3333 micro-USD: a fraction above one rounds up, not off
        ("1E-7", 1),
        ("0.00000000000000000000000000001", 1),
        ("0.003", 3_000),
        ("1.5", 1_500_000),
        ("12345678901234567890.0000001", 12345678901234567890000001),
    ],
)
def test_a_spent_amount_rounds_up_to_a_whole_micro_usd(amount: str, micro: int) -> None:
    assert to_micro_usd(Decimal(amount)) == micro


@pytest.mark.parametrize(
    ("amount", "micro"),
    [
        ("0", 0),
        ("0.0000001", 0),
        ("0.0000019", 1),
        ("1.000000", 1_000_000),
        ("12345678901234567890.0000001", 12345678901234567890000000),
    ],
)
def test_a_ceiling_rounds_down_to_a_whole_micro_usd(amount: str, micro: int) -> None:
    assert ceiling_to_micro_usd(Decimal(amount)) == micro


def test_the_conversion_ignores_the_precision_and_rounding_of_the_thread_context() -> None:
    with localcontext(Context(prec=3, rounding=ROUND_DOWN)):
        assert to_micro_usd(Decimal("0.0000001")) == 1
        assert to_micro_usd(Decimal("1234.5678901")) == 1_234_567_891


@pytest.mark.parametrize("amount", ["-0.000001", "NaN", "Infinity", "-Infinity"])
def test_an_amount_that_is_negative_or_not_finite_is_refused(
    ledger: SpendLedger, db: Database, amount: str
) -> None:
    bad = Decimal(amount)
    with pytest.raises(ValueError, match="невід'ємним"):
        to_micro_usd(bad)
    with pytest.raises(ValueError, match="невід'ємним"):
        ceiling_to_micro_usd(bad)
    with pytest.raises(ValueError, match="невід'ємним"):
        ledger.reserve(RUN, "req-1", MODEL, bad, Decimal("1"), {})
    with pytest.raises(ValueError, match="невід'ємним"):
        ledger.reserve(RUN, "req-1", MODEL, Decimal("0.1"), bad, {})
    entry = reserve(ledger, "req-2", "0.1")
    with pytest.raises(ValueError, match="невід'ємним"):
        ledger.complete(entry.id, bad)
    assert [(e.request_id, e.state) for e in stored(db)] == [("req-2", "reserved")]


# --- reserve -------------------------------------------------------------------------------------


def test_a_reservation_is_stored_with_its_micro_usd_and_price_snapshot(
    ledger: SpendLedger, db: Database
) -> None:
    entry = reserve(ledger, "req-1", "0.0500001")

    assert (entry.run_id, entry.request_id, entry.state, entry.model) == (
        RUN,
        "req-1",
        "reserved",
        MODEL,
    )
    assert (entry.reserved_micro_usd, entry.actual_micro_usd) == (50_001, None)
    # Decimal prices become plain decimal text: JSON has no exact decimal.
    assert entry.price_snapshot == {"usd_per_minute": "0.0030", "captured_at": "2026-11-15"}
    (row,) = stored(db)
    assert (row.id, row.reserved_micro_usd, row.actual_micro_usd) == (entry.id, 50_001, None)
    assert row.price_snapshot == entry.price_snapshot
    assert ledger.committed_micro_usd(RUN) == 50_001


def test_a_reservation_that_exactly_fills_the_remainder_passes_and_one_more_micro_usd_fails(
    ledger: SpendLedger, db: Database
) -> None:
    reserve(ledger, "req-1", "0.40")
    reserve(ledger, "req-2", "0.60")  # 0.40 + 0.60 == 1.00: equal to the remainder

    refusal = service_error(lambda: reserve(ledger, "req-3", "0.0000001"))

    assert isinstance(refusal, BudgetExceededError)
    assert (refusal.kind, refusal.code) == (ErrorKind.LIMIT_EXCEEDED, "BUDGET_EXCEEDED")
    assert refusal.details == {
        "committed_micro_usd": 1_000_000,
        "requested_micro_usd": 1,
        "ceiling_micro_usd": 1_000_000,
    }
    assert [e.request_id for e in stored(db)] == ["req-1", "req-2"]  # the refusal wrote nothing
    assert ledger.committed_micro_usd(RUN) == 1_000_000


def test_the_ceiling_rounds_down_so_a_sub_micro_remainder_does_not_pass(
    ledger: SpendLedger, db: Database
) -> None:
    refusal = service_error(
        lambda: ledger.reserve(
            RUN, "req-1", MODEL, Decimal("0.0000001"), Decimal("0.0000001"), SNAPSHOT
        )
    )

    assert isinstance(refusal, BudgetExceededError)
    assert refusal.details["ceiling_micro_usd"] == 0
    assert refusal.details["requested_micro_usd"] == 1
    assert stored(db) == []


def test_a_request_id_is_reserved_once_per_run_even_after_it_was_released(
    ledger: SpendLedger, db: Database
) -> None:
    first = reserve(ledger, "req-1", "0.10")
    ledger.release(first.id)

    again = service_error(lambda: reserve(ledger, "req-1", "0.10"))

    assert (again.kind, again.code) == (ErrorKind.CONFLICT, "SPEND_DUPLICATE_REQUEST")
    assert [(e.state, e.reserved_micro_usd) for e in stored(db)] == [("released", 100_000)]
    # The same request id in another run is another request.
    assert reserve(ledger, "req-1", "0.10", run_id="run-2").run_id == "run-2"


# --- what a run has committed --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settle", "committed"),
    [
        pytest.param(lambda ledger, entry: None, 500_000, id="reserved: the reservation"),
        pytest.param(
            lambda ledger, entry: ledger.complete(entry.id, Decimal("0.2")),
            200_000,
            id="completed: the actual cost, below the reservation",
        ),
        pytest.param(
            lambda ledger, entry: ledger.complete(entry.id, Decimal("0.7")),
            700_000,
            id="completed: the actual cost, above the reservation",
        ),
        pytest.param(
            lambda ledger, entry: ledger.complete(entry.id, Decimal("0")),
            0,
            id="completed: a free request",
        ),
        pytest.param(
            lambda ledger, entry: ledger.complete(entry.id, Decimal("0.0000001")),
            1,
            id="completed: the actual cost rounds up",
        ),
        pytest.param(
            lambda ledger, entry: ledger.complete(entry.id, None),
            500_000,
            id="completed without a cost: the reservation",
        ),
        pytest.param(
            lambda ledger, entry: ledger.mark_unknown(entry.id),
            500_000,
            id="unknown outcome: the reservation",
        ),
        pytest.param(lambda ledger, entry: ledger.release(entry.id), 0, id="released: nothing"),
    ],
)
def test_what_a_run_has_committed(
    ledger: SpendLedger, settle: Callable[[SpendLedger, SpendEntry], object], committed: int
) -> None:
    entry = reserve(ledger, "req-1", "0.5")
    settle(ledger, entry)
    reserve(ledger, "req-2", "0.1", run_id="run-2")  # another run is not counted

    assert ledger.committed_micro_usd(RUN) == committed
    assert ledger.committed_micro_usd("run-without-entries") == 0


# What an entry adds to its run's committed sum, per state, when it reserved 500 000 and carries an
# actual cost of 200 000. Written out on purpose: a state added to SPEND_STATES has no line here,
# and the two tests below fail until someone decides whether it counts.
COMMITTED_BY_STATE = {
    "reserved": 500_000,  # the reservation
    "completed": 200_000,  # the actual cost
    "unknown_outcome": 500_000,  # the reservation: the money may be gone
    "released": 0,  # excluded: it was never sent
}


def test_every_state_is_counted_or_excluded_on_purpose() -> None:
    groups = (SPEND_COUNTS_ACTUAL, SPEND_COUNTS_RESERVED, SPEND_COUNTS_NOTHING)
    placed = [state for group in groups for state in group]

    assert sorted(placed) == sorted(SPEND_STATES)  # each state in exactly one group, none foreign
    assert set(COMMITTED_BY_STATE) == set(SPEND_STATES)
    assert SPEND_COUNTS_NOTHING == ("released",)


@pytest.mark.parametrize("state", SPEND_STATES)
def test_the_committed_sum_of_an_entry_follows_its_state(db: Database, state: str) -> None:
    with db.write_session() as session:
        SpendRepository(session).add(
            run_id=RUN,
            request_id="req-1",
            state=state,
            reserved_micro_usd=500_000,
            model=MODEL,
            price_snapshot={},
            actual_micro_usd=200_000,
        )

    with db.session() as session:
        assert SpendRepository(session).committed_micro_usd(RUN) == COMMITTED_BY_STATE[state]


def test_completing_with_less_than_reserved_frees_the_difference_for_the_next_request(
    ledger: SpendLedger,
) -> None:
    first = reserve(ledger, "req-1", "0.60")
    assert isinstance(service_error(lambda: reserve(ledger, "req-2", "0.60")), BudgetExceededError)

    ledger.complete(first.id, Decimal("0.25"))

    reserve(ledger, "req-2", "0.60")
    assert ledger.committed_micro_usd(RUN) == 850_000


def test_an_unknown_outcome_keeps_blocking_and_a_release_gives_the_money_back(
    ledger: SpendLedger,
) -> None:
    unknown = reserve(ledger, "req-1", "0.60")
    ledger.mark_unknown(unknown.id)
    assert isinstance(service_error(lambda: reserve(ledger, "req-2", "0.60")), BudgetExceededError)

    unsent = reserve(ledger, "req-3", "0.40")
    ledger.release(unsent.id)

    reserve(ledger, "req-4", "0.40")
    assert ledger.committed_micro_usd(RUN) == 1_000_000


def test_complete_stores_the_actual_cost_and_returns_the_entry(
    ledger: SpendLedger, db: Database
) -> None:
    entry = reserve(ledger, "req-1", "0.5")

    completed = ledger.complete(entry.id, Decimal("0.2345678"))

    assert (completed.id, completed.state) == (entry.id, "completed")
    assert (completed.reserved_micro_usd, completed.actual_micro_usd) == (500_000, 234_568)
    (row,) = stored(db)
    assert (row.state, row.reserved_micro_usd, row.actual_micro_usd) == (
        "completed",
        500_000,
        234_568,
    )


# --- state changes -------------------------------------------------------------------------------

SETTLE: dict[str, Callable[[SpendLedger, str], SpendEntry]] = {
    "completed": lambda ledger, entry_id: ledger.complete(entry_id, Decimal("0.2")),
    "unknown_outcome": lambda ledger, entry_id: ledger.mark_unknown(entry_id),
    "released": lambda ledger, entry_id: ledger.release(entry_id),
}


@pytest.mark.parametrize("attempted", sorted(SETTLE))
@pytest.mark.parametrize("final", sorted(SETTLE))
def test_an_entry_that_left_reserved_cannot_change_again(
    ledger: SpendLedger, db: Database, final: str, attempted: str
) -> None:
    entry = reserve(ledger, "req-1", "0.5")
    SETTLE[final](ledger, entry.id)
    before = [(e.state, e.reserved_micro_usd, e.actual_micro_usd) for e in stored(db)]
    committed = ledger.committed_micro_usd(RUN)

    refusal = service_error(lambda: SETTLE[attempted](ledger, entry.id))

    assert (refusal.kind, refusal.code) == (ErrorKind.CONFLICT, "SPEND_INVALID_TRANSITION")
    assert refusal.details == {"state": final, "requested_state": attempted}
    assert [(e.state, e.reserved_micro_usd, e.actual_micro_usd) for e in stored(db)] == before
    assert ledger.committed_micro_usd(RUN) == committed


@pytest.mark.parametrize("operation", sorted(SETTLE))
def test_an_unknown_entry_is_not_found(ledger: SpendLedger, operation: str) -> None:
    refusal = service_error(lambda: SETTLE[operation](ledger, "no-such-entry"))

    assert (refusal.kind, refusal.code) == (ErrorKind.NOT_FOUND, "SPEND_ENTRY_NOT_FOUND")


# --- T-048: parallel reservations ----------------------------------------------------------------


@pytest.mark.parametrize("ledger_count", [1, 4])
def test_t048_twenty_threads_reserve_at_once_and_the_ceiling_holds(
    db: Database, ledger_count: int
) -> None:
    """One ledger shares one lock; several ledgers on one database share only the transaction, so
    this also shows that the check and the insert are atomic without the in-process lock."""
    ceiling = Decimal("1.00")  # 1 000 000 micro-USD
    upper = Decimal("0.07")  # 70 000 micro-USD: 14 fit (980 000), a 15th (1 050 000) does not
    ledgers = [SpendLedger(db) for _ in range(ledger_count)]
    barrier = threading.Barrier(THREADS)

    def attempt(index: int) -> int | None:
        ledger = ledgers[index % ledger_count]
        barrier.wait(timeout=30)
        try:
            ledger.reserve(RUN, f"req-{index}", MODEL, upper, ceiling, {})
        except BudgetExceededError:
            return None
        return ledger.committed_micro_usd(RUN)  # what this thread sees right after its commit

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(attempt, index) for index in range(THREADS)]
        observed = [future.result(timeout=120) for future in futures]

    successes = [seen for seen in observed if seen is not None]
    assert len(successes) == 14
    assert max(successes) <= 1_000_000
    assert max(successes) == 980_000
    entries = stored(db)
    assert len(entries) == 14
    assert {e.state for e in entries} == {"reserved"}
    assert {e.reserved_micro_usd for e in entries} == {70_000}
    committed = ledgers[0].committed_micro_usd(RUN)
    assert committed == 980_000
    assert committed <= 1_000_000


# --- parallel state changes and duplicate requests -----------------------------------------------


def in_parallel(calls: list[Callable[[], object]]) -> list[object]:
    """Each call in a thread of its own, all released at the same moment. A result is what the call
    returned or the ``ServiceError`` it raised; any other exception fails the test."""
    barrier = threading.Barrier(len(calls))

    def attempt(call: Callable[[], object]) -> object:
        barrier.wait(timeout=30)
        try:
            return call()
        except ServiceError as refusal:
            return refusal

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(attempt, call) for call in calls]
        return [future.result(timeout=120) for future in futures]


@pytest.mark.parametrize("ledger_count", [1, 2])
def test_complete_and_release_of_one_entry_at_once_let_exactly_one_win(
    db: Database, ledger_count: int
) -> None:
    """Ten entries, each settled by a complete and a release started together (20 threads). One
    ledger shares a lock; two ledgers share only the transaction."""
    ledgers = [SpendLedger(db) for _ in range(ledger_count)]
    entries = [reserve(ledgers[0], f"req-{number}", "0.5", ceiling="100") for number in range(10)]
    calls: list[Callable[[], object]] = []
    for entry in entries:
        calls.append(partial(ledgers[0].complete, entry.id, Decimal("0.2")))
        calls.append(partial(ledgers[-1].release, entry.id))

    results = in_parallel(calls)

    final = {e.id: e for e in stored(db)}
    completed = 0
    for number, entry in enumerate(entries):
        done, released = results[2 * number], results[2 * number + 1]
        # Exactly one of the two changed the entry; the other was refused, not applied after it.
        assert isinstance(done, SpendEntry) != isinstance(released, SpendEntry)
        if isinstance(done, SpendEntry):
            assert isinstance(released, ServiceError)
            assert (released.kind, released.code) == (
                ErrorKind.CONFLICT,
                "SPEND_INVALID_TRANSITION",
            )
            assert released.details == {"state": "completed", "requested_state": "released"}
            assert (final[entry.id].state, final[entry.id].actual_micro_usd) == (
                "completed",
                200_000,
            )
            completed += 1
        else:
            assert isinstance(done, ServiceError)
            assert (done.kind, done.code) == (ErrorKind.CONFLICT, "SPEND_INVALID_TRANSITION")
            assert done.details == {"state": "released", "requested_state": "completed"}
            assert (final[entry.id].state, final[entry.id].actual_micro_usd) == ("released", None)
    assert len(final) == 10
    # Committed is what the winners left: 200 000 per completed entry, nothing per released one.
    assert ledgers[0].committed_micro_usd(RUN) == completed * 200_000


@pytest.mark.parametrize("ledger_count", [1, 4])
def test_twenty_threads_reserve_the_same_request_id_and_exactly_one_succeeds(
    db: Database, ledger_count: int
) -> None:
    """The budget has room for all 20 (20 x 10 000 under 1 000 000), so only the rule "a request
    id is reserved once per run" can refuse the other 19. Several ledgers share only the
    transaction."""
    ledgers = [SpendLedger(db) for _ in range(ledger_count)]
    calls: list[Callable[[], object]] = [
        partial(reserve, ledgers[index % ledger_count], "req-same", "0.01")
        for index in range(THREADS)
    ]

    results = in_parallel(calls)

    winners = [result for result in results if isinstance(result, SpendEntry)]
    refusals = [result for result in results if isinstance(result, ServiceError)]
    assert len(winners) == 1
    assert len(refusals) == THREADS - 1
    assert {(r.kind, r.code) for r in refusals} == {(ErrorKind.CONFLICT, "SPEND_DUPLICATE_REQUEST")}
    (row,) = stored(db)
    assert (row.id, row.request_id, row.state) == (winners[0].id, "req-same", "reserved")
    assert ledgers[0].committed_micro_usd(RUN) == 10_000


# --- the database refuses what the ledger never writes -------------------------------------------


def add_row(db: Database, *, state: str, reserved: int, actual: int | None) -> None:
    with db.write_session() as session:
        SpendRepository(session).add(
            run_id=RUN,
            request_id="req-1",
            state=state,
            reserved_micro_usd=reserved,
            model=MODEL,
            price_snapshot={},
            actual_micro_usd=actual,
        )


@pytest.mark.parametrize(
    ("state", "reserved", "actual", "constraint"),
    [
        pytest.param("settled", 1, None, "ck_spend_entries_state", id="a state outside the list"),
        pytest.param("Reserved", 1, None, "ck_spend_entries_state", id="a state in another case"),
        pytest.param("", 1, None, "ck_spend_entries_state", id="an empty state"),
        pytest.param(
            "reserved", -1, None, "ck_spend_entries_reserved", id="a negative reservation"
        ),
        pytest.param("completed", 1, -1, "ck_spend_entries_actual", id="a negative actual cost"),
    ],
)
def test_the_database_refuses_an_unknown_state_and_negative_money(
    db: Database, state: str, reserved: int, actual: int | None, constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        add_row(db, state=state, reserved=reserved, actual=actual)

    assert stored(db) == []


@pytest.mark.parametrize(
    ("to_state", "actual", "constraint"),
    [
        pytest.param("settled", None, "ck_spend_entries_state", id="to a state outside the list"),
        pytest.param("completed", -1, "ck_spend_entries_actual", id="to a negative actual cost"),
    ],
)
def test_the_database_refuses_a_change_to_an_unknown_state_or_a_negative_cost(
    ledger: SpendLedger, db: Database, to_state: str, actual: int | None, constraint: str
) -> None:
    entry = reserve(ledger, "req-1", "0.5")

    with pytest.raises(IntegrityError, match=constraint), db.write_session() as session:
        SpendRepository(session).transition(
            entry.id, to_state, from_states=("reserved",), actual_micro_usd=actual
        )

    (row,) = stored(db)
    assert (row.state, row.reserved_micro_usd, row.actual_micro_usd) == ("reserved", 500_000, None)


# --- migration 0004 ------------------------------------------------------------------------------

LEGACY_SNAPSHOT = '{"schema_version":1,"value":{"usd_per_minute":0.003}}'
LEGACY_ROWS = [
    ("e1", "run-1", "req-1", "reserved", 0.05, None),
    ("e2", "run-1", "req-2", "settled", 1e-07, 0.031),  # a state the ledger does not know
    ("e3", "run-1", "req-3", "completed", 0.05, 0.0),
    ("e4", "run-1", "req-4", "released", 2.5, None),
    # 1333.3333 and 2666.6667 micro-USD: above one micro-USD, with a fraction. A rounding that
    # truncates or rounds to nearest gives 1333 and 2667, so this row pins "up" for both columns.
    ("e5", "run-1", "req-5", "completed", 0.0013333333, 0.0026666667),
]


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    return config


def run_alembic(database: Database, action: Callable[[Config], None]) -> None:
    config = alembic_config()
    with database.engine.connect() as connection:
        config.attributes["connection"] = connection
        action(config)
        connection.commit()


def spend_columns(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info(spend_entries)")]


def spend_rows(path: Path) -> list[tuple[object, ...]]:
    """``(request_id, state, reserved, actual, price_snapshot)`` in the units of the schema the
    database is at: micro-USD from 0004 on, USD before."""
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT * FROM spend_entries ORDER BY request_id").fetchall()
    return [(row[2], row[3], row[4], row[5], row[7]) for row in rows]


def table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {name for (name,) in rows}


def stored_revision(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        (version,) = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert isinstance(version, str)
    return version


def test_migration_0004_converts_float_usd_to_micro_usd_and_back(tmp_path: Path) -> None:
    only_0003 = tmp_path / "only-0003"
    (only_0003 / "versions").mkdir(parents=True)
    shutil.copyfile(MIGRATIONS_DIR / "env.py", only_0003 / "env.py")
    for name in ("r0001_initial.py", "r0002_previews.py", "r0003_transcripts.py"):
        shutil.copyfile(MIGRATIONS_DIR / "versions" / name, only_0003 / "versions" / name)
    data_dir = tmp_path / "data"
    db_path = data_dir / DB_FILENAME
    with open_database(data_dir, migrations_dir=only_0003):
        pass
    assert spend_columns(db_path) == [
        "id",
        "run_id",
        "request_id",
        "state",
        "reserved_usd",
        "actual_usd",
        "model",
        "price_snapshot",
    ]
    with closing(sqlite3.connect(db_path)) as connection:
        connection.executemany(
            "INSERT INTO spend_entries (id, run_id, request_id, state, reserved_usd, actual_usd, "
            "model, price_snapshot) VALUES (?, ?, ?, ?, ?, ?, 'm', ?)",
            [(*row, LEGACY_SNAPSHOT) for row in LEGACY_ROWS],
        )
        connection.commit()

    with open_database(data_dir) as upgraded:
        assert stored_revision(db_path) == head_revision()
        assert spend_columns(db_path) == [
            "id",
            "run_id",
            "request_id",
            "state",
            "reserved_micro_usd",
            "actual_micro_usd",
            "model",
            "price_snapshot",
        ]
        assert table_names(db_path).isdisjoint({"spend_entries_r0003", "spend_entries_r0004"})
        # req-2 had the unknown state "settled" with reserved 1e-07 USD (1 micro-USD) and actual
        # 0.031 USD (31 000). It becomes unknown_outcome, which counts its reservation, so the
        # reservation is raised to max(1, 31 000) = 31 000: it must not be counted for 1.
        expected_up = [
            ("req-1", "reserved", 50_000, None, LEGACY_SNAPSHOT),
            ("req-2", "unknown_outcome", 31_000, 31_000, LEGACY_SNAPSHOT),
            ("req-3", "completed", 50_000, 0, LEGACY_SNAPSHOT),
            ("req-4", "released", 2_500_000, None, LEGACY_SNAPSHOT),
            ("req-5", "completed", 1_334, 2_667, LEGACY_SNAPSHOT),
        ]
        assert spend_rows(db_path) == expected_up
        with upgraded.session() as session:  # the models read what the migration wrote
            entries = SpendRepository(session).list_for_run("run-1")
            assert [(e.request_id, e.reserved_micro_usd, e.actual_micro_usd) for e in entries] == [
                ("req-1", 50_000, None),
                ("req-2", 31_000, 31_000),
                ("req-3", 50_000, 0),
                ("req-4", 2_500_000, None),
                ("req-5", 1_334, 2_667),
            ]
            # Counted: reserved 50 000 + unknown 31 000 (its reservation, raised from 1 to the
            # 31 000 it had reported) + completed 0 + released 0 + completed 2 667 (the actual
            # cost of req-5). Before the fix the unknown row counted 1 and the total was 52 668.
            assert SpendRepository(session).committed_micro_usd("run-1") == 83_667
        backup = next((data_dir / BACKUP_DIRNAME).iterdir())
        assert backup.name.endswith("-from-0003.sqlite3")
        assert spend_columns(backup)[4] == "reserved_usd"

        run_alembic(upgraded, lambda config: command.downgrade(config, "0003"))
        assert stored_revision(db_path) == "0003"
        assert spend_columns(db_path)[4:6] == ["reserved_usd", "actual_usd"]
        assert table_names(db_path).isdisjoint({"spend_entries_r0003", "spend_entries_r0004"})
        assert spend_rows(db_path) == [
            ("req-1", "reserved", 0.05, None, LEGACY_SNAPSHOT),
            ("req-2", "unknown_outcome", 0.031, 0.031, LEGACY_SNAPSHOT),
            ("req-3", "completed", 0.05, 0.0, LEGACY_SNAPSHOT),
            ("req-4", "released", 2.5, None, LEGACY_SNAPSHOT),
            ("req-5", "completed", 0.001334, 0.002667, LEGACY_SNAPSHOT),
        ]

        run_alembic(upgraded, lambda config: command.upgrade(config, "head"))
        assert stored_revision(db_path) == head_revision()
        assert spend_rows(db_path) == expected_up
        with upgraded.engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
