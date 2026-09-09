"""The venue-fee ledger — the `FeeLedger` port over PostgreSQL.

What this table answers is narrow and it is asked constantly: *have we already
taken this charge out of our cash?* The venue's fee feed is re-read on every
reconcile, so the same charge is offered over and over, and the answer has to
be exactly right in both directions. Applied twice, our cash falls below the
venue's; applied never, it stays above — and the second is what halted the
worker on 2026-09-09 with $4.14 of unaccounted CAT, REG and TAF fees.

`atp_core.execution.fees` explains why the fee is a movement of cash rather
than something the reconciler's tolerance should absorb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

    async def record_unseen(
        self, activities: Sequence[FeeActivity], *, run_mode: RunMode
    ) -> list[FeeActivity]:
        """Insert what is new and return exactly that, in one statement.

        `ON CONFLICT DO NOTHING ... RETURNING` is what makes this safe without a
        lock: Postgres returns a row only for an insert that actually happened,
        so two workers reconciling at the same instant cannot both be told a
        charge is new. Doing this as a `SELECT` then an `INSERT` would leave
        exactly that race, and the prize for losing it is a double-charged
        ledger.

        The insert carries `applied_at` even though nothing has been applied
        yet, because by the time this returns the caller is committed to
        applying it — the row *is* the claim. `settle_broker_fees` documents
        why the record is written first and what the crash window costs.

        An empty input is not a database round trip. It is the normal case on a
        venue that charges nothing intraday.
        """
        if not activities:
            return []

        now = self._clock.now()
        by_id = {item.activity_id: item for item in activities}

        async with session_scope(self._session_factory) as session:
            result = await session.execute(
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
                            "applied_at": now,
                        }
                        for item in by_id.values()
                    ]
                )
                .on_conflict_do_nothing(index_elements=[BrokerFeeRow.activity_id])
                .returning(BrokerFeeRow.activity_id)
            )
            inserted = {row[0] for row in result.all()}

        # Rebuilt in the order they were *offered*, not the order the database
        # returned them: the caller logs these, and a fee list an operator reads
        # should look like the venue's own feed.
        unseen = [item for item in by_id.values() if item.activity_id in inserted]
        if unseen:
            log.info(
                "persistence.fees.recorded",
                count=len(unseen),
                offered=len(by_id),
                run_mode=run_mode.value,
            )
        return unseen
