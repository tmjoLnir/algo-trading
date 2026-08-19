"""Signal storage — the `SignalRepository` port over PostgreSQL.

`SignalRow` has been in the schema since the initial migration with nothing
writing to it. The consequence was not a missing feature so much as a missing
*explanation*: the dashboard's signal feed was a bounded in-memory ring on the
runner, so a restart emptied it, and no stored order could name the decision
that produced it.

Two things this stores that the ring could not.

**The refusals.** A signal the risk chain denied is kept, with the rule that
denied it. From the orders table alone a strategy whose every idea was refused is
indistinguishable from a strategy that had no ideas, and those two call for
opposite responses — loosen a limit, or replace the strategy.

**The indicator values at decision time.** `Signal.indicators` is what makes a
losing run diagnosable months later, when the bar series has been restated and
the indicator can no longer be recomputed to the number the strategy actually
saw.

Values in `indicators` are stored as strings, not JSON numbers, and that is rule
§1.1 rather than fussiness: an indicator value is usually a price — an SMA of
closes is denominated in dollars — and JSON has only binary floats to carry it.
The runner's published feed already stringifies for the same reason.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from atp_core.domain import Signal, SignalAction
from atp_core.logging import get_logger
from atp_core.persistence.db import session_scope
from atp_core.persistence.models import SignalRow
from atp_core.strategy.ports import SignalOutcome

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from atp_core.strategy.ports import SignalRepository

log = get_logger(__name__)


class PostgresSignalRepository:
    """`SignalRepository` over the `signals` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, signal: Signal, outcome: SignalOutcome) -> None:
        """Upsert one decision and its outcome."""
        async with session_scope(self._session_factory) as session:
            await session.execute(
                pg_insert(SignalRow)
                .values(
                    id=signal.id,
                    strategy_id=signal.strategy_id,
                    symbol=signal.symbol,
                    action=signal.action.value,
                    ts=signal.ts,
                    strength=signal.strength,
                    reason=signal.reason,
                    indicators={k: str(v) for k, v in signal.indicators.items()},
                    acted_on=outcome.acted_on,
                    rejection_reason=_reason(outcome),
                )
                # The outcome is the mutable half: a signal recorded on its way
                # into the router and updated when the router answers is one
                # decision whose fate became known, not two decisions. The
                # decision itself — symbol, action, instant, indicators — is
                # not updated, because a row whose action changed under one id
                # would be a rewrite of history this write must not express.
                .on_conflict_do_update(
                    index_elements=[SignalRow.id],
                    set_={
                        "acted_on": outcome.acted_on,
                        "rejection_reason": _reason(outcome),
                    },
                )
            )

    async def recent(
        self, strategy_id: str | None = None, *, limit: int = 200
    ) -> list[tuple[Signal, SignalOutcome]]:
        """Newest first, optionally for one strategy."""
        query = select(SignalRow).order_by(SignalRow.ts.desc()).limit(limit)
        if strategy_id is not None:
            query = query.where(SignalRow.strategy_id == strategy_id)
        async with session_scope(self._session_factory) as session:
            rows = (await session.execute(query)).scalars().all()
        return [_to_signal(row) for row in rows]

    async def between(
        self, start: datetime, end: datetime, *, strategy_id: str | None = None
    ) -> list[tuple[Signal, SignalOutcome]]:
        """Every signal in `[start, end]`, oldest first. Both bounds inclusive."""
        query = (
            select(SignalRow)
            .where(SignalRow.ts >= start, SignalRow.ts <= end)
            .order_by(SignalRow.ts)
        )
        if strategy_id is not None:
            query = query.where(SignalRow.strategy_id == strategy_id)
        async with session_scope(self._session_factory) as session:
            rows = (await session.execute(query)).scalars().all()
        return [_to_signal(row) for row in rows]


def _reason(outcome: SignalOutcome) -> str | None:
    """Flatten the two rejection fields into the one column the schema has.

    `SignalRow` stores `rejection_reason` and no `rejected_by`, so the rule name
    is prefixed onto the reason rather than dropped: "which rule refused this"
    is the question an operator asks first, and a reason without it sends them
    reading the whole chain. Adding a column would be the tidier answer and is
    not worth a migration for one string — but the flattening is lossy, so
    `_split_reason` below is its exact inverse and the round trip is tested.
    """
    if outcome.rejection_reason is None and outcome.rejected_by is None:
        return None
    if outcome.rejected_by is None:
        return outcome.rejection_reason
    return f"[{outcome.rejected_by}] {outcome.rejection_reason or ''}".rstrip()


def _split_reason(stored: str | None) -> tuple[str | None, str | None]:
    """Inverse of `_reason`: `(rejection_reason, rejected_by)`."""
    if stored is None:
        return None, None
    if stored.startswith("[") and "]" in stored:
        rule, _, rest = stored[1:].partition("]")
        return (rest.strip() or None), rule
    return stored, None


def _to_signal(row: SignalRow) -> tuple[Signal, SignalOutcome]:
    """Rebuild a decision and its outcome from one row.

    `indicators` come back as the strings they were stored as. They are not
    parsed to `Decimal` or `float` on the way out: the column holds values of
    several kinds — a price, a period count, a boolean crossover flag — and this
    layer has no way to know which is which. A reader that needs a number knows
    what it asked for; guessing here would turn a period of `20` into `20.0` and
    a price into a binary float.
    """
    reason, rejected_by = _split_reason(row.rejection_reason)
    signal = Signal(
        strategy_id=row.strategy_id,
        symbol=row.symbol,
        action=SignalAction(row.action),
        ts=row.ts,
        id=row.id,
        strength=row.strength if row.strength is not None else Decimal(1),
        reason=row.reason,
        indicators=dict(row.indicators or {}),
    )
    return signal, SignalOutcome(
        acted_on=row.acted_on, rejection_reason=reason, rejected_by=rejected_by
    )


def _typecheck(repo: PostgresSignalRepository) -> SignalRepository:
    """Structural conformance, checked by mypy rather than asserted at runtime."""
    return repo
