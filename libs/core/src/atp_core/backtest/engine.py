"""Backtest engine — requirement #2.

An event loop over historical bars, chronologically, with one invariant that the
entire value of this module rests on:

    A strategy may only see data that existed at its decision time.

Enforced structurally: the engine hands the strategy a `BacktestContext` whose
cursor cannot address a bar past the current index, and orders generated on bar
*i* are executed against bar *i+1*. If you find yourself relaxing either, stop —
you are not making the backtest pass, you are making it fictional.

The same loop shape runs live (`apps/worker/runner.py`); only the event source
and the broker differ. Keeping them structurally identical is what lets a
backtested strategy be trusted in paper and live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

import numpy as np

from atp_core.backtest.metrics import (
    TRADING_DAYS_PER_YEAR,
    PerformanceMetrics,
    compute_all,
)
from atp_core.clock import SimulatedClock
from atp_core.domain import (
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Portfolio,
    Position,
    Side,
    SignalAction,
    Timeframe,
    TimeInForce,
)
from atp_core.errors import BacktestError, DataGapError, LookaheadError
from atp_core.execution.idempotency import ENTRY, EXIT, STOP_LOSS, TAKE_PROFIT
from atp_core.execution.matching import intended_price
from atp_core.indicators import dispatch
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.backtest.costs import CostModel
    from atp_core.domain import Bar, Signal
    from atp_core.risk.engine import RiskEngine
    from atp_core.strategy.base import Strategy

log = get_logger(__name__)

#: Bars between progress reports. A multi-year minute-bar run walks hundreds of
#: thousands of bars, and a report on each would spend more on round trips than
#: on the backtest; at 500 a daily run over five years reports twice and a minute
#: run over a year reports ~200 times, which is a smooth enough bar.
PROGRESS_EVERY = 500

#: A regular US equity session, in seconds. Used only to annualise: a minute
#: backtest has ~390 bars a day, and annualising it at 252 would understate its
#: volatility by a factor of twenty.
_SESSION_SECONDS = 390 * 60


def _periods_per_year(timeframe: Timeframe) -> int:
    """Bars per year, for annualising a return series of this timeframe."""
    if timeframe is Timeframe.D1:
        return TRADING_DAYS_PER_YEAR
    return TRADING_DAYS_PER_YEAR * (_SESSION_SECONDS // timeframe.seconds)


class PositionSizer(Protocol):
    """Turns intent into a quantity.

    A seam, not a home: real sizing is risk-based and lands with the risk engine
    (docs/RISK.md 'Position sizing', roadmap Phase 3). The engine takes one
    rather than computing a quantity itself, so that when `risk.rules
    .position_size` exists there is nothing here to delete.
    """

    def __call__(self, signal: Signal, portfolio: Portfolio, price: Decimal) -> Decimal: ...


class ProgressCallback(Protocol):
    """Told how far through the timeline the run is, as it goes.

    Called with (bars completed, bars total) and expected to be cheap and
    non-raising: it is invoked from inside the event loop over every bar, and a
    callback that blocked on a socket would make the run's duration a property of
    the network. `BacktestEngine` calls it every `PROGRESS_EVERY` bars for the
    same reason — a multi-year minute-bar run has hundreds of thousands of bars,
    and one round trip each would cost more than the backtest.

    An exception raised here is **not** caught. A progress reporter that fails is
    a bug in the caller, and swallowing it would hide the reason the bar on the
    screen stopped moving. Callers that cannot guarantee delivery — which is all
    of them, since the store is a network hop away — swallow their own errors at
    the point where they know what failure means (`BacktestQueue.report`).
    """

    def __call__(self, bars_done: int, bars_total: int) -> None: ...


@dataclass(frozen=True, slots=True)
class FixedQtySizer:
    """A constant share count.

    For exercising engine mechanics only — never for evaluating a strategy, for
    the same reason `ZeroCostModel` is not an execution model. Sizing every
    trade identically ignores volatility, which is precisely the mistake
    docs/RISK.md exists to prevent.
    """

    qty: Decimal

    def __call__(self, signal: Signal, portfolio: Portfolio, price: Decimal) -> Decimal:
        return self.qty


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    symbols: list[str]
    start: datetime
    end: datetime
    timeframe: Timeframe
    starting_cash: Decimal = Decimal("100000")

    #: Fill orders at the next bar's open. Fills at the *signal* bar's close are
    #: the single most common way a backtest overstates returns.
    fill_at_next_open: bool = True

    #: Reject fills where our order exceeds this share of the bar's volume.
    #: Without it, a backtest happily "buys" 10× a small-cap's daily turnover.
    max_volume_participation: Decimal = Decimal("0.10")

    warmup_bars: int | None = None  # defaults to strategy.warmup_bars


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    strategy_name: str
    portfolio: Portfolio
    orders: list[Order] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_return(self) -> Decimal:
        """Fractional return on starting equity. 0.25 is +25%."""
        start = self.portfolio.starting_equity
        if start == 0:
            return Decimal(0)
        return (self.portfolio.equity - start) / start

    def to_report(self) -> dict[str, object]:
        """Serialisable summary for the API and the dashboard."""
        filled = [o for o in self.orders if o.filled_qty > 0]
        return {
            "strategy": self.strategy_name,
            "symbols": list(self.config.symbols),
            "timeframe": self.config.timeframe.value,
            "start": self.config.start.isoformat(),
            "end": self.config.end.isoformat(),
            "starting_equity": str(self.portfolio.starting_equity),
            "ending_equity": str(self.portfolio.equity),
            "total_return": str(self.total_return),
            "orders": len(self.orders),
            "filled_orders": len(filled),
            "signals": len(self.signals),
            "fees": str(sum((o.total_fees for o in self.orders), Decimal(0))),
            "metrics": dict(self.metrics),
            "warnings": list(self.warnings),
        }


class BacktestContext:
    """The strategy's window onto the world, bounded by a cursor.

    The lookahead guarantee is this class, not a convention: `_cursor` holds the
    index of the bar currently being decided on, and every accessor slices at
    `cursor + 1`. There is no code path that returns a later bar, so a strategy
    cannot read one by mistake — which is the only kind of lookahead that
    actually happens.
    """

    def __init__(
        self,
        bars: dict[str, list[Bar]],
        portfolio: Portfolio,
        clock: SimulatedClock,
        symbols: tuple[str, ...],
    ) -> None:
        self._bars = bars
        self._portfolio = portfolio
        self._clock = clock
        self._symbols = symbols
        #: symbol → index of the latest bar this strategy may see. -1 means the
        #: symbol has not printed yet in this run.
        self._cursor: dict[str, int] = dict.fromkeys(bars, -1)
        self._indicator_cache: dict[
            tuple[str, str, int, tuple[tuple[str, object], ...]], float
        ] = {}

    # ── engine-facing ───────────────────────────────────────────────────────

    def advance(self, symbol: str, index: int) -> None:
        """Move one symbol's cursor. Engine-only — a strategy never sees this."""
        if index < self._cursor[symbol]:
            raise LookaheadError(f"cursor for {symbol} cannot go backwards")
        self._cursor[symbol] = index
        self._indicator_cache.clear()

    def _visible(self, symbol: str) -> list[Bar]:
        index = self._cursor.get(symbol, -1)
        if index < 0:
            return []
        return self._bars[symbol][: index + 1]

    # ── StrategyContext ─────────────────────────────────────────────────────

    @property
    def now(self) -> datetime:
        return self._clock.now()

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def history(self, symbol: str, timeframe: Timeframe, lookback: int) -> list[Bar]:
        """The last `lookback` completed bars, oldest first.

        Raises rather than returning a short series: a 20-period SMA over 6 bars
        is not a 20-period SMA, and a strategy that silently got one would trade
        on a number that does not mean what its name says.
        """
        visible = self._visible(symbol)
        if len(visible) < lookback:
            raise DataGapError(
                f"{symbol} has {len(visible)} bars at {self.now.isoformat()}, needs {lookback}"
            )
        return visible[-lookback:]

    def closes(self, symbol: str, timeframe: Timeframe, lookback: int) -> np.ndarray:
        """Closing prices as a float array, for indicator maths.

        Unlike `history`, this returns what exists rather than raising — the
        reference strategies check the length themselves so that they can sit
        quietly through warmup instead of erroring on every early bar.
        """
        visible = self._visible(symbol)[-lookback:] if lookback > 0 else []
        return np.array([float(b.close) for b in visible], dtype=float)

    def last_price(self, symbol: str) -> Decimal | None:
        visible = self._visible(symbol)
        return visible[-1].close if visible else None

    def position(self, symbol: str) -> Position:
        return self._portfolio.position(symbol)

    @property
    def equity(self) -> Decimal:
        return self._portfolio.equity

    def indicator(self, name: str, symbol: str, **kwargs: object) -> float | None:
        """Cached indicator value, or None when there is not enough history.

        Cached per cursor position and shared across strategies in a run —
        computing SMA(50) on one symbol once per bar rather than once per
        strategy per bar is what keeps a few-hundred-symbol universe tractable.
        """
        raw_period = kwargs.get("period", 14)
        if not isinstance(raw_period, int):
            # Rejected rather than coerced: a rule spec carrying `period: "20"`
            # is a malformed spec, and quietly accepting it here would hide it
            # until someone wondered why two strategies disagreed.
            raise BacktestError(f"indicator {name!r} needs an integer period, got {raw_period!r}")
        period = raw_period
        key = (name, symbol, self._cursor.get(symbol, -1), tuple(sorted(kwargs.items())))
        if key in self._indicator_cache:
            return self._indicator_cache[key]

        visible = self._visible(symbol)
        value = _compute_indicator(name, visible, period)
        if value is not None:
            self._indicator_cache[key] = value
        return value


