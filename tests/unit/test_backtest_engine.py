"""Backtest engine — the lookahead guarantees.

If these pass and the engine is still wrong, every backtest the platform
produces is fiction. Treat a failure here as a correctness emergency, never as
a test to relax.

These inject a permissive risk double, and the reason has changed even though
the double has not. It was a phase boundary — the chain did not exist yet. Now
it does, and `build_engine` gives every real run `backtest_rules()`; the double
is what keeps *these* tests about fill timing rather than about how a rule sizes
a book. A fixture whose expected P&L moved because a limit was tuned would be a
lookahead test nobody could trust. `tests/unit/test_risk_engine.py` owns the
rules, and the tests below that assert the gate is *reached* still do.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
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
from atp_core.errors import DataGapError, LookaheadError, UnadjustedDataError
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
        # A synthetic series has no corporate actions, so the adjusted close is
        # the close — what an `--adjusted` backfill stores for a symbol that never
        # split. Present rather than null because the engine prices off adjusted
        # closes and refuses a series that has none (CLAUDE.md §5).
        adj_close=Decimal(str(close)),
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


def series(days: list[int], *, symbol: str = "TEST") -> list[Bar]:
    """Bars on exactly the given day offsets from `START`.

    `ramp` covers every calendar day, which is the one shape real daily data
    never has. Gap tests need to state their own spacing.
    """
    return [
        bar(i, open_=100 + i, high=101.5 + i, low=99 + i, close=100.5 + i, symbol=symbol)
        for i in days
    ]


class _RiskDouble:
    """The surface `BacktestEngine` uses of a `RiskEngine`, and only that.

    Two methods, because the engine calls two: `validate` on every order and
    `anchor_session` on every new session date. A double carrying one of them
    would pass until the day the engine started calling the other, which is
    exactly what happened when session anchoring landed — so both live here,
    and `anchored` is recorded because "was the day boundary reached" is worth
    asserting rather than assuming.
    """

    def __init__(self) -> None:
        self.seen: list[Order] = []
        self.anchored: list[Decimal] = []

    def anchor_session(self, equity: Decimal) -> int:
        self.anchored.append(equity)
        return 1


class _AllowAllRisk(_RiskDouble):
    def validate(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        self.seen.append(order)
        return RiskDecision.allow()


class _DenyAllRisk(_RiskDouble):
    def validate(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        self.seen.append(order)
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
    start: datetime = START,
    end: datetime | None = None,
    max_gap_days: int = 10,
) -> BacktestEngine:
    config = BacktestConfig(
        symbols=symbols or ["TEST"],
        start=start,
        end=end if end is not None else START + timedelta(days=365),
        timeframe=Timeframe.D1,
        starting_cash=cash,
        max_volume_participation=participation,
        max_gap_days=max_gap_days,
    )
    return BacktestEngine(
        strategy=strategy,
        config=config,
        cost_model=ZeroCostModel(),
        risk_engine=risk if risk is not None else _AllowAllRisk(),  # type: ignore[arg-type]  # a narrower surface than the class; see the double's docstring
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
        # The warning names the rule, not just the fact of a refusal. Both
        # stages that can refuse — the sizer and the chain — book through one
        # path now, so the rule name is what tells them apart in a result.
        assert any("refused (test_rule)" in w for w in result.warnings)
        assert result.orders[0].rejected_by == "test_rule"
        # A refusal in a backtest carries what the same refusal carries live.
        # The rule was already in the warning text above; this is the field a
        # reader can group by, and the engine is the mirror of the live loop.
        assert result.orders[0].rejected_by == "test_rule"
        assert result.orders[0].reject_reason == "denied by test"

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

    def test_an_interior_hole_is_refused(self) -> None:
        """The failure this check exists for: bars either side, none inside.

        Nothing downstream would have said so. `closes()` slices by position,
        so an average spanning the hole quietly mixes prices from both sides.
        """
        with pytest.raises(DataGapError, match="hole"):
            engine(ScriptedStrategy({})).run({"TEST": series([0, 1, 2, 3, 4, *range(700, 705)])})

    def test_the_hole_refusal_names_the_range_to_backfill(self) -> None:
        """The next thing anyone does with this error is re-fetch."""
        with pytest.raises(DataGapError) as caught:
            engine(ScriptedStrategy({})).run({"TEST": series([0, 1, 2, *range(400, 404)])})

        message = str(caught.value)
        assert "backfill_bars.py --symbols TEST" in message
        assert "--start 2024-01-04" in message  # the last bar before the hole
        assert "--end 2025-02-05" in message  # the first bar after it

    def test_weekends_and_holidays_are_not_holes(self) -> None:
        """Real daily data is full of three- and four-day steps. A check that
        fired on those would be turned off within a week."""
        weekdays = [0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 15, 16, 17, 18]  # incl. a Monday holiday
        result = engine(ScriptedStrategy({})).run({"TEST": series(weekdays)})
        assert not any("hole" in w for w in result.warnings)

    def test_max_gap_days_admits_a_genuine_closure(self) -> None:
        """A symbol really can stop trading for a month. The threshold is a
        setting so that case is expressible — not so a real hole can be hidden."""
        bars = series([0, 1, 2, *range(40, 44)])
        with pytest.raises(DataGapError, match="hole"):
            engine(ScriptedStrategy({})).run({"TEST": bars})
        engine(ScriptedStrategy({}), max_gap_days=60).run({"TEST": bars})

    def test_an_unrequested_empty_series_is_ignored(self) -> None:
        """A symbol outside `config.symbols` has no first or last bar to compare
        against the window, and must not take the run down on the way past."""
        result = engine(ScriptedStrategy({}), start=START, end=START + timedelta(days=9)).run(
            {"TEST": ramp(10), "EXTRA": []}
        )
        assert not any("coverage" in w for w in result.warnings)

    def test_a_late_start_warns_rather_than_refusing(self) -> None:
        """An ETF's inception is a legitimate reason to have no history, so this
        cannot refuse — but it was silent before, and silence is what let a
        nine-year request quietly become a five-year run."""
        result = engine(ScriptedStrategy({}), start=START - timedelta(days=400)).run(
            {"TEST": ramp(10)}
        )
        assert any("coverage" in w and "no bars until after" in w for w in result.warnings)

    def test_an_early_end_warns_too(self) -> None:
        result = engine(ScriptedStrategy({}), end=START + timedelta(days=400)).run(
            {"TEST": ramp(10)}
        )
        assert any("coverage" in w and "stop supplying bars before" in w for w in result.warnings)

    def test_a_full_window_warns_about_neither(self) -> None:
        result = engine(ScriptedStrategy({}), start=START, end=START + timedelta(days=9)).run(
            {"TEST": ramp(10)}
        )
        assert not any("coverage" in w for w in result.warnings)

    def test_coverage_is_one_warning_for_the_whole_universe(self) -> None:
        """Not one per symbol. Twenty lines would push every other warning out
        of the ten a CLI shows, which is the failure this is here to prevent."""
        symbols = [f"S{i}" for i in range(20)]
        result = engine(
            ScriptedStrategy({}),
            symbols=symbols,
            start=START - timedelta(days=400),
            end=START + timedelta(days=9),
        ).run({s: ramp(10, symbol=s) for s in symbols})

        coverage = [w for w in result.warnings if "coverage" in w]
        assert len(coverage) == 1
        assert "20 symbol(s)" in coverage[0]
        assert "and 12 more" in coverage[0]

    def test_coverage_warnings_lead_the_list(self) -> None:
        """A caller printing the first handful has to see them."""
        result = engine(
            ScriptedStrategy({5: SignalAction.ENTER_LONG}),
            risk=_DenyAllRisk(),
            start=START - timedelta(days=400),
        ).run({"TEST": ramp(10)})

        assert result.warnings
        assert "coverage" in result.warnings[0]
        assert any("refused" in w for w in result.warnings)


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


class TestRealisedVersusUnrealised:
    """A run that ends holding a position reports two different things, and only
    one of them is a track record.

    Every per-trade metric is computed from closed round trips, because that is
    the only point a trade's P&L is known. `ending_equity` marks the open ones
    to the last bar. Nothing on the report used to distinguish them, so a run
    whose closed trades lost money could still show a gain.
    """

    def held(self) -> BacktestResult:
        """Enters on bar 5 and never exits, over a rising ramp."""
        return engine(ScriptedStrategy({5: SignalAction.ENTER_LONG})).run({"TEST": ramp(20)})

    def test_an_open_winner_is_a_gain_with_no_completed_trade(self) -> None:
        result = self.held()

        assert result.portfolio.open_positions
        assert result.metrics["num_trades"] == 0  # nothing closed
        assert result.total_return > 0  # and yet
        assert result.unrealized_pnl > 0
        # Nothing has been realised at all: these engines use `ZeroCostModel`,
        # so entering cost nothing and the entire gain is a mark.
        assert result.realized_pnl == Decimal(0)

    def test_the_two_halves_reconcile_to_ending_equity(self) -> None:
        """Exactly, in Decimal. `realized_pnl` is the remainder for this reason:
        two independent sums would drift and the report would not add up."""
        result = self.held()
        equity = result.portfolio
        assert result.realized_pnl + result.unrealized_pnl == equity.equity - equity.starting_equity

    def test_a_closed_run_carries_nothing_unrealised(self) -> None:
        result = engine(ScriptedStrategy({5: SignalAction.ENTER_LONG, 12: SignalAction.EXIT})).run(
            {"TEST": ramp(20)}
        )

        assert result.portfolio.open_positions == []
        assert result.unrealized_pnl == Decimal(0)
        assert result.realized_pnl == result.portfolio.equity - result.portfolio.starting_equity

    def test_the_report_carries_the_split(self) -> None:
        report = self.held().to_report()

        assert report["open_positions"] == 1
        assert Decimal(str(report["unrealized_pnl"])) > 0
        assert Decimal(str(report["realized_pnl"])) + Decimal(str(report["unrealized_pnl"])) == (
            Decimal(str(report["ending_equity"])) - Decimal(str(report["starting_equity"]))
        )


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

        def boom(bars_done: int, bars_total: int) -> None:
            raise RuntimeError("reporter is broken")

        eng.on_progress = boom

        with pytest.raises(RuntimeError, match="reporter is broken"):
            eng.run({"TEST": ramp(5)})


#: A 1:8 reverse split at bar 5, in the shape the vendor stores it: raw prices
#: as traded on the day, and an `adj_close` that is continuous across the
#: action. Bars 0–4 are quoted pre-split at an eighth of the adjusted price;
#: bars 5–9 are quoted post-split, so raw and adjusted agree.
#:
#: The adjusted series is the boring one on purpose — bar i is worth 100 + i,
#: flat within the bar, so every fill price and every mark below is readable
#: straight off the index.
def split_bars(count: int = 10, *, split_at: int = 5, factor: int = 8) -> list[Bar]:
    series = []
    for index in range(count):
        adjusted = Decimal(100 + index)
        raw = adjusted if index >= split_at else adjusted / factor
        series.append(
            Bar(
                symbol="TEST",
                ts=START + timedelta(days=index),
                timeframe=Timeframe.D1,
                open=raw,
                high=raw,
                low=raw,
                close=raw,
                volume=Decimal(1_000_000),
                adj_close=adjusted,
            )
        )
    return series


def _adjusted(candle: Bar) -> Decimal:
    """`split_bars` sets one on every bar; the domain type allows `None`."""
    assert candle.adj_close is not None
    return candle.adj_close


def _quoted_at(candle: Bar, price: Decimal) -> Bar:
    """`candle` re-quoted at `price`. `split_bars` makes every bar flat within
    itself, so re-quoting one is four fields with the same value."""
    return replace(candle, open=price, high=price, low=price, close=price)


class TestCorporateActions:
    """A split must not be readable as a return.

    This is the bug the platform shipped with: GE's 1:8 reverse split on
    2021-08-02 octupled its raw price overnight, and a `buy_and_hold` run over
    twenty symbols booked it as a single +51.16% day — twenty-eight standard
    deviations of that run's own daily volatility, and the largest move in six
    years by a factor of four. Nothing in the result said so. It is the quietest
    way a backtest becomes fiction, which is why the engine now refuses to price
    off raw closes at all (CLAUDE.md §5, docs/adr/0017).
    """

    def test_a_reverse_split_earns_the_market_and_not_the_factor(self) -> None:
        """Hand-computed. 100 shares, no costs, 100,000 starting cash:

            bar 0  strategy says ENTER_LONG
            bar 1  fills at the adjusted open = 101   cash 100,000 - 10,100 = 89,900
            bar 5  the raw price octuples; the adjusted price does not
            bar 9  still held, marked at the adjusted close = 109 → 10,900

        Ending equity = 89,900 + 10,900 = 100,800, a return of 0.008 — which is
        exactly `qty x (last close - entry open) / starting equity`, the market's
        move over the window it was held.

        Pricing the same bars raw would buy at 12.625 for 1,262.50 and mark the
        position at 109 anyway, for an ending equity of 109,637.50 and a return
        of 0.096375: twelve times the truth, all of it the split factor.
        """
        bars = split_bars()

        # The fixture is only a test of anything if the discontinuity is really
        # in it: bar 4 is quoted at an eighth of bar 5 on the same true price.
        assert bars[4].close * 8 == Decimal("104")
        assert bars[5].close == Decimal("105")

        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        result = engine(strategy).run({"TEST": bars})

        (entry,) = result.orders
        assert entry.avg_fill_price == Decimal("101")  # adjusted open, not 12.625
        assert entry.filled_qty == Decimal(100)

        assert result.portfolio.cash == Decimal("89900")
        assert result.portfolio.equity == Decimal("100800")
        assert result.total_return == Decimal("0.008")

    def test_the_equity_curve_has_no_step_where_the_split_is(self) -> None:
        """The symptom a reader would actually have seen. Once held, the curve
        moves by `qty x 1` a bar because the adjusted price does — including
        across bar 5, where the raw series jumps by a factor of eight."""
        result = engine(ScriptedStrategy({0: SignalAction.ENTER_LONG})).run({"TEST": split_bars()})
        curve = [equity for _, equity in result.equity_curve]

        # Bars 1..9 are held, so each step is 100 shares x one unit of price.
        steps = {b - a for a, b in pairwise(curve[1:])}
        assert steps == {Decimal(100)}

        # And the bar the split lands on is not special in any way.
        assert curve[5] - curve[4] == Decimal(100)

    def test_a_forward_split_is_not_a_crash_either(self) -> None:
        """The mirror, and the more common one: AAPL's 4:1 in August 2020 would
        read as a position losing 75% overnight. `factor=4` here quotes bars 0–4
        at four times the adjusted price rather than an eighth of it."""
        bars = split_bars(factor=1)  # adjusted == raw, then re-quote the front
        bars = [
            candle if index >= 5 else _quoted_at(candle, _adjusted(candle) * 4)
            for index, candle in enumerate(bars)
        ]
        result = engine(ScriptedStrategy({0: SignalAction.ENTER_LONG})).run({"TEST": bars})

        assert result.total_return == Decimal("0.008")
        steps = {b - a for a, b in pairwise([e for _, e in result.equity_curve][1:])}
        assert steps == {Decimal(100)}

    def test_bars_with_no_adjusted_close_are_refused(self) -> None:
        """A raw-only backfill leaves the column unset. Running anyway is what
        produced the bug, so the run stops rather than falling back."""
        bars = [replace(candle, adj_close=None) for candle in split_bars()]

        with pytest.raises(UnadjustedDataError, match="TEST"):
            engine(ScriptedStrategy({0: SignalAction.ENTER_LONG})).run({"TEST": bars})

    def test_the_refusal_names_the_backfill_that_fixes_it(self) -> None:
        bars = [replace(candle, adj_close=None) for candle in split_bars()]

        with pytest.raises(UnadjustedDataError, match="--raw-only"):
            engine(ScriptedStrategy({})).run({"TEST": bars})

    def test_one_unadjusted_symbol_refuses_the_whole_run(self) -> None:
        """Not just its own series. A universe where nineteen symbols are
        adjusted and one is not produces a result that is right about nineteen
        of them, which is indistinguishable from being right."""
        clean = [replace(c, symbol="OK") for c in split_bars()]
        dirty = [replace(c, symbol="BAD", adj_close=None) for c in split_bars()]

        with pytest.raises(UnadjustedDataError, match="BAD"):
            engine(ScriptedStrategy({}), symbols=["OK", "BAD"]).run({"OK": clean, "BAD": dirty})

    def test_an_already_adjusted_series_is_priced_unchanged(self) -> None:
        """Most symbols in most windows have no corporate action, so their
        stored `adj_close` equals their close. That path must not move a price."""
        flat = [
            bar(i, open_=100 + i, high=101.5 + i, low=99 + i, close=100.5 + i) for i in range(10)
        ]
        assert all(candle.is_adjusted for candle in flat)

        result = engine(ScriptedStrategy({0: SignalAction.ENTER_LONG})).run({"TEST": flat})

        (entry,) = result.orders
        assert entry.avg_fill_price == Decimal("101")  # bar 1's open, untouched
