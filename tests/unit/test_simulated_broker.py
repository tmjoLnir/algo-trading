"""`SimulatedBroker` — the fill simulator.

The tests that matter here are the ones that would pass if the simulator were
dishonest. A simulator that fills everything instantly at a friendly price
passes any test asserting "the order filled", so most of what follows asserts
the *refusals*: the bar it will not fill on, the quantity it will not pretend
traded, and the exit it takes when a bar is ambiguous.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from atp_core.backtest.costs import ZeroCostModel, alpaca_equities_default
from atp_core.brokers import BrokerPort
from atp_core.brokers.simulated import SimulatedBroker
from atp_core.clock import SimulatedClock
from atp_core.domain import Bar, Order, OrderStatus, OrderType, Quote, Side, Timeframe, TimeInForce
from atp_core.errors import BrokerError, ExecutionError

OPEN = datetime(2024, 6, 3, 13, 30, tzinfo=UTC)
STEP = timedelta(minutes=1)


def bar(
    index: int,
    *,
    o: str,
    h: str,
    low: str,
    c: str,
    volume: str = "100000",
    symbol: str = "SPY",
) -> Bar:
    return Bar(
        symbol=symbol,
        ts=OPEN + index * STEP,
        timeframe=Timeframe.M1,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(volume),
        # No corporate actions in a synthetic series, so the adjusted close is the
        # close. The engine refuses a series with none of them (CLAUDE.md §5).
        adj_close=Decimal(c),
    )


def broker(**kwargs: object) -> SimulatedBroker:
    """A broker whose clock stands at bar 0's close, like the engine's."""
    clock = SimulatedClock(OPEN + STEP)
    return SimulatedBroker(clock=clock, cost_model=ZeroCostModel(), **kwargs)  # type: ignore[arg-type]


def market(qty: str = "100", side: Side = Side.BUY, symbol: str = "SPY") -> Order:
    return Order(symbol=symbol, side=side, qty=Decimal(qty), order_type=OrderType.MARKET)


class TestPortConformance:
    def test_satisfies_the_broker_port(self) -> None:
        assert isinstance(broker(), BrokerPort)


class TestTheOneBarRest:
    """The rule that decides whether this simulator lies about returns."""

    @pytest.mark.asyncio
    async def test_does_not_fill_on_a_bar_that_had_already_opened(self) -> None:
        """The whole point. An order decided on bar 0's close cannot be filled
        at bar 0's open — that price is in the past."""
        sim = broker()
        await sim.submit_order(market())

        assert sim.on_bar(bar(0, o="100", h="101", low="99", c="100")) == []

    @pytest.mark.asyncio
    async def test_fills_at_the_next_bars_open(self) -> None:
        sim = broker()
        await sim.submit_order(market())

        filled = sim.on_bar(bar(1, o="110", h="112", low="109", c="111"))

        assert len(filled) == 1
        assert filled[0].avg_fill_price == Decimal("110")
        assert filled[0].status is OrderStatus.FILLED


class TestAgreementWithTheBacktestEngine:
    """ADR 0006's central claim, tested head to head rather than asserted.

    The two components share `execution.matching.intended_price`, which makes
    them agree by construction — but "by construction" is what everyone says
    right up until the two copies drift. This drives a real `BacktestEngine`
    run and a `SimulatedBroker` over the *same* bars and compares the fills
    that come out, so the claim is pinned by behaviour rather than by an
    import.

    The bars carry `adj_close == close`, which is not incidental. ADR 0017 has
    the engine price off adjusted closes while the simulator, modelling a venue,
    stays on raw ones — so the two agree exactly on a series with no corporate
    action, which is every series a simulator is driven with. A fixture spanning
    a split would be comparing two different price spaces and failing for a
    reason that is not drift.
    """

    @pytest.mark.asyncio
    async def test_the_engine_and_the_simulator_fill_at_the_same_prices(self) -> None:
        from atp_core.domain import SignalAction
        from tests.unit.test_backtest_engine import ScriptedStrategy, engine, ramp

        bars = ramp(20)
        strategy = ScriptedStrategy({5: SignalAction.ENTER_LONG, 12: SignalAction.EXIT})
        result = engine(strategy).run({"TEST": bars})

        engine_fills = [
            (order.side, order.avg_fill_price) for order in result.orders if order.filled_qty > 0
        ]
        assert len(engine_fills) == 2, "the script should have produced an entry and an exit"

        # The same two decisions, replayed through the broker. The clock stands
        # at the deciding bar's close, exactly where the engine parks it before
        # calling the strategy.
        sim_fills: list[tuple[Side, Decimal | None]] = []
        clock = SimulatedClock(bars[0].ts)
        sim = SimulatedBroker(clock=clock, cost_model=ZeroCostModel())
        script = {5: Side.BUY, 12: Side.SELL}

        for index, current in enumerate(bars):
            for order in sim.on_bar(current):
                sim_fills.append((order.side, order.avg_fill_price))
            clock.set(current.close_ts)
            side = script.get(index)
            if side is not None:
                await sim.submit_order(
                    Order(
                        symbol="TEST",
                        side=side,
                        qty=Decimal(100),
                        order_type=OrderType.MARKET,
                        time_in_force=TimeInForce.GTC,
                    )
                )

        assert sim_fills == engine_fills

    @pytest.mark.asyncio
    async def test_they_agree_on_a_limit_the_bar_only_touched(self) -> None:
        """The boundary a drifting copy would move first: `<` versus `<=`.

        A limit resting exactly at the bar's low is the one case where two
        hand-written implementations most plausibly disagree, and the symptom
        would be a paper run whose fills quietly differ from the backtest.
        """
        from atp_core.execution.matching import intended_price

        touched = bar(1, o="100", h="101", low="95", c="100")
        limit = Order(
            symbol="SPY",
            side=Side.BUY,
            qty=Decimal(100),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("95"),
        )

        sim = broker()
        await sim.submit_order(limit)
        filled = sim.on_bar(touched)

        assert filled[0].avg_fill_price == intended_price(limit, touched)


