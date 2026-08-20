"""Backtest engine — the lookahead guarantees.

If these pass and the engine is still wrong, every backtest the platform
produces is fiction. Treat a failure here as a correctness emergency, never as
a test to relax.

The risk engine is Phase 3 and still a stub, so these inject a permissive
double. That is the phase boundary, not a shortcut: the engine's job here is to
*call* the gate on every order, and `_AllowAllRisk` is what lets the fill
mechanics be tested before the gate exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from atp_core.analytics.performance import PerformanceAnalyzer
from atp_core.backtest.costs import ZeroCostModel
from atp_core.backtest.engine import (
    PROGRESS_EVERY,
    BacktestConfig,
    BacktestContext,
    BacktestEngine,
    BacktestResult,
    FixedQtySizer,
)
from atp_core.clock import SimulatedClock
from atp_core.domain import (
    Bar,
    OrderStatus,
    Portfolio,
    Side,
    Signal,
    SignalAction,
    Timeframe,
)
from atp_core.errors import DataGapError, LookaheadError
from atp_core.execution.idempotency import ENTRY, EXIT, STOP_LOSS, TAKE_PROFIT
from atp_core.risk.engine import RiskDecision
from atp_core.strategy.base import Strategy

if TYPE_CHECKING:
    from atp_core.domain import Order
    from atp_core.strategy.context import StrategyContext

START = datetime(2024, 1, 2, tzinfo=UTC)


def bar(
    index: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1_000_000,
    symbol: str = "TEST",
) -> Bar:
    return Bar(
        symbol=symbol,
        ts=START + timedelta(days=index),
        timeframe=Timeframe.D1,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
    )


def ramp(count: int = 20, *, symbol: str = "TEST", volume: float = 1_000_000) -> list[Bar]:
    """Bar i: open 100+i, close 100.5+i, high close+1, low open-1.

    Deliberately boring and exactly predictable — the fixture below is computed
    by hand from these numbers.
    """
    return [
        bar(
            i,
            open_=100 + i,
            high=101.5 + i,
            low=99 + i,
            close=100.5 + i,
            volume=volume,
            symbol=symbol,
        )
        for i in range(count)
    ]


class _AllowAllRisk:
    """Stands in for `RiskEngine` until Phase 3 implements the chain."""

    def __init__(self) -> None:
        self.seen: list[Order] = []

    def validate(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        self.seen.append(order)
        return RiskDecision.allow()


class _DenyAllRisk:
    def validate(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        return RiskDecision.deny("test_rule", "denied by test")


class ScriptedStrategy(Strategy):
    """Emits whatever the script says on the n-th bar it is shown.

    Nothing is computed from prices, so a fixture's expected P&L depends only on
    the engine's fill timing — which is the thing under test.
    """

    name: ClassVar[str] = "scripted"

    def __init__(
        self,
        script: dict[int, SignalAction],
        *,
        warmup: int = 0,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        limit_price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(params)
        self.script = script
        self._warmup = warmup
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.limit_price = limit_price
        self.calls = 0
        self.seen_history: list[int] = []

    @property
    def warmup_bars(self) -> int:
        return self._warmup

    def on_bar(self, ctx: StrategyContext, bar_: Bar) -> list[Signal]:
        index = self.calls
        self.calls += 1
        self.seen_history.append(len(ctx.closes(bar_.symbol, Timeframe.D1, 1_000)))

        action = self.script.get(index)
        if action is None:
            return []
        return [
            Signal(
                strategy_id=self.name,
                symbol=bar_.symbol,
                action=action,
                ts=ctx.now,
                stop_loss_price=self.stop_loss if action is SignalAction.ENTER_LONG else None,
                take_profit_price=self.take_profit if action is SignalAction.ENTER_LONG else None,
                limit_price=self.limit_price if action is SignalAction.ENTER_LONG else None,
            )
        ]


def engine(
    strategy: Strategy,
    *,
    qty: Decimal = Decimal(100),
    cash: Decimal = Decimal(100_000),
    participation: Decimal = Decimal("0.10"),
    risk: Any = None,
    symbols: list[str] | None = None,
) -> BacktestEngine:
    config = BacktestConfig(
        symbols=symbols or ["TEST"],
        start=START,
        end=START + timedelta(days=365),
        timeframe=Timeframe.D1,
        starting_cash=cash,
        max_volume_participation=participation,
    )
    return BacktestEngine(
        strategy=strategy,
        config=config,
        cost_model=ZeroCostModel(),
        risk_engine=risk if risk is not None else _AllowAllRisk(),
        position_sizer=FixedQtySizer(qty),
    )


class TestNoLookahead:
    def test_context_cannot_see_future_bars(self) -> None:
        """`ctx.history()` never returns a bar closing after the current one."""
        bars = {"TEST": ramp(10)}
        portfolio = Portfolio(cash=Decimal(0), starting_equity=Decimal(0))
        ctx = BacktestContext(bars, portfolio, SimulatedClock(START), ("TEST",))

        ctx.advance("TEST", 4)
        visible = ctx.history("TEST", Timeframe.D1, 5)
        assert [b.ts for b in visible] == [bars["TEST"][i].ts for i in range(5)]
        assert ctx.last_price("TEST") == bars["TEST"][4].close
        assert len(ctx.closes("TEST", Timeframe.D1, 1_000)) == 5

        # Asking for more than exists is a gap, not a short series.
        with pytest.raises(DataGapError):
            ctx.history("TEST", Timeframe.D1, 6)

        # And the cursor is monotonic — nothing can rewind it to re-decide.
        with pytest.raises(LookaheadError):
            ctx.advance("TEST", 3)

    def test_strategy_never_sees_more_bars_than_have_closed(self) -> None:
        """The structural guarantee, observed from inside a real run."""
        strategy = ScriptedStrategy({})
        engine(strategy).run({"TEST": ramp(10)})
        assert strategy.seen_history == list(range(1, 11))

    def test_signal_fills_at_next_bar_open(self) -> None:
        """Not at the signal bar's close — you cannot trade at a price you only
        know once the bar is over. This single rule separates a real backtest
        from a flattering one."""
        bars = ramp(10)
        strategy = ScriptedStrategy({5: SignalAction.ENTER_LONG})
        result = engine(strategy).run({"TEST": bars})

        assert len(result.orders) == 1
        order = result.orders[0]
        assert order.filled_qty == Decimal(100)
        # Bar 6's open (106), not bar 5's close (105.5).
        assert order.avg_fill_price == bars[6].open == Decimal("106")
        assert order.avg_fill_price != bars[5].close

    def test_warmup_signals_discarded(self) -> None:
        bars = ramp(10)
        strategy = ScriptedStrategy(
            {0: SignalAction.ENTER_LONG, 1: SignalAction.ENTER_LONG, 3: SignalAction.ENTER_LONG},
            warmup=3,
        )
        result = engine(strategy).run({"TEST": bars})

        # Bars 0 and 1 fall inside warmup; only the bar-3 signal survives.
        assert len(result.orders) == 1
        assert result.orders[0].avg_fill_price == bars[4].open
        # The strategy still saw every bar, so its indicators warmed up.
        assert strategy.calls == 10


class TestFills:
    def test_limit_fills_only_if_range_reached_it(self) -> None:
        bars = ramp(10)
        # Bar 6 trades 105.0..107.5. A limit at 90 is never touched.
        unreachable = ScriptedStrategy({5: SignalAction.ENTER_LONG}, limit_price=Decimal("90"))
        result = engine(unreachable).run({"TEST": bars})
        assert result.orders[0].filled_qty == 0
        assert result.orders[0].status is OrderStatus.EXPIRED

        # A limit at 106 sits inside bar 6's range and fills.
        reachable = ScriptedStrategy({5: SignalAction.ENTER_LONG}, limit_price=Decimal("106"))
        result = engine(reachable).run({"TEST": bars})
        assert result.orders[0].filled_qty == Decimal(100)
        assert result.orders[0].avg_fill_price == Decimal("106")

    def test_stop_fills_worse_than_trigger(self) -> None:
        """A gap through the stop fills at the open. The market never traded at
        the stop price on that bar, and pretending it did is free money."""
        bars = [
            bar(0, open_=100, high=101, low=99, close=100),
            bar(1, open_=100, high=101, low=99, close=100),  # entry fills at 100
            bar(2, open_=90, high=91, low=89, close=90),  # gaps straight through 95
        ]
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG}, stop_loss=Decimal("95"))
        result = engine(strategy).run({"TEST": bars})

        stop_order = result.orders[-1]
        assert stop_order.side is Side.SELL
        assert stop_order.avg_fill_price == Decimal("90")  # the open, not 95

    def test_volume_participation_capped(self) -> None:
        """Cannot buy 10x the bar's volume."""
        bars = ramp(5, volume=500)  # cap = 10% of 500 = 50 shares
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        result = engine(strategy, qty=Decimal(100)).run({"TEST": bars})

        order = result.orders[0]
        assert order.filled_qty == Decimal(50)
        assert order.status is OrderStatus.EXPIRED  # DAY order, remainder dies
        assert any("volume cap" in w for w in result.warnings)

    def test_stop_assumed_first_when_bar_spans_stop_and_target(self) -> None:
        """The pessimistic reading is the only honest one at bar resolution."""
        bars = [
            bar(0, open_=100, high=101, low=99, close=100),
            bar(1, open_=100, high=101, low=99, close=100),  # entry at 100
            bar(2, open_=100, high=110, low=90, close=100),  # spans 95 and 105
        ]
        strategy = ScriptedStrategy(
            {0: SignalAction.ENTER_LONG},
            stop_loss=Decimal("95"),
            take_profit=Decimal("105"),
        )
        result = engine(strategy).run({"TEST": bars})

        exit_order = result.orders[-1]
        assert exit_order.avg_fill_price == Decimal("95")  # the stop, not the target
        assert result.portfolio.position("TEST").realized_pnl == Decimal(100) * Decimal("-5")

    def test_risk_denial_produces_no_fill(self) -> None:
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        result = engine(strategy, risk=_DenyAllRisk()).run({"TEST": ramp(5)})

        assert result.orders[0].status is OrderStatus.REJECTED_RISK
        assert result.portfolio.position("TEST").is_flat
        assert any("risk denied" in w for w in result.warnings)

    def test_every_order_is_shown_to_the_risk_engine(self) -> None:
        """Rule §1.5 — there is no path to a fill that skips the gate."""
        risk = _AllowAllRisk()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG, 3: SignalAction.EXIT})
        engine(strategy, risk=risk).run({"TEST": ramp(8)})
        assert len(risk.seen) == 2


