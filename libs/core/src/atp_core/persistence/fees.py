"""The venue-fee ledger — the `FeeLedger` port over PostgreSQL.

What this table answers is narrow: *which charges has the venue told us
about, and what do they come to?* It does **not** record what has been applied
to cash — that claim lives on `equity_snapshots.fees_settled`, written by the
same statement as the cash it describes, because a claim about a balance that
is one transaction away from the balance is a claim that can be wrong. It was,
on 2026-09-09: these rows committed, the corrected cash never reached a
snapshot, and $4.14 was lost with the ledger still insisting it had been
applied (ADR 0031).

`atp_core.execution.fees` explains why the fee is a movement of cash rather
than something the reconciler's tolerance should absorb.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from atp_core.logging import get_logger
from atp_core.persistence.db import session_scope
from atp_core.persistence.models import BrokerFeeRow

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from atp_core.brokers.ports import FeeActivity
    from atp_core.clock import Clock
    from atp_core.domain import RunMode

log = get_logger(__name__)


class PostgresFeeLedger:
    """`FeeLedger` over the `broker_fees` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def record_seen(self, activities: Sequence[FeeActivity], *, run_mode: RunMode) -> Decimal:
        """Insert what is new, then total everything, in one transaction.

        `ON CONFLICT DO NOTHING` rather than `... RETURNING`: which rows this
        particular call inserted is no longer interesting. What the caller needs
        is the total across every charge the venue has ever told us about, and
        that is the same number whether this call inserted three rows, none, or
        raced another worker for them. The race that mattered under ADR 0030 —
        two workers both being told a charge is theirs to apply — cannot arise
        when nobody is told that at all.

        The SUM runs in the same transaction as the INSERT, so the total
        returned includes the rows just written and excludes nothing committed
        before them.

        `COALESCE` because `SUM` over no rows is NULL, and a ledger with nothing
        in it owes zero rather than an error.
        """
        now = self._clock.now()
        by_id = {item.activity_id: item for item in activities}

        async with session_scope(self._session_factory) as session:
            if by_id:
                await session.execute(
                    pg_insert(BrokerFeeRow)
                    .values(
                        [
                            {
                                "activity_id": item.activity_id,
                                "run_mode": run_mode.value,
                                "booked_on": item.booked_on,
                                "amount": item.amount,
                                "sub_type": item.sub_type,
                                "description": item.description,
                                "seen_at": now,
                            }
                            for item in by_id.values()
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=[BrokerFeeRow.activity_id])
                )
            total = (
                await session.execute(
                    select(func.coalesce(func.sum(BrokerFeeRow.amount), 0)).where(
                        BrokerFeeRow.run_mode == run_mode.value
                    )
                )
            ).scalar_one()

        return Decimal(total)