class TestLimitOrders:
    @pytest.mark.asyncio
    async def test_does_not_fill_when_the_range_never_reached_the_limit(self) -> None:
        sim = broker()
        await sim.submit_order(
            Order(
                symbol="SPY",
                side=Side.BUY,
                qty=Decimal(100),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("95"),
                time_in_force=TimeInForce.GTC,
            )
        )

        assert sim.on_bar(bar(1, o="100", h="101", low="99", c="100")) == []

    @pytest.mark.asyncio
    async def test_takes_the_better_open_rather_than_the_limit(self) -> None:
        """A bar that opened through our limit filled at the open. Paying the
        limit for something offered cheaper would understate the strategy."""
        sim = broker()
        await sim.submit_order(
            Order(
                symbol="SPY",
                side=Side.BUY,
                qty=Decimal(100),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("100"),
            )
        )

        filled = sim.on_bar(bar(1, o="97", h="98", low="96", c="97"))

        assert filled[0].avg_fill_price == Decimal("97")

    @pytest.mark.asyncio
    async def test_require_through_refuses_a_limit_only_touched(self) -> None:
        """Touching the extreme means you were last in the queue."""
        touched = bar(1, o="100", h="101", low="95", c="100")
        limit = Order(
            symbol="SPY",
            side=Side.BUY,
            qty=Decimal(100),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("95"),
        )

        optimistic = broker()
        await optimistic.submit_order(limit)
        assert len(optimistic.on_bar(touched)) == 1

        pessimistic = broker(require_through=True)
        await pessimistic.submit_order(limit)
        assert pessimistic.on_bar(touched) == []


class TestStops:
    @pytest.mark.asyncio
    async def test_a_gap_through_the_stop_fills_at_the_open_not_the_trigger(self) -> None:
        """The expensive direction, and the one a naive simulator gets wrong:
        a stop is a market order once triggered, so a gap fills where the
        market actually was."""
        sim = broker()
        await sim.submit_order(
            Order(
                symbol="SPY",
                side=Side.SELL,
                qty=Decimal(100),
                order_type=OrderType.STOP,
                stop_price=Decimal("95"),
            )
        )

        filled = sim.on_bar(bar(1, o="90", h="91", low="88", c="89"))

        assert filled[0].avg_fill_price == Decimal("90")

    @pytest.mark.asyncio
    async def test_a_bar_spanning_stop_and_target_takes_the_stop(self) -> None:
        """The bar cannot say which came first, so assume the loss.

        Reading it the other way reports the winning exit on every ambiguous
        bar, which flatters every strategy that uses brackets.
        """
        sim = broker()
        await sim.submit_order(
            Order(
                symbol="SPY",
                side=Side.SELL,
                qty=Decimal(100),
                order_type=OrderType.STOP,
                stop_price=Decimal("95"),
            )
        )
        await sim.submit_order(
            Order(
                symbol="SPY",
                side=Side.SELL,
                qty=Decimal(100),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("105"),
            )
        )

        filled = sim.on_bar(bar(1, o="100", h="106", low="94", c="100"))

        assert next(f.order_type for f in filled) is OrderType.STOP