class TestOrdering:
    def test_stops_checked_before_new_signals(self) -> None:
        """Mirrors the live runner. Reordering lets a strategy exit at a price
        it could not have obtained."""
        bars = [
            bar(0, open_=100, high=101, low=99, close=100),
            bar(1, open_=100, high=101, low=99, close=100),  # entry at 100
            bar(2, open_=100, high=101, low=94, close=100),  # dips to 94, stop at 95
            bar(3, open_=100, high=101, low=99, close=100),
        ]
        # The strategy also asks to exit on bar 2. The stop got there first.
        strategy = ScriptedStrategy(
            {0: SignalAction.ENTER_LONG, 2: SignalAction.EXIT}, stop_loss=Decimal("95")
        )
        result = engine(strategy).run({"TEST": bars})

        stop_exit = result.orders[1]
        assert stop_exit.avg_fill_price == Decimal("95")
        # The strategy's own exit found nothing left to sell, so it never
        # became an order at all.
        assert len(result.orders) == 2
        assert result.portfolio.position("TEST").is_flat


class TestValidation:
    def test_unsorted_bars_are_refused(self) -> None:
        bars = ramp(5)
        bars[2], bars[3] = bars[3], bars[2]
        with pytest.raises(DataGapError, match="chronological"):
            engine(ScriptedStrategy({})).run({"TEST": bars})

    def test_duplicate_timestamps_are_refused(self) -> None:
        bars = ramp(5)
        bars[3] = bars[2]
        with pytest.raises(DataGapError, match="duplicate"):
            engine(ScriptedStrategy({})).run({"TEST": bars})

    def test_missing_symbol_is_refused(self) -> None:
        with pytest.raises(DataGapError, match="no bars supplied"):
            engine(ScriptedStrategy({}), symbols=["TEST", "OTHER"]).run({"TEST": ramp(5)})

    def test_wrong_timeframe_is_refused(self) -> None:
        bars = ramp(3)
        bars[1] = Bar(
            symbol="TEST",
            ts=bars[1].ts,
            timeframe=Timeframe.M1,
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(100),
            volume=Decimal(1000),
        )
        with pytest.raises(DataGapError, match="not 1d"):
            engine(ScriptedStrategy({})).run({"TEST": bars})


