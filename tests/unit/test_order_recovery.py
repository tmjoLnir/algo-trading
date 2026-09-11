"""Recovering venue events nobody was listening for — the day-3 halt loop.

Day 3 of the paper week ended with a book whose equity was right to two cents
and whose every constituent was wrong: a market BUY of 10 MSFT filled at the
venue while the trade-updates consumer was dead, and the worker then booted 131
times and quarantined 131 times on the same `missing_position` and the same
$4,932.32 cash drift (docs/paper-week/day-3-review.md, F3 and F9).

Nothing in that loop was a reconciler bug. The reconciler was right every time.
What was missing is the step these tests cover: asking the venue what our own
orders did before comparing books.

The assertions that matter are the refusals. A recovery that books everything
the venue says is `adopt_broker_state` with no operator and no audit trail, so
most of what follows drives a venue that reports something we should *not*
adopt and proves the divergence is left standing for layer 7 to halt on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from atp_core.brokers.ports import TradeUpdate
from atp_core.domain import Fill, Order, OrderStatus, OrderType, Portfolio, Position, Side
from atp_core.errors import BrokerConnectionError
from atp_core.execution.recovery import RECOVERED, missed_updates, read_missed_updates
from atp_core.execution.trade_updates import apply_trade_update
from tests.fakes import FakeBroker

NOW = datetime(2026, 9, 11, 0, 27, tzinfo=UTC)
FILLED_AT = datetime(2026, 9, 10, 14, 44, tzinfo=UTC)


def ours(
    *,
    qty: str = "10",
    client_order_id: str = "atp-e82c4109cc2c0c8886b0e0b5",
    broker_order_id: str | None = "venue-1",
    status: OrderStatus = OrderStatus.SUBMITTED,
    symbol: str = "MSFT",
) -> Order:
    """Our copy: submitted, acknowledged, and never heard from again."""
    order = Order(
        symbol=symbol,
        side=Side.BUY,
        qty=Decimal(qty),
        order_type=OrderType.MARKET,
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
    )
    order.status = status
    return order


def theirs(
    *,
    qty: str = "10",
    filled: str = "10",
    avg: str | None = "493.232",
    status: OrderStatus = OrderStatus.FILLED,
    client_order_id: str = "atp-e82c4109cc2c0c8886b0e0b5",
    broker_order_id: str = "venue-1",
    symbol: str = "MSFT",
) -> Order:
    """The venue's copy, as `AlpacaBroker._from_alpaca_order` builds one: one
    synthetic fill for the running total, because REST cannot reconstruct the
    individual prints."""
    order = Order(
        symbol=symbol,
        side=Side.BUY,
        qty=Decimal(qty),
        order_type=OrderType.MARKET,
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
    )
    if Decimal(filled) > 0 and avg is not None:
        order.apply_fill(
            Fill(order_id=order.id, ts=FILLED_AT, qty=Decimal(filled), price=Decimal(avg))
        )
    elif Decimal(filled) > 0:
        # A venue reporting a quantity with no price — the payload the Alpaca
        # adapter refuses. Built by hand because `apply_fill` cannot make one.
        order.filled_qty = Decimal(filled)
        order.avg_fill_price = None
        order.status = OrderStatus.PARTIALLY_FILLED
    if status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
        order.status = status
    return order


class TestTheDayThreeFill:
    """The exact numbers from the halt loop, end to end."""

    def test_a_fill_we_never_heard_becomes_one_update_at_the_venues_price(self) -> None:
        updates = missed_updates(ours(), theirs(), now=NOW, broker_name="alpaca-paper")

        assert len(updates) == 1
        update = updates[0]
        assert update.fill is not None
        assert update.fill.qty == Decimal("10")
        # Exactly the venue's average, not a rounded one: we have booked
        # nothing, so the missing tranche *is* the whole fill.
        assert update.fill.price == Decimal("493.232")
        assert update.event == RECOVERED
        assert update.broker == "alpaca-paper"
        # The venue's own execution time, not now. `Position.opened_at` and
        # every bars-held calculation downstream read it.
        assert update.fill.ts == FILLED_AT

    def test_applying_it_reproduces_the_brokers_book_to_the_cent(self) -> None:
        """100,090.05 − 4,932.32 = 95,157.73, which is what the venue said all
        day while the platform insisted it held only cash."""
        order = ours()
        portfolio = Portfolio(cash=Decimal("100090.05"), starting_equity=Decimal("100090.05"))

        (update,) = missed_updates(order, theirs(), now=NOW)
        assert apply_trade_update(order, update)

        fill = order.fills[-1]
        position = portfolio.position(order.symbol)
        position.apply_fill(fill, fill.qty * order.side.sign)
        portfolio.cash -= fill.qty * fill.price * order.side.sign + fill.fee

        assert portfolio.cash == Decimal("95157.73")
        assert position.qty == Decimal("10")
        assert order.status is OrderStatus.FILLED
        assert order.filled_qty == Decimal("10")

    def test_the_order_stops_being_working(self) -> None:
        """F9's symptom: 131 boots re-adopted an order that no longer existed."""
        order = ours()
        (update,) = missed_updates(order, theirs(), now=NOW)
        apply_trade_update(order, update)
        assert order.is_complete