def _compute_indicator(name: str, bars: list[Bar], period: int) -> float | None:
    """Dispatch a name from a rule spec onto `indicators.ta`.

    Delegates to `indicators.dispatch`, which the live runner also calls. A
    second copy here would mean a strategy could compute a different SMA(20)
    live than the one its backtest approved — the one divergence this
    platform's premise cannot survive (ADR 0006).
    """
    return dispatch.compute(name, bars, period)


class BacktestEngine:
    """Replays history bar by bar.

    Per bar, in this exact order — the ordering is the semantics:

        1. Advance the simulated clock to the bar's close.
        2. Mark open positions to the new price.
        3. Check stops and take-profits against this bar's HIGH/LOW.
        4. Fill orders resting from the previous bar.
        5. Call `strategy.on_bar()`; collect signals.
        6. Size signals, run them through the risk engine.
        7. Queue approved orders for the NEXT bar.

    Stops (3) resolve before new signals (5) because in reality a stop can fire
    before the strategy would have acted; running them the other way lets a
    strategy exit at a price it could not have got.

    Stops also resolve before fills (4), which has a consequence worth stating:
    a position opened by this bar's fill cannot be stopped out on the same bar.
    That follows the documented order rather than reinterpreting it, and at bar
    resolution there is no evidence about which came first anyway.
    """

    def __init__(
        self,
        strategy: Strategy,
        config: BacktestConfig,
        cost_model: CostModel,
        risk_engine: RiskEngine,
        position_sizer: PositionSizer | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self.strategy = strategy
        self.config = config
        self.cost_model = cost_model
        self.risk_engine = risk_engine
        self.position_sizer = position_sizer
        #: Called as the timeline advances, if a caller supplied one. A callback
        #: rather than anything this class does itself: reporting progress means
        #: writing somewhere, and core writes nowhere (CLAUDE.md §1.3). The CLI
        #: passes none and is unaffected; the queue worker passes one that
        #: publishes to Redis.
        self.on_progress = on_progress

        self._portfolio = Portfolio(cash=config.starting_cash, starting_equity=config.starting_cash)
        self._pending: list[Order] = []
        self._result: BacktestResult | None = None
        #: order id → (stop, target), armed onto the position once it fills.
        self._protection: dict[str, tuple[Decimal | None, Decimal | None]] = {}
        #: Net P&L of each completed round trip, and how long each was held.
        #: Accumulated as positions return to flat rather than by matching fills
        #: after the fact — the engine watches every fill go by, so it does not
        #: need the FIFO reconstruction the analytics layer does.
        self._trade_pnls: list[Decimal] = []
        self._holding_hours: list[float] = []
        #: symbol → (realised − fees) at the moment the position last opened.
        #: `Position.realized_pnl` is cumulative for the symbol, so a round
        #: trip's own P&L is the difference across its life.
        self._trade_base: dict[str, Decimal] = {}
        self._traded_notional = Decimal(0)
        self._bars_in_market = 0

    # ── public ──────────────────────────────────────────────────────────────

    def run(self, bars: dict[str, list[Bar]]) -> BacktestResult:
        """Execute the backtest.

        `bars` is symbol → chronologically sorted bars. Validate before starting:
        gaps, duplicate timestamps and unsorted input each produce plausible but
        wrong results, so fail loudly (`DataGapError`) rather than proceeding.
        """
        self._validate(bars)

        warmup = (
            self.config.warmup_bars
            if self.config.warmup_bars is not None
            else self.strategy.warmup_bars
        )
        symbols = tuple(self.config.symbols)
        step = timedelta(seconds=self.config.timeframe.seconds)

        first_ts = min(series[0].ts for series in bars.values() if series)
        clock = SimulatedClock(first_ts)
        context = BacktestContext(bars, self._portfolio, clock, symbols)

        result = BacktestResult(
            config=self.config, strategy_name=self.strategy.name, portfolio=self._portfolio
        )
        self._result = result
        self._pending = []
        self._protection = {}
        self._trade_pnls = []
        self._holding_hours = []
        self._trade_base = {}
        self._traded_notional = Decimal(0)
        self._bars_in_market = 0

        self.strategy.on_start()

        # One merged timeline. Symbols do not have to share a bar grid — a
        # halted name simply has no bar at some timestamps — so the loop walks
        # timestamps and asks which symbols printed, rather than assuming every
        # series is the same length.
        index_of: dict[str, dict[datetime, int]] = {
            symbol: {bar.ts: i for i, bar in enumerate(series)} for symbol, series in bars.items()
        }
        timeline = sorted({bar.ts for series in bars.values() for bar in series})

        seen: dict[str, int] = dict.fromkeys(bars, 0)
        total = len(timeline)
        self._report_progress(0, total)

        for done, ts in enumerate(timeline, start=1):
            # 1. The clock stands at the bar's CLOSE: `Bar.ts` is its open, and
            #    a decision taken on a completed bar is taken once it has ended.
            clock.set(ts + step)

            printed = [s for s in symbols if ts in index_of.get(s, {})]
            for symbol in printed:
                bar = bars[symbol][index_of[symbol][ts]]
                context.advance(symbol, index_of[symbol][ts])
                seen[symbol] += 1

                # 2. Mark. `Portfolio.mark` appends an equity point per call,
                #    which would emit one per symbol per timestamp; the curve is
                #    appended once below instead, after every symbol is marked.
                self._portfolio.position(symbol).last_price = bar.close

                self._check_stops(bar, result)  # 3
                self._fill_pending_for(bar, result)  # 4

            # Re-mark to the close before snapshotting equity. Step 2 marks so
            # that stops and fills see a valued book, but a position opened by
            # step 4 is left marked at its fill price by `Position.apply_fill`,
            # and an end-of-bar equity point carrying the fill price instead of
            # the close is wrong on every bar that traded.
            for symbol in printed:
                self._portfolio.position(symbol).last_price = bars[symbol][
                    index_of[symbol][ts]
                ].close

            equity = self._portfolio.equity
            self._portfolio.equity_curve.append((clock.now(), equity))
            result.equity_curve.append((clock.now(), equity))
            if self._portfolio.open_positions:
                self._bars_in_market += 1

            for symbol in printed:
                bar = bars[symbol][index_of[symbol][ts]]
                signals = self.strategy.on_bar(context, bar) or []  # 5
                if seen[symbol] <= warmup:
                    # Discarded, not suppressed upstream: the strategy still
                    # sees the bars so its indicators warm up, but nothing it
                    # says during warmup is allowed to trade.
                    continue
                result.signals.extend(signals)
                for signal in signals:
                    self._handle_signal(signal, bar, result)  # 6, 7

            # After the bar is fully resolved, so a reported count is a count of
            # bars whose decisions have been taken rather than started.
            if done % PROGRESS_EVERY == 0:
                self._report_progress(done, total)

        # Unconditionally, so the last report is always the whole timeline
        # rather than whatever the last multiple of PROGRESS_EVERY happened to
        # be. A bar that stops at 96% on a finished run is a support question.
        self._report_progress(total, total)

        self.strategy.on_stop()
        result.metrics = self._metrics(result, len(timeline)).to_dict()

        log.info(
            "backtest.done",
            strategy=self.strategy.name,
            bars=len(timeline),
            orders=len(result.orders),
            signals=len(result.signals),
            total_return=str(result.total_return),
        )
        return result

    def _report_progress(self, done: int, total: int) -> None:
        """Tell the caller how far along we are, if it asked to be told."""
        if self.on_progress is not None:
            self.on_progress(done, total)

    def _metrics(self, result: BacktestResult, bars: int) -> PerformanceMetrics:
        """Fold the run's own bookkeeping into the shared metric set.

        The three things an equity curve cannot answer are supplied from what
        the engine watched happen: how long each round trip was held, how many
        bars the book was in the market for, and how much notional crossed.
        """
        starting = self._portfolio.starting_equity
        return compute_all(
            list(result.equity_curve),
            self._trade_pnls,
            periods_per_year=_periods_per_year(self.config.timeframe),
            avg_holding_period_hours=(
                sum(self._holding_hours) / len(self._holding_hours) if self._holding_hours else 0.0
            ),
            exposure_pct=self._bars_in_market / bars if bars else 0.0,
            turnover=float(self._traded_notional / starting) if starting else 0.0,
        )

    # ── validation ──────────────────────────────────────────────────────────

    def _validate(self, bars: dict[str, list[Bar]]) -> None:
        missing = [s for s in self.config.symbols if s not in bars or not bars[s]]
        if missing:
            raise DataGapError(f"no bars supplied for {', '.join(missing)}")

        for symbol, series in bars.items():
            timestamps = [b.ts for b in series]
            if timestamps != sorted(timestamps):
                raise DataGapError(f"{symbol} bars are not in chronological order")
            if len(set(timestamps)) != len(timestamps):
                raise DataGapError(f"{symbol} has duplicate bar timestamps")
            wrong = [b for b in series if b.timeframe is not self.config.timeframe]
            if wrong:
                raise DataGapError(
                    f"{symbol} has {len(wrong)} bar(s) that are not {self.config.timeframe.value}"
                )

    # ── 3. stops ────────────────────────────────────────────────────────────

    def _check_stops(self, bar: Bar, result: BacktestResult) -> None:
        """Resolve protective levels against this bar's high and low.

        When the bar's range spans both the stop and the target, the stop is
        assumed to have filled first. The bar cannot say which came first, and
        the pessimistic reading is the only honest one at this resolution
        (docs/BACKTESTING.md).
        """
        position = self._portfolio.position(bar.symbol)
        if position.is_flat:
            return

        stop, target = position.stop_loss_price, position.take_profit_price
        long = position.is_long
        hit_stop = stop is not None and (bar.low <= stop if long else bar.high >= stop)
        hit_target = target is not None and (bar.high >= target if long else bar.low <= target)

        if not hit_stop and not hit_target:
            return

        if hit_stop:
            # A stop becomes a market order once triggered, so the fill is
            # routinely worse than the trigger — especially on the gaps where
            # stops matter most.
            intended = stop
            reason = STOP_LOSS
        else:
            intended = target
            reason = TAKE_PROFIT
        assert intended is not None

        side = Side.SELL if long else Side.BUY
        order = Order(
            symbol=bar.symbol,
            side=side,
            qty=abs(position.qty),
            order_type=OrderType.STOP if hit_stop else OrderType.LIMIT,
            stop_price=stop if hit_stop else None,
            limit_price=None if hit_stop else target,
            strategy_id=self.strategy.name,
            created_at=bar.ts,
            status=OrderStatus.SUBMITTED,
            # Which level fired, carried on the order. Without it every exit
            # this engine produces defaults to `entry`, and the trade
            # reconstruction that reads it reports a stop-out as an exit "by
            # signal" — a wrong label rather than a missing one, on the number
            # that decides whether a strategy's stops are misplaced.
            purpose=reason,
        )
        # A gap through the level fills at the open, not at the level: the
        # market never traded at the stop price on this bar.
        if hit_stop:
            price = min(bar.open, intended) if long else max(bar.open, intended)
        else:
            price = max(bar.open, intended) if long else min(bar.open, intended)

        self._execute(order, bar, price, result, note=reason)
        result.orders.append(order)

    # ── 4. fills ────────────────────────────────────────────────────────────

    def _fill_pending(self, bar: Bar) -> list[Order]:
        """Fill resting orders against this bar.

        Market → next open plus slippage. Limit → only if the bar's range
        actually reached the price. Stop → triggers on the extreme, fills with
        slippage past it, because a stop becomes a market order in a moving
        market and the fill is routinely worse than the trigger.
        """
        result = self._result
        if result is None:  # pragma: no cover - run() always sets it
            raise BacktestError("_fill_pending called outside run()")
        return self._fill_pending_for(bar, result)

    def _fill_pending_for(self, bar: Bar, result: BacktestResult) -> list[Order]:
        filled: list[Order] = []
        still_resting: list[Order] = []

        for order in self._pending:
            if order.symbol != bar.symbol:
                still_resting.append(order)
                continue

            price = self._intended_price(order, bar)
            if price is None:
                # Never touched. A DAY order dies at the session's end rather
                # than resting forever and filling on some unrelated later bar.
                if order.time_in_force is TimeInForce.DAY:
                    order.status = OrderStatus.EXPIRED
                else:
                    still_resting.append(order)
                continue

            self._execute(order, bar, price, result)
            if order.remaining_qty > 0:
                if order.time_in_force is TimeInForce.DAY:
                    order.status = OrderStatus.EXPIRED
                else:
                    still_resting.append(order)
            filled.append(order)

        self._pending = still_resting
        return filled

    def _intended_price(self, order: Order, bar: Bar) -> Decimal | None:
        """The price this order would touch on this bar, or None if it would not.

        Delegates to `execution.matching`, which `SimulatedBroker` also calls.
        The rule used to live here, and having a second copy of it in the
        simulator would mean a paper run could fill on a bar this engine would
        not — which would make the backtest that preceded it incomparable, and
        comparing them is the entire reason to paper trade first.
        """
        return intended_price(order, bar)

    def _execute(
        self,
        order: Order,
        bar: Bar,
        intended_price: Decimal,
        result: BacktestResult,
        note: str = "",
    ) -> None:
        """Apply slippage and the volume cap, then book the fill."""
        slippage = self.cost_model.slippage(order, bar, intended_price)
        price = intended_price + slippage
        if price <= 0:
            raise BacktestError(f"slippage produced a non-positive fill price {price}")

        qty = order.remaining_qty
        cap = self.config.max_volume_participation * bar.volume
        if qty > cap:
            # The excess is refused rather than pretended: a backtest that buys
            # ten times a bar's turnover is describing a market that was not
            # there. What fits, fills; the rest is left to the next bar.
            result.warnings.append(
                f"{bar.ts.isoformat()} {order.symbol}: wanted {qty}, volume cap allowed {cap}"
            )
            qty = cap
        if qty <= 0:
            return

        fee = self.cost_model.commission(order, price, qty)
        fill = Fill(order_id=order.id, ts=bar.ts, qty=qty, price=price, fee=fee)
        order.apply_fill(fill)

        position = self._portfolio.position(order.symbol)
        was_flat = position.is_flat
        opened_at = position.opened_at
        pnl_before = position.realized_pnl - position.fees_paid

        position.apply_fill(fill, qty * order.side.sign)
        self._portfolio.cash -= qty * price * order.side.sign + fee
        self._traded_notional += qty * price

        if was_flat:
            self._trade_base[order.symbol] = pnl_before
        if position.is_flat:
            # A completed round trip. Its P&L is the change in the symbol's
            # cumulative realised total across the position's life, net of the
            # fees paid getting in and out — which is what a human means by
            # "what did that trade make". Reading `realized_pnl` directly would
            # report the symbol's whole history on every exit.
            base = self._trade_base.pop(order.symbol, pnl_before)
            self._trade_pnls.append((position.realized_pnl - position.fees_paid) - base)
            if opened_at is not None:
                self._holding_hours.append((fill.ts - opened_at).total_seconds() / 3600)

        # Arm the protective levels now, not when the order was queued: a stop
        # sitting on a flat position would be measured against the next bar's
        # range with nothing to protect, and `Position.apply_fill` clears them
        # again the moment the position goes flat.
        levels = self._protection.pop(order.id, None)
        if levels is not None and not position.is_flat:
            position.stop_loss_price, position.take_profit_price = levels

        log.debug(
            "backtest.fill",
            symbol=order.symbol,
            side=order.side.value,
            qty=str(qty),
            price=str(price),
            note=note,
        )

    # ── 5-7. signals ────────────────────────────────────────────────────────

    def _handle_signal(self, signal: Signal, bar: Bar, result: BacktestResult) -> None:
        """Size, risk-check, and queue for the next bar."""
        if signal.action is SignalAction.HOLD:
            return

        position = self._portfolio.position(signal.symbol)

        if signal.action in (SignalAction.EXIT, SignalAction.SCALE_OUT):
            if position.is_flat:
                return
            if signal.action is SignalAction.SCALE_OUT:
                raise BacktestError(
                    "SCALE_OUT is not modelled yet — the fraction to close is "
                    "undefined, and guessing it would silently change results"
                )
            side = Side.SELL if position.is_long else Side.BUY
            qty = abs(position.qty)
            purpose = EXIT
        elif signal.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
            if self.position_sizer is None:
                raise BacktestError(
                    "an entry signal needs a position_sizer; real sizing is "
                    "risk-based and lands with the risk engine (docs/RISK.md)"
                )
            side = Side.BUY if signal.action is SignalAction.ENTER_LONG else Side.SELL
            qty = self.position_sizer(signal, self._portfolio, bar.close)
            purpose = ENTRY
        else:
            raise BacktestError(f"{signal.action} is not modelled by the backtest engine")

        if qty <= 0:
            return

        order = Order(
            symbol=signal.symbol,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT if signal.limit_price else OrderType.MARKET,
            limit_price=signal.limit_price,
            strategy_id=signal.strategy_id,
            signal_id=signal.id,
            created_at=bar.ts,
            # An exit by signal, told apart from an exit by a level. Both close a
            # position and only this distinguishes them afterwards.
            purpose=purpose,
        )

        decision = self.risk_engine.validate(order, self._portfolio)
        if not decision.approved:
            order.status = OrderStatus.REJECTED_RISK
            order.reject_reason = decision.reason
            result.warnings.append(
                f"{bar.ts.isoformat()} {order.symbol}: risk denied "
                f"({decision.rule}) {decision.reason}"
            )
            result.orders.append(order)
            return

        if decision.adjusted_qty is not None:
            if decision.adjusted_qty <= 0:
                return
            order.qty = decision.adjusted_qty

        order.status = OrderStatus.SUBMITTED
        order.submitted_at = bar.ts
        self._pending.append(order)
        result.orders.append(order)

        # The protective levels ride with the order and arm once it fills.
        if signal.stop_loss_price is not None or signal.take_profit_price is not None:
            self._protection[order.id] = (signal.stop_loss_price, signal.take_profit_price)
