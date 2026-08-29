"""Stops, as a backtest actually applies them.

`StopManager` has existed in `atp_core.risk` since Phase 3 opened, with a live
caller in `StrategyRunner`. What did not exist is any of it reaching a replay:
`BacktestEngine` watched only the levels a `Signal` happened to carry, and none
of the shipped strategies emits one. So a strategy configured live with an ATR
stop was backtested naked, and the number the backtest reported was produced by
a strategy that does not exist.

Five properties, each a way the wiring could be present and still wrong:

1. **Opt-in.** A spec stored before stops were configurable still reproduces.
   Silently defaulting an unconfigured run to `atr` would change what every
   historical run reports.
2. **The stop precedes the sizing.** `risk_pct` is *defined* off the distance to
   the stop, so a stop derived after sizing is a stop that arrives too late to
   size anything — which is why every `risk_pct` entry was refused before this.
3. **The engine arms what the live router arms.** Same signal, same fill, same
   two levels. Anything else and a backtest is measuring a different strategy.
4. **The levels are tested against bar extremes, through one implementation.**
   `should_trigger` and `target_hit`, not a second copy of the comparison.
5. **No lookahead in the stop.** An ATR over the whole series would place stops
   using volatility that had not happened — the quietest way to fake a result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from atp_core.backtest.costs import ZeroCostModel
from atp_core.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    FixedQtySizer,
    RiskBasedSizer,
)
from atp_core.backtest.ports import BacktestRunSpec
from atp_core.backtest.runner import STOP_TYPES, resolve_stop_config
from atp_core.domain import (
    SIZING,
    Bar,
    OrderStatus,
    Portfolio,
    Position,
    Side,
    Signal,
    SignalAction,
    Timeframe,
)
from atp_core.domain.enums import StopType
from atp_core.errors import ConfigError
from atp_core.execution.idempotency import ENTRY, STOP_LOSS, TAKE_PROFIT, TIME_EXIT
from atp_core.indicators import dispatch
from atp_core.risk.engine import RiskDecision
from atp_core.risk.rules import position_size
from atp_core.risk.stops import FROM_ENTRY_TYPES, StopConfig, StopManager, target_hit
from atp_core.strategy.base import Strategy

if TYPE_CHECKING:
    from collections.abc import Iterable

    from atp_core.backtest.engine import BacktestResult
    from atp_core.domain import Order
    from atp_core.strategy.context import StrategyContext

START = datetime(2024, 1, 2, tzinfo=UTC)
SYMBOL = "TEST"


# ── fixtures ────────────────────────────────────────────────────────────────


def bar(
    index: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1_000_000,
    symbol: str = SYMBOL,
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
        # No corporate actions in a synthetic series, so the adjusted close is the
        # close. The engine refuses a series with none of them (CLAUDE.md §5).
        adj_close=Decimal(str(close)),
    )


def ramp(count: int = 20) -> list[Bar]:
    """Bar i: open 100+i, close 100.5+i. Every level below is hand-computed
    from these numbers, so a changed fixture is a changed expectation."""
    return [
        bar(i, open_=100 + i, high=101.5 + i, low=99 + i, close=100.5 + i) for i in range(count)
    ]


def flat(count: int, *, price: float = 100.0, span: float = 1.0) -> list[Bar]:
    """A series that goes nowhere, so a stop only fires when one is placed
    somewhere it should not be."""
    return [
        bar(i, open_=price, high=price + span, low=price - span, close=price) for i in range(count)
    ]


class _Risk:
    """The two methods `BacktestEngine` calls on a `RiskEngine`, approving
    everything. These tests are about where a stop lands, not about how a limit
    sizes a book; `tests/unit/test_backtest_risk.py` owns the chain."""

    def anchor_session(self, equity: Decimal) -> int:
        return 1

    def validate(
        self, order: Order, portfolio: Portfolio, pending: Iterable[Order] = ()
    ) -> RiskDecision:
        return RiskDecision.allow()


class Scripted(Strategy):
    """Emits whatever the script says on the n-th bar it is shown, so what the
    engine does with a stop is never entangled with what an indicator did."""

    name: ClassVar[str] = "scripted"

    def __init__(
        self,
        script: dict[int, SignalAction],
        *,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> None:
        super().__init__(None)
        self.script = script
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.calls = 0

    @property
    def warmup_bars(self) -> int:
        return 0

    def on_bar(self, ctx: StrategyContext, bar_: Bar) -> list[Signal]:
        index = self.calls
        self.calls += 1
        action = self.script.get(index)
        if action is None:
            return []
        entering = action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT)
        return [
            Signal(
                strategy_id=self.name,
                symbol=bar_.symbol,
                action=action,
                ts=ctx.now,
                stop_loss_price=self.stop_loss if entering else None,
                take_profit_price=self.take_profit if entering else None,
            )
        ]


def engine(
    strategy: Strategy,
    *,
    stop_config: StopConfig | None = None,
    sizer: Any = None,
    cash: Decimal = Decimal(100_000),
) -> BacktestEngine:
    return BacktestEngine(
        strategy=strategy,
        config=BacktestConfig(
            symbols=[SYMBOL],
            start=START,
            end=START + timedelta(days=365),
            timeframe=Timeframe.D1,
            starting_cash=cash,
        ),
        cost_model=ZeroCostModel(),
        risk_engine=_Risk(),  # type: ignore[arg-type]  # a narrower surface than the class; see the double's docstring
        position_sizer=sizer if sizer is not None else FixedQtySizer(Decimal(10)),
        stop_config=stop_config,
    )


def run(
    strategy: Strategy, bars: list[Bar], **kwargs: Any
) -> tuple[BacktestEngine, BacktestResult]:
    eng = engine(strategy, **kwargs)
    return eng, eng.run({SYMBOL: bars})


def a_spec(**overrides: object) -> BacktestRunSpec:
    fields: dict[str, object] = {
        "strategy_id": "sma_crossover",
        "symbols": (SYMBOL,),
        "start": START,
        "end": START + timedelta(days=90),
        "timeframe": "1d",
        "starting_cash": "100000",
        "cost_model": "alpaca_equities",
        "params": {},
        "qty": "10",
    }
    fields.update(overrides)
    return BacktestRunSpec(**fields)  # type: ignore[arg-type]


def fills(result: BacktestResult, purpose: str) -> list[Order]:
    return [
        order
        for order in result.orders
        if order.purpose == purpose and order.status is OrderStatus.FILLED
    ]


# ── 1. opt-in ───────────────────────────────────────────────────────────────


class TestAnUnconfiguredRunIsUnchanged:
    """A stored spec has to keep reproducing. The fields default to "no stop",
    which is what this engine did unconditionally before they existed."""

    def test_a_spec_written_before_stops_existed_asks_for_none(self) -> None:
        assert resolve_stop_config(a_spec()) is None

    def test_the_engine_arms_nothing_it_was_not_given(self) -> None:
        eng, _ = run(Scripted({0: SignalAction.ENTER_LONG}), ramp(5))
        position = eng._portfolio.position(SYMBOL)
        assert not position.is_flat
        assert position.stop_loss_price is None
        assert position.take_profit_price is None

    def test_the_same_run_with_a_config_arms_one(self) -> None:
        """The pair matters more than either half: it is the difference between
        "stops are off by default" and "stops do not work"."""
        eng, _ = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            ramp(5),
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.10"), broker_side=False
            ),
        )
        assert eng._portfolio.position(SYMBOL).stop_loss_price is not None

    def test_a_signals_own_level_is_never_overridden(self) -> None:
        """A strategy that named a stop chose it deliberately; a configured
        default landing on top of it would silently overrule the strategy."""
        eng, _ = run(
            Scripted({0: SignalAction.ENTER_LONG}, stop_loss=Decimal("95")),
            ramp(5),
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.10"), broker_side=False
            ),
        )
        position = eng._portfolio.position(SYMBOL)
        assert position.stop_loss_price == Decimal("95")
        # ...while the gap the signal left is still filled: 101 × 1.10.
        assert position.take_profit_price == Decimal("111.10")


# ── 2. the stop precedes the sizing ─────────────────────────────────────────


class TestTheStopArrivesBeforeTheSizer:
    """`risk_pct` sizing is defined off `|entry − stop|`. This ordering is the
    whole reason the derivation sits in `_handle_signal` rather than at fill."""

    def test_risk_pct_with_no_stop_anywhere_is_refused_and_recorded(self) -> None:
        _, result = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            ramp(5),
            sizer=RiskBasedSizer("risk_pct", Decimal("0.01")),
        )
        refused = [o for o in result.orders if o.status is OrderStatus.REJECTED_RISK]
        assert len(refused) == 1
        assert refused[0].rejected_by == SIZING
        assert not fills(result, ENTRY)

    def test_a_configured_stop_makes_the_same_signal_sizeable(self) -> None:
        """And the quantity is `position_size`'s, not the engine's: one sizing
        function, two callers (ADR 0006)."""
        _, result = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            ramp(5),
            sizer=RiskBasedSizer("risk_pct", Decimal("0.01")),
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.10"), broker_side=False
            ),
        )
        entries = fills(result, ENTRY)
        assert len(entries) == 1

        # Sized at the decision bar's close (100.5) against the stop derived
        # from it (90.45): $1,000 of risk over $10.05 a share.
        expected = position_size(
            "risk_pct",
            Decimal(100_000),
            Decimal("100.5"),
            stop_price=Decimal("90.45"),
            risk_pct=Decimal("0.01"),
        )
        assert expected == Decimal(99)
        assert entries[0].filled_qty == expected

    def test_a_time_stop_does_not_pretend_to_be_a_level(self) -> None:
        """It says when to leave, not where, so it cannot make a `risk_pct`
        entry sizeable — and refusing is the honest answer rather than
        measuring risk against an invented distance."""
        _, result = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            ramp(5),
            sizer=RiskBasedSizer("risk_pct", Decimal("0.01")),
            stop_config=StopConfig(stop_type=StopType.TIME, bars=3, broker_side=False),
        )
        refused = [o for o in result.orders if o.status is OrderStatus.REJECTED_RISK]
        assert [o.rejected_by for o in refused] == [SIZING]


# ── 3. the engine arms what the live router arms ────────────────────────────


class TestTheLevelsMatchTheLivePath:
    def test_stop_from_the_decision_price_and_target_from_the_fill(self) -> None:
        """The asymmetry is deliberate and it is *live's*. `_with_stop` derives
        the stop from the last price and it rides into the router's requested
        protection; `submit_protective_orders` then fills only the gap, so the
        target alone is anchored to the average fill. Matching that beats being
        tidier than it: two anchors in both places is one strategy."""
        eng, _ = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            ramp(5),
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.10"), broker_side=False
            ),
        )
        position = eng._portfolio.position(SYMBOL)
        assert position.avg_entry_price == Decimal(101)  # next bar's open
        assert position.stop_loss_price == Decimal("100.5") * Decimal("0.90")  # decision close
        assert position.take_profit_price == Decimal(101) * Decimal("1.10")  # the fill

    def test_the_engine_and_the_router_agree_level_for_level(self) -> None:
        """Computed through the same `StopManager` calls the router makes, with
        the same anchors. If this drifts, a backtest is measuring a strategy
        production does not run."""
        config = StopConfig(stop_type=StopType.FIXED_PCT, value=Decimal("0.10"), broker_side=False)
        eng, _ = run(Scripted({0: SignalAction.ENTER_LONG}), ramp(5), stop_config=config)
        position = eng._portfolio.position(SYMBOL)

        manager = StopManager()
        assert position.stop_loss_price == manager.initial_stop(Decimal("100.5"), Side.BUY, config)
        assert position.take_profit_price == manager.take_profit_level(
            position.avg_entry_price, Side.BUY, config
        )

    def test_a_short_puts_the_stop_above_and_the_target_below(self) -> None:
        """A sign error here is catastrophic and silent, so both sides are
        driven through the engine rather than only the manager."""
        eng, _ = run(
            Scripted({0: SignalAction.ENTER_SHORT}),
            ramp(5),
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.10"), broker_side=False
            ),
        )
        position = eng._portfolio.position(SYMBOL)
        assert not position.is_long
        assert position.stop_loss_price == Decimal("100.5") * Decimal("1.10")
        assert position.take_profit_price == Decimal(101) * Decimal("0.90")

    def test_no_target_from_a_config_that_cannot_express_one(self) -> None:
        """A trailing rule says when to leave, not where to aim. Arming a target
        anyway would need a distance nothing supplied."""
        eng, _ = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            ramp(5),
            stop_config=StopConfig(
                stop_type=StopType.TRAILING_PCT, value=Decimal("0.05"), broker_side=False
            ),
        )
        position = eng._portfolio.position(SYMBOL)
        assert position.stop_loss_price is not None
        assert position.take_profit_price is None
        assert StopType.TRAILING_PCT not in FROM_ENTRY_TYPES


# ── 4. triggering, through one implementation ───────────────────────────────


class TestTheLevelsFire:
    def test_a_stop_exit_is_labelled_as_one(self) -> None:
        """Not folded into `exit`. Exit-reason attribution is how a strategy's
        stops are judged; a bucket absorbing a second kind of exit makes it
        lie."""
        # Enter at bar 1's open of 100; bar 3 trades down to 90.
        bars = [
            bar(0, open_=100, high=101, low=99, close=100),
            bar(1, open_=100, high=101, low=99, close=100),
            bar(2, open_=100, high=101, low=99, close=100),
            bar(3, open_=100, high=100, low=88, close=89),
        ]
        _, result = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            bars,
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.10"), broker_side=False
            ),
        )
        stopped = fills(result, STOP_LOSS)
        assert len(stopped) == 1
        assert stopped[0].filled_qty == Decimal(10)

    def test_a_wick_through_the_target_counts(self) -> None:
        """The bar reached it. Comparing against the close would pretend a spike
        that hit the target and settled back never happened."""
        bars = [
            bar(0, open_=100, high=101, low=99, close=100),
            bar(1, open_=100, high=101, low=99, close=100),
            bar(2, open_=100, high=115, low=99, close=100),  # spike and back
        ]
        _, result = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            bars,
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.10"), broker_side=False
            ),
        )
        assert len(fills(result, TAKE_PROFIT)) == 1

    def test_a_trailing_stop_ratchets_and_never_widens(self) -> None:
        # 0 signals, 1 fills the entry at 100 (stop 90 from bar 0's close),
        # 2 makes a new high of 120 (stop → 108), 3 makes a lower high (stop
        # must stay), 4 dips through it.
        bars = [
            bar(0, open_=100, high=100.5, low=99.5, close=100),
            bar(1, open_=100, high=100.5, low=99.5, close=100),
            bar(2, open_=113, high=120, low=112, close=120),
            bar(3, open_=115, high=118, low=110, close=112),
            bar(4, open_=110, high=111, low=105, close=106),
        ]
        config = StopConfig(
            stop_type=StopType.TRAILING_PCT, value=Decimal("0.10"), broker_side=False
        )

        ratcheted = engine(Scripted({0: SignalAction.ENTER_LONG}), stop_config=config)
        ratcheted.run({SYMBOL: bars[:3]})
        assert ratcheted._portfolio.position(SYMBOL).stop_loss_price == Decimal(120) * Decimal(
            "0.90"
        )

        # Bar 3 peaks below the high-water mark. A stop recomputed from that
        # bar would sit at 106.2 — a widened stop, which is how a planned small
        # loss becomes an unplanned large one.
        held = engine(Scripted({0: SignalAction.ENTER_LONG}), stop_config=config)
        held.run({SYMBOL: bars[:4]})
        position = held._portfolio.position(SYMBOL)
        assert position.stop_loss_price == Decimal("108.0")
        assert position.high_water_mark == Decimal(120)

        _, result = run(Scripted({0: SignalAction.ENTER_LONG}), bars, stop_config=config)
        assert len(fills(result, STOP_LOSS)) == 1

    def test_the_ratchet_happens_before_the_level_is_tested(self) -> None:
        """A bar that both extends the move and retraces into the *new* stop is
        judged against the stop it justified — which is what a venue-side
        trailing stop would have done. Testing first would let it survive."""
        bars = [
            bar(0, open_=100, high=100.5, low=99.5, close=100),
            bar(1, open_=100, high=100.5, low=99.5, close=100),  # entry at 100, stop 90
            bar(2, open_=110, high=120, low=105, close=110),  # new high 120 → stop 108, low 105
        ]
        config = StopConfig(
            stop_type=StopType.TRAILING_PCT, value=Decimal("0.10"), broker_side=False
        )
        _, result = run(Scripted({0: SignalAction.ENTER_LONG}), bars, stop_config=config)

        # 105 is above the stop bar 1 justified (90) and below the one bar 2
        # justified (108). Testing before ratcheting would let it survive.
        assert len(fills(result, STOP_LOSS)) == 1
        assert fills(result, STOP_LOSS)[0].avg_fill_price == Decimal("108.0")

    def test_a_time_exit_leaves_after_its_bars_at_that_bars_close(self) -> None:
        """A time stop has no level, so the honest price for a decision taken on
        a completed bar is that bar's close."""
        bars = ramp(8)
        _, result = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            bars,
            stop_config=StopConfig(stop_type=StopType.TIME, bars=3, broker_side=False),
        )
        timed = fills(result, TIME_EXIT)
        assert len(timed) == 1
        # Filled at bar 1's open; three completed bars later is bar 4.
        assert timed[0].avg_fill_price == bars[4].close
        assert timed[0].filled_at == bars[4].ts

    def test_a_time_stop_arms_no_price_levels(self) -> None:
        eng = engine(
            Scripted({0: SignalAction.ENTER_LONG}),
            stop_config=StopConfig(stop_type=StopType.TIME, bars=99, broker_side=False),
        )
        eng.run({SYMBOL: ramp(5)})
        position = eng._portfolio.position(SYMBOL)
        assert position.stop_loss_price is None
        assert position.take_profit_price is None

    def test_the_clock_starts_at_the_fill_not_the_signal(self) -> None:
        """`bars=1` exits on the bar after the entry. Counting from the signal
        bar would exit a bar early and shorten every holding period."""
        bars = ramp(6)
        _, result = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            bars,
            stop_config=StopConfig(stop_type=StopType.TIME, bars=1, broker_side=False),
        )
        timed = fills(result, TIME_EXIT)
        assert [o.filled_at for o in timed] == [bars[2].ts]

    def test_a_level_exit_beats_a_time_exit_on_the_same_bar(self) -> None:
        """A position stopped out on a bar left at the stop; it did not also run
        out of time on it. Counting both would double the exit."""
        bars = [
            bar(0, open_=100, high=101, low=99, close=100),
            bar(1, open_=100, high=101, low=99, close=100),
            bar(2, open_=100, high=101, low=80, close=85),
        ]
        eng = engine(
            Scripted({0: SignalAction.ENTER_LONG}, stop_loss=Decimal(90)),
            stop_config=StopConfig(stop_type=StopType.TIME, bars=1, broker_side=False),
        )
        result = eng.run({SYMBOL: bars})
        assert len(fills(result, STOP_LOSS)) == 1
        assert not fills(result, TIME_EXIT)


