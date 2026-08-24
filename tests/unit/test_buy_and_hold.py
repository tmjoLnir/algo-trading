"""The buy-and-hold benchmark.

A benchmark is only worth having if it is honest, and the ways it can quietly
stop being honest are all here: buying at a price nobody could have paid,
re-entering after a stop so that it stops being a fixed baseline, doubling its
position across a restart, or drifting from the market's own return.

The load-bearing case is `test_it_earns_exactly_the_market_over_the_window`. If
that holds, the number this produces is the market's return and every strategy
compared against it is being compared against the right thing. If it does not,
every comparison the platform draws is off by the difference — and nothing else
in a result would show it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from atp_core.backtest.costs import ZeroCostModel
from atp_core.backtest.engine import (
    BacktestConfig,
    BacktestContext,
    BacktestEngine,
    FixedQtySizer,
)
from atp_core.clock import SimulatedClock
from atp_core.domain import (
    Bar,
    OrderStatus,
    Portfolio,
    Position,
    Side,
    SignalAction,
    Timeframe,
)
from atp_core.risk.engine import RiskDecision
from atp_core.strategy import registry
from atp_core.strategy.examples.buy_and_hold import BuyAndHold

if TYPE_CHECKING:
    from atp_core.domain import Order, Signal

START = datetime(2024, 1, 2, tzinfo=UTC)
QTY = Decimal(100)
CASH = Decimal(100_000)


def bar(index: int, close: float, *, symbol: str = "SPY") -> Bar:
    price = Decimal(str(close))
    return Bar(
        symbol=symbol,
        ts=START + timedelta(days=index),
        timeframe=Timeframe.D1,
        # Open half a point under the close, so "filled at the next bar's open"
        # and "filled at this bar's close" are different numbers and the
        # lookahead assertion below has something to catch.
        open=price - Decimal("0.5"),
        high=price + Decimal(1),
        low=price - Decimal(1),
        close=price,
        volume=Decimal(1_000_000),
    )


def ramp(count: int = 10, *, symbol: str = "SPY", start: float = 100.0) -> list[Bar]:
    """Bar i closes at `start + i`. A shape with a return worth checking."""
    return [bar(index, start + index, symbol=symbol) for index in range(count)]


def slide(count: int = 10, *, symbol: str = "SPY") -> list[Bar]:
    """The mirror: bar i closes at 100 − i. Holding must survive it."""
    return [bar(index, 100 - index, symbol=symbol) for index in range(count)]


class _AllowAllRisk:
    """The surface `BacktestEngine` uses of a `RiskEngine`, and only that.

    These tests are about what the benchmark decides, not about how a risk rule
    sizes a book; `tests/unit/test_risk_engine.py` owns the rules.
    """

    def anchor_session(self, equity: Decimal) -> int:
        return 1

    def validate(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        return RiskDecision.allow()


def drive(
    bars: list[Bar],
    *,
    portfolio: Portfolio | None = None,
    symbol: str = "SPY",
    strategy: BuyAndHold | None = None,
) -> list[Signal]:
    """Feed every bar and collect what it emits, with no fills.

    The real `BacktestContext` rather than a fake: its cursor is the lookahead
    guarantee, so a strategy driven through it cannot read a bar that has not
    closed. Nothing fills here, which is exactly what makes it the right harness
    for "does it keep trying" — the book stays flat all the way through.
    """
    chosen = strategy
    if chosen is None:
        chosen = BuyAndHold()
        # What `BacktestEngine.run` and `StrategyRunner.warmup` both do before
        # the first bar. A caller that passes its own strategy owns that call
        # instead — the restart cases below drive one instance twice.
        chosen.on_start()
    book = portfolio if portfolio is not None else Portfolio(cash=CASH, starting_equity=CASH)
    clock = SimulatedClock(START)
    ctx = BacktestContext({symbol: bars}, book, clock, (symbol,))

    emitted: list[Signal] = []
    for index, current in enumerate(bars):
        clock.set(current.ts)
        ctx.advance(symbol, index)
        emitted.extend(chosen.on_bar(ctx, current))
    return emitted


def run_backtest(bars: list[Bar], *, symbol: str = "SPY", qty: Decimal = QTY) -> Any:
    config = BacktestConfig(
        symbols=[symbol],
        start=bars[0].ts,
        end=bars[-1].ts,
        timeframe=Timeframe.D1,
        starting_cash=CASH,
    )
    return BacktestEngine(
        strategy=BuyAndHold(),
        config=config,
        cost_model=ZeroCostModel(),
        risk_engine=_AllowAllRisk(),
        position_sizer=FixedQtySizer(qty),
    ).run({symbol: bars})


def holding(symbol: str = "SPY", *, at: float = 100.0) -> Portfolio:
    book = Portfolio(cash=CASH, starting_equity=CASH)
    book.positions[symbol] = Position(
        symbol=symbol,
        qty=QTY,
        avg_entry_price=Decimal(str(at)),
        last_price=Decimal(str(at)),
    )
    return book


class TestTheRules:
    def test_it_is_registered_under_its_name(self) -> None:
        """The registry is how a config, the backtest form and the CLI reach it.
        A class nobody can name is a class nobody can run."""
        assert registry.get("buy_and_hold") is BuyAndHold

    def test_it_buys_on_the_first_decidable_bar(self) -> None:
        emitted = drive(ramp())
        assert emitted[0].action is SignalAction.ENTER_LONG
        assert emitted[0].ts == ramp()[0].ts

    def test_it_needs_no_warmup(self) -> None:
        """Every bar spent warming up is a bar of the market's return the
        benchmark did not capture and the strategy under test did."""
        assert BuyAndHold().warmup_bars == 0

    def test_it_buys_once_and_then_has_no_further_opinions(self) -> None:
        """Driven with no fills, so the book stays flat for all ten bars. A
        strategy that re-tried while flat would emit ten signals here."""
        assert len(drive(ramp())) == 1

    def test_it_never_sells(self) -> None:
        assert not [s for s in drive(ramp(60)) if s.is_exit]

    def test_it_holds_through_a_decline(self) -> None:
        """Holding is the whole strategy. A benchmark that got out of a falling
        market would flatter every strategy measured against it."""
        emitted = drive(slide(40))
        assert len(emitted) == 1
        assert emitted[0].action is SignalAction.ENTER_LONG

    def test_it_does_not_re_enter_after_a_position_is_closed(self) -> None:
        """The difference between a benchmark and a re-entry system.

        Entering whenever flat is one line shorter and turns this into buy, get
        stopped out, buy again — a strategy whose results depend on the stop.
        Here the book opens a position and then goes flat again, exactly as a
        triggered stop would leave it, and the answer is silence.
        """
        strategy = BuyAndHold()
        book = holding()
        drive(ramp(3), portfolio=book, strategy=strategy)

        book.positions.pop("SPY")
        assert drive(ramp(20), portfolio=book, strategy=strategy) == []

    def test_it_carries_no_stop_or_target(self) -> None:
        """A benchmark that could be stopped out is not measuring the market's
        return any more. This is also why `risk_pct` cannot size it."""
        first = drive(ramp())[0]
        assert first.stop_loss_price is None
        assert first.take_profit_price is None

    def test_it_populates_the_reason_and_the_close(self) -> None:
        """Authoring rule 4 — not optional even for a benchmark, since a
        baseline that took an unexplained trade is a baseline nobody can audit.
        """
        first = drive(ramp())[0]
        assert "buy and hold" in first.reason
        assert first.indicators["close"] == pytest.approx(100.0)

    def test_each_symbol_gets_its_own_single_entry(self) -> None:
        """A universe of n is n full-sized positions, which is what the sizing
        note on the signal is warning about."""
        strategy = BuyAndHold()
        spy = drive(ramp(5), strategy=strategy, symbol="SPY")
        qqq = drive(ramp(5, symbol="QQQ"), strategy=strategy, symbol="QQQ")
        assert [s.symbol for s in spy + qqq] == ["SPY", "QQQ"]


class TestRestartSafety:
    """`on_start` clears the set; the book is what actually decides."""

    def test_a_restart_holding_a_position_does_not_buy_it_again(self) -> None:
        """A restarted runner must not double a position it already holds.

        Held here by the `is_flat` check alone, which is why the case below
        exists as well — that one is what the marking is actually for.
        """
        strategy = BuyAndHold()
        assert len(drive(ramp(3), portfolio=holding(), strategy=strategy)) == 0

        strategy.on_start()

        assert drive(ramp(20), portfolio=holding(), strategy=strategy) == []

    def test_a_restart_and_then_a_close_does_not_buy_back_in(self) -> None:
        """The reason this reads the position rather than counting its signals.

        The combination is what bites: restart, so the set is empty; observe the
        open position, which re-marks it; then have that position close, as a
        stop would leave it. A version that only marked when it *emitted* a
        signal would have nothing recorded across the restart and would buy back
        in here — turning the benchmark into a re-entry system at exactly the
        moment nobody is watching.
        """
        strategy, book = BuyAndHold(), holding()
        drive(ramp(3), portfolio=book, strategy=strategy)

        strategy.on_start()
        drive(ramp(3), portfolio=book, strategy=strategy)

        book.positions.pop("SPY")
        assert drive(ramp(20), portfolio=book, strategy=strategy) == []

    def test_a_restart_while_flat_and_never_filled_still_gets_on(self) -> None:
        """The other direction, and the reason a restart is not simply ignored:
        a benchmark whose one entry was refused has not bought anything, and
        standing down forever would leave the comparison with no baseline."""
        strategy = BuyAndHold()
        assert len(drive(ramp(3), strategy=strategy)) == 1

        strategy.on_start()

        assert len(drive(ramp(3), strategy=strategy)) == 1


class TestThroughTheEngine:
    def test_a_run_is_one_buy_and_nothing_else(self) -> None:
        result = run_backtest(ramp(20))
        filled = [o for o in result.orders if o.status is OrderStatus.FILLED]
        assert [o.side for o in filled] == [Side.BUY]
        assert not result.warnings

    def test_the_position_is_still_open_at_the_end(self) -> None:
        """Which is the point, and why the return is an unrealised mark rather
        than a realised trade."""
        result = run_backtest(ramp(20))
        assert result.portfolio.position("SPY").qty == QTY

    def test_it_fills_at_the_second_bars_open_not_the_first_bars_close(self) -> None:
        """The benchmark is not exempt from the lookahead rule, and must not be.

        It is the thing every strategy is compared against, so buying it in at a
        price nobody could have paid would understate every strategy in the
        platform by the same amount.
        """
        bars = ramp(20)
        filled = next(o for o in run_backtest(bars).orders if o.status is OrderStatus.FILLED)
        assert filled.avg_fill_price == bars[1].open
        assert filled.avg_fill_price != bars[0].close

    def test_it_earns_exactly_the_market_over_the_window(self) -> None:
        """The property that makes this a benchmark rather than a strategy.

        Hand-computed on a 10-bar ramp: 100 shares bought at bar 1's open of
        100.5, marked at bar 9's close of 109.

            cash    100,000 − 100 × 100.5 = 89,950
            mark    100 × 109             = 10,900
            equity  89,950 + 10,900       = 100,850
            return  850 / 100,000         = 0.0085

        Asserted against the arithmetic as well as the constant, so a change to
        the fixture cannot leave a stale number passing.
        """
        bars = ramp(10)
        result = run_backtest(bars)

        entry, final = bars[1].open, bars[-1].close
        assert entry == Decimal("100.5")
        assert final == Decimal(109)

        assert result.portfolio.cash == CASH - QTY * entry
        assert result.total_return == QTY * (final - entry) / CASH
        assert result.total_return == Decimal("0.0085")

    def test_a_falling_market_produces_the_loss_and_not_an_exit(self) -> None:
        """The failure path: a benchmark that quietly cut its loss would make
        every strategy compared against it look worse than it is."""
        bars = slide(10)
        result = run_backtest(bars)

        filled = [o for o in result.orders if o.status is OrderStatus.FILLED]
        assert [o.side for o in filled] == [Side.BUY]
        assert result.total_return == QTY * (bars[-1].close - bars[1].open) / CASH
        assert result.total_return == Decimal("-0.0075")
        assert result.total_return < 0
