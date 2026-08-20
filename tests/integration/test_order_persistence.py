"""Order and book storage against a real PostgreSQL.

These cannot be unit tests. What is worth checking is behaviour of the database
rather than of Python: whether `ON CONFLICT` actually merges instead of
duplicating, whether a NUMERIC column hands back the `Decimal` that went in,
and whether a snapshot written as several rows reads back as one coherent book.

The repositories are the last thing between a restart and a book it invented,
so the tests that matter are the idempotence ones — saving twice must not
double a fill, and a snapshot must not be readable half-written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import asyncpg
import pytest

from atp_core.domain import (
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Portfolio,
    Position,
    RunMode,
    Side,
    TimeInForce,
)
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.orders import PostgresOrderRepository
from atp_core.persistence.positions import PostgresPortfolioRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

T0 = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


@pytest.fixture
async def clean_execution_tables(migrated_db: str) -> AsyncIterator[str]:
    """Empty `fills`, `orders`, and both snapshot tables.

    Truncated before rather than after, so a failed test leaves its rows behind
    to be inspected. `fills` first: it carries a foreign key to `orders`.
    """
    conn = await asyncpg.connect(migrated_db.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute(
            "TRUNCATE TABLE fills, orders, position_snapshots, equity_snapshots CASCADE"
        )
    finally:
        await conn.close()
    yield migrated_db


@pytest.fixture
async def orders(clean_execution_tables: str) -> AsyncIterator[PostgresOrderRepository]:
    engine = create_engine(clean_execution_tables)
    try:
        yield PostgresOrderRepository(create_session_factory(engine))
    finally:
        await engine.dispose()


@pytest.fixture
async def book(clean_execution_tables: str) -> AsyncIterator[PostgresPortfolioRepository]:
    engine = create_engine(clean_execution_tables)
    try:
        yield PostgresPortfolioRepository(create_session_factory(engine))
    finally:
        await engine.dispose()


def an_order(client_order_id: str = "atp-1", qty: str = "100") -> Order:
    return Order(
        symbol="SPY",
        side=Side.BUY,
        qty=Decimal(qty),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
        broker_order_id="brk-1",
        created_at=T0,
        submitted_at=T0,
    )


def a_fill(order: Order, qty: str, price: str, venue_fill_id: str | None = "exec-1") -> Fill:
    return Fill(
        order_id=order.id,
        ts=T0,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0.01"),
        venue_fill_id=venue_fill_id,
    )


class TestOrderRoundTrip:
    @pytest.mark.asyncio
    async def test_a_working_order_comes_back(self, orders: PostgresOrderRepository) -> None:
        order = an_order()
        order.status = OrderStatus.SUBMITTED

        await orders.save(order, run_mode=RunMode.PAPER)
        restored = await orders.open_orders(RunMode.PAPER)

        assert [o.client_order_id for o in restored] == ["atp-1"]
        assert restored[0].qty == Decimal("100")
        assert isinstance(restored[0].qty, Decimal)

    @pytest.mark.asyncio
    async def test_fills_replay_into_the_same_average(
        self, orders: PostgresOrderRepository
    ) -> None:
        """Rebuilt through `apply_fill` rather than assigned, so the stored
        average has to agree with the stored prints.

        Left partially filled deliberately. `open_orders` is the only reader,
        and it excludes terminal orders — filling a 100-share order 40 then 60
        makes it FILLED, and asking that reader for it back is asking the wrong
        question rather than finding a bug. The average being checked is the
        same arithmetic at either size.
        """
        order = an_order(qty="200")
        order.status = OrderStatus.SUBMITTED
        order.apply_fill(a_fill(order, "40", "100", "e1"))
        order.apply_fill(a_fill(order, "60", "110", "e2"))
        expected = order.avg_fill_price
        assert order.status is OrderStatus.PARTIALLY_FILLED

        await orders.save(order, run_mode=RunMode.PAPER)
        restored = (await orders.open_orders(RunMode.PAPER))[0]

        assert len(restored.fills) == 2
        assert restored.filled_qty == Decimal("100")
        assert restored.avg_fill_price == expected == Decimal("106")
        assert restored.status is OrderStatus.PARTIALLY_FILLED

    @pytest.mark.asyncio
    async def test_a_terminal_order_is_not_returned_as_working(
        self, orders: PostgresOrderRepository
    ) -> None:
        order = an_order()
        order.status = OrderStatus.CANCELLED

        await orders.save(order, run_mode=RunMode.PAPER)

        assert await orders.open_orders(RunMode.PAPER) == []

    @pytest.mark.asyncio
    async def test_a_completed_order_keeps_its_prints_though_no_reader_returns_it(
        self, orders: PostgresOrderRepository, clean_execution_tables: str
    ) -> None:
        """A filled order is written and then never handed back. That is the
        intended shape, and it is stated here because it does not look like it.

        `open_orders` is the port's only reader because the port exists to
        restore what is still working, and a completed order needs no
        restoring — its effect is already in the position snapshot. The prints
        are still stored: they are the audit trail P&L is reconstructed from,
        so this asserts against the table rather than through a reader that is
        correct to exclude them.

        What this deliberately does not claim: that the order row and the book
        cannot disagree. A crash between booking a fill and snapshotting the
        book leaves a FILLED order whose position was never written, and
        nothing here replays it. Reconciliation against the venue is what
        catches that, which is why it runs at boot.
        """
        order = an_order()
        order.status = OrderStatus.SUBMITTED
        order.apply_fill(a_fill(order, "40", "100", "e1"))
        order.apply_fill(a_fill(order, "60", "110", "e2"))
        assert order.status is OrderStatus.FILLED

        await orders.save(order, run_mode=RunMode.PAPER)
        await orders.save(order, run_mode=RunMode.PAPER)

        assert await orders.open_orders(RunMode.PAPER) == []

        conn = await asyncpg.connect(
            clean_execution_tables.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            prints = await conn.fetch(
                "SELECT qty, price FROM fills WHERE order_id = $1 ORDER BY qty", order.id
            )
            status = await conn.fetchval("SELECT status FROM orders WHERE id = $1", order.id)
        finally:
            await conn.close()

        # Twice saved, printed once each: the derived fill id is what stops the
        # second save from doubling the audit trail.
        assert [(row["qty"], row["price"]) for row in prints] == [
            (Decimal("40"), Decimal("100")),
            (Decimal("60"), Decimal("110")),
        ]
        assert status == OrderStatus.FILLED.value

    @pytest.mark.asyncio
    async def test_paper_and_live_do_not_see_each_other(
        self, orders: PostgresOrderRepository
    ) -> None:
        """They share a table. A paper order counted as live would be an orphan
        that never resolves."""
        paper, live = an_order("atp-paper"), an_order("atp-live")
        paper.status = live.status = OrderStatus.SUBMITTED

        await orders.save(paper, run_mode=RunMode.PAPER)
        await orders.save(live, run_mode=RunMode.LIVE)

        assert [o.client_order_id for o in await orders.open_orders(RunMode.PAPER)] == ["atp-paper"]
        assert [o.client_order_id for o in await orders.open_orders(RunMode.LIVE)] == ["atp-live"]


class TestSavingTwice:
    """The property the whole design rests on: an order is saved repeatedly as
    it fills, and none of those saves may duplicate anything."""

    @pytest.mark.asyncio
    async def test_re_saving_does_not_duplicate_the_order(
        self, orders: PostgresOrderRepository
    ) -> None:
        order = an_order()
        order.status = OrderStatus.SUBMITTED

        await orders.save(order, run_mode=RunMode.PAPER)
        await orders.save(order, run_mode=RunMode.PAPER)

        assert len(await orders.open_orders(RunMode.PAPER)) == 1

    @pytest.mark.asyncio
    async def test_re_saving_does_not_duplicate_fills(
        self, orders: PostgresOrderRepository
    ) -> None:
        """A double-counted fill is a double-counted position."""
        order = an_order()
        order.status = OrderStatus.SUBMITTED
        order.apply_fill(a_fill(order, "40", "100", "e1"))

        await orders.save(order, run_mode=RunMode.PAPER)
        await orders.save(order, run_mode=RunMode.PAPER)

        restored = (await orders.open_orders(RunMode.PAPER))[0]
        assert len(restored.fills) == 1
        assert restored.filled_qty == Decimal("40")

    @pytest.mark.asyncio
    async def test_a_later_save_carries_the_new_fill(self, orders: PostgresOrderRepository) -> None:
        """Partially filled for the same reason as the replay test above: the
        order has to still be working for `open_orders` to hand it back."""
        order = an_order(qty="200")
        order.status = OrderStatus.SUBMITTED
        order.apply_fill(a_fill(order, "40", "100", "e1"))
        await orders.save(order, run_mode=RunMode.PAPER)

        order.apply_fill(a_fill(order, "60", "110", "e2"))
        await orders.save(order, run_mode=RunMode.PAPER)

        restored = (await orders.open_orders(RunMode.PAPER))[0]
        assert len(restored.fills) == 2
        assert restored.filled_qty == Decimal("100")
        assert restored.avg_fill_price == Decimal("106")

    @pytest.mark.asyncio
    async def test_the_status_moves_on_a_re_save(self, orders: PostgresOrderRepository) -> None:
        order = an_order()
        order.status = OrderStatus.SUBMITTED
        await orders.save(order, run_mode=RunMode.PAPER)

        order.status = OrderStatus.CANCELLED
        await orders.save(order, run_mode=RunMode.PAPER)

        assert await orders.open_orders(RunMode.PAPER) == []


class TestBookSnapshots:
    @staticmethod
    def a_portfolio() -> Portfolio:
        portfolio = Portfolio(cash=Decimal("50000.25"), starting_equity=Decimal("100000"))
        portfolio.positions["SPY"] = Position(
            symbol="SPY",
            qty=Decimal("100"),
            avg_entry_price=Decimal("500.125"),
            last_price=Decimal("512.30"),
            realized_pnl=Decimal("12.50"),
            fees_paid=Decimal("1.25"),
            opened_at=T0,
            stop_loss_price=Decimal("480"),
            take_profit_price=Decimal("560"),
            high_water_mark=Decimal("515"),
        )
        return portfolio

    @pytest.mark.asyncio
    async def test_nothing_stored_reads_as_none(self, book: PostgresPortfolioRepository) -> None:
        """A first boot, and not an error — the caller adopts the broker's book
        on this answer, so it must be distinguishable from a failure."""
        assert await book.latest(RunMode.PAPER) is None

    @pytest.mark.asyncio
    async def test_a_book_round_trips(self, book: PostgresPortfolioRepository) -> None:
        await book.snapshot(self.a_portfolio(), at=T0, run_mode=RunMode.PAPER)

        restored = await book.latest(RunMode.PAPER)

        assert restored is not None
        assert restored.cash == Decimal("50000.25")
        assert restored.positions["SPY"].qty == Decimal("100")
        assert restored.positions["SPY"].avg_entry_price == Decimal("500.125")

    @pytest.mark.asyncio
    async def test_every_protective_level_survives(self, book: PostgresPortfolioRepository) -> None:
        """The reason the migration exists. A trailing stop reloaded without its
        high-water mark re-anchors on the current bar, and the monotonicity
        invariant then holds around a mark that has moved down."""
        await book.snapshot(self.a_portfolio(), at=T0, run_mode=RunMode.PAPER)

        restored = await book.latest(RunMode.PAPER)

        assert restored is not None
        position = restored.positions["SPY"]
        assert position.stop_loss_price == Decimal("480")
        assert position.take_profit_price == Decimal("560")
        assert position.high_water_mark == Decimal("515")
        assert position.opened_at == T0
        assert position.fees_paid == Decimal("1.25")

    @pytest.mark.asyncio
    async def test_the_newest_snapshot_wins(self, book: PostgresPortfolioRepository) -> None:
        first = self.a_portfolio()
        await book.snapshot(first, at=T0, run_mode=RunMode.PAPER)
        later = self.a_portfolio()
        later.cash = Decimal("9999")
        await book.snapshot(later, at=T0 + timedelta(minutes=1), run_mode=RunMode.PAPER)

        restored = await book.latest(RunMode.PAPER)

        assert restored is not None
        assert restored.cash == Decimal("9999")

    @pytest.mark.asyncio
    async def test_a_read_never_mixes_two_snapshots(
        self, book: PostgresPortfolioRepository
    ) -> None:
        """Every row of one snapshot shares its timestamp, which is what makes
        a coherent read possible at all."""
        first = self.a_portfolio()
        first.positions["QQQ"] = Position(
            symbol="QQQ",
            qty=Decimal("50"),
            avg_entry_price=Decimal("400"),
            last_price=Decimal("400"),
        )
        await book.snapshot(first, at=T0, run_mode=RunMode.PAPER)

        later = self.a_portfolio()  # QQQ has since been closed
        await book.snapshot(later, at=T0 + timedelta(minutes=1), run_mode=RunMode.PAPER)

        restored = await book.latest(RunMode.PAPER)

        assert restored is not None
        assert set(restored.positions) == {"SPY"}, "the closed position must not reappear"

    @pytest.mark.asyncio
    async def test_a_flat_position_is_not_snapshotted(
        self, book: PostgresPortfolioRepository
    ) -> None:
        portfolio = self.a_portfolio()
        portfolio.positions["QQQ"] = Position(symbol="QQQ", qty=Decimal(0))

        await book.snapshot(portfolio, at=T0, run_mode=RunMode.PAPER)
        restored = await book.latest(RunMode.PAPER)

        assert restored is not None
        assert "QQQ" not in restored.positions

    @pytest.mark.asyncio
    async def test_paper_and_live_books_are_separate(
        self, book: PostgresPortfolioRepository
    ) -> None:
        paper = self.a_portfolio()
        live = self.a_portfolio()
        live.cash = Decimal("777")

        await book.snapshot(paper, at=T0, run_mode=RunMode.PAPER)
        await book.snapshot(live, at=T0, run_mode=RunMode.LIVE)

        paper_book = await book.latest(RunMode.PAPER)
        live_book = await book.latest(RunMode.LIVE)
        assert paper_book is not None and live_book is not None
        assert paper_book.cash == Decimal("50000.25")
        assert live_book.cash == Decimal("777")


def a_portfolio(cash: str = "1000") -> Portfolio:
    """A book worth exactly 1000 of exposure, so the arithmetic below is legible."""
    book = Portfolio(cash=Decimal(cash), starting_equity=Decimal("10000"))
    book.positions["SPY"] = Position(
        symbol="SPY",
        qty=Decimal("10"),
        avg_entry_price=Decimal("95"),
        last_price=Decimal("100"),
        opened_at=T0,
    )
    return book


class TestEquityHistory:
    """The series the dashboard's chart is drawn from, and the day-P&L anchor.

    Written by `snapshot` on every runner pass and read back here. Worth an
    integration test rather than a unit one because the whole of it is SQL:
    an inclusive `BETWEEN`, an ascending order, and a `run_mode` filter — each
    of which is a one-character mistake away from a chart that quietly omits its
    newest point or mixes a paper account into a live one.
    """

    @pytest.mark.asyncio
    async def test_nothing_recorded_reads_as_an_empty_series(
        self, book: PostgresPortfolioRepository
    ) -> None:
        history = await book.equity_history(
            RunMode.PAPER, start=T0 - timedelta(days=1), end=T0 + timedelta(days=1)
        )

        assert history == []

    @pytest.mark.asyncio
    async def test_points_come_back_oldest_first(self, book: PostgresPortfolioRepository) -> None:
        """`downsample` keeps the last point in each bucket, so an unordered
        series would hand the chart whichever point the planner returned last."""
        for minutes in (30, 0, 15):
            await book.snapshot(
                a_portfolio(cash=str(1000 + minutes)),
                at=T0 + timedelta(minutes=minutes),
                run_mode=RunMode.PAPER,
            )

        history = await book.equity_history(RunMode.PAPER, start=T0, end=T0 + timedelta(hours=1))

        assert [p.ts for p in history] == sorted(p.ts for p in history)
        assert len(history) == 3

    @pytest.mark.asyncio
    async def test_both_ends_of_the_window_are_included(
        self, book: PostgresPortfolioRepository
    ) -> None:
        """The arguments are instants a caller computed from a session's bounds.
        A range that quietly dropped its last point would put the wrong number
        under "day P&L"."""
        await book.snapshot(a_portfolio(), at=T0, run_mode=RunMode.PAPER)
        await book.snapshot(a_portfolio(), at=T0 + timedelta(hours=1), run_mode=RunMode.PAPER)

        history = await book.equity_history(RunMode.PAPER, start=T0, end=T0 + timedelta(hours=1))

        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_a_point_outside_the_window_is_excluded(
        self, book: PostgresPortfolioRepository
    ) -> None:
        """Yesterday's close is not today's open. Anchoring day P&L on it would
        report the overnight gap as part of the day's move."""
        await book.snapshot(a_portfolio(), at=T0 - timedelta(days=1), run_mode=RunMode.PAPER)
        await book.snapshot(a_portfolio(), at=T0, run_mode=RunMode.PAPER)

        history = await book.equity_history(RunMode.PAPER, start=T0, end=T0 + timedelta(hours=1))

        assert [p.ts for p in history] == [T0]

    @pytest.mark.asyncio
    async def test_run_modes_do_not_share_a_series(self, book: PostgresPortfolioRepository) -> None:
        await book.snapshot(a_portfolio(), at=T0, run_mode=RunMode.PAPER)

        assert await book.equity_history(RunMode.LIVE, start=T0, end=T0) == []

    @pytest.mark.asyncio
    async def test_equity_and_exposure_survive_exactly(
        self, book: PostgresPortfolioRepository
    ) -> None:
        """`NUMERIC`, not `DOUBLE PRECISION` (rule §1.1). The fractional cash
        below is the part a float would round."""
        await book.snapshot(a_portfolio(cash="1234.56789"), at=T0, run_mode=RunMode.PAPER)

        point = (await book.equity_history(RunMode.PAPER, start=T0, end=T0))[0]

        assert point.cash == Decimal("1234.56789")
        assert point.gross_exposure == Decimal("1000")
        assert point.equity == Decimal("2234.56789")


