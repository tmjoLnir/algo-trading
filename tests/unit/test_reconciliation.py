"""Reconciliation — docs/SAFETY.md's layer 7.

The tests that matter are the refusals and the halts. A reconciler that reports
"clean" is indistinguishable from one that does nothing, so most of what follows
drives a book that has genuinely drifted and asserts both what is reported *and*
that trading stopped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from atp_core.clock import SimulatedClock
from atp_core.domain import Order, OrderType, Portfolio, Position, Side
from atp_core.errors import BrokerConnectionError
from atp_core.execution.reconciliation import Reconciler
from atp_core.risk.killswitch import HaltReason
from tests.fakes import FakeBroker, FakeKillSwitch

NOW = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


def build(cash: str = "100000") -> tuple[Reconciler, FakeBroker, FakeKillSwitch, Portfolio]:
    broker = FakeBroker()
    switch = FakeKillSwitch()
    reconciler = Reconciler(broker, switch, SimulatedClock(NOW))
    portfolio = Portfolio(cash=Decimal(cash), starting_equity=Decimal(cash))
    return reconciler, broker, switch, portfolio


def hold(portfolio: Portfolio, symbol: str, qty: str, price: str = "500") -> None:
    portfolio.positions[symbol] = Position(
        symbol=symbol,
        qty=Decimal(qty),
        avg_entry_price=Decimal(price),
        last_price=Decimal(price),
    )


def an_order(client_order_id: str = "atp-1", symbol: str = "SPY") -> Order:
    return Order(
        symbol=symbol,
        side=Side.SELL,
        qty=Decimal("100"),
        order_type=OrderType.STOP,
        stop_price=Decimal("480"),
        client_order_id=client_order_id,
    )


class TestACleanBook:
    @pytest.mark.asyncio
    async def test_matching_state_reports_clean_and_does_not_halt(self) -> None:
        reconciler, broker, switch, portfolio = build()
        broker.hold("SPY", Decimal("100"), Decimal("500"))
        hold(portfolio, "SPY", "100")

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert report.is_clean
        assert report.summary() == "clean"
        assert switch.engaged is False

    @pytest.mark.asyncio
    async def test_checked_at_comes_from_the_clock(self) -> None:
        """Never `datetime.now()` — a backtest and production must agree
        (rule §1.2)."""
        reconciler, _, _, portfolio = build()

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert report.checked_at == NOW

    @pytest.mark.asyncio
    async def test_a_flat_local_position_is_not_a_discrepancy(self) -> None:
        """A closed position leaves a zero-quantity entry behind; the broker
        simply stops reporting it. Those two say the same thing."""
        reconciler, _, switch, portfolio = build()
        hold(portfolio, "SPY", "0")

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert report.is_clean
        assert switch.engaged is False


class TestPositionDiscrepancies:
    @pytest.mark.asyncio
    async def test_a_different_quantity_halts_trading(self) -> None:
        """The 100-versus-1,000 case the module docstring is about."""
        reconciler, broker, switch, portfolio = build()
        broker.hold("SPY", Decimal("1000"), Decimal("500"))
        hold(portfolio, "SPY", "100")

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert not report.is_clean
        assert [d.kind for d in report.discrepancies] == ["position_qty"]
        assert report.discrepancies[0].ours == Decimal("100")
        assert report.discrepancies[0].theirs == Decimal("1000")
        assert switch.engaged is True
        assert HaltReason.RECONCILIATION_MISMATCH.value in switch.engagements[0][1]

    @pytest.mark.asyncio
    async def test_a_position_only_the_broker_has(self) -> None:
        reconciler, broker, switch, portfolio = build()
        broker.hold("SPY", Decimal("100"), Decimal("500"))

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert [d.kind for d in report.discrepancies] == ["missing_position"]
        assert switch.engaged is True

    @pytest.mark.asyncio
    async def test_a_position_only_we_have(self) -> None:
        reconciler, _, switch, portfolio = build()
        hold(portfolio, "SPY", "100")

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert [d.kind for d in report.discrepancies] == ["unknown_position"]
        assert switch.engaged is True

    @pytest.mark.asyncio
    async def test_a_long_we_believe_is_a_short_is_caught(self) -> None:
        """Compared on *signed* quantity. Matching on magnitude would call this
        clean, and it is the disagreement that doubles the loss when acted on:
        every exit would be sized in the wrong direction."""
        reconciler, broker, switch, portfolio = build()
        broker.hold("SPY", Decimal("-100"), Decimal("500"))
        hold(portfolio, "SPY", "100")

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert [d.kind for d in report.discrepancies] == ["position_qty"]
        assert switch.engaged is True

    @pytest.mark.asyncio
    async def test_checking_does_not_invent_local_positions(self) -> None:
        """`Portfolio.position()` creates on access. Reading the book through
        it would leave an entry for every symbol the broker holds, so the
        second run would report the drift as fixed."""
        reconciler, broker, _, portfolio = build()
        broker.hold("SPY", Decimal("100"), Decimal("500"))

        first = await reconciler.reconcile(portfolio, known_orders=[], halt_on_mismatch=False)
        second = await reconciler.reconcile(portfolio, known_orders=[], halt_on_mismatch=False)

        assert "SPY" not in portfolio.positions
        assert [d.kind for d in first.discrepancies] == [d.kind for d in second.discrepancies]


class TestOrphanOrders:
    @pytest.mark.asyncio
    async def test_an_order_we_do_not_know_is_reported(self) -> None:
        reconciler, broker, switch, portfolio = build()
        await broker.submit_order(an_order("atp-from-a-previous-life"))

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert report.orphan_order_ids == ["atp-from-a-previous-life"]
        assert switch.engaged is True

    @pytest.mark.asyncio
    async def test_an_orphan_is_never_auto_cancelled(self) -> None:
        """It is most often a protective stop placed before a restart.
        Cancelling it blindly leaves the position it guards naked, which is a
        worse state than the one being reported."""
        reconciler, broker, _, portfolio = build()
        await broker.submit_order(an_order())

        await reconciler.reconcile(portfolio, known_orders=[])

        assert broker.cancelled == []
        assert len(await broker.get_open_orders()) == 1

    @pytest.mark.asyncio
    async def test_an_order_we_know_about_is_not_an_orphan(self) -> None:
        reconciler, broker, switch, portfolio = build()
        ours = an_order("atp-ours")
        await broker.submit_order(ours)

        report = await reconciler.reconcile(portfolio, known_orders=[ours])

        assert report.orphan_order_ids == []
        assert report.is_clean
        assert switch.engaged is False


class TestCash:
    @pytest.mark.asyncio
    async def test_small_drift_is_not_a_discrepancy(self) -> None:
        """Fees settle late and interest accrues daily. Halting on a cent makes
        layer 7 fire constantly and get switched off."""
        reconciler, broker, switch, portfolio = build(cash="100000.40")
        broker.equity = Decimal("100000")

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert report.is_clean
        assert switch.engaged is False

    @pytest.mark.asyncio
    async def test_drift_beyond_the_tolerance_halts(self) -> None:
        """Cash is arithmetic on fills, so a real gap means a fill one of us
        does not know about."""
        reconciler, broker, switch, portfolio = build(cash="95000")
        broker.equity = Decimal("100000")

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert [d.kind for d in report.discrepancies] == ["cash"]
        assert report.discrepancies[0].symbol == ""
        assert switch.engaged is True

    @pytest.mark.asyncio
    async def test_the_tolerance_is_configurable(self) -> None:
        reconciler, broker, _, portfolio = build(cash="99990")
        broker.equity = Decimal("100000")

        report = await reconciler.reconcile(
            portfolio, known_orders=[], halt_on_mismatch=False, cash_tolerance=Decimal("50")
        )

        assert report.is_clean

    @pytest.mark.asyncio
    async def test_equity_is_not_compared(self) -> None:
        """Our marks come from our feed and theirs from theirs. Two feeds a tick
        apart on an open position is not a book discrepancy, and reporting it
        would make this fire on every volatile day."""
        reconciler, broker, switch, portfolio = build()
        broker.hold("SPY", Decimal("100"), Decimal("500"))
        hold(portfolio, "SPY", "100", price="507.25")  # a different mark

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert report.is_clean
        assert switch.engaged is False


class TestHalting:
    @pytest.mark.asyncio
    async def test_halting_can_be_turned_off_for_a_read_only_check(self) -> None:
        """A dashboard asking "are we in sync?" must not be able to halt the
        platform by asking."""
        reconciler, broker, switch, portfolio = build()
        broker.hold("SPY", Decimal("100"), Decimal("500"))

        report = await reconciler.reconcile(portfolio, known_orders=[], halt_on_mismatch=False)

        assert not report.is_clean
        assert switch.engaged is False

    @pytest.mark.asyncio
    async def test_the_halt_detail_names_the_symbol(self) -> None:
        """ "3 discrepancies" sends a human to a dashboard; naming SPY sends
        them to the position."""
        reconciler, broker, switch, portfolio = build()
        broker.hold("SPY", Decimal("100"), Decimal("500"))

        await reconciler.reconcile(portfolio, known_orders=[])

        assert "SPY" in switch.engagements[0][3]


class TestABrokerWeCannotRead:
    @pytest.mark.asyncio
    async def test_an_unreachable_broker_halts_rather_than_passing(self) -> None:
        """Layer 7's own failure mode is "reconciliation itself is not
        running". An unverified book is the same thing as a wrong one to
        anyone about to size an order against it."""
        reconciler, broker, switch, portfolio = build()
        broker.reads_fail = True

        with pytest.raises(BrokerConnectionError):
            await reconciler.reconcile(portfolio, known_orders=[])

        assert switch.engaged is True
        assert HaltReason.BROKER_UNREACHABLE.value in switch.engagements[0][1]

    @pytest.mark.asyncio
    async def test_it_still_raises_when_halting_is_off(self) -> None:
        """`halt_on_mismatch=False` says "do not stop trading over a
        disagreement", not "pretend the check ran"."""
        reconciler, broker, switch, portfolio = build()
        broker.reads_fail = True

        with pytest.raises(BrokerConnectionError):
            await reconciler.reconcile(portfolio, known_orders=[], halt_on_mismatch=False)

        assert switch.engaged is False