class TestTheVolumeCap:
    @pytest.mark.asyncio
    async def test_caps_the_fill_at_a_share_of_the_bars_volume(self) -> None:
        sim = broker()
        await sim.submit_order(market(qty="10000"))

        filled = sim.on_bar(bar(1, o="100", h="101", low="99", c="100", volume="1000"))

        # 10% of 1,000 traded shares, not the 10,000 we asked for.
        assert filled[0].filled_qty == Decimal("100")
        assert filled[0].status is OrderStatus.PARTIALLY_FILLED

    @pytest.mark.asyncio
    async def test_refuses_to_fill_against_a_bar_that_did_not_trade(self) -> None:
        """A fill on zero volume describes a market that was not there."""
        sim = broker()
        await sim.submit_order(market())

        assert sim.on_bar(bar(1, o="100", h="100", low="100", c="100", volume="0")) == []


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_a_resubmit_of_a_known_key_does_not_open_a_second_order(self) -> None:
        """Rule §1.4. The behaviour the router's timeout path depends on."""
        sim = broker()
        order = market()

        first = await sim.submit_order(order)
        second = await sim.submit_order(order)

        assert first.broker_order_id == second.broker_order_id
        assert len(await sim.get_open_orders()) == 1

    @pytest.mark.asyncio
    async def test_hands_back_a_copy_not_the_callers_order(self) -> None:
        """A venue holds its own record. An adapter returning the caller's own
        object would make reconciliation compare our book against itself."""
        sim = broker()
        order = market()

        accepted = await sim.submit_order(order)

        assert accepted is not order
        assert order.status is OrderStatus.PENDING_RISK  # ours is untouched
        assert accepted.status is OrderStatus.SUBMITTED


class TestDayOrders:
    @pytest.mark.asyncio
    async def test_a_day_order_that_could_not_fill_expires(self) -> None:
        """Rather than resting forever and filling on an unrelated later bar."""
        sim = broker()
        await sim.submit_order(
            Order(
                symbol="SPY",
                side=Side.BUY,
                qty=Decimal(100),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("50"),
                time_in_force=TimeInForce.DAY,
            )
        )

        sim.on_bar(bar(1, o="100", h="101", low="99", c="100"))

        assert await sim.get_open_orders() == []

    @pytest.mark.asyncio
    async def test_a_gtc_order_keeps_resting(self) -> None:
        sim = broker()
        await sim.submit_order(
            Order(
                symbol="SPY",
                side=Side.BUY,
                qty=Decimal(100),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("50"),
                time_in_force=TimeInForce.GTC,
            )
        )

        sim.on_bar(bar(1, o="100", h="101", low="99", c="100"))

        assert len(await sim.get_open_orders()) == 1


class TestAccounting:
    @pytest.mark.asyncio
    async def test_cash_and_position_move_with_the_fill(self) -> None:
        sim = broker()
        await sim.submit_order(market(qty="100"))

        sim.on_bar(bar(1, o="110", h="112", low="109", c="111"))

        account = await sim.get_account()
        assert account.cash == Decimal("100000") - Decimal("11000")
        positions = await sim.get_positions()
        assert positions[0].qty == Decimal("100")
        assert positions[0].avg_entry_price == Decimal("110")

    @pytest.mark.asyncio
    async def test_buying_power_is_cash_not_leverage(self) -> None:
        """This broker extends no margin. Reporting some would let a strategy
        size against leverage that will not be there on a real venue."""
        account = await broker().get_account()
        assert account.buying_power == account.cash

    @pytest.mark.asyncio
    async def test_costs_are_charged_through_the_cost_model(self) -> None:
        sim = SimulatedBroker(
            clock=SimulatedClock(OPEN + STEP), cost_model=alpaca_equities_default()
        )
        await sim.submit_order(market(qty="100", side=Side.SELL))

        filled = sim.on_bar(bar(1, o="110", h="112", low="109", c="111"))

        # Slippage is adverse on both sides: a sell fills below the open.
        assert filled[0].avg_fill_price is not None
        assert filled[0].avg_fill_price < Decimal("110")

    @pytest.mark.asyncio
    async def test_a_partial_fill_leaves_the_order_open_across_bars(self) -> None:
        """A position update must handle a fill *sequence* (CLAUDE.md §5)."""
        sim = broker()
        await sim.submit_order(
            Order(
                symbol="SPY",
                side=Side.BUY,
                qty=Decimal("200"),
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
            )
        )

        sim.on_bar(bar(1, o="100", h="101", low="99", c="100", volume="1000"))
        sim.on_bar(bar(2, o="100", h="101", low="99", c="100", volume="1000"))

        positions = await sim.get_positions()
        assert positions[0].qty == Decimal("200")
        # Both tranches filled, so the order left the book.
        assert await sim.get_open_orders() == []