#: 20 daily bars: (open, high, low, close). Deliberately NOT a straight ramp.
#: On a linear path a fill at the signal bar's close and a fill at the next
#: bar's open produce the same P&L, so the headline number would pass under
#: exactly the bug this fixture exists to catch. The jumps into bar 6 and out of
#: bar 13 are what make the two readings disagree.
FIXTURE_BARS: list[tuple[float, float, float, float]] = [
    (100, 102, 99, 101),  # 0
    (101, 103, 100, 102),  # 1
    (102, 104, 101, 103),  # 2
    (103, 105, 102, 104),  # 3
    (104, 106, 103, 105),  # 4
    (105, 106, 99, 100),  # 5  ENTER_LONG on this close (100)
    (110, 112, 108, 111),  # 6  entry fills at THIS open (110)
    (111, 113, 110, 112),  # 7
    (112, 114, 111, 113),  # 8
    (113, 115, 112, 114),  # 9
    (114, 116, 113, 115),  # 10
    (115, 117, 114, 116),  # 11
    (116, 151, 115, 150),  # 12 EXIT on this close (150)
    (130, 132, 128, 131),  # 13 exit fills at THIS open (130)
    (131, 133, 130, 132),  # 14
    (132, 134, 131, 133),  # 15
    (133, 135, 132, 134),  # 16
    (134, 136, 133, 135),  # 17
    (135, 137, 134, 136),  # 18
    (136, 138, 135, 137),  # 19
]