class TestAdoptingBrokerState:
    @pytest.mark.asyncio
    async def test_it_overwrites_positions_and_cash(self) -> None:
        reconciler, broker, _, portfolio = build(cash="95000")
        broker.equity = Decimal("100000")
        broker.hold("SPY", Decimal("1000"), Decimal("500"))
        hold(portfolio, "QQQ", "50")

        await reconciler.adopt_broker_state(portfolio)

        assert set(portfolio.positions) == {"SPY"}
        assert portfolio.positions["SPY"].qty == Decimal("1000")
        assert portfolio.cash == Decimal("100000")

    @pytest.mark.asyncio
    async def test_an_adopted_position_carries_no_protective_levels(self) -> None:
        """The broker knows a position exists; it does not know the stop we
        intended for it. Inventing one would arm a level no strategy chose."""
        reconciler, broker, _, portfolio = build()
        broker.hold("SPY", Decimal("100"), Decimal("500"))
        hold(portfolio, "SPY", "100")
        portfolio.positions["SPY"].stop_loss_price = Decimal("480")

        await reconciler.adopt_broker_state(portfolio)

        assert portfolio.positions["SPY"].stop_loss_price is None

    @pytest.mark.asyncio
    async def test_adopting_makes_a_mismatched_book_reconcile_clean(self) -> None:
        """The recovery action in docs/RUNBOOK.md, end to end."""
        reconciler, broker, _switch, portfolio = build()
        broker.hold("SPY", Decimal("1000"), Decimal("500"))
        hold(portfolio, "SPY", "100")
        assert not (await reconciler.reconcile(portfolio, known_orders=[])).is_clean

        await reconciler.adopt_broker_state(portfolio)

        assert (await reconciler.reconcile(portfolio, known_orders=[])).is_clean

    @pytest.mark.asyncio
    async def test_it_is_not_something_reconcile_does_on_its_own(self) -> None:
        """Silently adopting hides the bug that caused the drift, and if the
        cause is duplicate submission it is how you do it again tomorrow."""
        reconciler, broker, _, portfolio = build()
        broker.hold("SPY", Decimal("1000"), Decimal("500"))
        hold(portfolio, "SPY", "100")

        await reconciler.reconcile(portfolio, known_orders=[], halt_on_mismatch=False)

        assert portfolio.positions["SPY"].qty == Decimal("100")
