"""Book storage — the `PortfolioRepository` port over PostgreSQL.

One snapshot is a set of `position_snapshots` rows plus one `equity_snapshots`
row, every one stamped with the same instant. That shared timestamp is what
makes a read coherent: `latest` finds the newest equity row and takes exactly
the positions written with it, so a restart never reconstructs a book that is
half of one snapshot and half of the next.

Positions and cash are stored separately because the schema already separated
them, and the split is right — cash is an account-level fact and a position is
a per-symbol one. It does mean a snapshot is only complete if both halves land,
which is why they are written in one transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from atp_core.domain import Portfolio, Position
from atp_core.logging import get_logger
from atp_core.persistence.db import session_scope
from atp_core.persistence.models import EquitySnapshotRow, PositionSnapshotRow

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from atp_core.domain import RunMode
    from atp_core.execution.ports import PortfolioRepository

log = get_logger(__name__)


class PostgresPortfolioRepository:
    """`PortfolioRepository` over PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def snapshot(self, portfolio: Portfolio, *, at: datetime, run_mode: RunMode) -> None:
        """Write positions, cash and equity as one transaction."""
        async with session_scope(self._session_factory) as session:
            session.add(
                EquitySnapshotRow(
                    ts=at,
                    equity=portfolio.equity,
                    cash=portfolio.cash,
                    gross_exposure=portfolio.gross_exposure,
                    run_mode=run_mode.value,
                )
            )
            for position in portfolio.open_positions:
                session.add(
                    PositionSnapshotRow(
                        ts=at,
                        symbol=position.symbol,
                        qty=position.qty,
                        avg_entry_price=position.avg_entry_price,
                        # `last_price` is not nullable in the schema. An
                        # unmarked position falls back to its own basis rather
                        # than to zero — a zero mark would make every
                        # percentage limit read this position as free.
                        last_price=(
                            position.last_price
                            if position.last_price is not None
                            else position.avg_entry_price
                        ),
                        unrealized_pnl=position.unrealized_pnl,
                        realized_pnl=position.realized_pnl,
                        fees_paid=position.fees_paid,
                        stop_loss_price=position.stop_loss_price,
                        take_profit_price=position.take_profit_price,
                        high_water_mark=position.high_water_mark,
                        opened_at=position.opened_at,
                        run_mode=run_mode.value,
                    )
                )

    async def latest(self, run_mode: RunMode) -> Portfolio | None:
        """The newest complete snapshot, or None if there has never been one."""
        async with session_scope(self._session_factory) as session:
            equity_row = (
                await session.execute(
                    select(EquitySnapshotRow)
                    .where(EquitySnapshotRow.run_mode == run_mode.value)
                    .order_by(EquitySnapshotRow.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if equity_row is None:
                return None
            positions = await self._positions_at(session, equity_row.ts, run_mode)

        portfolio = Portfolio(
            cash=equity_row.cash,
            # Not the original starting equity — that is not stored, and this
            # is the honest substitute rather than a guess. It means
            # `total_return` restarts from the reload point, so P&L since
            # inception must be computed from the `equity_snapshots` history
            # instead of read off a reloaded portfolio. Stated because a return
            # that quietly resets to zero on a restart is the kind of number
            # somebody reports.
            starting_equity=equity_row.equity,
        )
        for position in positions:
            portfolio.positions[position.symbol] = position
        return portfolio

    @staticmethod
    async def _positions_at(
        session: AsyncSession, ts: datetime, run_mode: RunMode
    ) -> list[Position]:
        rows = (
            (
                await session.execute(
                    select(PositionSnapshotRow).where(
                        PositionSnapshotRow.ts == ts,
                        PositionSnapshotRow.run_mode == run_mode.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            Position(
                symbol=row.symbol,
                qty=row.qty,
                avg_entry_price=row.avg_entry_price,
                realized_pnl=row.realized_pnl,
                fees_paid=row.fees_paid,
                opened_at=row.opened_at,
                last_price=row.last_price,
                stop_loss_price=row.stop_loss_price,
                take_profit_price=row.take_profit_price,
                high_water_mark=row.high_water_mark,
            )
            for row in rows
        ]


def _typecheck(repo: PostgresPortfolioRepository) -> PortfolioRepository:
    """Structural conformance, checked by mypy rather than asserted at runtime."""
    return repo
