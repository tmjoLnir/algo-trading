"""The live strategy loop — requirement #1, and the mirror of the backtest engine.

Structurally identical to `atp_core.backtest.engine.BacktestEngine.run()`. That
is the whole point: if the live loop's ordering diverges from the backtest's,
every backtest becomes a claim about a system that does not exist.

Per evaluation, in this exact order:

    1. Refresh marks for open positions.
    2. Check stops and take-profits (engine-side ones; broker-side fire on their own).
    3. Process fills that arrived since the last pass.
    4. Call `strategy.on_bar()` for each symbol whose bar just closed.
    5. Size signals, run the risk engine, submit approved orders.
    6. Persist state and publish updates.

Stops before signals, same as the backtest. Never reorder one without the other.

Two things this file is deliberately not:

**It is not a second submission path.** Every order it causes goes through
`OrderRouter`, which is the only thing that reaches a broker (rule §1.5). The
runner decides *what* to ask for and never *how* to send it.

**It is not a second indicator implementation.** The context below serves the
strategy from an in-memory window of the same `Bar` objects the backtest reads,
through the same `indicators.dispatch`. A strategy computing a different SMA(20)
live than the one its backtest approved is the single divergence this platform's
premise cannot survive.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import numpy as np

from atp_core import metrics
from atp_core.channels import CHANNEL_ORDERS, CHANNEL_SIGNALS
from atp_core.dashboard import SignalSummary, build_snapshot
from atp_core.dashboard.snapshot import DEFAULT_SIGNAL_LIMIT
from atp_core.domain import Order, Portfolio, RunMode, Side, SignalAction
from atp_core.domain.enums import StopType
from atp_core.errors import ATPError, DataGapError, ExecutionError
from atp_core.execution.idempotency import STOP_LOSS, TAKE_PROFIT, TIME_EXIT
from atp_core.execution.trade_updates import apply_trade_update
from atp_core.indicators import dispatch
from atp_core.logging import correlation_id, get_logger
from atp_core.risk.killswitch import HaltReason, HaltScope
from atp_core.risk.stops import target_hit
from atp_core.strategy.ports import SignalOutcome, StrategyRecord

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from atp_core.brokers.ports import TradeUpdate
    from atp_core.clock import Clock, TradingCalendar
    from atp_core.dashboard.ports import SnapshotStore
    from atp_core.data.ports import BarRepository, EventPublisher, QuoteCache
    from atp_core.domain import Bar, Fill, Position, Quote, Signal, Timeframe
    from atp_core.execution.ports import OrderRepository, PortfolioRepository
    from atp_core.execution.reconciliation import Reconciler
    from atp_core.execution.router import OrderRouter, SubmitResult
    from atp_core.risk.killswitch import KillSwitch
    from atp_core.risk.stops import StopConfig, StopManager
    from atp_core.strategy.base import Strategy
    from atp_core.strategy.ports import SignalRepository, StrategyRepository
    from atp_core.strategy.rules import PositionSizeSpec

log = get_logger(__name__)

#: Consecutive failed evaluations before the runner halts trading. A runner
#: erroring every tick is not trading, but it looks alive to a health check —
#: which is the state this counter exists to end. Three rather than one,
#: because a single transient read failure should not stop a strategy.
MAX_CONSECUTIVE_ERRORS = 3

#: How long to sleep when the market is shut and the calendar cannot say when
#: it next opens. A bounded nap rather than a spin, and short enough that a
#: calendar that starts answering is picked up promptly.
CLOSED_MARKET_FALLBACK_SECONDS = 300.0


@dataclass(slots=True)
class RunnerStats:
    started_at: datetime | None = None
    evaluations: int = 0
    signals_generated: int = 0
    orders_submitted: int = 0
    orders_rejected_by_risk: int = 0
    last_evaluation_at: datetime | None = None
    errors: int = 0
    consecutive_errors: int = 0
    fills_applied: int = 0
    stops_triggered: int = 0


@dataclass(slots=True)
class _AppliedFill:
    """A fill the runner has already booked, waiting for `strategy.on_fill`.

    Held rather than dispatched at the moment it lands, because the strategy
    hook belongs inside the loop's documented ordering while *booking* the fill
    and protecting the position does not — that has to happen the instant the
    event arrives.
    """

    order: Order
    fill: Fill


class LiveContext:
    """The strategy's window onto the world, live.

    The counterpart of `BacktestContext`, and deliberately the same shape. It
    serves a rolling in-memory window of completed bars rather than slicing a
    fixed array, but the guarantee it provides is identical: a strategy sees
    completed bars and nothing else. There is no cursor here because there is
    no future to accidentally address — the window holds what has closed.

    Sync, because `StrategyContext` is sync and a strategy must not be able to
    await inside a decision. Everything it serves is loaded before the hook is
    called, which is what `warmup` and the loop's bar refresh are for.
    """

    def __init__(
        self,
        bars: dict[str, list[Bar]],
        quotes: dict[str, Quote],
        portfolio: Portfolio,
        clock: Clock,
        symbols: tuple[str, ...],
    ) -> None:
        self._bars = bars
        self._quotes = quotes
        self._portfolio = portfolio
        self._clock = clock
        self._symbols = symbols
        self._indicator_cache: dict[
            tuple[str, str, int, tuple[tuple[str, object], ...]], float
        ] = {}

    def invalidate(self) -> None:
        """Drop cached indicator values. Called when the window moves."""
        self._indicator_cache.clear()

    # ── StrategyContext ─────────────────────────────────────────────────────

    @property
    def now(self) -> datetime:
        return self._clock.now()

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def history(self, symbol: str, timeframe: Timeframe, lookback: int) -> list[Bar]:
        """The last `lookback` completed bars, oldest first.

        Raises rather than returning a short series, exactly as the backtest
        does: a 20-period SMA over 6 bars is not a 20-period SMA. Live, this
        fires when warmup could not load enough history — which is a reason to
        stop rather than to trade on a number that does not mean what its name
        says.
        """
        available = self._bars.get(symbol, [])
        if len(available) < lookback:
            raise DataGapError(
                f"{symbol} has {len(available)} bars at {self.now.isoformat()}, needs {lookback}"
            )
        return available[-lookback:]

    def closes(self, symbol: str, timeframe: Timeframe, lookback: int) -> np.ndarray:
        """Closing prices as a float array, for indicator maths.

        Returns what exists rather than raising, matching `BacktestContext` —
        the reference strategies check the length themselves so they can sit
        quietly through warmup.
        """
        available = self._bars.get(symbol, [])
        window = available[-lookback:] if lookback > 0 else []
        return np.array([float(b.close) for b in window], dtype=float)

    def last_price(self, symbol: str) -> Decimal | None:
        """The last completed bar's close.

        Deliberately the bar rather than the live quote, even though a quote is
        fresher. This is what a strategy *decides* on, and a decision taken on a
        mid-quote inside an unfinished bar is a decision the backtest can never
        reproduce. Marking a position for P&L is the opposite case and does use
        the quote — see `StrategyRunner._mark`.
        """
        available = self._bars.get(symbol, [])
        return available[-1].close if available else None

    def position(self, symbol: str) -> Position:
        return self._portfolio.position(symbol)

    @property
    def equity(self) -> Decimal:
        return self._portfolio.equity

    def indicator(self, name: str, symbol: str, **kwargs: object) -> float | None:
        """Cached indicator value, or None when there is not enough history."""
        raw_period = kwargs.get("period", 14)
        if not isinstance(raw_period, int):
            raise ExecutionError(f"indicator {name!r} needs an integer period, got {raw_period!r}")
        key = (name, symbol, len(self._bars.get(symbol, [])), tuple(sorted(kwargs.items())))
        if key in self._indicator_cache:
            return self._indicator_cache[key]

        value = dispatch.compute(name, self._bars.get(symbol, []), raw_period)
        if value is not None:
            self._indicator_cache[key] = value
        return value


class StrategyRunner:
    """Runs one strategy against live (or paper) market data.

    Four dependencies the skeleton did not name are required here, each because
    something documented is impossible without it:

    - `reconciler` — `warmup` must reconcile before the first evaluation, and a
      runner that built its own could not be tested against a fake broker.
    - `sizing` — `OrderRouter.submit_signal` takes a `PositionSizeSpec`; there
      is no default that is safe for every strategy.
    - `stop_config` — protective orders need one, and it is also what lets a
      signal without an explicit stop still be sized by `risk_pct`.
    - `timeframe` — which series this strategy trades. The repository holds
      several, and guessing would silently run a daily strategy on minutes.

    None of them have defaults, for the reason `default_rules()` has none: a
    dependency that quietly defaulted would be one nobody chose.
    """

    def __init__(
        self,
        strategy: Strategy,
        symbols: list[str],
        router: OrderRouter,
        stop_manager: StopManager,
        kill_switch: KillSwitch,
        bar_repo: BarRepository,
        quote_cache: QuoteCache,
        clock: Clock,
        calendar: TradingCalendar,
        *,
        reconciler: Reconciler,
        sizing: PositionSizeSpec,
        stop_config: StopConfig,
        timeframe: Timeframe,
        run_mode: RunMode,
        order_repo: OrderRepository,
        portfolio_repo: PortfolioRepository,
        strategy_repo: StrategyRepository,
        signal_repo: SignalRepository,
        snapshot_store: SnapshotStore | None = None,
        publisher: EventPublisher | None = None,
        signal_limit: int = DEFAULT_SIGNAL_LIMIT,
        tick_interval_seconds: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.strategy = strategy
        self.symbols = symbols
        self.router = router
        self.stop_manager = stop_manager
        self.kill_switch = kill_switch
        self.bar_repo = bar_repo
        self.quote_cache = quote_cache
        self.clock = clock
        self.calendar = calendar
        self.reconciler = reconciler
        self.sizing = sizing
        self.stop_config = stop_config
        self.timeframe = timeframe
        self.run_mode = run_mode
        #: Required rather than optional. A runner without them keeps its book
        #: in memory only, so every restart adopts the broker's wholesale and
        #: reconciliation across a restart becomes clean by construction — the
        #: exact hole `execution/ports.py` exists to close. Making them
        #: defaultable would make that hole reachable by omission.
        self.order_repo = order_repo
        self.portfolio_repo = portfolio_repo
        #: Required for the same reason, and with one extra teeth to it: an
        #: order now stores `strategy_id` and `signal_id`, and both are foreign
        #: keys. A runner without these would write orders naming decisions that
        #: have no row, which the database refuses outright — so an optional
        #: version would not degrade to "no attribution", it would degrade to
        #: "no order was saved". Attribution is the join between the three, and
        #: a join with a missing side is not a weaker answer but no answer.
        self.strategy_repo = strategy_repo
        self.signal_repo = signal_repo
        #: Optional, unlike the two above, and the asymmetry is deliberate.
        #: Losing the durable book is a correctness failure — it is what a
        #: restart reads. Losing the published one costs a dashboard its
        #: freshness and nothing else, so a worker configured without either is
        #: a worker running blind rather than a worker running wrong, and
        #: refusing to start over it would stop trading to protect a screen.
        self.snapshot_store = snapshot_store
        self.publisher = publisher
        self.tick_interval_seconds = tick_interval_seconds
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else asyncio.sleep
        )
        self.stats = RunnerStats()

        self._bars: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
        self._quotes: dict[str, Quote] = {}
        #: Bound for real by `warmup`, which is the only thing that knows the
        #: portfolio. Empty rather than `None` so every accessor can be typed
        #: without an optional, and replaced rather than mutated.
        self._portfolio = Portfolio(cash=Decimal(0), starting_equity=Decimal(0))
        self._context = LiveContext(
            self._bars, self._quotes, self._portfolio, clock, tuple(symbols)
        )
        #: What we believe is working at the venue, keyed by `client_order_id`.
        #: This is what `Reconciler.reconcile` needs and had no source for: the
        #: runner is the thing that knows what it submitted.
        self._open_orders: dict[str, Order] = {}
        #: Fills booked since the last pass, awaiting `strategy.on_fill`.
        self._pending_fills: list[_AppliedFill] = []
        #: What the strategy decided lately and what became of it, newest last.
        #: Bounded, and in memory only: this is a feed, not the audit trail.
        #: `persistence.models.SignalRow` is where the durable record belongs
        #: and nothing writes it yet — see docs/ROADMAP.md Phase 5, "Trade
        #: reconstruction". Until then a restart loses the feed, which is worth
        #: knowing when the dashboard is empty after a deploy.
        self._recent_signals: deque[SignalSummary] = deque(maxlen=signal_limit)
        #: Serialises a pushed fill against an in-flight evaluation, so a fill
        #: cannot land halfway through the ordered pass it is supposed to be a
        #: step of.
        self._lock = asyncio.Lock()
        self._running = False

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def open_orders(self) -> list[Order]:
        """What we believe is working at the venue. Handed to the reconciler."""
        return list(self._open_orders.values())

    async def warmup(self, portfolio: Portfolio) -> None:
        """Load history and rebuild state before the first evaluation.

        Two things must happen here, and skipping either produces a runner that
        appears to work:

        1. Load `strategy.warmup_bars` of history, so indicators are correct
           from the first live bar rather than from bar 50.
        2. Reconcile against the broker (`execution.reconciliation`). We may
           have restarted holding positions we no longer know about — a runner
           that starts assuming it is flat will happily double a position.

        A dirty reconciliation **refuses to start**. The reconciler has already
        engaged the kill switch, so the risk chain would refuse every order
        anyway; raising here means the operator sees why at startup rather than
        finding a process that is running and silently declining to trade.
        """
        self._portfolio = portfolio
        self._context = LiveContext(
            self._bars, self._quotes, portfolio, self.clock, tuple(self.symbols)
        )

        # First, before anything can produce a signal or an order. Both store a
        # foreign key to this row, so a runner that reached step 5 without it
        # would fail every write from there on — and it fails here instead,
        # where the message says which strategy has no row rather than surfacing
        # as an integrity error inside an evaluation.
        await self._ensure_strategy_row()

        needed = max(self.strategy.warmup_bars, 1)

        for symbol in self.symbols:
            bars = await self.bar_repo.get_last_n_bars(symbol, self.timeframe, needed)
            self._bars[symbol] = list(bars)
            if len(bars) < needed:
                # Loud, and not fatal on its own: `LiveContext.history` refuses
                # for whoever actually needs the missing bars, while a strategy
                # that checks its own lengths can still run.
                log.warning(
                    "runner.warmup_short_history",
                    symbol=symbol,
                    have=len(bars),
                    needed=needed,
                )
        self._context.invalidate()

        # What we believed was working before the restart. Restored *before*
        # reconciling, because it is the set reconciliation compares against —
        # without it every order resting at the venue reads as an orphan.
        restored = await self.order_repo.open_orders(self.run_mode)
        for order in restored:
            self._open_orders[order.client_order_id] = order
        if restored:
            log.info(
                "runner.restored_open_orders",
                count=len(restored),
                client_order_ids=[o.client_order_id for o in restored],
            )

        report = await self.reconciler.reconcile(portfolio, known_orders=self.open_orders)
        if not report.is_clean:
            raise ExecutionError(
                f"refusing to start: the book does not match the broker's — {report.summary()}. "
                "See docs/RUNBOOK.md 'Reconciliation mismatch'."
            )

        # The day's starting equity, and **nothing was setting it**.
        # `default_rules()` has always included `DailyLossLimitRule`, that rule
        # is default-closed and denies every entry until it is anchored, and no
        # path in this platform ever called `anchor` — so this runner was
        # configured to refuse every entry it would ever produce. It went
        # unnoticed because a chain refusing everything and a chain nothing has
        # reached look identical from outside, and nothing has traded paper yet.
        #
        # Here rather than in `run`'s loop because this is the session boundary:
        # `warmup` is re-run at each open, and anchoring per iteration would
        # re-anchor to a drawn-down number and grant the day a second allowance.
        # After reconciliation, so the anchor is the book the broker agrees we
        # hold rather than the one we believed before checking.
        anchored = self.router.risk_engine.anchor_session(portfolio.equity)
        log.info("runner.session_anchored", equity=str(portfolio.equity), rules=anchored)

        self.strategy.on_start()
        self.stats.started_at = self.clock.now()
        log.info(
            "runner.warmed_up",
            strategy=self.strategy.name,
            symbols=len(self.symbols),
            bars=sum(len(b) for b in self._bars.values()),
        )

    async def _ensure_strategy_row(self) -> None:
        """Give this strategy the row every signal and order points at.

        The id is `strategy.name`, not a generated uuid, because that is what
        `Signal.strategy_id` already carries everywhere in the platform —
        `SmaCrossover` emits signals naming `"sma_crossover"`, the router copies
        it onto the order, and both foreign keys resolve against it. Minting a
        separate primary key here would leave every one of those references
        pointing at nothing.

        `kind` is `coded`: this runner is handed a `Strategy` instance, and the
        declarative `ruleset` variant is compiled to one before it gets here.
        Recording it as a ruleset would put a rule spec in a column nothing
        wrote a rule spec into.

        Idempotent, and re-run on every session open along with the rest of
        `warmup` — which is why `ensure` touches only `updated_at` on a row that
        already exists.
        """
        await self.strategy_repo.ensure(
            StrategyRecord(
                id=self.strategy.name,
                name=self.strategy.name,
                kind="coded",
                class_name=type(self.strategy).__name__,
                params=dict(self.strategy.params),
                universe=tuple(self.symbols),
                timeframe=self.timeframe.value,
            )
        )

    async def run(self, portfolio: Portfolio) -> None:
        """The main loop. Runs until cancelled.

        Between sessions, sleep until `calendar.next_open()` rather than
        spinning — and re-run `warmup()` at each open, because overnight
        corporate actions and after-hours fills change the picture.
        """
        self._running = True
        await self.warmup(portfolio)

        while self._running:
            now = self.clock.now()
            if not self.calendar.is_open(now):
                await self._sleep_until_open(now)
                if not self._running:
                    return
                # A new session is a new picture: positions may have been
                # adjusted for a split overnight, and fills can land after the
                # close. Re-reconciling here is what stops the first order of
                # the day being sized against yesterday's book.
                await self.warmup(portfolio)
                continue

            await self.evaluate(portfolio)
            await self._sleep(self.tick_interval_seconds)

    async def _sleep_until_open(self, now: datetime) -> None:
        """Wait out a closed market without spinning."""
        try:
            opens_at = self.calendar.next_open(now)
        except ValueError:
            # The calendar cannot see that far — a bounded nap rather than a
            # spin, and it will be asked again.
            log.warning("runner.no_next_open", now=now.isoformat())
            await self._sleep(CLOSED_MARKET_FALLBACK_SECONDS)
            return

        seconds = (opens_at - now).total_seconds()
        log.info(
            "runner.market_closed", sleeping_seconds=int(seconds), opens_at=opens_at.isoformat()
        )
        await self._sleep(max(seconds, 0.0))

    async def shutdown(self, close_positions: bool = False) -> None:
        """Stop cleanly.

        Default is to leave positions open with their broker-side stops intact.
        Liquidating on every deploy would turn a routine restart into a taxable
        event and a guaranteed spread cost — the stops are there precisely so
        the position can survive us not running.
        """
        self._running = False
        self.strategy.on_stop()
        if not close_positions:
            log.info("runner.stopped", positions_left_open=True)
            return

        # Through the router, like everything else — `flatten` still passes the
        # risk chain, and a refusal is reported rather than worked around
        # (ADR 0005).
        for position in list(self._portfolio.open_positions):
            result = await self.router.flatten(position.symbol, self._portfolio)
            if not result.submitted:
                log.error(
                    "runner.shutdown_flatten_refused",
                    symbol=position.symbol,
                    reason=result.decision.reason,
                )
                # The book is still open and the worker is going home. This is
                # the row that says so tomorrow morning.
                await self._record_refusal(result)
        log.warning("runner.stopped", positions_left_open=False)

    # ── one pass ────────────────────────────────────────────────────────────

    async def evaluate(self, portfolio: Portfolio) -> None:
        """One pass of the ordering documented at the top of this module.

        Wrap in a try/except: an unhandled exception here must not kill the
        loop silently. Log it, increment `stats.errors`, and engage the kill
        switch after N consecutive failures — a runner erroring every tick is
        not trading, but it looks alive to a health check.

        `asyncio.CancelledError` is deliberately not caught. It is a shutdown,
        not a failure, and swallowing it would make the loop unkillable.
        """
        # One pass is a unit of work: an id here puts the same key on every line
        # the pass writes, from the risk engine's refusal to the router's submit
        # to the broker adapter's retry, none of which knows a loop exists. On a
        # busy watchlist that is the difference between reading a log and
        # reconstructing one.
        with correlation_id():
            started = time.perf_counter()
            async with self._lock:
                try:
                    await self._evaluate_once(portfolio)
                except asyncio.CancelledError:
                    raise
                except (ATPError, Exception) as exc:
                    self.stats.errors += 1
                    self.stats.consecutive_errors += 1
                    metrics.strategy_evaluated(
                        self.strategy.name, "failed", time.perf_counter() - started
                    )
                    log.error(
                        "runner.evaluation_failed",
                        strategy=self.strategy.name,
                        error=str(exc),
                        consecutive=self.stats.consecutive_errors,
                    )
                    if self.stats.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        self.kill_switch.engage(
                            HaltScope.STRATEGY,
                            HaltReason.UNHANDLED_EXCEPTION,
                            engaged_by="strategy_runner",
                            detail=(
                                f"{self.strategy.name} failed {self.stats.consecutive_errors} "
                                f"evaluations in a row: {exc}"
                            ),
                            target=self.strategy.name,
                        )
                    return

            metrics.strategy_evaluated(
                self.strategy.name, "succeeded", time.perf_counter() - started
            )

        self.stats.consecutive_errors = 0

    async def _evaluate_once(self, portfolio: Portfolio) -> None:
        closed = await self._refresh_bars()  # feeds steps 1, 2 and 4

        await self._mark(portfolio)  # 1
        await self._check_stops(portfolio, closed)  # 2
        signals = self._drain_fills(portfolio)  # 3
        signals.extend(self._poll_strategy(closed))  # 4
        await self._submit(signals, portfolio)  # 5
        await self._persist(portfolio)  # 6

        self.stats.evaluations += 1
        self.stats.last_evaluation_at = self.clock.now()

    async def _persist(self, portfolio: Portfolio) -> None:
        """Step 6. Write the book, then publish it.

        Orders first, then the durable snapshot. If the process dies between
        them, a restart reads a slightly stale book alongside a current order
        set — which reconciliation notices and halts on. The other order would
        restore a book claiming fills whose orders were never recorded, and
        that disagreement is the one nothing downstream could detect.

        Publishing comes last, after both durable writes, and its failures are
        swallowed. The ordering is the same argument `persistence/events.py`
        makes about pub/sub: anything that must not be lost is written first
        and announced second, so a dashboard can never show a fill the database
        does not have. Swallowing is the other half — an unreachable Redis must
        not fail an evaluation, because three failed evaluations halt trading
        and stopping a strategy because a screen went dark is a cure worse than
        the disease. It is logged at warning, once per pass, and the missing
        snapshot is visible on the dashboard as an age that stops advancing.
        """
        for order in self._open_orders.values():
            await self.order_repo.save(order, run_mode=self.run_mode)
        at = self.clock.now()
        await self.portfolio_repo.snapshot(portfolio, at=at, run_mode=self.run_mode)
        await self._publish_snapshot(portfolio, at)

    async def _publish_snapshot(self, portfolio: Portfolio, at: datetime) -> None:
        """Hand the dashboard one consistent picture of the book.

        Built from `portfolio` and the runner's own working-order set rather
        than re-read from the repositories it just wrote to: those two are the
        live objects the evaluation just finished with, so the published
        picture is the same one the loop acted on. Reading it back would
        introduce a second view that can differ, which is exactly the
        disagreement one aggregate snapshot exists to prevent.
        """
        if self.snapshot_store is None:
            return
        try:
            # The whole watchlist, read fresh, rather than `self._quotes`. That
            # cache is filled by `_mark`, which returns early when nothing is
            # open and never prunes what it holds — so a flat book with a
            # perfectly healthy feed would publish "no data has ever arrived",
            # and a symbol exited an hour ago would keep answering for the
            # feed's pulse long after we stopped watching it.
            quotes = await self.quote_cache.get_quotes(list(self.symbols))
            await self.snapshot_store.put(
                build_snapshot(
                    portfolio,
                    at=at,
                    run_mode=self.run_mode,
                    working_orders=self._open_orders.values(),
                    recent_signals=self._recent_signals,
                    quotes=quotes,
                    symbols=self.symbols,
                    strategy=self.strategy.name,
                )
            )
        except Exception as exc:
            log.warning(
                "runner.snapshot_publish_failed",
                strategy=self.strategy.name,
                error=str(exc),
                msg="the dashboard will show an age that stops advancing",
            )

    async def _refresh_bars(self) -> list[Bar]:
        """Append any newly completed bars; return the ones that just closed.

        A bar is "new" when its timestamp is past the last one we hold. Compared
        on the timestamp rather than on a count, because a repository that
        re-serves the same bar — an idempotent upsert re-running, a restatement
        landing — must not read as a fresh close and re-trigger a strategy.
        """
        just_closed: list[Bar] = []
        for symbol in self.symbols:
            latest = await self.bar_repo.get_last_n_bars(symbol, self.timeframe, 1)
            if not latest:
                continue
            bar = latest[-1]
            held = self._bars.get(symbol) or []
            if held and bar.ts <= held[-1].ts:
                continue
            held.append(bar)
            self._bars[symbol] = held
            just_closed.append(bar)

        if just_closed:
            self._context.invalidate()
        return just_closed

    async def _mark(self, portfolio: Portfolio) -> None:
        """Step 1. Value open positions off the freshest price we have.

        The quote, not the last bar close — the opposite of what the strategy
        decides on. A mark is about what the book is worth *now*, and every
        percentage risk limit is denominated in it, so a stale mark makes a
        breached limit look compliant. `Portfolio.unmarked_symbols` already
        refuses to price a book it cannot value; this is what keeps that list
        empty.
        """
        open_symbols = [p.symbol for p in portfolio.open_positions]
        if not open_symbols:
            return

        quotes = await self.quote_cache.get_quotes(open_symbols)
        for symbol in open_symbols:
            quote = quotes.get(symbol)
            if quote is not None:
                self._quotes[symbol] = quote
                portfolio.position(symbol).last_price = quote.mid
                continue
            # No quote: fall back to the last completed bar rather than leaving
            # the position unmarked, and say so — an unmarked holding is what
            # makes every percentage limit compute too small.
            fallback = self._context.last_price(symbol)
            if fallback is not None:
                portfolio.position(symbol).last_price = fallback
            log.warning("runner.no_quote_for_mark", symbol=symbol)

    async def _check_stops(self, portfolio: Portfolio, closed: list[Bar]) -> None:
        """Step 2. Engine-side protective levels, and trailing ratchets.

        Broker-side stops are not checked here — they are resting at the venue
        and fire without us, which is the entire reason docs/SAFETY.md makes
        them layer 5. What this owns is the part the venue cannot do: ratcheting
        a trailing stop as the high-water mark moves, and time exits, which are
        not a price level at all.

        A triggered level exits through `OrderRouter.flatten`, so the exit
        passes the risk chain like everything else. Six of the nine default
        rules can refuse an exit; a refusal is logged loudly rather than
        retried around, because a stop that silently did not fire is the worst
        thing this file could hide.
        """
        by_symbol = {bar.symbol: bar for bar in closed}
        for position in list(portfolio.open_positions):
            bar = by_symbol.get(position.symbol)
            if bar is None:
                continue

            if self.stop_config.stop_type in (StopType.TRAILING_PCT, StopType.CHANDELIER):
                atr = self._atr(position.symbol)
                moved = self.stop_manager.update_trailing(position, bar, self.stop_config, atr)
                if moved is not None:
                    log.info(
                        "runner.trailing_stop_ratcheted",
                        symbol=position.symbol,
                        level=str(moved),
                    )

            reason = self._exit_reason(position, bar)
            if reason is None:
                continue

            self.stats.stops_triggered += 1
            log.warning("runner.stop_triggered", symbol=position.symbol, reason=reason)
            # `reason` is the exit's `purpose`, so it rides into the order's
            # idempotency key and into storage. Without it all three engine-side
            # exits would store as `flatten` and the exit-reason attribution
            # that makes this data worth keeping would have one bucket.
            result = await self.router.flatten(position.symbol, portfolio, purpose=reason)
            if not result.submitted:
                log.error(
                    "runner.stop_exit_refused",
                    symbol=position.symbol,
                    reason=result.decision.reason,
                )
                # The stop triggered and the exit was refused, so the position
                # is still on. Of the four refusals recorded here this is the
                # one most likely to cost money.
                await self._record_refusal(result)
            elif result.order is not None:
                self._track(result.order)

    def _exit_reason(self, position: Position, bar: Bar) -> str | None:
        """Why this position should be closed now, or None to hold it.

        Returns a `purpose` from `execution.idempotency`, which is what the exit
        order is keyed and stored under — so the answer here is also the answer
        `analytics.performance` reports when asked why the trade ended.

        A time stop is checked separately from a price one because it is not a
        level: `StopManager` refuses to give it a price, and a caller that
        asked for one would be inventing it.

        **The take-profit is checked here, and it was not before.**
        `BacktestEngine._check_stops` resolves both levels against the bar and
        names `take_profit` as an exit reason; this method only ever returned
        `stop_loss` or `time_exit`, so an armed target was never acted on live.
        The router arms one on every position whose signal or `StopConfig`
        carries one, and `Position.take_profit_price` is restored across a
        restart (migration `a1c4e77b91d2` added the column for it) — so the
        level existed, was persisted, and nothing looked at it. A strategy
        backtested with a target and run live without one is not the same
        strategy, which is the divergence ADR 0006 exists to refuse.

        The tie-break matches the engine exactly: when one bar's range spans
        both levels, **the stop is assumed to have filled first**. The bar
        cannot say which came first and the pessimistic reading is the only
        honest one at this resolution (docs/BACKTESTING.md). Assuming the target
        would make every backtest and every live report flatter than the truth.
        """
        if self.stop_config.stop_type is StopType.TIME:
            held = self._bars_held(position)
            if held is not None and self.stop_manager.time_exit_due(held, self.stop_config):
                return TIME_EXIT
            return None

        if self.stop_config.broker_side:
            # The *stop* is resting at the venue; checking it here as well would
            # double-exit on the bar the venue also fills. The target is not —
            # `submit_protective_orders` arms it on the position rather than
            # sending a second order — so it is still ours to watch, and
            # returning early on both would leave a broker-side configuration
            # with no upside exit at all.
            return TAKE_PROFIT if target_hit(position, bar) else None

        if self.stop_manager.should_trigger(position, bar):
            return STOP_LOSS
        return TAKE_PROFIT if target_hit(position, bar) else None

    def _bars_held(self, position: Position) -> int | None:
        """How many completed bars since the position opened."""
        if position.opened_at is None:
            return None
        return sum(1 for bar in self._bars.get(position.symbol, []) if bar.ts >= position.opened_at)

    def _drain_fills(self, portfolio: Portfolio) -> list[Signal]:
        """Step 3. Hand booked fills to the strategy.

        The fill itself was applied the moment it arrived — see
        `on_fill_event`, where protecting the position cannot wait for the next
        pass. What waits for the pass is the *strategy's* reaction to it, which
        belongs inside the documented ordering like any other signal source.
        """
        if not self._pending_fills:
            return []

        pending, self._pending_fills = self._pending_fills, []
        signals: list[Signal] = []
        for applied in pending:
            signals.extend(self.strategy.on_fill(self._context, applied.order, applied.fill) or [])
        return signals

    def _poll_strategy(self, closed: list[Bar]) -> list[Signal]:
        """Step 4. `on_bar` for each symbol whose bar just closed."""
        signals: list[Signal] = []
        for bar in closed:
            signals.extend(self.strategy.on_bar(self._context, bar) or [])
        return signals

    async def _submit(self, signals: list[Signal], portfolio: Portfolio) -> None:
        """Step 5. Size, risk-check and send.

        A `HOLD` never reaches the router: it is the strategy saying nothing
        happened, and routing it would spend a rate-limit slot and an audit
        entry to be refused.
        """
        for signal in signals:
            if signal.action is SignalAction.HOLD:
                continue
            self.stats.signals_generated += 1

            result = await self.router.submit_signal(
                self._with_stop(signal), portfolio, self.sizing
            )
            await self._record_signal(signal, result)

            if not result.submitted:
                self.stats.orders_rejected_by_risk += 1
                log.info(
                    "runner.signal_refused",
                    symbol=signal.symbol,
                    action=signal.action.value,
                    rule=result.decision.rule,
                    reason=result.decision.reason,
                )
                # Already durable as a decision, above. Recorded as an order
                # too, because the two answer different questions: the signal
                # says what the strategy wanted, this says what was actually
                # composed — the quantity after sizing, the type, the limit —
                # and `/orders` is where a person looks for that.
                await self._record_refusal(result)
                continue

            self.stats.orders_submitted += 1
            if result.order is not None:
                self._track(result.order)

    async def _record_refusal(self, result: SubmitResult) -> None:
        """Store an order the risk chain refused, so it survives the log.

        **`GET /orders` was built for exactly this row and had never seen one.**
        Its own docstring says the orders that matter most are the ones that
        never filled, and that "a rejection appears in no other read in the
        platform"; `OrderHistoryTable` renders `rejected_risk` in rose and
        shows the reason beside it. None of it could ever fire, because a
        refused order was dropped on the floor at every one of the four places
        the runner can be refused. The read path was complete and the write
        path did not exist.

        The gap was not evenly serious. A refused *signal* was already durable
        as a decision (`_record_signal`), so `/risk/rejections` could find it.
        The other three were logged and lost, and they are the worse ones:

        - a **stop exit** refused leaves a position open that should have
          closed — docs/SAFETY.md layer 5 failing;
        - a **protective stop** refused leaves a position that never had
          protection at all, which is the same layer failing at the other end;
        - a **shutdown flatten** refused leaves the book open after the worker
          believes it has gone home.

        Nothing to store when `result.order` is None. That is not a gap: a
        refusal from sizing or routing happens *before* an order is built, so
        there is no order to record — those exist as signals, or as nothing,
        and inventing a row for them would put orders in the table that were
        never composed.

        **A failure here is swallowed and logged, never raised**, which is the
        opposite of what `_record_signal` does and the difference is the
        ordering. A signal is written on the way *into* the router, and the
        order that follows carries a foreign key to it — so a signal that
        cannot be written must stop what comes next. This is written on the way
        *out*, about something that has already happened and is already in the
        structured log. Raising would make recording a refusal into a failed
        evaluation, and three of those halt trading: the record of a refused
        stop would become the thing that stops the platform. It would also
        break `stop()`, where a raise would leave the worker unable to shut
        down because it could not write down why it had not flattened.
        """
        order = result.order
        if result.submitted or order is None:
            return
        try:
            await self.order_repo.save(order, run_mode=self.run_mode)
        except Exception as exc:
            log.critical(
                "runner.refusal_unrecorded",
                symbol=order.symbol,
                status=order.status.value,
                reason=order.reject_reason,
                error=str(exc),
                effect="the refusal happened and is in this log, but /orders will not show it",
            )

    async def _record_signal(self, signal: Signal, result: SubmitResult) -> None:
        """Keep the decision and its fate, and announce it.

        Recorded whatever the outcome, which is the point: a strategy whose
        every signal is refused by a risk rule looks, from any other vantage
        point in the system, exactly like a strategy that had no ideas. The
        dashboard is where that difference is meant to be visible (requirement
        #7), so a refusal is as much a feed entry as a fill is.

        "Not acted on" covers two different things and `rejected_by` separates
        them: a rule that denied the order, and `no_action` — a HOLD-shaped
        outcome such as an exit for a position that is already flat, which the
        router reports as *approved* precisely so it does not inflate the
        rejection count an operator reads to judge whether risk is too tight.

        **Written durably before it is announced**, and before step 6 saves the
        order that references it. That ordering is what `persistence/events.py`
        asks for — anything that must not be lost is written first and
        announced second — and here it is also a foreign key: `orders.signal_id`
        points at this row, so an order saved before its signal existed would be
        refused by the database. The whole evaluation is under one lock, so no
        fill event can slip an order save in between.

        The write is allowed to raise, unlike the announcement below. It is in
        the same class as the order and the book: a durable record whose absence
        is a correctness problem rather than a dark screen. There is also no new
        failure mode in raising — a Postgres that cannot take this signal cannot
        take the order it produced either.
        """
        decision = result.decision
        await self.signal_repo.save(
            signal,
            SignalOutcome(
                acted_on=result.submitted,
                rejection_reason=None if result.submitted else decision.reason or None,
                rejected_by=None if result.submitted else decision.rule or None,
            ),
        )
        summary = SignalSummary(
            id=signal.id,
            ts=signal.ts,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            action=signal.action.value,
            reason=signal.reason,
            # Rendered as strings, not floats. An indicator value is usually a
            # price — an SMA of closes is denominated in dollars — and rule
            # §1.1's exemption for "indicator maths" covers computing it, not
            # putting it on a wire that only has binary floats to carry it.
            indicators={k: str(v) for k, v in signal.indicators.items()},
            acted_on=result.submitted,
            rejection_reason=None if result.submitted else decision.reason or None,
            rejected_by=None if result.submitted else decision.rule or None,
        )
        self._recent_signals.append(summary)
        await self._announce(CHANNEL_SIGNALS, _signal_message(summary))

    def _with_stop(self, signal: Signal) -> Signal:
        """Give an entry signal a stop level if it did not carry one.

        This closes the dependency #33 recorded against this item: `risk_pct`
        sizing with an ATR stop is the documented default pair, no `Signal`
        carries an ATR-derived level, and so every entry from a
        default-configured strategy was refused at sizing for want of a stop to
        measure risk against. The runner is the thing holding the bar history
        the ATR needs.

        A signal that already names a stop is left exactly as it is. The
        strategy's own level always wins — deriving one over the top would
        override a deliberate choice with a configured default.
        """
        if signal.stop_loss_price is not None:
            return signal
        if signal.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
            return signal
        if self.stop_config.stop_type is StopType.TIME:
            return signal  # not a level; `time_exit_due` owns it

        price = self._context.last_price(signal.symbol)
        if price is None:
            return signal  # nothing to measure from; the router will refuse it

        side = Side.BUY if signal.action is SignalAction.ENTER_LONG else Side.SELL
        try:
            level = self.stop_manager.initial_stop(
                price, side, self.stop_config, self._atr(signal.symbol)
            )
        except (ValueError, ATPError) as exc:
            # Refused rather than guessed — an ATR stop with no ATR is exactly
            # the input `StopManager` declines to default.
            log.warning("runner.could_not_derive_stop", symbol=signal.symbol, error=str(exc))
            return signal

        return replace(signal, stop_loss_price=level)

    def _atr(self, symbol: str) -> Decimal | None:
        """ATR over the configured period, as a `Decimal` for the stop maths.

        Float in, `Decimal` out via `str` — never `Decimal(float)`, which
        inherits the binary rounding error rule §1.1 exists to avoid. ATR is a
        statistic so computing it in float is fine; the moment it becomes a
        price distance it stops being one.
        """
        value = dispatch.compute("atr", self._bars.get(symbol, []), self.stop_config.period)
        return None if value is None else Decimal(str(value))

    # ── fills ───────────────────────────────────────────────────────────────

    async def on_fill_event(self, update: TradeUpdate, portfolio: Portfolio) -> None:
        """Handle a broker fill.

        On an entry fill, place protective orders IMMEDIATELY (`router
        .submit_protective_orders`). Every millisecond between owning a position
        and having a stop on it is unprotected exposure.

        Takes a `TradeUpdate` rather than the skeleton's `dict[str, object]`:
        #37 gave the platform a typed one carrying the venue's own execution id,
        and the untyped version cannot express the duplicate-fill discard that
        stops a redelivered event doubling a position.

        Booking the fill goes through `execution.trade_updates`, which owns the
        guard against a fill landing on an order our book has already killed.
        The runner does not re-implement any of that; what it adds is the
        protective order and the position accounting.
        """
        async with self._lock:
            order = self._open_orders.get(update.client_order_id)
            if order is None:
                # Not ours to book. Reconciliation reports it as an orphan; the
                # runner must not invent an order to hang it on, because the
                # quantity would then be applied to a position twice when the
                # real order turns up.
                log.warning(
                    "runner.fill_for_unknown_order",
                    client_order_id=update.client_order_id,
                    symbol=update.symbol,
                )
                return

            before = order.filled_qty
            if not apply_trade_update(order, update):
                return

            fill = order.fills[-1] if order.fills else None
            if fill is not None and order.filled_qty > before:
                self._apply_to_portfolio(order, fill, portfolio)
                self.stats.fills_applied += 1
                self._pending_fills.append(_AppliedFill(order=order, fill=fill))
                await self._protect(order, portfolio)
                # Announced only now: after the book has it and after the stop
                # is armed. A dashboard told about a fill before the position
                # is protected would be showing exposure nothing is yet
                # managing, and a reader who acts on that is acting earlier
                # than the system did.
                await self._announce(CHANNEL_ORDERS, _fill_message(order, fill))

            if order.is_complete:
                # Saved before it leaves the working set: `_persist` only walks
                # open orders, so a fill that completed an order would
                # otherwise never reach storage.
                await self.order_repo.save(order, run_mode=self.run_mode)
                self._open_orders.pop(order.client_order_id, None)

    def _apply_to_portfolio(self, order: Order, fill: Fill, portfolio: Portfolio) -> None:
        """Fold a fill into cash and the position.

        The same arithmetic the backtest engine performs on a fill, and it has
        to stay that way: a live P&L computed differently from the backtested
        one makes the comparison between them meaningless.
        """
        position = portfolio.position(order.symbol)
        position.apply_fill(fill, fill.qty * order.side.sign)
        portfolio.cash -= fill.qty * fill.price * order.side.sign + fill.fee

    async def _protect(self, order: Order, portfolio: Portfolio) -> None:
        """Arm protection on a position that just opened or grew.

        Only for entries. A fill that *reduces* a position needs no new stop —
        and asking for one would place a stop on the way out of a trade.
        """
        position = portfolio.position(order.symbol)
        if position.is_flat:
            return
        if (position.qty > 0) != (order.side is Side.BUY):
            return  # a reducing fill

        result = await self.router.submit_protective_orders(
            order, portfolio, stop_config=self.stop_config, atr_value=self._atr(order.symbol)
        )
        for protective in result.placed:
            self._track(protective)
        if not result.is_fully_protected:
            # Loud: this is docs/SAFETY.md layer 5 not holding, and the position
            # is real whether or not the stop is. `unprotected_qty` is measured
            # after the risk chain, so a stop the chain shrank reports the
            # shares it actually covers rather than the ones it asked for.
            log.error(
                "runner.position_unprotected",
                symbol=order.symbol,
                unprotected_qty=str(result.unprotected_qty),
                refusals=[r.decision.reason for r in result.refused],
            )
            # A list, because a position can be left unprotected by more than
            # one refused child. Each is its own row: "the stop was refused" and
            # "the stop and the target were both refused" are different states
            # of the same position.
            for refusal in result.refused:
                await self._record_refusal(refusal)

    def _track(self, order: Order) -> None:
        """Remember an order we believe is working at the venue."""
        if order.is_complete:
            self._open_orders.pop(order.client_order_id, None)
            return
        self._open_orders[order.client_order_id] = order

    # ── fan-out ─────────────────────────────────────────────────────────────

    async def _announce(self, channel: str, message: dict[str, Any]) -> None:
        """Tell whoever is listening. Never let it matter whether anyone was.

        Best-effort by contract (`data.ports.EventPublisher`), and the
        swallowing belongs here rather than in the adapter because this is the
        call site that knows a dropped message is survivable: the dashboard
        polls every five minutes and the poll is the authoritative path. What
        must not happen is an evaluation failing over it — three of those halt
        trading, and Redis being unable to gossip is not a reason to stop.
        """
        if self.publisher is None:
            return
        try:
            await self.publisher.publish(channel, message)
        except Exception as exc:
            log.warning("runner.publish_failed", channel=channel, error=str(exc))


def _signal_message(summary: SignalSummary) -> dict[str, Any]:
    """The wire shape `atp_api.ws` forwards to the dashboard.

    Deliberately not `encode_snapshot`'s signal document. That one is a storage
    record inside a larger snapshot; this is a client protocol with a `type`
    discriminator, versioned by whatever the dashboard understands. They look
    alike today and are free to diverge — the same separation `persistence
    .quotes` keeps from `data.stream._quote_message`, and for the same reason.
    """
    return {
        "type": "signal",
        "id": summary.id,
        "ts": summary.ts.isoformat(),
        "strategy": summary.strategy_id,
        "symbol": summary.symbol,
        "action": summary.action,
        "reason": summary.reason,
        "acted_on": summary.acted_on,
        "rejected_by": summary.rejected_by,
        "rejection_reason": summary.rejection_reason,
    }


def _fill_message(order: Order, fill: Fill) -> dict[str, Any]:
    """One execution, as the dashboard hears about it.

    Every number is a string. `RedisEventPublisher` refuses a float outright
    (rule §1.1), which is the guard that catches this being written the other
    way — but the guard fires at publish time on a live worker, and the point
    of rendering correctly here is that it never has to.
    """
    return {
        "type": "fill",
        "order_id": order.id,
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "qty": str(fill.qty),
        "price": str(fill.price),
        "filled_qty": str(order.filled_qty),
        "remaining_qty": str(order.remaining_qty),
        "status": order.status.value,
        "ts": fill.ts.isoformat(),
    }
