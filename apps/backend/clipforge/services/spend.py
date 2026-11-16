"""Budget ledger of paid requests (FR-049, docs/05 section 6).

Before a paid request is sent, its upper cost estimate is *reserved* against the ceiling of its
run. The entry then ends in exactly one of three states::

    reserved --complete-------> completed        the request was paid; the real cost is known
             --mark_unknown---> unknown_outcome  sent, but the answer (and the bill) is unknown
             --release--------> released         never sent; the reservation is given back

No other change is allowed: an entry that left ``reserved`` is final. What a run has committed is
the sum of its entries: ``completed`` counts the actual cost (the reservation when the provider
reported none), ``reserved`` and ``unknown_outcome`` count the reservation, ``released`` nothing.
An ``unknown_outcome`` is never forgiven by this module: the money may have been spent.

Money is ``Decimal`` USD at the border and whole micro-USD inside (1 USD = 1 000 000). An amount
that is spent rounds UP to a micro-USD, so a sum is never below the truth. The ceiling rounds DOWN,
so ``committed <= ceiling`` in micro-USD implies the same in exact USD.

The check against the ceiling and the insert of the reservation are one step: under a lock of this
ledger and inside one ``Database.write_session()`` (``BEGIN IMMEDIATE``). The lock orders the
threads of this process; the transaction is what keeps another ledger object or process on the
same database from reserving between the check and the insert.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal
from typing import Any, Final

from sqlalchemy.orm import Session

from clipforge.services.errors import ErrorKind, ServiceError, conflict, not_found
from clipforge.storage import (
    SPEND_COMPLETED,
    SPEND_RELEASED,
    SPEND_RESERVED,
    SPEND_UNKNOWN_OUTCOME,
    Database,
    SpendEntry,
    SpendRepository,
)

_MICRO_PER_USD: Final = Decimal(1_000_000)


class BudgetExceededError(ServiceError):
    """The next request does not fit the remaining budget of its run; nothing was reserved.

    ``details`` carries the three whole micro-USD figures: what the run has committed, what the
    request would reserve and the ceiling.
    """

    def __init__(
        self, *, committed_micro_usd: int, requested_micro_usd: int, ceiling_micro_usd: int
    ) -> None:
        super().__init__(
            ErrorKind.LIMIT_EXCEEDED,
            "BUDGET_EXCEEDED",
            "Ліміт витрат вичерпано: наступний запит не вміщується в бюджет",
            details={
                "committed_micro_usd": committed_micro_usd,
                "requested_micro_usd": requested_micro_usd,
                "ceiling_micro_usd": ceiling_micro_usd,
            },
        )


def to_micro_usd(amount: Decimal) -> int:
    """A spent or reserved amount in whole micro-USD, rounded UP (0.0000001 USD is 1)."""
    return _to_micro_usd(amount, ROUND_CEILING)


def ceiling_to_micro_usd(amount: Decimal) -> int:
    """A budget ceiling in whole micro-USD, rounded DOWN: the limit is never loosened."""
    return _to_micro_usd(amount, ROUND_FLOOR)


def _to_micro_usd(amount: Decimal, rounding: str) -> int:
    if not amount.is_finite() or amount < 0:
        raise ValueError("Сума має бути скінченним невід'ємним числом")
    # A context of its own: the thread's context has 28 digits and a rounding that may go down.
    context = Context(prec=200, rounding=rounding)
    return int(context.multiply(amount, _MICRO_PER_USD).to_integral_value(rounding=rounding))


class SpendLedger:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()

    def reserve(
        self,
        run_id: str,
        request_id: str,
        model: str,
        upper_usd: Decimal,
        ceiling_usd: Decimal,
        price_snapshot: Mapping[str, Any],
    ) -> SpendEntry:
        """Reserve ``upper_usd`` (the upper estimate of one request) for the run, or raise
        ``BudgetExceededError`` when the run's committed money plus it would pass ``ceiling_usd``
        (a reservation that exactly fills the rest is allowed). ``price_snapshot`` is stored as
        given, with ``Decimal`` values as plain decimal text. A ``request_id`` is reserved once
        per run: a second try raises 409 ``SPEND_DUPLICATE_REQUEST``, so a request that may
        already have been sent is never silently paid for twice."""
        upper = to_micro_usd(upper_usd)
        ceiling = ceiling_to_micro_usd(ceiling_usd)
        snapshot = _json_safe(price_snapshot)
        with self._write() as session:
            spend = SpendRepository(session)
            if spend.find(run_id, request_id) is not None:
                raise conflict(
                    "SPEND_DUPLICATE_REQUEST", "Для цього запиту бюджет уже зарезервовано"
                )
            committed = spend.committed_micro_usd(run_id)
            if committed + upper > ceiling:
                raise BudgetExceededError(
                    committed_micro_usd=committed,
                    requested_micro_usd=upper,
                    ceiling_micro_usd=ceiling,
                )
            return spend.add(
                run_id=run_id,
                request_id=request_id,
                state=SPEND_RESERVED,
                reserved_micro_usd=upper,
                model=model,
                price_snapshot=snapshot,
            )

    def complete(self, entry_id: str, actual_usd: Decimal | None) -> SpendEntry:
        """The request was paid. ``actual_usd`` is the provider's cost; ``None`` when it reported
        none: the reservation then keeps counting in full."""
        actual = to_micro_usd(actual_usd) if actual_usd is not None else None
        return self._settle(entry_id, SPEND_COMPLETED, actual)

    def mark_unknown(self, entry_id: str) -> SpendEntry:
        """The request was sent but its outcome is unknown (a timeout after sending): the
        reservation keeps counting, because the money may be gone."""
        return self._settle(entry_id, SPEND_UNKNOWN_OUTCOME, None)

    def release(self, entry_id: str) -> SpendEntry:
        """The request was never sent: the reservation stops counting."""
        return self._settle(entry_id, SPEND_RELEASED, None)

    def committed_micro_usd(self, run_id: str) -> int:
        """What the run has committed, in whole micro-USD (see the module text)."""
        with self._database.session() as session:
            return SpendRepository(session).committed_micro_usd(run_id)

    def _settle(self, entry_id: str, to_state: str, actual_micro_usd: int | None) -> SpendEntry:
        with self._write() as session:
            spend = SpendRepository(session)
            changed = spend.transition(
                entry_id,
                to_state,
                from_states=(SPEND_RESERVED,),
                actual_micro_usd=actual_micro_usd,
            )
            entry = spend.get(entry_id)
            if entry is None:
                raise not_found("SPEND_ENTRY_NOT_FOUND", "Запис обліку витрат не знайдено")
            if not changed:
                raise conflict(
                    "SPEND_INVALID_TRANSITION",
                    "Запис обліку витрат уже закрито: змінити його стан неможливо",
                    details={"state": entry.state, "requested_state": to_state},
                )
            return entry

    @contextmanager
    def _write(self) -> Iterator[Session]:
        with self._lock, self._database.write_session() as session:
            yield session


def _json_safe(value: Any) -> Any:
    """``Decimal`` as plain decimal text (JSON has no exact decimal); the rest unchanged."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value