class TestAgainstKnownFixture:
    def test_hand_computed_20_bar_scenario(self) -> None:
        """A fixture whose expected P&L was worked out by hand. The only real
        defence against an engine that is self-consistently wrong.

        100 shares, no costs, 100,000 starting cash, no stops:

            bar  5  strategy says ENTER_LONG  (close 100)
            bar  6  order fills at the OPEN = 110   cash 100,000 - 11,000 = 89,000
            bar 12  strategy says EXIT        (close 150)
            bar 13  order fills at the OPEN = 130   cash  89,000 + 13,000 = 102,000

        Realised P&L = 100 × (130 - 110) = 2,000
        Ending equity = 102,000 (flat), total return = 2,000 / 100,000 = 0.02

        Filling at the signal bars' closes instead would buy at 100 and sell at
        150 for a P&L of 5,000 — two and a half times the truth, off one line of
        fill timing. That is the size of the lie this rule prevents.
        """
        bars = [
            bar(i, open_=o, high=h, low=lo, close=c) for i, (o, h, lo, c) in enumerate(FIXTURE_BARS)
        ]
        strategy = ScriptedStrategy({5: SignalAction.ENTER_LONG, 12: SignalAction.EXIT})
        result = engine(strategy).run({"TEST": bars})

        entry, exit_ = result.orders
        assert entry.side is Side.BUY
        assert entry.avg_fill_price == Decimal("110")
        assert entry.filled_qty == Decimal(100)

        assert exit_.side is Side.SELL
        assert exit_.avg_fill_price == Decimal("130")
        assert exit_.filled_qty == Decimal(100)

        position = result.portfolio.position("TEST")
        assert position.is_flat
        assert position.realized_pnl == Decimal(2000)

        assert result.portfolio.cash == Decimal("102000")
        assert result.portfolio.equity == Decimal("102000")
        assert result.total_return == Decimal("0.02")

        # One equity point per bar, and the curve ends where the cash does.
        assert len(result.equity_curve) == 20
        assert result.equity_curve[-1][1] == Decimal("102000")

    def test_equity_curve_marks_the_open_position_each_bar(self) -> None:
        """Between entry and exit the curve has to move with the mark, not sit
        flat at cash — a curve that only steps on fills understates drawdown."""
        bars = [
            bar(i, open_=o, high=h, low=lo, close=c) for i, (o, h, lo, c) in enumerate(FIXTURE_BARS)
        ]
        strategy = ScriptedStrategy({5: SignalAction.ENTER_LONG, 12: SignalAction.EXIT})
        result = engine(strategy).run({"TEST": bars})

        # Bar 6: cash 89,000 + 100 shares marked at that bar's close (111).
        assert result.equity_curve[6][1] == Decimal("89000") + Decimal(100) * Decimal("111")
        # Bar 12: still holding, marked at 150.
        assert result.equity_curve[12][1] == Decimal("89000") + Decimal(100) * Decimal("150")

    def test_report_is_serialisable_and_reports_the_same_numbers(self) -> None:
        bars = [
            bar(i, open_=o, high=h, low=lo, close=c) for i, (o, h, lo, c) in enumerate(FIXTURE_BARS)
        ]
        strategy = ScriptedStrategy({5: SignalAction.ENTER_LONG, 12: SignalAction.EXIT})
        result = engine(strategy).run({"TEST": bars})
        report = result.to_report()

        assert report["strategy"] == "scripted"
        assert report["ending_equity"] == "102000"
        assert report["total_return"] == "0.02"
        assert report["filled_orders"] == 2
        assert isinstance(report["metrics"], dict)
        assert report["metrics"]["num_trades"] == 1