class TestCancellation:
    @pytest.mark.asyncio
    async def test_a_cancelled_order_stops_filling(self) -> None:
        sim = broker()
        accepted = await sim.submit_order(market())
        assert accepted.broker_order_id is not None

        await sim.cancel_order(accepted.broker_order_id)

        assert sim.on_bar(bar(1, o="110", h="112", low="109", c="111")) == []
        held = await sim.get_order(accepted.broker_order_id)
        assert held is not None
        assert held.status is OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancelling_an_unknown_order_is_not_an_error(self) -> None:
        """A race we lost. The fill stands and the caller gets what it wanted."""
        await broker().cancel_order("sim-does-not-exist")


class TestClosingPositions:
    @pytest.mark.asyncio
    async def test_close_position_rests_like_any_other_order(self) -> None:
        """A venue that closed instantly at a price of its choosing would be
        exactly the flattering lie this file exists to avoid."""
        sim = broker()
        await sim.submit_order(market(qty="100"))
        sim.on_bar(bar(1, o="100", h="101", low="99", c="100"))

        closing = await sim.close_position("SPY")

        assert closing.side is Side.SELL
        assert closing.status is OrderStatus.SUBMITTED
        assert (await sim.get_positions())[0].qty == Decimal("100")  # not yet flat

    @pytest.mark.asyncio
    async def test_closing_a_position_we_do_not_hold_raises(self) -> None:
        with pytest.raises(BrokerError, match="no open position"):
            await broker().close_position("SPY")


class TestQuoteDrivenFills:
    @staticmethod
    def quote(seconds: float, *, bid: str, ask: str, size: str = "1000") -> Quote:
        return Quote(
            symbol="SPY",
            ts=OPEN + STEP + timedelta(seconds=seconds),
            bid=Decimal(bid),
            ask=Decimal(ask),
            bid_size=Decimal(size),
            ask_size=Decimal(size),
        )

    @pytest.mark.asyncio
    async def test_latency_stops_a_strategy_trading_on_the_quote_it_reacted_to(self) -> None:
        """At tick level this is the difference between a plausible simulation
        and a time machine."""
        sim = broker()
        await sim.submit_order(market())

        assert sim.on_quote(self.quote(0.01, bid="99", ask="101")) == []
        assert len(sim.on_quote(self.quote(0.2, bid="99", ask="101"))) == 1

    @pytest.mark.asyncio
    async def test_a_buy_lifts_the_ask_never_the_mid(self) -> None:
        """The mid is a price nobody is offering."""
        sim = broker()
        await sim.submit_order(market())

        filled = sim.on_quote(self.quote(1, bid="99", ask="101"))

        assert filled[0].avg_fill_price == Decimal("101")

    @pytest.mark.asyncio
    async def test_size_is_capped_at_what_the_book_was_offering(self) -> None:
        sim = broker()
        await sim.submit_order(market(qty="5000"))

        filled = sim.on_quote(self.quote(1, bid="99", ask="101", size="300"))

        assert filled[0].filled_qty == Decimal("300")


class TestUnmodelledOrders:
    @pytest.mark.asyncio
    async def test_a_trailing_stop_raises_rather_than_silently_never_filling(self) -> None:
        """None means "the market did not reach it". A caller that could not
        tell that apart from "we cannot price this" would silently expire every
        trailing stop it was handed."""
        sim = broker()
        await sim.submit_order(
            Order(
                symbol="SPY",
                side=Side.SELL,
                qty=Decimal(100),
                order_type=OrderType.TRAILING_STOP,
                trail_percent=Decimal("2"),
            )
        )

        with pytest.raises(ExecutionError, match="not modelled"):
            sim.on_bar(bar(1, o="100", h="101", low="99", c="100"))

    @pytest.mark.asyncio
    async def test_a_quote_refuses_the_same_order_the_same_way(self) -> None:
        """One exception type, whichever half of the simulator you fed."""
        sim = broker()
        await sim.submit_order(
            Order(
                symbol="SPY",
                side=Side.SELL,
                qty=Decimal(100),
                order_type=OrderType.TRAILING_STOP,
                trail_percent=Decimal("2"),
            )
        )

        with pytest.raises(ExecutionError, match="not modelled"):
            sim.on_quote(
                Quote(
                    symbol="SPY",
                    ts=OPEN + STEP + timedelta(seconds=1),
                    bid=Decimal("99"),
                    ask=Decimal("101"),
                )
            )
