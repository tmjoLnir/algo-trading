"""The venue-fee ledger against a real PostgreSQL.

Not a unit test, and the distinction is the whole point here. `FakeFeeLedger`
implements exactly-once with a Python set, so a unit test that passes against it
proves the *fake* is idempotent and says nothing about the SQL — which is where
the property actually lives:

    INSERT ... ON CONFLICT DO NOTHING RETURNING activity_id

Three things only the database can be asked.

1. **The total spans every charge ever recorded, not the batch just offered.**
   That total is one operand of `total_seen - fees_settled`, and summing only
   the rows in the caller's lookback window would make the figure fall as old
   charges age out — read as a credit owed back to cash, which is drift in the
   direction that looks like diligence.
2. **Re-offering a charge does not double-count it.** `ON CONFLICT DO NOTHING`
   on the venue's own id, asked here rather than of a fake whose idempotency is
   a Python dict.
3. **Two workers racing arrive at the same total.** `Reconciler` runs this at
   `warmup` and on a five-minute schedule, so a restart during a scheduled run
   puts two processes in this statement at once. Neither may see a total that
   counts a charge twice.

`NUMERIC(20, 8)` is checked in passing for the reason its neighbours check it:
the amount is money (rule §1.1), and a column that padded or rounded it would
settle a different number against cash than the venue charged. The sum is
checked as a `Decimal` for the same reason — a total that arrived as a float
would be a rounding error applied to a balance.
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
async def test_the_first_pass_totals_every_charge(ledger: PostgresFeeLedger) -> None:
    total = await ledger.record_seen(DAY_TWO, run_mode=RunMode.PAPER)

    assert total == Decimal("4.14")
    assert isinstance(total, Decimal), "a float here is a rounding error on a balance"


@pytest.mark.asyncio
async def test_the_same_feed_offered_again_does_not_double_the_total(
    ledger: PostgresFeeLedger,
) -> None:
    """The feed is re-read on every reconcile — every five minutes, and again on
    each re-read of a disagreement. A total that grew each time would walk cash
    below the venue's without limit."""
    await ledger.record_seen(DAY_TWO, run_mode=RunMode.PAPER)

    assert await ledger.record_seen(DAY_TWO, run_mode=RunMode.PAPER) == Decimal("4.14")


@pytest.mark.asyncio
async def test_the_total_spans_charges_older_than_the_batch(
    ledger: PostgresFeeLedger,
) -> None:
    """The operand is "everything the venue has ever charged", not "everything
    in this sweep". As a charge ages out of the caller's lookback window the
    total must not fall — the difference would read as a credit owed back."""
    await ledger.record_seen(DAY_TWO, run_mode=RunMode.PAPER)
    todays = FeeActivity(
        activity_id="20260909000000000::a-new-one",
        booked_on=date(2026, 9, 9),
        amount=Decimal("1.19"),
        sub_type="REG",
    )

    total = await ledger.record_seen([todays], run_mode=RunMode.PAPER)

    assert total == Decimal("5.33"), "4.14 already recorded plus 1.19 offered now"


@pytest.mark.asyncio
async def test_an_empty_offer_still_reports_what_is_owed(ledger: PostgresFeeLedger) -> None:
    """The state 2026-09-09 left behind: charges recorded, a book that never
    settled them. If a quiet sweep skipped the ledger the book would stay stuck
    there forever, waiting for a fee that has already been recorded."""
    await ledger.record_seen(DAY_TWO, run_mode=RunMode.PAPER)

    assert await ledger.record_seen([], run_mode=RunMode.PAPER) == Decimal("4.14")


@pytest.mark.asyncio
async def test_an_empty_ledger_owes_nothing_rather_than_raising(
    ledger: PostgresFeeLedger,
) -> None:
    """SUM over no rows is NULL, and NULL is not a number to subtract from cash."""
    assert await ledger.record_seen([], run_mode=RunMode.PAPER) == Decimal(0)


@pytest.mark.asyncio
async def test_paper_money_and_real_money_are_totalled_apart(
    ledger: PostgresFeeLedger,
) -> None:
    """Different accounts with their own fee streams. A live total that included
    the paper account's charges would take real money out of a real book."""
    await ledger.record_seen(DAY_TWO, run_mode=RunMode.PAPER)

    assert await ledger.record_seen([], run_mode=RunMode.LIVE) == Decimal(0)


@pytest.mark.asyncio
async def test_two_workers_racing_agree_on_the_total(ledger: PostgresFeeLedger) -> None:
    """`warmup` and the five-minute job can be inside this statement at the same
    instant — a restart during a scheduled reconcile is all it takes. Neither
    may come away with a total that counts a charge twice."""
    both = await asyncio.gather(
        ledger.record_seen(DAY_TWO, run_mode=RunMode.PAPER),
        ledger.record_seen(DAY_TWO, run_mode=RunMode.PAPER),
    )

    assert list(both) == [Decimal("4.14"), Decimal("4.14")]


@pytest.mark.asyncio
async def test_the_amount_survives_the_column_exactly(
    ledger: PostgresFeeLedger, raw: asyncpg.Connection
) -> None:
    """`NUMERIC(20, 8)` pads. What matters is that it does not *round*: the value
    settled against cash has to be the value the venue charged (rule §1.1)."""
    await ledger.record_seen(DAY_TWO, run_mode=RunMode.PAPER)

    stored = await raw.fetchval("SELECT amount FROM broker_fees WHERE sub_type = 'REG'")

    assert Decimal(stored) == Decimal("3.82")


@pytest.mark.asyncio
async def test_the_row_records_what_it_is_and_when_it_was_seen(
    ledger: PostgresFeeLedger, raw: asyncpg.Connection
) -> None:
    """`seen_at`, not `applied_at`. The column was called the second thing for
    one day and was wrong for all of it — rows stamped 10:25:29 on 2026-09-09
    had never been applied to any durable balance."""
    await ledger.record_seen(DAY_TWO, run_mode=RunMode.PAPER)

    row = await raw.fetchrow(
        "SELECT run_mode, booked_on, seen_at FROM broker_fees WHERE sub_type = 'TAF'"
    )

    assert row["run_mode"] == RunMode.PAPER.value
    assert row["booked_on"] == date(2026, 9, 8)
    assert row["seen_at"] == NOW, "from the injected clock, not the wall clock"