class TestMetricsReconcile:
    """The metrics the engine reports have to agree with the fixture it just
    ran. A metric set computed from its own separate bookkeeping is exactly
    where a plausible-but-wrong number comes from."""

    def result(self) -> BacktestResult:
        bars = [
            bar(i, open_=o, high=h, low=lo, close=c) for i, (o, h, lo, c) in enumerate(FIXTURE_BARS)
        ]
        strategy = ScriptedStrategy({5: SignalAction.ENTER_LONG, 12: SignalAction.EXIT})
        return engine(strategy).run({"TEST": bars})

    def test_one_round_trip_worth_its_hand_computed_pnl(self) -> None:
        m = self.result().metrics
        assert m["num_trades"] == 1
        assert m["expectancy"] == pytest.approx(2000.0)  # the fixture's realised P&L
        assert m["win_rate"] == pytest.approx(1.0)
        assert m["largest_win"] == pytest.approx(2000.0)
        assert m["profit_factor"] == float("inf")  # no losing trade to divide by

    def test_total_return_matches_the_result(self) -> None:
        result = self.result()
        assert result.metrics["total_return"] == pytest.approx(float(result.total_return))

    def test_turnover_is_both_legs_over_starting_equity(self) -> None:
        """100 × 110 in, 100 × 130 out = 24,000 traded on 100,000."""
        assert self.result().metrics["turnover"] == pytest.approx(0.24)

    def test_exposure_counts_only_the_bars_actually_held(self) -> None:
        """Filled on bar 6, exited on bar 13's open — so the book is in the
        market at the close of bars 6 through 12, seven of twenty."""
        assert self.result().metrics["exposure_pct"] == pytest.approx(7 / 20)

    def test_holding_period_runs_from_entry_fill_to_exit_fill(self) -> None:
        """Bar 6 to bar 13 is seven daily bars — 168 hours."""
        assert self.result().metrics["avg_holding_period_hours"] == pytest.approx(168.0)

    def test_drawdown_is_measured_on_the_marked_curve(self) -> None:
        """Equity peaks at 104,000 on bar 12 (100 shares marked at 150) and
        lands at 102,000 once the exit fills at 130. Without the mark-to-close
        the curve would step only on fills and this drawdown would vanish."""
        assert self.result().metrics["max_drawdown"] == pytest.approx((102_000 - 104_000) / 104_000)


class TestOrderPurpose:
    """Every order the engine creates says what it is for.

    It did not, until the queued path needed it. `Order.purpose` defaults to
    `"entry"`, and `PerformanceAnalyzer.build_trades` reads it to label how a
    round trip ended — so with the default left in place every exit this engine
    produced reconstructed as an exit "by signal", stop-outs and targets
    included. That is a **wrong** label rather than a missing one, on the field
    that decides whether a strategy's stops are misplaced, which is exactly what
    `UNKNOWN_EXIT`'s own comment in `analytics.performance` says is worse.

    A live-vs-backtest divergence, in the same family as the take-profit one
    recorded against `StrategyRunner` in #58: the live loop set `purpose` and the
    engine never did, so the two produced trade tables that disagreed about why
    positions closed.
    """

    def test_an_entry_and_a_signal_exit_are_told_apart(self) -> None:
        strategy = ScriptedStrategy({2: SignalAction.ENTER_LONG, 5: SignalAction.EXIT})
        result = engine(strategy).run({"TEST": ramp(12)})

        purposes = [order.purpose for order in result.orders]

        assert purposes == [ENTRY, EXIT]

    def test_a_triggered_stop_says_it_was_a_stop(self) -> None:
        """The row this whole change is for. A stop-out and a signal exit are two
        different facts about a trade, and only `purpose` separates them."""
        strategy = ScriptedStrategy({1: SignalAction.ENTER_LONG}, stop_loss=Decimal("101.5"))
        # Falling prices, so the stop is taken out rather than the position
        # simply being held to the end of the run.
        bars = [bar(i, open_=110 - i, high=111 - i, low=108 - i, close=109 - i) for i in range(12)]

        result = engine(strategy).run({"TEST": bars})
        exits = [order.purpose for order in result.orders if order.purpose != ENTRY]

        assert exits == [STOP_LOSS]

    def test_a_hit_target_says_it_was_a_target(self) -> None:
        strategy = ScriptedStrategy({1: SignalAction.ENTER_LONG}, take_profit=Decimal("105"))

        result = engine(strategy).run({"TEST": ramp(12)})
        exits = [order.purpose for order in result.orders if order.purpose != ENTRY]

        assert exits == [TAKE_PROFIT]

    def test_the_reconstruction_reads_them_back(self) -> None:
        """End to end, because the value of `purpose` is entirely in what reads it.

        Same fold the live analytics use, so a backtested trade and a live one are
        the same shape — which is the precondition for comparing them at all.
        """
        strategy = ScriptedStrategy({2: SignalAction.ENTER_LONG}, stop_loss=Decimal("101.5"))
        bars = [bar(i, open_=110 - i, high=111 - i, low=108 - i, close=109 - i) for i in range(12)]

        result = engine(strategy).run({"TEST": bars})
        trades = PerformanceAnalyzer().build_trades(result.orders)

        assert [trade.exit_reason for trade in trades] == ["stop_loss"]


