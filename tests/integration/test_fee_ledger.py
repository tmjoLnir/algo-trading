"""The venue-fee ledger against a real PostgreSQL.

Not a unit test, and the distinction is the whole point here. `FakeFeeLedger`
implements exactly-once with a Python set, so a unit test that passes against it
proves the *fake* is idempotent and says nothing about the SQL — which is where
the property actually lives:

    INSERT ... ON CONFLICT DO NOTHING RETURNING activity_id

Three things only the database can be asked.

1. **`RETURNING` yields a row only for an insert that happened.** That single
   clause is the entire exactly-once guarantee. If it returned the conflicting
   row too, every reconcile would re-apply every fee it had ever seen and walk
   our cash steadily below the venue's — the 2026-09-09 halt with its sign
   flipped, and harder to read because the drift would grow in the direction
   that looks like diligence.
2. **A mixed batch splits correctly.** The normal case after the first run is a
   feed that is mostly known with one new charge on the end, and returning the
   batch or nothing would both be wrong.
3. **Two workers racing hand the charge to exactly one.** `Reconciler` runs this
   at `warmup` and on a five-minute schedule, so a restart during a scheduled
   run has two processes in this statement at once. A `SELECT` then an `INSERT`
   would lose that race; this is the test that says the one-statement version
   does not.

`NUMERIC(20, 8)` is checked in passing for the reason its neighbours check it:
the amount is money (rule §1.1), and a column that padded or rounded it would
settle a different number against cash than the venue charged.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import asyncpg
import pytest

from atp_core.brokers.ports import FeeActivity
from atp_core.clock import SimulatedClock
from atp_core.domain import RunMode
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.fees import PostgresFeeLedger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 9, 9, 15, tzinfo=UTC)

#: The account feed for 2026-09-08, which is the batch this table was built for.
DAY_TWO = [
    FeeActivity(
        activity_id="20260908000000000::da7b17a5-2eb4-4990-858c-ff53b830f8de",
        booked_on=date(2026, 9, 8),
        amount=Decimal("0.01"),
        sub_type="CAT",
        description="CAT fee for proceed of 174 trades on 2026-09-08",
    ),
    FeeActivity(
        activity_id="20260908000000000::97e8ab4e-6a81-42f5-8338-1bd6b5609864",
        booked_on=date(2026, 9, 8),
        amount=Decimal("3.82"),
        sub_type="REG",
        description="REG fee for proceed of $185271.37 on 2026-09-08",
    ),
    FeeActivity(
        activity_id="20260908000000000::6fa7483d-449e-474d-8909-b1bc9a8e029b",
        booked_on=date(2026, 9, 8),
        amount=Decimal("0.31"),
        sub_type="TAF",
        description="TAF fee for proceed of 1572 shares (89 trades)",
    ),
]


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture
async def ledger(migrated_db: str) -> AsyncIterator[PostgresFeeLedger]:
    """An empty `broker_fees`, and a ledger over it."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated_db))
    try:
        await connection.execute("TRUNCATE broker_fees")
    finally:
        await connection.close()

    engine = create_engine(migrated_db)
    try:
        yield PostgresFeeLedger(create_session_factory(engine), SimulatedClock(NOW))
    finally:
        await engine.dispose()


@pytest.fixture
async def raw(migrated_db: str) -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(_asyncpg_dsn(migrated_db))
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_the_first_pass_takes_every_charge(ledger: PostgresFeeLedger) -> None:
    unseen = await ledger.record_unseen(DAY_TWO, run_mode=RunMode.PAPER)

    assert sum(item.amount for item in unseen) == Decimal("4.14")
    assert [item.sub_type for item in unseen] == ["CAT", "REG", "TAF"], "the venue's order"


@pytest.mark.asyncio
async def test_the_same_feed_offered_again_yields_nothing(ledger: PostgresFeeLedger) -> None:
    """`RETURNING` after `DO NOTHING` is the exactly-once guarantee, and this is
    the assertion that it is a guarantee rather than a hope."""
    await ledger.record_unseen(DAY_TWO, run_mode=RunMode.PAPER)

    assert await ledger.record_unseen(DAY_TWO, run_mode=RunMode.PAPER) == []


@pytest.mark.asyncio
async def test_a_mostly_known_batch_returns_only_the_new_charge(
    ledger: PostgresFeeLedger,
) -> None:
    """The normal shape of every run after the first: a wide lookback window
    that is nearly all charges already applied, with one new row on the end."""
    await ledger.record_unseen(DAY_TWO, run_mode=RunMode.PAPER)
    todays = FeeActivity(
        activity_id="20260909000000000::a-new-one",
        booked_on=date(2026, 9, 9),
        amount=Decimal("1.19"),
        sub_type="REG",
    )

    unseen = await ledger.record_unseen([*DAY_TWO, todays], run_mode=RunMode.PAPER)

    assert [item.activity_id for item in unseen] == [todays.activity_id]


@pytest.mark.asyncio
async def test_two_workers_racing_hand_the_charge_to_exactly_one(
    ledger: PostgresFeeLedger,
) -> None:
    """`warmup` and the five-minute job can be inside this statement at the same
    instant — a restart during a scheduled reconcile is all it takes. A `SELECT`
    then an `INSERT` would give the charge to both and double-charge cash."""
    both = await asyncio.gather(
        ledger.record_unseen(DAY_TWO, run_mode=RunMode.PAPER),
        ledger.record_unseen(DAY_TWO, run_mode=RunMode.PAPER),
    )

    won = [item for batch in both for item in batch]
    assert len(won) == 3, "three charges, claimed once between the two callers"
    assert {item.activity_id for item in won} == {item.activity_id for item in DAY_TWO}


@pytest.mark.asyncio
async def test_the_amount_survives_the_column_exactly(
    ledger: PostgresFeeLedger, raw: asyncpg.Connection
) -> None:
    """`NUMERIC(20, 8)` pads. What matters is that it does not *round*: the value
    settled against cash has to be the value the venue charged (rule §1.1)."""
    await ledger.record_unseen(DAY_TWO, run_mode=RunMode.PAPER)

    stored = await raw.fetchval(
        "SELECT amount FROM broker_fees WHERE sub_type = 'REG'",
    )

    assert Decimal(stored) == Decimal("3.82")


@pytest.mark.asyncio
async def test_the_row_records_what_it_is_for(
    ledger: PostgresFeeLedger, raw: asyncpg.Connection
) -> None:
    """The table answers "have we charged this?", so the run mode it was charged
    under and the day the venue booked it both have to be on the row — the
    second is not the day we heard about it."""
    await ledger.record_unseen(DAY_TWO, run_mode=RunMode.PAPER)

    row = await raw.fetchrow(
        "SELECT run_mode, booked_on, applied_at FROM broker_fees WHERE sub_type = 'TAF'",
    )

    assert row["run_mode"] == RunMode.PAPER.value
    assert row["booked_on"] == date(2026, 9, 8)
    assert row["applied_at"] == NOW, "from the injected clock, not the wall clock"


@pytest.mark.asyncio
async def test_nothing_offered_is_not_a_round_trip(ledger: PostgresFeeLedger) -> None:
    assert await ledger.record_unseen([], run_mode=RunMode.PAPER) == []
