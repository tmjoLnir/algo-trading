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
from atp_core.execution.router import NO_ACTION
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
                    rejection_reason=outcome.rejection_reason,
                    rejected_by=outcome.rejected_by,
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
                        "rejection_reason": outcome.rejection_reason,
                        "rejected_by": outcome.rejected_by,
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

    async def rejections(
        self,
        *,
        strategy_id: str | None = None,
        rule: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[tuple[Signal, SignalOutcome]]:
        """Refusals only, newest first. What `/risk/rejections` reads.

        **Filtered in SQL rather than in the caller, and that is the whole
        point of the method existing.** Taking the newest N signals and keeping
        the refused ones would answer "were any of the last hundred decisions
        refused" — a completely different question, and one whose answer is
        "no" for a strategy that has been blocked for a week and has since
        emitted anything at all. An operator reading an empty list would
        conclude nothing is being refused.

        `no_action` is excluded here rather than left to the caller. It is not a
        refusal: `SubmitResult.no_action` marks a HOLD, or an exit against an
        already-flat position, and the router reports it as *approved* on
        purpose so it does not inflate the count an operator reads to decide
        whether the risk config is too tight. Including it would put that
        inflation back one layer up, where the reason it is wrong is much less
        visible.
        """
        query = (
            select(SignalRow)
            .where(SignalRow.rejected_by.is_not(None), SignalRow.rejected_by != NO_ACTION)
            .order_by(SignalRow.ts.desc())
            .limit(limit)
        )
        if strategy_id is not None:
            query = query.where(SignalRow.strategy_id == strategy_id)
        if rule is not None:
            query = query.where(SignalRow.rejected_by == rule)
        if since is not None:
            query = query.where(SignalRow.ts >= since)

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


def _split_reason(stored: str | None) -> tuple[str | None, str | None]:
    """Unpack a legacy `"[rule] reason"` value into `(reason, rule)`.

    Both fields have their own column as of `f4d2e8b1a075`, which backfills
    every stored row by this same grammar. This survives it as a **read-side
    fallback only**, for a row whose `rejected_by` is null while its
    `rejection_reason` still carries a bracketed prefix — a row written by a
    worker running older code against a migrated database, which is what a
    rolling deploy looks like for the minutes it takes.

    Nothing writes the packed form any more. When the fallback stops firing in
    practice it can go, and the migration is what makes that safe to check.
    """
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
    # The columns, unless this is a row from a straggling writer — see
    # `_split_reason`. Checking `rejected_by` first means the fallback costs
    # nothing on every row written since the migration.
    reason: str | None
    rejected_by: str | None
    if row.rejected_by is not None:
        reason, rejected_by = row.rejection_reason, row.rejected_by
    else:
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
