"""Strategies, signals, and the join that makes an order attributable.

These cannot be unit tests. What is under test is the database's behaviour, not
Python's: whether the foreign keys actually refuse an order naming a decision
that was never recorded, whether `ON CONFLICT` updates a signal's *outcome*
while leaving the decision itself alone, and whether `strategies.id` survives an
`ensure` that runs on every session open without resetting the row.

The foreign keys are the point. `orders.strategy_id` and `orders.signal_id` were
literal `None` in every stored row because their targets did not exist, and a
null is how that gap stayed invisible for four phases. A test that asserted the
columns are *populated* would pass against a schema with the constraints dropped;
these assert that the database refuses the broken case.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import asyncpg
import pytest
import sqlalchemy

from atp_core.clock import SimulatedClock
from atp_core.domain import (
    Fill,
    Order,
    OrderStatus,
    RunMode,
    Side,
    Signal,
    SignalAction,
    StrategyState,
)
from atp_core.execution.idempotency import ENTRY, STOP_LOSS, UNKNOWN_PURPOSE
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.orders import PostgresOrderRepository
from atp_core.persistence.signals import PostgresSignalRepository
from atp_core.persistence.strategies import PostgresStrategyRepository
from atp_core.strategy.ports import SignalOutcome, StrategyRecord

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
STRATEGY = "sma_crossover"


@pytest.fixture
async def clean_decision_tables(migrated_db: str) -> AsyncIterator[str]:
    """Empty everything the decision record touches, children first.

    Truncated before rather than after, so a failed test leaves its rows to be
    inspected. `CASCADE` covers the foreign keys between them.
    """
    conn = await asyncpg.connect(migrated_db.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute("TRUNCATE TABLE fills, orders, signals, strategies CASCADE")
    finally:
        await conn.close()
    yield migrated_db


@pytest.fixture
async def repos(
    clean_decision_tables: str,
) -> AsyncIterator[
    tuple[PostgresStrategyRepository, PostgresSignalRepository, PostgresOrderRepository]
]:
    engine = create_engine(clean_decision_tables)
    try:
        factory = create_session_factory(engine)
        yield (
            PostgresStrategyRepository(factory, SimulatedClock(T0)),
            PostgresSignalRepository(factory),
            PostgresOrderRepository(factory),
        )
    finally:
        await engine.dispose()


def a_record(strategy_id: str = STRATEGY) -> StrategyRecord:
    return StrategyRecord(
        id=strategy_id,
        name=strategy_id,
        kind="coded",
        class_name="SmaCrossover",
        params={"fast": 20, "slow": 50},
        universe=("SPY", "QQQ"),
        timeframe="1d",
    )


def a_signal(
    signal_id: str = "sig-1",
    *,
    action: SignalAction = SignalAction.ENTER_LONG,
    ts: datetime | None = None,
    strategy_id: str = STRATEGY,
) -> Signal:
    return Signal(
        strategy_id=strategy_id,
        symbol="SPY",
        action=action,
        ts=ts or T0,
        id=signal_id,
        reason="SMA(20) crossed above SMA(50)",
        indicators={"sma_fast": Decimal("101.25"), "sma_slow": Decimal("100.10")},
    )


def an_order(
    *,
    client_order_id: str = "atp-1",
    signal_id: str | None = "sig-1",
    strategy_id: str | None = STRATEGY,
    purpose: str = ENTRY,
    filled: bool = True,
    created_at: datetime | None = None,
) -> Order:
    order = Order(
        symbol="SPY",
        side=Side.BUY,
        qty=Decimal("100"),
        client_order_id=client_order_id,
        broker_order_id=f"brk-{client_order_id}",
        strategy_id=strategy_id,
        signal_id=signal_id,
        purpose=purpose,
        created_at=created_at or T0,
        submitted_at=created_at or T0,
    )
    order.status = OrderStatus.SUBMITTED
    if filled:
        order.apply_fill(
            Fill(
                order_id=order.id,
                ts=(created_at or T0) + timedelta(minutes=1),
                qty=Decimal("100"),
                price=Decimal("100"),
                fee=Decimal("1"),
                venue_fill_id=f"exec-{client_order_id}",
            )
        )
    return order


class TestTheStrategyRow:
    @pytest.mark.asyncio
    async def test_it_is_created_and_read_back(self, repos: tuple) -> None:
        strategies, _, _ = repos

        await strategies.ensure(a_record())
        stored = await strategies.get(STRATEGY)

        assert stored is not None
        assert stored.id == STRATEGY
        assert stored.class_name == "SmaCrossover"
        assert stored.params == {"fast": 20, "slow": 50}
        assert stored.universe == ("SPY", "QQQ")

    @pytest.mark.asyncio
    async def test_ensuring_twice_does_not_duplicate_or_reset(self, repos: tuple) -> None:
        """`warmup` calls this at every session open.

        The upsert deliberately touches only `updated_at`, because the row is
        also where a strategy-management API will keep configuration a booting
        worker is not an authority on. An upsert that reset `state` would stop a
        strategy by restarting it.
        """
        strategies, _, _ = repos
        await strategies.ensure(a_record())

        # Second boot, with a worker that thinks it knows better.
        await strategies.ensure(
            StrategyRecord(id=STRATEGY, name=STRATEGY, kind="coded", params={"fast": 999})
        )
        stored = await strategies.get(STRATEGY)

        assert stored is not None
        assert stored.params == {"fast": 20, "slow": 50}

    @pytest.mark.asyncio
    async def test_a_strategy_with_no_row_reads_as_none(self, repos: tuple) -> None:
        strategies, _, _ = repos
        assert await strategies.get("never_registered") is None

    @pytest.mark.asyncio
    async def test_the_database_refuses_a_state_that_is_not_a_rung(
        self, clean_decision_tables: str, repos: tuple
    ) -> None:
        """The guardrail that was missing, tested where it lives.

        `state` was a bare `String(20)`, so `ensure` writing `"active"` — a
        value `StrategyState` has never contained — was accepted silently by
        every layer for four phases. Nothing in Python would have caught it
        either: the repository writes the column, so a type annotation on the
        way out cannot see a bad value going in.

        This is why the fix is a CHECK rather than only a corrected literal. A
        literal can be mistyped again; the column now cannot hold the mistake.
        """
        strategies, _, _ = repos
        await strategies.ensure(a_record())

        engine = create_engine(clean_decision_tables)
        try:
            factory = create_session_factory(engine)
            async with (
                factory() as session,
                pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_strategies_state"),
            ):
                await session.execute(
                    sqlalchemy.text("UPDATE strategies SET state = :bad WHERE id = :id"),
                    {"bad": "active", "id": STRATEGY},
                )
                await session.commit()
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_every_rung_of_the_enum_is_storable(
        self, clean_decision_tables: str, repos: tuple
    ) -> None:
        """The other half, and the one a too-tight constraint would break.

        A CHECK that refused a legitimate rung would be worse than the bug it
        replaced — a strategy could not be promoted at all. Both directions are
        asserted because the constraint is generated from the enum, and a
        generator is exactly the thing that can go wrong in one direction only.
        """
        strategies, _, _ = repos
        await strategies.ensure(a_record())

        engine = create_engine(clean_decision_tables)
        try:
            factory = create_session_factory(engine)
            for rung in StrategyState:
                async with factory() as session:
                    await session.execute(
                        sqlalchemy.text("UPDATE strategies SET state = :rung WHERE id = :id"),
                        {"rung": rung.value, "id": STRATEGY},
                    )
                    await session.commit()
                assert (await strategies.get(STRATEGY)) is not None, rung
        finally:
            await engine.dispose()


class TestListingStrategies:
    """`list_all` — the read behind `GET /api/v1/strategies`.

    A different shape from `get`: it returns the whole row rather than the thin
    record a worker writes, and the two fields that matter most on a screen are
    exactly the two `ensure` treats asymmetrically.
    """

    @pytest.mark.asyncio
    async def test_the_whole_row_comes_back(self, repos: tuple) -> None:
        """`StrategyRecord` is deliberately thin because a worker writes it;
        `StoredStrategy` is what a reader needs."""
        strategies, _, _ = repos
        await strategies.ensure(a_record())

        rows = await strategies.list_all()

        assert len(rows) == 1
        assert rows[0].id == STRATEGY
        assert rows[0].universe == ("SPY", "QQQ")
        assert rows[0].params == {"fast": 20, "slow": 50}
        # Written by `ensure` on a first boot, and never touched again. It is
        # `draft` — the ratchet's first rung — and asserted against the enum
        # rather than a literal, because a literal is exactly what let this
        # column hold `"active"` for four phases (migration e2b6d1a70f93).
        assert rows[0].state == StrategyState.DRAFT
        assert rows[0].state in set(StrategyState), "the stored state must be a real rung"

    @pytest.mark.asyncio
    async def test_a_second_boot_moves_only_the_timestamp(
        self, clean_decision_tables: str, repos: tuple
    ) -> None:
        """What `last_started_at` on the screen actually reports.

        A restart does not edit the row — only its `updated_at` moves — which is
        why the API serves that column under a name that says so rather than one
        that invites "somebody changed this today".

        The second boot needs its own repository because the clock is injected
        at construction: reusing the fixture's would stamp the same instant
        twice and prove nothing.
        """
        strategies, _, _ = repos
        await strategies.ensure(a_record())
        first = (await strategies.list_all())[0]

        engine = create_engine(clean_decision_tables)
        try:
            tomorrow = PostgresStrategyRepository(
                create_session_factory(engine), SimulatedClock(T0 + timedelta(days=1))
            )
            await tomorrow.ensure(a_record())
        finally:
            await engine.dispose()

        second = (await strategies.list_all())[0]
        assert second.created_at == first.created_at
        assert second.updated_at > first.updated_at
        assert second.params == first.params

    @pytest.mark.asyncio
    async def test_the_state_filter_narrows_the_list(self, repos: tuple) -> None:
        strategies, _, _ = repos
        await strategies.ensure(a_record("one"))
        await strategies.ensure(a_record("two"))

        assert len(await strategies.list_all(state=StrategyState.DRAFT)) == 2
        assert await strategies.list_all(state=StrategyState.PAUSED) == []

    @pytest.mark.asyncio
    async def test_nothing_stored_is_an_empty_list(self, repos: tuple) -> None:
        """A worker that has never booted has written no rows — the default
        posture of this platform rather than a fault."""
        strategies, _, _ = repos
        assert await strategies.list_all() == []


class TestTheSignalRecord:
    @pytest.mark.asyncio
    async def test_a_decision_round_trips(self, repos: tuple) -> None:
        strategies, signals, _ = repos
        await strategies.ensure(a_record())

        await signals.save(a_signal(), SignalOutcome(acted_on=True))
        ((stored, outcome),) = await signals.recent()

        assert stored.id == "sig-1"
        assert stored.action is SignalAction.ENTER_LONG
        assert stored.reason == "SMA(20) crossed above SMA(50)"
        assert outcome.acted_on is True

    @pytest.mark.asyncio
    async def test_indicator_values_survive_as_strings(self, repos: tuple) -> None:
        """Rule §1.1 through a JSON column.

        An SMA of closes is denominated in dollars, and JSON has only binary
        floats to carry it. Stored as strings, so `101.25` comes back as
        `101.25` rather than as whatever the nearest double is.
        """
        strategies, signals, _ = repos
        await strategies.ensure(a_record())

        await signals.save(a_signal(), SignalOutcome(acted_on=True))
        ((stored, _),) = await signals.recent()

        assert stored.indicators == {"sma_fast": "101.25", "sma_slow": "100.10"}

    @pytest.mark.asyncio
    async def test_a_refusal_keeps_the_rule_that_refused_it(self, repos: tuple) -> None:
        """A signal the risk engine blocked is what you want when asking why a
        strategy underperformed its backtest."""
        strategies, signals, _ = repos
        await strategies.ensure(a_record())

        await signals.save(
            a_signal(),
            SignalOutcome(
                acted_on=False,
                rejection_reason="daily loss limit reached",
                rejected_by="daily_loss_limit",
            ),
        )
        ((_, outcome),) = await signals.recent()

        assert outcome.acted_on is False
        assert outcome.rejected_by == "daily_loss_limit"
        assert outcome.rejection_reason == "daily loss limit reached"

    @pytest.mark.asyncio
    async def test_re_saving_updates_the_outcome_and_not_the_decision(self, repos: tuple) -> None:
        """One decision whose fate became known, not two decisions."""
        strategies, signals, _ = repos
        await strategies.ensure(a_record())
        await signals.save(a_signal(), SignalOutcome(acted_on=False, rejected_by="kill_switch"))

        await signals.save(a_signal(action=SignalAction.EXIT), SignalOutcome(acted_on=True))
        rows = await signals.recent()

        assert len(rows) == 1
        stored, outcome = rows[0]
        assert outcome.acted_on is True
        assert outcome.rejected_by is None
        # The decision itself is not rewritten — a row whose action changed under
        # one id would be a rewrite of history this write must not express.
        assert stored.action is SignalAction.ENTER_LONG

    @pytest.mark.asyncio
    async def test_a_signal_naming_a_strategy_with_no_row_is_refused(self, repos: tuple) -> None:
        """The foreign key doing its job.

        The alternative — a nullable column and a signal pointing nowhere — is
        exactly the state this whole change exists to end.
        """
        _, signals, _ = repos

        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await signals.save(
                a_signal(strategy_id="never_registered"), SignalOutcome(acted_on=True)
            )

    @pytest.mark.asyncio
    async def test_recent_is_newest_first_and_between_is_oldest_first(self, repos: tuple) -> None:
        """A feed is read backwards; a period report is read forwards."""
        strategies, signals, _ = repos
        await strategies.ensure(a_record())
        for index in range(3):
            await signals.save(
                a_signal(f"sig-{index}", ts=T0 + timedelta(hours=index)),
                SignalOutcome(acted_on=True),
            )

        newest = [s.id for s, _ in await signals.recent()]
        oldest = [s.id for s, _ in await signals.between(T0, T0 + timedelta(hours=5))]

        assert newest == ["sig-2", "sig-1", "sig-0"]
        assert oldest == ["sig-0", "sig-1", "sig-2"]

    @pytest.mark.asyncio
    async def test_between_includes_both_bounds(self, repos: tuple) -> None:
        strategies, signals, _ = repos
        await strategies.ensure(a_record())
        await signals.save(a_signal("sig-a", ts=T0), SignalOutcome(acted_on=True))
        await signals.save(
            a_signal("sig-b", ts=T0 + timedelta(hours=2)), SignalOutcome(acted_on=True)
        )

        found = await signals.between(T0, T0 + timedelta(hours=2))

        assert [s.id for s, _ in found] == ["sig-a", "sig-b"]


class TestTheOrderJoin:
    @pytest.mark.asyncio
    async def test_an_order_stores_the_decision_that_caused_it(self, repos: tuple) -> None:
        """The columns that were hardcoded `None`."""
        strategies, signals, orders = repos
        await strategies.ensure(a_record())
        await signals.save(a_signal(), SignalOutcome(acted_on=True))

        await orders.save(an_order(), run_mode=RunMode.PAPER)
        restored = await orders.filled_orders(RunMode.PAPER, until=T0 + timedelta(days=1))

        assert len(restored) == 1
        assert restored[0].strategy_id == STRATEGY
        assert restored[0].signal_id == "sig-1"

    @pytest.mark.asyncio
    async def test_an_order_naming_a_signal_nobody_recorded_is_refused(self, repos: tuple) -> None:
        """The intended outcome, not a regression.

        A null was how this gap stayed invisible; an integrity error is the
        database saying the caller skipped a write it was supposed to make.
        """
        strategies, _, orders = repos
        await strategies.ensure(a_record())

        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await orders.save(an_order(signal_id="never-recorded"), run_mode=RunMode.PAPER)

    @pytest.mark.asyncio
    async def test_an_order_with_no_strategy_is_still_storable(self, repos: tuple) -> None:
        """A manual order from the dashboard has neither, and both are nullable."""
        _, _, orders = repos

        await orders.save(
            an_order(strategy_id=None, signal_id=None, purpose="manual"),
            run_mode=RunMode.PAPER,
        )
        restored = await orders.filled_orders(RunMode.PAPER, until=T0 + timedelta(days=1))

        assert restored[0].strategy_id is None
        assert restored[0].purpose == "manual"


class TestThePurposeColumn:
    @pytest.mark.asyncio
    async def test_it_round_trips(self, repos: tuple) -> None:
        _, _, orders = repos

        await orders.save(
            an_order(strategy_id=None, signal_id=None, purpose=STOP_LOSS),
            run_mode=RunMode.PAPER,
        )
        restored = await orders.filled_orders(RunMode.PAPER, until=T0 + timedelta(days=1))

        assert restored[0].purpose == STOP_LOSS

    @pytest.mark.asyncio
    async def test_a_row_written_before_the_column_reads_as_unknown(
        self, clean_decision_tables: str, repos: tuple
    ) -> None:
        """Migration `c3f8b2d5e714` left existing rows null, deliberately.

        `Order` refuses an empty purpose, so the fallback has to be *something* —
        and it is `unknown` rather than `entry`, because an exit relabelled as an
        entry lands on the wrong side of a reconstructed round trip. That is a
        wrong number where this is a missing one.
        """
        _, _, orders = repos
        await orders.save(an_order(strategy_id=None, signal_id=None), run_mode=RunMode.PAPER)

        conn = await asyncpg.connect(
            clean_decision_tables.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            await conn.execute("UPDATE orders SET purpose = NULL")
        finally:
            await conn.close()

        restored = await orders.filled_orders(RunMode.PAPER, until=T0 + timedelta(days=1))
        assert restored[0].purpose == UNKNOWN_PURPOSE


class TestReadingHistory:
    @pytest.mark.asyncio
    async def test_only_orders_that_moved_quantity_come_back(self, repos: tuple) -> None:
        """An order that never filled belongs to the signal record, not a trade."""
        _, _, orders = repos
        await orders.save(
            an_order(client_order_id="filled", strategy_id=None, signal_id=None),
            run_mode=RunMode.PAPER,
        )
        await orders.save(
            an_order(
                client_order_id="never-filled",
                strategy_id=None,
                signal_id=None,
                filled=False,
            ),
            run_mode=RunMode.PAPER,
        )

        restored = await orders.filled_orders(RunMode.PAPER, until=T0 + timedelta(days=1))

        assert [o.client_order_id for o in restored] == ["filled"]

    @pytest.mark.asyncio
    async def test_a_cancelled_order_that_filled_first_is_not_dropped(self, repos: tuple) -> None:
        """Filtered on `filled_qty`, not on status.

        A partial fill nobody accounted for is a position the reconstruction
        believes closed when it did not — so a cancel that landed after a
        partial print must still reach the reconstruction.
        """
        _, _, orders = repos
        order = an_order(client_order_id="part", strategy_id=None, signal_id=None, filled=False)
        order.apply_fill(Fill(order_id=order.id, ts=T0, qty=Decimal("40"), price=Decimal("100")))
        order.status = OrderStatus.CANCELLED

        await orders.save(order, run_mode=RunMode.PAPER)
        restored = await orders.filled_orders(RunMode.PAPER, until=T0 + timedelta(days=1))

        assert [o.client_order_id for o in restored] == ["part"]
        assert restored[0].status is OrderStatus.CANCELLED
        assert restored[0].filled_qty == Decimal("40")

    @pytest.mark.asyncio
    async def test_the_other_run_modes_orders_are_not_included(self, repos: tuple) -> None:
        """Paper and live share a table, and mixing them mixes two accounts."""
        _, _, orders = repos
        await orders.save(
            an_order(client_order_id="paper", strategy_id=None, signal_id=None),
            run_mode=RunMode.PAPER,
        )
        await orders.save(
            an_order(client_order_id="backtest", strategy_id=None, signal_id=None),
            run_mode=RunMode.BACKTEST,
        )

        restored = await orders.filled_orders(RunMode.PAPER, until=T0 + timedelta(days=1))

        assert [o.client_order_id for o in restored] == ["paper"]

    @pytest.mark.asyncio
    async def test_until_bounds_the_read_at_the_end(self, repos: tuple) -> None:
        _, _, orders = repos
        await orders.save(
            an_order(client_order_id="early", strategy_id=None, signal_id=None),
            run_mode=RunMode.PAPER,
        )
        await orders.save(
            an_order(
                client_order_id="late",
                strategy_id=None,
                signal_id=None,
                created_at=T0 + timedelta(days=10),
            ),
            run_mode=RunMode.PAPER,
        )

        restored = await orders.filled_orders(RunMode.PAPER, until=T0 + timedelta(days=1))

        assert [o.client_order_id for o in restored] == ["early"]

    @pytest.mark.asyncio
    async def test_oldest_first_by_decision_instant(self, repos: tuple) -> None:
        """The order the FIFO matcher wants.

        An entry decided before an exit is the entry that exit closes, even on
        the rare occasion the venue prints them out of sequence.
        """
        _, _, orders = repos
        for index in reversed(range(3)):
            await orders.save(
                an_order(
                    client_order_id=f"o{index}",
                    strategy_id=None,
                    signal_id=None,
                    created_at=T0 + timedelta(hours=index),
                ),
                run_mode=RunMode.PAPER,
            )

        restored = await orders.filled_orders(RunMode.PAPER, until=T0 + timedelta(days=1))

        assert [o.client_order_id for o in restored] == ["o0", "o1", "o2"]

    @pytest.mark.asyncio
    async def test_filtering_by_strategy(self, repos: tuple) -> None:
        strategies, signals, orders = repos
        await strategies.ensure(a_record("alpha"))
        await strategies.ensure(a_record("beta"))
        await signals.save(a_signal("sig-alpha", strategy_id="alpha"), SignalOutcome(acted_on=True))
        await signals.save(a_signal("sig-beta", strategy_id="beta"), SignalOutcome(acted_on=True))
        await orders.save(
            an_order(client_order_id="a", strategy_id="alpha", signal_id="sig-alpha"),
            run_mode=RunMode.PAPER,
        )
        await orders.save(
            an_order(client_order_id="b", strategy_id="beta", signal_id="sig-beta"),
            run_mode=RunMode.PAPER,
        )

        restored = await orders.filled_orders(
            RunMode.PAPER, until=T0 + timedelta(days=1), strategy_id="alpha"
        )

        assert [o.client_order_id for o in restored] == ["a"]