class TestProgressReporting:
    """The engine tells a caller how far it has got, if asked.

    A callback rather than anything the engine does itself: reporting means
    writing somewhere, and core writes nowhere (CLAUDE.md §1.3). The CLI passes
    none and is unaffected.
    """

    def test_no_callback_is_the_default_and_changes_nothing(self) -> None:
        strategy = ScriptedStrategy({2: SignalAction.ENTER_LONG})

        result = engine(strategy).run({"TEST": ramp(10)})

        assert len(result.equity_curve) == 10

    def test_the_first_and_last_reports_are_zero_and_the_whole_timeline(self) -> None:
        """Both unconditional, and each for its own reason.

        The first, so a run whose bars are still being walked shows 0 rather than
        nothing at all. The last, so a finished run reports the *whole* timeline
        rather than whatever the final multiple of `PROGRESS_EVERY` happened to
        be — a bar stuck at 96% on a completed run is a support question.
        """
        seen: list[tuple[int, int]] = []
        bars = ramp(10)

        eng = engine(ScriptedStrategy({}))
        eng.on_progress = lambda done, total: seen.append((done, total))
        eng.run({"TEST": bars})

        assert seen[0] == (0, 10)
        assert seen[-1] == (10, 10)

    def test_a_short_run_reports_only_those_two(self) -> None:
        """`PROGRESS_EVERY` is 500, so a ten-bar run hits no interval report at
        all — which is the case that would show nothing without them."""
        assert PROGRESS_EVERY > 10
        seen: list[tuple[int, int]] = []

        eng = engine(ScriptedStrategy({}))
        eng.on_progress = lambda done, total: seen.append((done, total))
        eng.run({"TEST": ramp(10)})

        assert seen == [(0, 10), (10, 10)]

    def test_a_report_lands_on_the_interval(self) -> None:
        """With enough bars to cross `PROGRESS_EVERY` once, the middle report
        appears — so the bar moves during a long run rather than jumping from 0
        to done."""
        count = PROGRESS_EVERY + 5
        seen: list[tuple[int, int]] = []

        eng = engine(ScriptedStrategy({}))
        eng.on_progress = lambda done, total: seen.append((done, total))
        eng.run({"TEST": ramp(count)})

        assert (PROGRESS_EVERY, count) in seen
        assert seen[-1] == (count, count)

    def test_a_raising_callback_is_not_swallowed(self) -> None:
        """Deliberately not caught here.

        A progress reporter that fails is a bug in the caller, and swallowing it
        would hide the reason the bar stopped moving. The adapter that *can* fail
        for ordinary reasons — a Redis hop — swallows its own errors, where
        "failed" has a known meaning (`BacktestQueue.report`).
        """
        eng = engine(ScriptedStrategy({}))

        def boom(done: int, total: int) -> None:
            raise RuntimeError("reporter is broken")

        eng.on_progress = boom

        with pytest.raises(RuntimeError, match="reporter is broken"):
            eng.run({"TEST": ramp(5)})
