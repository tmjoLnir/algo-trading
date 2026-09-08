"""Reconciliation — docs/SAFETY.md's layer 7.

The tests that matter are the refusals and the halts. A reconciler that reports
"clean" is indistinguishable from one that does nothing, so most of what follows
drives a book that has genuinely drifted and asserts both what is reported *and*
that trading stopped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from atp_core.alerts.ports import Alert

from atp_core.clock import SimulatedClock
from atp_core.domain import Order, OrderType, Portfolio, Position, Side
from atp_core.errors import BrokerConnectionError
from atp_core.execution.reconciliation import DiscrepancyKind, Reconciler
from atp_core.risk.killswitch import HaltReason, RedisKillSwitch
from tests.fakes import FakeBroker, FakeKillSwitch, FakeRedis

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


class TestWhatAMismatchImpugns:
    """ADR 0029. The halt now carries *which positions* the reconcile could not
    prove, and `KillSwitchRule` voids the exit carve-out for exactly those.

    The narrowness is the whole point. `is_clean` is false for a cash drift past
    a dollar and for an orphaned order, and a halt that impugned the book on
    either would refuse every exit and every protective stop across the whole
    account, unattended, every five minutes — day 1's F3 with docs/SAFETY.md's
    layers 5 and 6 failing together.
    """

    def unproven(self, switch: FakeKillSwitch) -> frozenset[str]:
        return switch.halt_state().halts[0].unproven_symbols

    @pytest.mark.asyncio
    async def test_a_quantity_mismatch_names_its_symbol(self) -> None:
        reconciler, broker, switch, portfolio = build()
        broker.hold("SPY", Decimal("1000"), Decimal("500"))
        hold(portfolio, "SPY", "100")

        await reconciler.reconcile(portfolio, known_orders=[])

        assert self.unproven(switch) == frozenset({"SPY"})

    @pytest.mark.asyncio
    async def test_a_position_only_the_broker_has_is_impugned(self) -> None:
        """We believe we hold nothing. Sizing an exit off that belief is sizing
        off zero, which is not a number to trade on when the venue disagrees."""
        reconciler, broker, switch, portfolio = build()
        broker.hold("SPY", Decimal("100"), Decimal("500"))

        await reconciler.reconcile(portfolio, known_orders=[])

        assert self.unproven(switch) == frozenset({"SPY"})

    @pytest.mark.asyncio
    async def test_a_position_only_we_have_is_impugned(self) -> None:
        reconciler, _broker, switch, portfolio = build()
        hold(portfolio, "SPY", "100")

        await reconciler.reconcile(portfolio, known_orders=[])

        assert self.unproven(switch) == frozenset({"SPY"})

    @pytest.mark.asyncio
    async def test_a_cash_drift_halts_and_impugns_nothing(self) -> None:
        """The trap, in the module that would spring it. A dollar of
        late-settling fees is a real discrepancy and a real halt, and it says
        nothing whatever about any quantity."""
        reconciler, broker, switch, portfolio = build(cash="95000")
        broker.equity = Decimal("100000")

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert not report.is_clean
        assert switch.engaged is True
        assert self.unproven(switch) == frozenset()

    @pytest.mark.asyncio
    async def test_an_orphan_order_halts_and_impugns_nothing(self) -> None:
        """This module's own comment calls an orphan "most often a protective
        stop we placed before a restart". Refusing to close the book because a
        stop we already own is working is the wrong way round."""
        reconciler, broker, switch, portfolio = build()
        broker.hold("SPY", Decimal("100"), Decimal("500"))
        hold(portfolio, "SPY", "100")
        await broker.submit_order(an_order("someone-elses-order"))

        report = await reconciler.reconcile(portfolio, known_orders=[])

        assert not report.is_clean
        assert switch.engaged is True
        assert self.unproven(switch) == frozenset()

    @pytest.mark.asyncio
    async def test_only_the_mismatched_symbol_is_impugned(self) -> None:
        """A book of five positions with one bad quantity keeps four closeable.
        This is the property that makes the exception survivable in production."""
        reconciler, broker, switch, portfolio = build()
        for symbol in ("SPY", "QQQ", "IWM"):
            broker.hold(symbol, Decimal("100"), Decimal("500"))
            hold(portfolio, symbol, "100")
        broker.hold("SPY", Decimal("999"), Decimal("500"))

        await reconciler.reconcile(portfolio, known_orders=[])

        assert self.unproven(switch) == frozenset({"SPY"})

    @pytest.mark.asyncio
    async def test_an_unreachable_broker_halts_without_impugning_the_book(self) -> None:
        """The distinction `Impugnment` exists to make. `broker_unreachable`
        from *here* means only that we could not read the venue — an unverified
        book, not a disproven one — while the same reason from
        `OrderRouter._resolve_indeterminate` names the one order whose outcome
        is genuinely unknown. Same `HaltReason`, opposite verdicts, which is why
        the carve-out reads the evidence instead.
        """
        reconciler, broker, switch, portfolio = build()
        hold(portfolio, "SPY", "100")
        broker.reads_fail = True

        with pytest.raises(BrokerConnectionError):
            await reconciler.reconcile(portfolio, known_orders=[])

        assert switch.engaged is True
        assert self.unproven(switch) == frozenset()


class TestTheClassificationIsTotal:
    def test_every_kind_declares_whether_it_impugns_a_position(self) -> None:
        """`assert_never` makes a sixth kind a typecheck failure rather than a
        silent default — but only if something calls the property on all of
        them. Both defaults are wrong: one strands every stop on a benign
        finding, the other lets a flatten through against a quantity nobody can
        vouch for."""
        impugning = {kind for kind in DiscrepancyKind if kind.impugns_position}

        assert impugning == {
            DiscrepancyKind.POSITION_QTY,
            DiscrepancyKind.MISSING_POSITION,
            DiscrepancyKind.UNKNOWN_POSITION,
        }


class TestTheAlertBodyIsNotAnImpugnmentTest:
    """An operator cannot tell what is impugned by reading the halt alert, and
    docs/RUNBOOK.md says so because this test says so.

    `_alert_engaged` carries `report.summary()`, and the summary names a symbol
    for *every* kind of finding — `orphan_order` included, whose symbol is the
    resting order's. So the alert for a protective stop left behind by a restart
    reads `orphan_order: SPY` while SPY is perfectly closeable. An earlier draft
    of the runbook told the operator "if the alert named no symbols, nothing is
    impugned", which gives the opposite answer in exactly this case. The signal
    that works is the *second* alert, or `scripts/halt.py status`.
    """

    class Sink:
        def __init__(self) -> None:
            self.sent: list[Alert] = []

        def send(self, alert: Alert) -> None:
            self.sent.append(alert)

    @pytest.mark.asyncio
    async def test_an_orphan_names_a_symbol_it_does_not_impugn(self) -> None:
        broker, redis, sink = FakeBroker(), FakeRedis(), self.Sink()
        switch = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]
        reconciler = Reconciler(broker, switch, SimulatedClock(NOW))
        portfolio = Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))
        broker.hold("SPY", Decimal("100"), Decimal("500"))
        hold(portfolio, "SPY", "100")
        await broker.submit_order(an_order("a-stop-from-before-the-restart"))

        await reconciler.reconcile(portfolio, known_orders=[])

        assert "SPY" in sink.sent[0].body, "the summary names it — that is the trap"
        assert not switch.halt_state(symbol="SPY").position_is_unproven("SPY")
        assert len(sink.sent) == 1, "and no second alert, which is the signal that works"

    @pytest.mark.asyncio
    async def test_a_real_mismatch_does_send_the_second_alert(self) -> None:
        """The other half. The signal has to fire when it should, or "no second
        alert" means nothing."""
        broker, redis, sink = FakeBroker(), FakeRedis(), self.Sink()
        switch = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]
        reconciler = Reconciler(broker, switch, SimulatedClock(NOW))
        portfolio = Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))
        broker.hold("SPY", Decimal("1000"), Decimal("500"))
        hold(portfolio, "SPY", "100")

        await reconciler.reconcile(portfolio, known_orders=[])

        assert len(sink.sent) == 2
        assert "cannot prove SPY" in sink.sent[1].title
        assert switch.halt_state(symbol="SPY").position_is_unproven("SPY")
