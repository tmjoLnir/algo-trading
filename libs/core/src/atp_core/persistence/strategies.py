"""Strategy storage — the `StrategyRepository` port over PostgreSQL.

The `strategies` table's job here is narrow: be the row that
`signals.strategy_id` and `orders.strategy_id` can point at. Nothing wrote it,
so both those columns were always null and no order could be traced to the
strategy that placed it.

`ensure` is an upsert that deliberately updates almost nothing, and the
asymmetry between insert and update is the point. On a **first** boot the worker
is the only thing that knows this strategy exists, so it writes what it has —
including `state="active"`, because a strategy a worker is running is not a
draft. On every **later** boot the row may have been edited by a
strategy-management API that knows more about it than a booting worker does, so
only `updated_at` moves. That keeps "when did a worker last run this?"
answerable without letting a restart quietly reset a strategy someone had
configured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from atp_core.logging import get_logger
from atp_core.persistence.db import session_scope
from atp_core.persistence.models import StrategyRow
from atp_core.strategy.ports import StoredStrategy, StrategyRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from atp_core.clock import Clock
    from atp_core.strategy.ports import StrategyRepository

log = get_logger(__name__)


class PostgresStrategyRepository:
    """`StrategyRepository` over the `strategies` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        # Injected rather than `datetime.now()` (rule §1.2), and it matters more
        # here than it looks: a backtest or a replay running on a `SimulatedClock`
        # must stamp these rows with market time, not with the wall clock of the
        # machine doing the replaying.
        self._clock = clock

    async def ensure(self, record: StrategyRecord) -> None:
        """Create the row if it is absent; otherwise only bump `updated_at`."""
        now = self._clock.now()
        async with session_scope(self._session_factory) as session:
            await session.execute(
                pg_insert(StrategyRow)
                .values(
                    id=record.id,
                    name=record.name,
                    description="",
                    kind=record.kind,
                    class_name=record.class_name,
                    params=dict(record.params or {}),
                    ruleset=None,
                    state="active",
                    universe=list(record.universe),
                    timeframe=record.timeframe,
                    created_at=now,
                    updated_at=now,
                )
                # Only the timestamp. See the module docstring: everything else
                # in this row may have been edited by someone who knows more
                # about it than a booting worker does, and an upsert that reset
                # `state` would stop a strategy by restarting it.
                .on_conflict_do_update(
                    index_elements=[StrategyRow.id],
                    set_={"updated_at": now},
                )
            )

    async def get(self, strategy_id: str) -> StrategyRecord | None:
        """The stored identity, or None."""
        async with session_scope(self._session_factory) as session:
            row = (
                await session.execute(select(StrategyRow).where(StrategyRow.id == strategy_id))
            ).scalar_one_or_none()
        return None if row is None else _to_record(row)

    async def list_all(self, *, state: str | None = None) -> list[StoredStrategy]:
        """Every stored strategy, newest first.

        Ordered by `created_at` descending with the id as a tie-break, so the
        list is stable: two strategies a worker registered in the same
        microsecond on first boot would otherwise swap places between reads.

        `created_at` rather than `updated_at`, even though the latter is the
        livelier column. `updated_at` moves on every worker boot, so ordering by
        it would reshuffle the whole screen each morning — the reader's mental
        map of a short list is worth more than putting the most recently booted
        strategy on top, and the timestamp is a column they can read.
        """
        query = select(StrategyRow)
        if state is not None:
            query = query.where(StrategyRow.state == state)
        query = query.order_by(StrategyRow.created_at.desc(), StrategyRow.id.desc())

        async with session_scope(self._session_factory) as session:
            rows = (await session.execute(query)).scalars().all()
        return [_to_stored(row) for row in rows]


def _to_stored(row: StrategyRow) -> StoredStrategy:
    """The whole row. See `StoredStrategy` for what `state` and `updated_at`
    actually mean, both of which are narrower than their names."""
    return StoredStrategy(
        id=row.id,
        name=row.name,
        description=row.description or "",
        kind=row.kind,
        class_name=row.class_name,
        params=dict(row.params or {}),
        ruleset=dict(row.ruleset) if row.ruleset else None,
        state=row.state,
        universe=tuple(row.universe or ()),
        timeframe=row.timeframe,
        risk_config=dict(row.risk_config or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_record(row: StrategyRow) -> StrategyRecord:
    return StrategyRecord(
        id=row.id,
        name=row.name,
        kind=row.kind,
        class_name=row.class_name,
        params=dict(row.params or {}),
        universe=tuple(row.universe or ()),
        timeframe=row.timeframe,
    )


def _typecheck(repo: PostgresStrategyRepository) -> StrategyRepository:
    """Structural conformance, checked by mypy rather than asserted at runtime."""
    return repo