class TestTheHistoryRead:
    """`recent_orders` — the display read behind `GET /api/v1/orders`.

    Its shape is the opposite of `filled_orders` in two ways that only a real
    database can confirm: it comes back newest-first, and it is bounded. Both
    are safe here and would be bugs there, so both are worth pinning against the
    SQL rather than against a fake that was written to agree with it.
    """

    async def _store(
        self,
        orders: PostgresOrderRepository,
        client_order_id: str,
        *,
        created_at: datetime,
        status: OrderStatus = OrderStatus.FILLED,
        symbol: str = "SPY",
        strategy_id: str | None = None,
        reject_reason: str | None = None,
        run_mode: RunMode = RunMode.PAPER,
    ) -> Order:
        order = Order(
            symbol=symbol,
            side=Side.BUY,
            qty=Decimal("10"),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
            created_at=created_at,
            strategy_id=strategy_id,
        )
        order.status = status
        order.reject_reason = reject_reason
        await orders.save(order, run_mode=run_mode)
        return order

    @pytest.mark.asyncio
    async def test_newest_first(self, orders: PostgresOrderRepository) -> None:
        """The reverse of `filled_orders`, which FIFO matching needs oldest-first.

        Nothing here is matched against anything — this is a list a person reads
        from the top, and the top is what just happened.
        """
        await self._store(orders, "atp-old", created_at=T0)
        await self._store(orders, "atp-mid", created_at=T0 + timedelta(hours=1))
        await self._store(orders, "atp-new", created_at=T0 + timedelta(hours=2))

        rows = await orders.recent_orders(RunMode.PAPER)

        assert [o.client_order_id for o in rows] == ["atp-new", "atp-mid", "atp-old"]

    @pytest.mark.asyncio
    async def test_an_order_that_never_filled_comes_back(
        self, orders: PostgresOrderRepository
    ) -> None:
        """The row this read exists for.

        `filled_orders` selects on `filled_qty > 0` and so cannot return this
        one; the book never held it and no round trip contains it. If it is
        absent here it is absent everywhere, and a strategy refused every
        morning looks exactly like a strategy that never placed an order.
        """
        await self._store(
            orders,
            "atp-refused",
            created_at=T0,
            status=OrderStatus.REJECTED_RISK,
            reject_reason="MaxPositionSize: 500 exceeds the 100 limit",
        )

        rows = await orders.recent_orders(RunMode.PAPER)

        assert [o.client_order_id for o in rows] == ["atp-refused"]
        assert rows[0].reject_reason == "MaxPositionSize: 500 exceeds the 100 limit"
        assert await orders.filled_orders(RunMode.PAPER, until=T0 + timedelta(days=1)) == []

    @pytest.mark.asyncio
    async def test_the_limit_keeps_the_newest_rather_than_the_first(
        self, orders: PostgresOrderRepository
    ) -> None:
        """A truncation that kept the oldest rows would be a screen showing the
        account's first week forever."""
        for i in range(5):
            await self._store(orders, f"atp-{i}", created_at=T0 + timedelta(hours=i))

        rows = await orders.recent_orders(RunMode.PAPER, limit=2)

        assert [o.client_order_id for o in rows] == ["atp-4", "atp-3"]

    @pytest.mark.asyncio
    async def test_paper_and_live_do_not_share_a_screen(
        self, orders: PostgresOrderRepository
    ) -> None:
        await self._store(orders, "atp-paper", created_at=T0, run_mode=RunMode.PAPER)
        await self._store(orders, "atp-live", created_at=T0, run_mode=RunMode.LIVE)

        rows = await orders.recent_orders(RunMode.PAPER)

        assert [o.client_order_id for o in rows] == ["atp-paper"]

    @pytest.mark.asyncio
    async def test_the_filters_compose(self, orders: PostgresOrderRepository) -> None:
        await self._store(
            orders, "atp-hit", created_at=T0, status=OrderStatus.REJECTED, strategy_id=None
        )
        await self._store(orders, "atp-wrong-status", created_at=T0, status=OrderStatus.FILLED)
        await self._store(
            orders,
            "atp-wrong-symbol",
            created_at=T0,
            status=OrderStatus.REJECTED,
            symbol="AAPL",
        )
        await self._store(
            orders,
            "atp-too-old",
            created_at=T0 - timedelta(days=2),
            status=OrderStatus.REJECTED,
        )

        rows = await orders.recent_orders(
            RunMode.PAPER,
            status=OrderStatus.REJECTED,
            symbol="SPY",
            since=T0 - timedelta(hours=1),
        )

        assert [o.client_order_id for o in rows] == ["atp-hit"]

    @pytest.mark.asyncio
    async def test_a_symbol_filter_is_uppercased(self, orders: PostgresOrderRepository) -> None:
        """A symbol is always an uppercase ticker (CLAUDE.md §4), and an empty
        table reads as "no such orders" rather than as "no such spelling"."""
        await self._store(orders, "atp-1", created_at=T0, symbol="SPY")

        rows = await orders.recent_orders(RunMode.PAPER, symbol="spy")

        assert [o.client_order_id for o in rows] == ["atp-1"]

    @pytest.mark.asyncio
    async def test_fills_travel_with_the_row(self, orders: PostgresOrderRepository) -> None:
        """The screen shows a filled quantity and an average price, and both are
        rebuilt from the stored prints rather than trusted from a column."""
        order = an_order(client_order_id="atp-filled")
        order.apply_fill(a_fill(order, "40", "100.25", venue_fill_id="exec-a"))
        order.apply_fill(a_fill(order, "60", "100.75", venue_fill_id="exec-b"))
        await orders.save(order, run_mode=RunMode.PAPER)

        row = (await orders.recent_orders(RunMode.PAPER))[0]

        assert row.filled_qty == Decimal("100")
        assert row.avg_fill_price == Decimal("100.55")