class TestTargetHitIsSharedNotCopied:
    """It moved to `atp_core.risk.stops` because it had two callers, and two
    implementations of "did the bar reach the target" is the divergence ADR 0006
    exists to refuse."""

    def test_the_live_runner_imports_the_same_function(self) -> None:
        from atp_worker import runner as live

        # As above: the point is that the runner resolved the same object,
        # not that `atp_worker.runner` publishes one.
        assert live.target_hit is target_hit  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        ("qty", "target", "high", "low", "expected"),
        [
            (10, "110", 110, 100, True),  # long, touched exactly
            (10, "110", 109.99, 100, False),
            (10, "110", 115, 100, True),  # long, wicked through
            (-10, "90", 100, 90, True),  # short, touched exactly
            (-10, "90", 100, 90.01, False),
            (-10, "90", 100, 85, True),
        ],
    )
    def test_the_bar_extreme_decides_it(
        self, qty: int, target: str, high: float, low: float, expected: bool
    ) -> None:
        position = Position(symbol=SYMBOL, qty=Decimal(qty), avg_entry_price=Decimal(100))
        position.take_profit_price = Decimal(target)
        assert target_hit(position, bar(0, open_=100, high=high, low=low, close=100)) is expected

    def test_no_target_and_no_position_are_both_false(self) -> None:
        priced = bar(0, open_=100, high=200, low=1, close=100)
        unarmed = Position(symbol=SYMBOL, qty=Decimal(10), avg_entry_price=Decimal(100))
        assert target_hit(unarmed, priced) is False

        empty = Position(symbol=SYMBOL)
        empty.take_profit_price = Decimal(110)
        assert target_hit(empty, priced) is False