class TestPartialFills:
    def test_only_the_tranche_we_are_missing_is_booked(self) -> None:
        order = ours()
        order.apply_fill(
            Fill(order_id=order.id, ts=FILLED_AT, qty=Decimal("4"), price=Decimal("493.20"))
        )

        (update,) = missed_updates(order, theirs(), now=NOW)

        assert update.fill is not None
        assert update.fill.qty == Decimal("6")

    def test_the_price_is_the_one_that_makes_our_average_match_the_venues(self) -> None:
        """REST reports running totals, so the tranche's price is derived from
        the notional the two books disagree about — never read off the venue's
        average, which describes the whole order and not the part we missed."""
        order = ours()
        order.apply_fill(
            Fill(order_id=order.id, ts=FILLED_AT, qty=Decimal("4"), price=Decimal("493.20"))
        )

        (update,) = missed_updates(order, theirs(), now=NOW)
        apply_trade_update(order, update)

        assert order.filled_qty == Decimal("10")
        assert order.avg_fill_price is not None
        # The residue is `Decimal` division at 28 significant digits, twenty
        # orders of magnitude below a cent — not float error in a ledger.
        assert abs(order.avg_fill_price - Decimal("493.232")) < Decimal("0.0000001")

    def test_a_partial_that_was_cancelled_books_the_fill_then_the_cancel(self) -> None:
        """Order matters: `state.transition` refuses a fill from `CANCELLED`,
        so a cancel applied first would strand the shares at the venue."""
        order = ours()
        updates = missed_updates(
            order, theirs(filled="4", avg="493.20", status=OrderStatus.CANCELLED), now=NOW
        )

        assert [u.fill is not None for u in updates] == [True, False]
        assert updates[1].status is OrderStatus.CANCELLED

        for update in updates:
            apply_trade_update(order, update)
        assert order.filled_qty == Decimal("4")
        assert order.status is OrderStatus.CANCELLED


class TestWhatItRefusesToAdopt:
    """The line between recovering our own order and adopting the venue's book."""

    def test_a_venue_that_reports_less_filled_than_we_have_booked_is_left_alone(self) -> None:
        """Neither side can be shown wrong, and the guess that "corrects" our
        book downwards releases buying power against a position that may be
        real. Reported by the reconciler instead, which halts."""
        order = ours()
        order.apply_fill(
            Fill(order_id=order.id, ts=FILLED_AT, qty=Decimal("10"), price=Decimal("493.232"))
        )

        assert missed_updates(order, theirs(filled="4", avg="493.20"), now=NOW) == []

    def test_a_filled_quantity_with_no_average_price_books_nothing(self) -> None:
        assert missed_updates(ours(), theirs(avg=None), now=NOW) == []

    def test_a_refused_fill_does_not_retire_the_order_with_a_status(self) -> None:
        """The status alone would take the order out of the working set and
        with it the only handle on the unbooked quantity — so the next boot
        would find a bare `missing_position` and not even try."""
        assert missed_updates(ours(), theirs(avg=None, status=OrderStatus.CANCELLED), now=NOW) == []

    def test_a_tranche_that_prices_at_or_below_zero_books_nothing(self) -> None:
        """Our notional exceeds the venue's on a larger quantity, so the
        difference divides to a non-positive price. A number like that in a
        P&L ledger is indistinguishable from a real one afterwards."""
        order = ours(qty="20")
        order.apply_fill(
            Fill(order_id=order.id, ts=FILLED_AT, qty=Decimal("4"), price=Decimal("900"))
        )

        assert missed_updates(order, theirs(qty="20", filled="10", avg="300"), now=NOW) == []

    def test_a_status_the_fill_owns_is_never_asserted_separately(self) -> None:
        """`Order.apply_fill` sets FILLED and PARTIALLY_FILLED from the
        arithmetic. A status event carrying one would be asserting something
        only the fill can know."""
        updates = missed_updates(ours(), theirs(), now=NOW)
        assert [u.status for u in updates] == [None]


