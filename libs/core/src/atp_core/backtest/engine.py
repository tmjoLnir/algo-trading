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

from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING, Protocol

import numpy as np

from atp_core.backtest.metrics import (
    PerformanceMetrics,
    compute_all,
    periods_per_year_for,
)
from atp_core.clock import SimulatedClock
from atp_core.domain import (
    SIZING,
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
from atp_core.domain.enums import StopType
from atp_core.errors import (
    ATPError,
    BacktestError,
    DataGapError,
    LookaheadError,
    UnadjustedDataError,
)
from atp_core.execution.idempotency import ENTRY, EXIT, STOP_LOSS, TAKE_PROFIT, TIME_EXIT
from atp_core.execution.matching import intended_price
from atp_core.indicators import dispatch
from atp_core.logging import get_logger
from atp_core.risk.rules import position_size
from atp_core.risk.stops import FROM_ENTRY_TYPES, TRAILING_TYPES, StopManager, target_hit

if TYPE_CHECKING:
    from datetime import date, datetime

    from atp_core.backtest.costs import CostModel
    from atp_core.domain import Bar, Signal
    from atp_core.risk.engine import RiskEngine
    from atp_core.risk.stops import StopConfig
    from atp_core.strategy.base import Strategy

log = get_logger(__name__)

#: Bars between progress reports. A multi-year minute-bar run walks hundreds of
#: thousands of bars, and a report on each would spend more on round trips than
#: on the backtest; at 500 a daily run over five years reports twice and a minute
#: run over a year reports ~200 times, which is a smooth enough bar.
PROGRESS_EVERY = 500


class PositionSizer(Protocol):
    """Turns intent into a quantity.

    A seam, not a home: real sizing is risk-based and lives with the risk engine
    (docs/RISK.md 'Position sizing'). The engine takes one rather than computing
    a quantity itself, and that seam has held — `RiskBasedSizer` below delegates
    to `risk.rules.position_size` and there was nothing here to delete.

    An implementation **may raise `ValueError`** to mean "this cannot be sized",
    which is `position_size`'s own contract for the two inputs it refuses to
    default. `_handle_signal` books that as a refused order naming the sizing
    stage rather than letting it end the run, so an unsizeable signal is one
    line in the result instead of a crash or a silence.
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
class RiskBasedSizer:
    """Sizes through `risk.rules.position_size` — the same call the live router
    makes, with the same arguments in the same order.

    That identity is the point rather than a convenience. `OrderRouter._size`
    and this are the two places a quantity is decided in this platform, and a
    backtest that sized by its own arithmetic would produce a return the live
    strategy could never reproduce — the divergence CLAUDE.md §5 names as the
    hardest class of bug here to notice. There is one sizing function; both
    callers pass a `PositionSizeSpec` to it and neither does any maths.

    **Refuses by raising, and the engine records the refusal.** `position_size`
    raises `ValueError` for the two inputs it will not default — a stop for
    `risk_pct`, a volatility for `volatility_target` — and that exception is the
    honest answer to "how big should this be" when the answer is undefined. The
    engine catches it and books a refused order naming the sizing stage, exactly
    as the router returns `SubmitResult.refused(SIZING, ...)`. Returning zero
    instead would drop the signal silently, and a strategy whose every entry was
    dropped is indistinguishable from one that never signalled.

    `strength` is deliberately not applied. `Signal.strength` says it scales
    position size "when sizing allows", nothing in the live path scales by it
    today, and a backtest that did would report returns from a rule production
    does not run.
    """

    method: str
    value: Decimal

    def __call__(self, signal: Signal, portfolio: Portfolio, price: Decimal) -> Decimal:
        return position_size(
            self.method,
            portfolio.equity,
            price,
            stop_price=signal.stop_loss_price,
            risk_pct=self.value,
        )


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

    #: A stretch of this many calendar days with no bar is a hole in the data
    #: rather than a closed market, and `_validate` refuses the run.
    #:
    #: Calendar days against a flat threshold rather than sessions against an
    #: exchange calendar, deliberately: the engine is handed bars, not a venue,
    #: and one run's symbols can trade on exchanges whose holidays disagree. The
    #: longest closure US equities have had this century is six calendar days
    #: (September 2001), so the default clears every weekend, holiday and
    #: half-day by a wide margin while still catching a hole large enough to
    #: distort an indicator.
    max_gap_days: int = 10


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

    @property
    def unrealized_pnl(self) -> Decimal:
        """Mark-to-market on positions still open at the last bar.

        This is the half of the return that no trade statistic can see. Every
        per-trade metric — `num_trades`, `win_rate`, `profit_factor`,
        `expectancy` — is computed from completed round trips, because that is
        the only point a trade's P&L is known. A run that ends holding winners
        therefore reports an `ending_equity` its closed trades never earned.
        """
        return sum((p.unrealized_pnl for p in self.portfolio.open_positions), Decimal(0))

    @property
    def realized_pnl(self) -> Decimal:
        """What the closed round trips actually made, net of fees.

        The remainder rather than a second sum over trades, so this and
        `unrealized_pnl` always add back to `ending_equity - starting_equity`
        exactly. It also puts the entry fees of a still-open position on the
        realised side, which is where that cash has genuinely gone.
        """
        return self.portfolio.equity - self.portfolio.starting_equity - self.unrealized_pnl

    def to_report(self) -> dict[str, object]:
        """Serialisable summary for the API and the dashboard.

        `realized_pnl` and `unrealized_pnl` are reported separately because
        `ending_equity` alone is readable two ways and only one of them is a
        track record. They are money, so they are strings here and stay out of
        `metrics`, which is a bag of floats by contract (CLAUDE.md §1.1).
        """
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
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "open_positions": len(self.portfolio.open_positions),
            "orders": len(self.orders),
            "filled_orders": len(filled),
            "signals": len(self.signals),
            "fees": str(sum((o.total_fees for o in self.orders), Decimal(0))),
            "metrics": dict(self.metrics),
            "warnings": list(self.warnings),
        }


def _coverage_warning(
    complaint: str, boundary: datetime, found: list[tuple[str, datetime]]
) -> list[str]:
    """One aggregated line per kind of shortfall, never one per symbol.

    A twenty-symbol universe whose history all begins late would otherwise emit
    twenty warnings and push everything else out of the handful a CLI prints —
    which is the failure this warning exists to prevent, reintroduced.
    """
    if not found:
        return []
    names = sorted(symbol for symbol, _ in found)
    shown = ", ".join(names[:8])
    more = "" if len(names) <= 8 else f", and {len(names) - 8} more"
    edges = [ts for _, ts in found]
    return [
        f"coverage: {len(names)} symbol(s) {complaint} the requested {boundary.date()} "
        f"(spanning {min(edges).date()} to {max(edges).date()}): {shown}{more}. "
        f"The run measures a shorter window than it was asked for."
    ]


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

    def visible(self, symbol: str) -> list[Bar]:
        """Every completed bar for this symbol, oldest first.

        For a caller that wants the whole window rather than a fixed lookback —
        the engine's own ATR, which feeds stop placement, is the one today. It
        goes through the cursor like everything else, so a stop can no more be
        placed from a bar that has not happened than a strategy can trade on one.

        Returns what exists rather than raising, matching `closes` and unlike
        `history`: an indicator with too few bars answers `None`, which is the
        honest state during warmup rather than an error.
        """
        return self._visible(symbol)

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
        stop_config: StopConfig | None = None,
    ) -> None:
        self.strategy = strategy
        self.config = config
        self.cost_model = cost_model
        self.risk_engine = risk_engine
        self.position_sizer = position_sizer
        #: How every entry is protected, or None for a run that arms only what
        #: its strategy asks for. None is what this engine did unconditionally
        #: until now — `sma_crossover` emits no stop, so an ATR-stopped strategy
        #: could be configured live and backtested naked, which is the divergence
        #: ADR 0006 exists to refuse.
        self.stop_config = stop_config
        #: Stateless; held rather than constructed per call so the engine and
        #: the live runner are visibly using one implementation.
        self.stop_manager = StopManager()
        #: Called as the timeline advances, if a caller supplied one. A callback
        #: rather than anything this class does itself: reporting progress means
        #: writing somewhere, and core writes nowhere (CLAUDE.md §1.3). The CLI
        #: passes none and is unaffected; the queue worker passes one that
        #: publishes to Redis.
        self.on_progress = on_progress

        self._portfolio = Portfolio(cash=config.starting_cash, starting_equity=config.starting_cash)
        #: Set for the duration of a run. Held so stop placement can read the
        #: same cursor-bounded window a strategy reads — an ATR computed off the
        #: whole series would place stops with volatility that had not happened.
        self._context: BacktestContext | None = None
        self._pending: list[Order] = []
        self._result: BacktestResult | None = None
        #: order id → (stop, target), armed onto the position once it fills.
        self._protection: dict[str, tuple[Decimal | None, Decimal | None]] = {}
        #: symbol → how many bars the open position has been held for, which is
        #: what a `time` stop counts. Kept here rather than derived from
        #: `position.opened_at` and the bar series, because the engine already
        #: walks every bar and counting as it goes is exact — inferring it would
        #: mean re-scanning history on every bar of every position.
        self._bars_held: dict[str, int] = {}
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

        The run then prices off **adjusted** closes throughout (`_to_adjusted`),
        so every number it reports is in adjusted space. Bars carrying no
        `adj_close` are refused (`UnadjustedDataError`) rather than priced raw.
        """
        coverage = self._validate(bars)
        # Every price below this line is in adjusted space. Done once, here,
        # rather than at each read site: the engine marks, fills, sizes, stops
        # and computes indicators off six different fields, and a conversion
        # applied at five of them is a bug nobody would find.
        bars = self._to_adjusted(bars)

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
        self._context = context

        result = BacktestResult(
            config=self.config, strategy_name=self.strategy.name, portfolio=self._portfolio
        )
        # Ahead of anything the loop appends, so a short window stays inside the
        # handful of warnings a caller prints. A run that refuses three hundred
        # orders would otherwise bury the reason its window was not the one
        # asked for.
        result.warnings.extend(coverage)
        self._result = result
        self._pending = []
        self._protection = {}
        self._bars_held = {}
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

        session: date | None = None

        for done, ts in enumerate(timeline, start=1):
            # 1. The clock stands at the bar's CLOSE: `Bar.ts` is its open, and
            #    a decision taken on a completed bar is taken once it has ended.
            clock.set(ts + step)

            # A new session: tell the rules that own a day boundary where this
            # one started. Done *before* this bar is marked or filled, so the
            # anchor is the equity carried in overnight rather than a number
            # this session has already moved.
            #
            # Keyed on the UTC date, which is the session date for every equity
            # market this platform trades: the cash session runs 13:30–21:00 UTC
            # at the widest and a daily bar is stamped at exchange-local
            # midnight, so neither straddles a UTC day boundary. The same
            # assumption `PerformanceAnalyzer.daily_returns` documents, and it
            # would need the exchange's own trading day for an overnight future.
            if session != ts.date():
                session = ts.date()
                self.risk_engine.anchor_session(self._portfolio.equity)

            printed = [s for s in symbols if ts in index_of.get(s, {})]
            for symbol in printed:
                bar = bars[symbol][index_of[symbol][ts]]
                context.advance(symbol, index_of[symbol][ts])
                seen[symbol] += 1

                # 2. Mark. `Portfolio.mark` appends an equity point per call,
                #    which would emit one per symbol per timestamp; the curve is
                #    appended once below instead, after every symbol is marked.
                self._portfolio.position(symbol).last_price = bar.close

                # A completed bar the position lived through, counted before the
                # stops are asked so that a `bars=1` time stop exits on the bar
                # after the entry rather than two later. Only bars this symbol
                # printed count: a halted name does not age its own position.
                if symbol in self._bars_held:
                    self._bars_held[symbol] += 1

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
            periods_per_year=periods_per_year_for(self.config.timeframe),
            avg_holding_period_hours=(
                sum(self._holding_hours) / len(self._holding_hours) if self._holding_hours else 0.0
            ),
            exposure_pct=self._bars_in_market / bars if bars else 0.0,
            turnover=float(self._traded_notional / starting) if starting else 0.0,
        )

    # ── validation ──────────────────────────────────────────────────────────

    @property
    def _gap_limit(self) -> timedelta:
        return timedelta(days=self.config.max_gap_days)

    def _validate(self, bars: dict[str, list[Bar]]) -> list[str]:
        """Refuse input that would produce a plausible but wrong result.

        Returns the coverage warnings the run should carry. A series that starts
        after `config.start` or ends before `config.end` has legitimate causes —
        an ETF's inception, a delisting, a backfill that has not caught up — so
        it is reported rather than refused. Reported loudly, though: the run
        then measures a shorter window than the one asked for, and until this
        existed nothing said so.
        """
        missing = [s for s in self.config.symbols if s not in bars or not bars[s]]
        if missing:
            raise DataGapError(f"no bars supplied for {', '.join(missing)}")

        starts_late: list[tuple[str, datetime]] = []
        ends_early: list[tuple[str, datetime]] = []

        for symbol, series in bars.items():
            timestamps = [b.ts for b in series]
            if not timestamps:
                # Only reachable for a symbol outside `config.symbols` — the
                # check above already refused an empty series for one the run
                # asked for. Nothing below has a first or last bar to read.
                continue
            if timestamps != sorted(timestamps):
                raise DataGapError(f"{symbol} bars are not in chronological order")
            if len(set(timestamps)) != len(timestamps):
                raise DataGapError(f"{symbol} has duplicate bar timestamps")
            wrong = [b for b in series if b.timeframe is not self.config.timeframe]
            if wrong:
                raise DataGapError(
                    f"{symbol} has {len(wrong)} bar(s) that are not {self.config.timeframe.value}"
                )
            self._refuse_holes(symbol, timestamps)
            if timestamps[0] - self.config.start > self._gap_limit:
                starts_late.append((symbol, timestamps[0]))
            if self.config.end - timestamps[-1] > self._gap_limit:
                ends_early.append((symbol, timestamps[-1]))

        return [
            *_coverage_warning("have no bars until after", self.config.start, starts_late),
            *_coverage_warning("stop supplying bars before", self.config.end, ends_early),
        ]

    @staticmethod
    def _to_adjusted(bars: dict[str, list[Bar]]) -> dict[str, list[Bar]]:
        """The same series with every candle moved into adjusted space.

        **This is what stops a corporate action being read as a return.** Raw
        closes are the prices as traded, so a split lands in them as a
        discontinuity: the price of GE octupled overnight on 2021-08-02 for a
        1:8 reverse split, and a backtest holding a fixed share count through
        it books an 8x gain that never happened. Adjusted closes are continuous
        across the action, which is why CLAUDE.md §5 says to backtest on them
        and trade on raw.

        A missing `adj_close` refuses the whole run rather than falling back to
        the raw close for that symbol. The fallback is the more helpful-looking
        option and it is the one that produced the bug: it completes, it
        reports, and the only trace is a number in the equity curve that looks
        like a very good day. `_refuse_holes` takes the same position about a
        gap, and for the same reason — the message names the backfill that
        fixes it, because that is the next thing anyone does with this error.
        """
        unadjusted = sorted(
            symbol
            for symbol, series in bars.items()
            if any(candle.adj_close is None for candle in series)
        )
        if unadjusted:
            shown = ", ".join(unadjusted[:8])
            more = "" if len(unadjusted) <= 8 else f", and {len(unadjusted) - 8} more"
            raise UnadjustedDataError(
                f"{len(unadjusted)} symbol(s) have bars with no adj_close, and a backtest "
                f"prices off adjusted closes (CLAUDE.md §5): {shown}{more}. A raw-only "
                f"backfill leaves the column unset — refill without --raw-only: "
                f"scripts/backfill_bars.py --symbols {','.join(unadjusted[:8])}"
            )
        return {symbol: [candle.adjusted() for candle in series] for symbol, series in bars.items()}

    def _refuse_holes(self, symbol: str, timestamps: list[datetime]) -> None:
        """Raise on an interior hole — bars either side of a stretch, none inside.

        This is a refusal rather than a warning because there is no benign
        reading of one. `BacktestContext.closes` returns the last N closes *by
        position, not by date*, so a 50-bar average spanning a hole silently
        averages prices from either side of it, and a bar-counting stop measures
        the hole as a single bar. Neither announces itself: the run completes and
        reports a number about a series that never existed, which is the quietest
        way a backtest becomes fiction (CLAUDE.md §5).

        The message names the exact re-fetch, because the next thing anyone does
        with this error is backfill the range it found.
        """
        holes = [(a, b) for a, b in pairwise(timestamps) if b - a > self._gap_limit]
        if not holes:
            return
        shown = "; ".join(f"{a.date()} → {b.date()} ({(b - a).days}d)" for a, b in holes[:3])
        more = "" if len(holes) <= 3 else f", and {len(holes) - 3} more"
        first, second = holes[0]
        raise DataGapError(
            f"{symbol} has {len(holes)} hole(s) in its stored history, each longer than the "
            f"{self.config.max_gap_days}-day `max_gap_days`: {shown}{more}. Backfill before "
            f"running: scripts/backfill_bars.py --symbols {symbol} "
            f"--start {first.date()} --end {second.date()}"
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

        # Ratchet first, so a bar that both extends the move and retraces into
        # the *old* level is judged against the stop that bar justified — which
        # is what a venue-side trailing stop would have done.
        self._maintain_trailing(bar)

        stop, target = position.stop_loss_price, position.take_profit_price
        long = position.is_long
        # `StopManager.should_trigger` and `target_hit` rather than the same
        # comparisons written out here. Both were inline in this method and in
        # `StrategyRunner`, and two implementations of "did the bar reach the
        # level" is the divergence ADR 0006 refuses: a strategy backtested
        # against one and run live against the other is not one strategy.
        hit_stop = self.stop_manager.should_trigger(position, bar)
        hit_target = target_hit(position, bar)

        # A time stop is not a level, so it is asked separately and answers last
        # — a position that would have been stopped out or hit its target on
        # this bar did that, and only a position still open when the bar closes
        # runs out of time on it.
        if not hit_stop and not hit_target and self._time_exit_due(bar):
            self._exit_at_market(position, bar, result, purpose=TIME_EXIT)
            return

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

    def _exit_at_market(
        self, position: Position, bar: Bar, result: BacktestResult, *, purpose: str
    ) -> None:
        """Close a position at this bar's close, for a reason that is not a level.

        The `time` stop is the only caller and the price is the reason it needs
        its own path. A level exit fills at the level or at the open when the bar
        gapped through it; a time exit has no level — the rule is "leave after n
        bars", and the honest price for a decision taken on a completed bar is
        that bar's close, which is where the clock stands when the engine asks.

        `purpose` rides onto the order and into the trade reconstruction, so a
        time exit is attributable as one rather than folding into `exit`. That
        matters for the same reason the stop/target labels do: exit-reason
        attribution is how a strategy's stops are judged, and a bucket that
        silently absorbs a second kind of exit makes it lie.
        """
        order = Order(
            symbol=bar.symbol,
            side=Side.SELL if position.is_long else Side.BUY,
            qty=abs(position.qty),
            order_type=OrderType.MARKET,
            strategy_id=self.strategy.name,
            created_at=bar.ts,
            status=OrderStatus.SUBMITTED,
            purpose=purpose,
        )
        self._execute(order, bar, bar.close, result, note=purpose)
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
        if not position.is_flat:
            # Fills whatever the order did not carry, from the price we actually
            # got. `OrderRouter.submit_protective_orders` does this at the same
            # point and on the same condition — a level the request supplied is
            # left alone, and only the gaps are derived against the average fill.
            self._arm_from_config(position, order)
            self._bars_held.setdefault(order.symbol, 0)
        else:
            self._bars_held.pop(order.symbol, None)

        log.debug(
            "backtest.fill",
            symbol=order.symbol,
            side=order.side.value,
            qty=str(qty),
            price=str(price),
            note=note,
        )

    # ── 5-7. signals ────────────────────────────────────────────────────────

    # ── stops ───────────────────────────────────────────────────────────────

    def _atr(self, symbol: str) -> Decimal | None:
        """ATR over the configured period, from the bars visible *so far*.

        The lookahead guarantee applies here as much as to anything a strategy
        reads: this is computed off `BacktestContext`'s cursor, so it can only
        ever see completed bars. An ATR over the whole series would place stops
        using volatility that had not happened yet — the quietest possible way
        to make a backtest fictional.

        Float in, `Decimal` out via `str`, matching `StrategyRunner._atr` — never
        `Decimal(float)`, which inherits the binary rounding rule §1.1 exists to
        avoid. ATR is a statistic, so computing it in float is fine; the moment
        it becomes a price distance it stops being one.
        """
        if self.stop_config is None or self._context is None:
            return None
        history = self._context.visible(symbol)
        value = dispatch.compute("atr", history, self.stop_config.period)
        return None if value is None else Decimal(str(value))

    def _with_derived_stop(self, signal: Signal, bar: Bar) -> Signal:
        """A signal with a protective level, if the config describes one.

        The strategy's own level always wins — a signal that names a stop is
        returned untouched, because deriving one over the top would override a
        deliberate choice with a configured default. `StrategyRunner._derive_stop`
        takes the identical position.

        Anchored to this bar's close, which is the price the decision was taken
        at and the price the sizer measures risk from — and the level the
        position goes on to carry, because `_arm_from_config` fills gaps rather
        than overwriting. The fill lands at the next bar's open, so the realised
        risk per share differs slightly from the assumed one. Re-anchoring to
        the fill would close that gap and open a worse one: live, the derived
        level rides into `OrderRouter._requested_protection` and is likewise
        kept, so a backtest that re-anchored would arm a different stop than
        production from the same signal.

        A failure is a refusal, not a guess: an ATR stop with no ATR is exactly
        the input `StopManager` declines to default, and returning the signal
        unchanged hands the decision to the sizer — which will refuse it in turn
        and say so on the result.
        """
        if self.stop_config is None or signal.stop_loss_price is not None:
            return signal
        if self.stop_config.stop_type is StopType.TIME:
            return signal  # not a level; `time_exit_due` owns it

        side = Side.BUY if signal.action is SignalAction.ENTER_LONG else Side.SELL
        try:
            level = self.stop_manager.initial_stop(
                bar.close, side, self.stop_config, self._atr(signal.symbol)
            )
        except (ValueError, ATPError):
            return signal
        return replace(signal, stop_loss_price=level)

    def _arm_from_config(self, position: Position, order: Order) -> None:
        """Put the configured protection on a position that has just filled.

        Only fills the gaps, and the asymmetry that produces is deliberate
        rather than an oversight. An entry whose stop was derived at decision
        time arrives here already carrying it, so what this actually derives is
        the take-profit — against the fill, while the stop stays anchored to the
        close the decision was taken at.

        That is precisely what `OrderRouter.submit_protective_orders` does with
        a `_with_stop`-derived signal live: same condition, same two anchors. It
        is a poorer story than "both anchored to the fill", and it is the one
        that matters, because a backtest and a live run arming different levels
        from the same signal is the divergence ADR 0006 refuses. Change it in
        one place and it must change in both.

        The take-profit is armed here and only from a config that can express
        one — `StopManager.take_profit_level` refuses anything that is not a
        fixed distance from entry, because a trailing or time rule says *when*
        to leave rather than *where*, and a target that quietly does not exist
        is a position with no upside exit.
        """
        if self.stop_config is None or position.is_flat:
            return
        if self.stop_config.stop_type is StopType.TIME:
            return

        side = Side.BUY if position.is_long else Side.SELL
        entry = position.avg_entry_price
        if position.stop_loss_price is None:
            # Suppressed, not defaulted: left unprotected rather than protected
            # at an invented level. A position that looks guarded and is not is
            # worse than one openly unguarded (`risk/stops.py`).
            with suppress(ValueError, ATPError):
                position.stop_loss_price = self.stop_manager.initial_stop(
                    entry, side, self.stop_config, self._atr(order.symbol)
                )
        if position.take_profit_price is None and self.stop_config.stop_type in FROM_ENTRY_TYPES:
            with suppress(ValueError, ATPError):
                position.take_profit_price = self.stop_manager.take_profit_level(
                    entry, side, self.stop_config
                )

    def _maintain_trailing(self, bar: Bar) -> None:
        """Ratchet a trailing stop against this bar.

        Runs before the level is tested, so a bar that both extends the move and
        retraces into the *old* stop is judged against the level that bar
        justified — which is what a venue-side trailing stop would have done.

        `update_trailing` mutates the position and returns the new level only
        when it actually moved, so a stop can never be widened by this call.
        """
        if self.stop_config is None:
            return
        if self.stop_config.stop_type not in TRAILING_TYPES:
            return
        position = self._portfolio.position(bar.symbol)
        if position.is_flat:
            return
        # A chandelier with no ATR yet — during warmup, before the period has
        # enough bars. The stop simply does not ratchet on this bar.
        with suppress(ValueError, ATPError):
            self.stop_manager.update_trailing(
                position, bar, self.stop_config, self._atr(bar.symbol)
            )

    def _time_exit_due(self, bar: Bar) -> bool:
        """Whether a `time` stop has run out on this symbol's position.

        Counted from fills rather than inferred from `opened_at` and the bar
        series: this engine walks every bar anyway, so counting is exact and
        costs nothing, where inferring would re-scan history per position per
        bar.
        """
        if self.stop_config is None or self.stop_config.stop_type is not StopType.TIME:
            return False
        held = self._bars_held.get(bar.symbol)
        if held is None:
            return False
        return self.stop_manager.time_exit_due(held, self.stop_config)

    @staticmethod
    def _book_refusal(
        order: Order, bar: Bar, result: BacktestResult, *, rule: str, reason: str
    ) -> None:
        """Record an order this run refused, and why.

        One path for both stages that can refuse — the sizer and the rule chain
        — because a reader counting refusals should not have to know which of
        them produced a given row. `rejected_by` carries the rule name or the
        stage label from the same vocabulary production uses
        (`domain.order.SIZING`), so "refusals by rule" is countable across a
        backtest and a live record alike.

        The order goes into `result.orders` rather than being dropped. A refused
        order is the most informative row a run produces: docs/ANALYTICS.md's
        point about the signals table applies here exactly — a strategy whose
        every idea was refused is otherwise indistinguishable from one that had
        no ideas, and those two call for opposite responses.
        """
        order.status = OrderStatus.REJECTED_RISK
        order.reject_reason = reason
        order.rejected_by = rule
        result.warnings.append(f"{bar.ts.isoformat()} {order.symbol}: refused ({rule}) {reason}")
        result.orders.append(order)

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
            # Before sizing, because `risk_pct` is *defined* off the distance to
            # the stop. `StrategyRunner._derive_stop` does this at the same point
            # and for the same reason: the documented default pair is an ATR stop
            # with risk-based sizing, no `Signal` carries an ATR-derived level,
            # and without this every entry from a default-configured strategy is
            # refused at sizing for want of a stop to measure against.
            signal = self._with_derived_stop(signal, bar)
            if self.position_sizer is None:
                raise BacktestError(
                    "an entry signal needs a position_sizer; real sizing is "
                    "risk-based and lands with the risk engine (docs/RISK.md)"
                )
            side = Side.BUY if signal.action is SignalAction.ENTER_LONG else Side.SELL
            try:
                qty = self.position_sizer(signal, self._portfolio, bar.close)
            except ValueError as exc:
                # Undefined, not zero. `position_size` refuses to invent a stop
                # for `risk_pct` or a volatility for `volatility_target`, and a
                # strategy configured to size by risk while emitting signals
                # with no stop is one strategy misconfigured — the same reading
                # `OrderRouter._size` takes, where it becomes a refusal on the
                # dashboard rather than an exception up the runner's loop.
                #
                # Booked as a refused order rather than dropped, because a
                # strategy whose every entry was silently discarded produces an
                # empty result indistinguishable from one that never signalled.
                self._book_refusal(
                    Order(
                        symbol=signal.symbol,
                        side=side,
                        # A refused order still has to be a valid `Order`, and an
                        # `Order` refuses a non-positive quantity. One share is
                        # the smallest honest placeholder for "we never got as
                        # far as deciding how many" — the reason says so, and
                        # `status` keeps it out of every fill and P&L path.
                        qty=Decimal(1),
                        order_type=OrderType.MARKET,
                        strategy_id=signal.strategy_id,
                        signal_id=signal.id,
                        created_at=bar.ts,
                        purpose=ENTRY,
                    ),
                    bar,
                    result,
                    rule=SIZING,
                    reason=str(exc),
                )
                return
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
            self._book_refusal(order, bar, result, rule=decision.rule, reason=decision.reason)
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