# ── 5. no lookahead in the stop ─────────────────────────────────────────────


class TestTheAtrThatPlacesTheStopCannotSeeAhead:
    def test_the_level_uses_only_bars_that_had_closed(self) -> None:
        """Volatility explodes after the entry. A stop placed from the whole
        series would be far wider than one placed from what had happened, and
        the difference is a backtest that survives drawdowns it would not
        have."""
        quiet = [bar(i, open_=100, high=101, low=99, close=100) for i in range(20)]
        wild = [bar(20 + i, open_=100, high=140, low=60, close=100) for i in range(20)]
        bars = quiet + wild

        eng, _ = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            bars,
            stop_config=StopConfig(
                stop_type=StopType.ATR,
                multiplier=Decimal(2),
                period=14,
                broker_side=False,
            ),
        )
        position = eng._portfolio.position(SYMBOL)

        # The decision was taken on bar 0's close, so only bar 0 had closed.
        # With one bar the ATR is undefined, so no level is derivable and the
        # engine leaves the position openly unguarded rather than guessing.
        assert position.stop_loss_price is None

    def test_a_derivable_level_matches_the_visible_window_not_the_series(self) -> None:
        # The wild bars are wide but they never trade down to the stop, so the
        # position stays open and the level it is carrying can be read directly.
        quiet = [bar(i, open_=100, high=101, low=99, close=100) for i in range(20)]
        wild = [bar(20 + i, open_=100, high=140, low=99.5, close=100) for i in range(20)]
        bars = quiet + wild
        config = StopConfig(
            stop_type=StopType.ATR, multiplier=Decimal(2), period=14, broker_side=False
        )

        # Signal on bar 15, so 16 bars have closed — all of them quiet.
        eng, _ = run(Scripted({15: SignalAction.ENTER_LONG}), bars, stop_config=config)
        position = eng._portfolio.position(SYMBOL)
        assert not position.is_flat
        assert position.stop_loss_price is not None

        visible = Decimal(str(dispatch.compute("atr", bars[:16], 14)))
        whole = Decimal(str(dispatch.compute("atr", bars, 14)))
        assert visible != whole  # the fixture would prove nothing otherwise

        manager = StopManager()
        assert position.stop_loss_price == manager.initial_stop(
            bars[15].close, Side.BUY, config, visible
        )

    def test_an_underivable_stop_leaves_the_position_openly_unguarded(self) -> None:
        """Not defaulted to something. A position that looks guarded and is not
        is worse than one openly unguarded (`risk/stops.py`)."""
        eng, result = run(
            Scripted({0: SignalAction.ENTER_LONG}),
            flat(4),
            stop_config=StopConfig(
                stop_type=StopType.CHANDELIER,
                multiplier=Decimal(3),
                period=14,
                broker_side=False,
            ),
        )
        position = eng._portfolio.position(SYMBOL)
        assert not position.is_flat
        assert position.stop_loss_price is None
        assert len(fills(result, ENTRY)) == 1  # and the entry still happened