class TestReadingTheVenue:
    @pytest.mark.asyncio
    async def test_it_asks_only_about_orders_we_submitted(self) -> None:
        """An order working at the venue that we do not know about is an
        orphan, and stays one. Nothing here reads the venue's order list."""
        broker = FakeBroker()
        broker.accepted["atp-someone-else"] = theirs(
            client_order_id="atp-someone-else", broker_order_id="venue-9"
        )

        assert await read_missed_updates(broker, [], now=NOW) == []

    @pytest.mark.asyncio
    async def test_an_order_the_venue_never_acknowledged_is_reported_not_guessed(self) -> None:
        """No venue id to ask about. Resolving it belongs to the router's
        submit path (rule §1.4), not here."""
        broker = FakeBroker()
        assert await read_missed_updates(broker, [ours(broker_order_id=None)], now=NOW) == []

    @pytest.mark.asyncio
    async def test_a_venue_that_will_not_answer_defers_to_the_reconcile(self) -> None:
        """Layer 7's own failure mode, and layer 7 is where it is decided: the
        reconcile that follows halts on BROKER_UNREACHABLE. Raising here would
        produce a second outage for the same cause."""
        broker = FakeBroker()
        broker.accepted["atp-e82c4109cc2c0c8886b0e0b5"] = theirs()
        broker.reads_fail = True

        assert await read_missed_updates(broker, [ours()], now=NOW) == []

    @pytest.mark.asyncio
    async def test_an_id_that_answers_with_a_different_order_books_nothing(self) -> None:
        """Applying that fill would move a position the order never touched."""
        broker = FakeBroker()
        broker.accepted["atp-other"] = theirs(
            client_order_id="atp-other", broker_order_id="venue-1"
        )

        assert await read_missed_updates(broker, [ours()], now=NOW) == []

    @pytest.mark.asyncio
    async def test_it_recovers_the_fill_for_an_order_the_venue_has_completed(self) -> None:
        broker = FakeBroker()
        broker.accepted["atp-e82c4109cc2c0c8886b0e0b5"] = theirs()

        (update,) = await read_missed_updates(broker, [ours()], now=NOW)

        assert update.fill is not None
        assert update.fill.qty == Decimal("10")
        assert update.client_order_id == "atp-e82c4109cc2c0c8886b0e0b5"


class TestRunningItTwice:
    """It runs on every boot and every reconnect, so it has to be idempotent —
    a double-counted fill is a double-counted position."""

    @pytest.mark.asyncio
    async def test_a_second_pass_over_an_unchanged_venue_books_nothing_new(self) -> None:
        broker = FakeBroker()
        broker.accepted["atp-e82c4109cc2c0c8886b0e0b5"] = theirs()
        order = ours()

        for update in await read_missed_updates(broker, [order], now=NOW):
            apply_trade_update(order, update)
        first = order.filled_qty

        # The venue's answer has not changed, and neither has ours now.
        assert await read_missed_updates(broker, [order], now=NOW) == []
        assert order.filled_qty == first == Decimal("10")

    def test_the_same_recovery_replayed_is_discarded_as_a_duplicate(self) -> None:
        """The deterministic `venue_fill_id` is what makes the replay safe, not
        the caller remembering it already ran."""
        order = ours()
        (update,) = missed_updates(order, theirs(), now=NOW)

        assert apply_trade_update(order, update) is True
        assert apply_trade_update(order, update) is False
        assert order.filled_qty == Decimal("10")

    def test_a_venue_that_filled_more_since_the_last_pass_is_booked(self) -> None:
        """The id is keyed on the venue's cumulative total, so a later, larger
        total is a different event rather than a suppressed one."""
        order = ours()
        (first,) = missed_updates(order, theirs(filled="4", avg="493.20"), now=NOW)
        apply_trade_update(order, first)

        (second,) = missed_updates(order, theirs(), now=NOW)
        assert second.fill is not None
        assert second.fill.venue_fill_id != first.fill.venue_fill_id  # type: ignore[union-attr]
        apply_trade_update(order, second)
        assert order.filled_qty == Decimal("10")


class TestItIsNotAdoption:
    @pytest.mark.asyncio
    async def test_a_broker_position_with_no_order_of_ours_is_not_touched(self) -> None:
        """`adopt_broker_state` is the thing that takes the venue's word for a
        position, it is manual on purpose, and this is not it."""
        broker = FakeBroker()
        broker.positions["AAPL"] = Position(
            symbol="AAPL",
            qty=Decimal("50"),
            avg_entry_price=Decimal("200"),
            last_price=Decimal("200"),
        )

        assert await read_missed_updates(broker, [], now=NOW) == []


def test_broker_connection_error_is_a_broker_error() -> None:
    """The per-order swallow above is typed on `BrokerError`; this is the
    subclass an unreachable venue actually raises."""
    from atp_core.errors import BrokerError

    assert issubclass(BrokerConnectionError, BrokerError)


def test_every_recovered_update_is_labelled_as_reconstructed() -> None:
    """`event` is documented as the venue's own event name. These were not
    delivered, and a reader of the log should be able to tell."""
    updates = missed_updates(
        ours(), theirs(filled="4", avg="493.20", status=OrderStatus.CANCELLED), now=NOW
    )
    assert {u.event for u in updates} == {RECOVERED}
    assert all(isinstance(u, TradeUpdate) for u in updates)