# ── the spec → config translation ───────────────────────────────────────────


class TestResolveStopConfig:
    def test_every_type_the_manager_knows_is_accepted(self) -> None:
        """The two lists are the same list. A name this accepted and
        `StopManager` did not would fail inside the queue worker rather than as
        a 400 at the door."""
        assert {t.value for t in StopType} == STOP_TYPES

    @pytest.mark.parametrize("stop_type", sorted(STOP_TYPES))
    def test_a_replay_never_claims_a_broker_side_stop(self, stop_type: str) -> None:
        """There is no venue in a replay — the engine *is* the fill model, so a
        config claiming the level rests elsewhere would describe protection
        nothing in the run provides."""
        spec = a_spec(stop_type=stop_type, stop_value="2", stop_bars=5)
        config = resolve_stop_config(spec)
        assert config is not None
        assert config.broker_side is False

    def test_atr_reads_its_value_as_a_multiplier(self) -> None:
        config = resolve_stop_config(a_spec(stop_type="atr", stop_value="2.5", stop_period=20))
        assert config is not None
        assert config.multiplier == Decimal("2.5")
        assert config.value is None
        assert config.period == 20

    def test_a_percentage_stop_reads_its_value_as_a_value(self) -> None:
        config = resolve_stop_config(a_spec(stop_type="fixed_pct", stop_value="0.03"))
        assert config is not None
        assert config.value == Decimal("0.03")
        assert config.multiplier is None

    def test_a_time_stop_reads_bars_and_ignores_value(self) -> None:
        config = resolve_stop_config(a_spec(stop_type="time", stop_bars=7))
        assert config is not None
        assert config.bars == 7
        assert config.value is None

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"stop_type": "trailing"}, "stop_type must be one of"),
            ({"stop_type": "time", "stop_bars": 0}, "positive stop_bars"),
            ({"stop_type": "atr"}, "needs stop_value"),
            ({"stop_type": "atr", "stop_value": "0"}, "stop_value"),
            ({"stop_type": "atr", "stop_value": "-2"}, "stop_value"),
            ({"stop_type": "atr", "stop_value": "two"}, "stop_value"),
            ({"stop_type": "atr", "stop_value": "2", "stop_period": 0}, "stop_period"),
        ],
    )
    def test_a_bad_request_is_a_config_error_at_the_door(
        self, overrides: dict[str, object], expected: str
    ) -> None:
        """`ConfigError` rather than anything else, because that is what the API
        turns into a 400 before the run is ever queued."""
        with pytest.raises(ConfigError, match=expected):
            resolve_stop_config(a_spec(**overrides))
