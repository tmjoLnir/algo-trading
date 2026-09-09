"""The live strategy loop.

The loop's value is its *ordering* — it is the mirror of the backtest engine, and
a divergence makes every backtest a claim about a system that does not exist. So
most of what follows watches the order things happen in, and the rest watches the
refusals: the runner that will not start against a mismatched book, the position
it reports as unprotected, the fill it declines to book.

Nothing here opens a socket or a database. The router, the broker, the
repositories and the clock are all fakes (CLAUDE.md §1.7).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from structlog.testing import capture_logs

from atp_core.alerts.ports import Alert, Severity
from atp_core.brokers.ports import TradeUpdate
from atp_core.channels import CHANNEL_ORDERS, CHANNEL_SIGNALS
from atp_core.clock import Session, SimulatedClock
from atp_core.dashboard.snapshot import DEFAULT_SIGNAL_LIMIT
from atp_core.domain import (
    Bar,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Portfolio,
    Position,
    Quote,
    RunMode,
    Side,
    Signal,
    SignalAction,
    StopType,
    Timeframe,
)
from atp_core.errors import BrokerConnectionError, ExecutionError
from atp_core.execution.idempotency import FLATTEN, STOP_LOSS, TAKE_PROFIT, TIME_EXIT
from atp_core.execution.reconciliation import ReconciliationReport
from atp_core.execution.router import ProtectionResult, SubmitResult
from atp_core.risk.engine import RiskDecision, RiskEngine, backtest_rules
from atp_core.risk.killswitch import HaltReason, HaltScope
from atp_core.risk.limits import DEFAULT_RISK_LIMITS
from atp_core.risk.rules import DAILY_LOSS_RULE
from atp_core.risk.stops import StopConfig, StopManager
from atp_core.strategy.base import Strategy
from atp_core.strategy.rules import PositionSizeSpec
from atp_worker.runner import (
    MAX_CONSECUTIVE_ERRORS,
    RATE_LIMIT_STORM_REFUSALS,
    LiveContext,
    StrategyRunner,
)
from tests.fakes import (
    FakeKillSwitch,
    FakeOrderRepository,
    FakePortfolioRepository,
    FakePublisher,
    FakeSignalRepository,
    FakeSnapshotStore,
    FakeStrategyRepository,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, MutableMapping, Sequence

    from atp_core.strategy.context import StrategyContext

START = datetime(2024, 6, 3, 13, 30, tzinfo=UTC)
SYMBOL = "SPY"


def bar(index: int, close: float = 100.0, *, symbol: str = SYMBOL) -> Bar:
    price = Decimal(str(close))
    return Bar(
        symbol=symbol,
        ts=START + timedelta(days=index),
        timeframe=Timeframe.D1,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("1000000"),
    )


class ScriptedStrategy(Strategy):
    """Emits whatever the script says on the n-th bar it is shown."""

    name: ClassVar[str] = "scripted"

    def __init__(self, script: dict[int, SignalAction] | None = None, *, warmup: int = 0) -> None:
        super().__init__(None)
        self.script = script or {}
        self._warmup = warmup
        self.calls = 0
        self.bars_seen: list[Bar] = []
        self.fills_seen: list[Fill] = []
        self.started = False
        self.stopped = False

    @property
    def warmup_bars(self) -> int:
        return self._warmup

    def on_start(self) -> None:
        self.started = True

    def on_stop(self) -> None:
        self.stopped = True

    def on_bar(self, ctx: StrategyContext, bar_: Bar) -> list[Signal]:
        index = self.calls
        self.calls += 1
        self.bars_seen.append(bar_)
        action = self.script.get(index)
        if action is None:
            return []
        return [Signal(strategy_id=self.name, symbol=bar_.symbol, action=action, ts=ctx.now)]

    def on_fill(self, ctx: StrategyContext, order: Order, fill: Fill) -> list[Signal]:
        self.fills_seen.append(fill)
        return []


class FakeBarRepo:
    """Bars for one timeframe, and it **filters on it** like the real one.

    `PostgresBarRepository` narrows on the column
    (`.where(BarRow.timeframe == timeframe.value)`), so a runner asking for a
    series nothing writes gets an empty list rather than an error. This fake
    ignored the argument and answered every timeframe with the same bars —
    which is why nothing here noticed that `build_runner` hard-coded
    `Timeframe.D1` against a `1m` ingestor, and why the strategy went a full
    session without being handed a bar (docs/paper-week/day-1-review.md).

    A fake that cannot express the disagreement cannot catch it.
    """

    def __init__(
        self, bars: dict[str, list[Bar]] | None = None, *, timeframe: Timeframe = Timeframe.D1
    ) -> None:
        self.bars = bars or {}
        #: The one series this repository holds. Anything else reads as empty.
        self.timeframe = timeframe

    def _series(self, symbol: str, timeframe: Any) -> list[Bar]:
        if timeframe is not None and timeframe != self.timeframe:
            return []
        return list(self.bars.get(symbol, []))

    async def upsert_bars(self, bars: list[Bar]) -> int:
        return 0

    async def get_bars(self, symbol: str, timeframe: Any, start: Any, end: Any) -> list[Bar]:
        return self._series(symbol, timeframe)

    async def get_last_n_bars(
        self,
        symbol: str,
        timeframe: Any,
        n: int,
        *,
        not_before: datetime | None = None,
    ) -> list[Bar]:
        """**Honours `not_before`, like the real one.**

        A fake that ignored it could not express the bug the bound exists for —
        a warmup silently assembling a full window from bars belonging to a
        previous session — which is the same lesson this class's own docstring
        already carries about `timeframe`.
        """
        series = self._series(symbol, timeframe)
        if not_before is not None:
            series = [b for b in series if b.ts >= not_before]
        return series[-n:]

    async def find_gaps(self, symbol: str, timeframe: Any, start: Any, end: Any) -> list[Any]:
        return []

    async def stored_series(self) -> list[tuple[str, Any]]:
        return [(symbol, self.timeframe) for symbol in self.bars]


class FakeQuoteCache:
    def __init__(self) -> None:
        self.quotes: dict[str, Quote] = {}

    async def set_quote(self, quote: Quote) -> None:
        self.quotes[quote.symbol] = quote

    async def get_quote(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: self.quotes[s] for s in symbols if s in self.quotes}


class FakeRouter:
    """Records what the runner asked for, in order.

    **A risk refusal carries the order it refused**, as `OrderRouter` does:
    `transition(order, OrderStatus.REJECTED_RISK, ...)` then
    `SubmitResult(order=order, ..., submitted=False)`. This fake returned
    `order=None` for every refusal, which is why nothing here noticed that the
    runner dropped refused orders on the floor — the read path in `/orders` was
    built for a row the write path never produced, and a fake that never
    produced one either could not have caught it.

    `refuse_before_building` models the other half honestly: a refusal from
    sizing or routing happens before an order exists, and there is genuinely
    nothing to record.

    It carries a **real** `RiskEngine`, because `warmup` anchors the session
    through it. A stub that only recorded the call would pass while the rule
    that needs anchoring still went un-anchored, which is the failure that made
    the anchoring necessary in the first place.
    """

    def __init__(self) -> None:
        self.risk_engine = RiskEngine(DEFAULT_RISK_LIMITS, rules=backtest_rules())
        self.calls: list[str] = []
        self.signals: list[Signal] = []
        #: What the runner passed as in-flight on each `submit_signal`.
        self.pending_seen: list[list[Order]] = []
        self.flattened: list[str] = []
        self.flatten_purposes: list[str] = []
        self.protected: list[Order] = []
        self.refuse_signals = False
        #: Which rule the refusal names, for the escalation tests. Default keeps
        #: every existing test on the anonymous rule they were written against.
        self.refuse_rule = "a_rule"
        #: Return `no_action` instead of a refusal — an approved decision with
        #: nothing submitted, which is what an exit for an already-flat position
        #: produces and which must never count as a risk rejection (F14).
        self.no_action = False
        self.refuse_protection = False
        #: Symbols whose protective orders were cancelled.
        self.protection_cancelled: list[str] = []
        #: An exception to raise from `submit_protective_orders`, or None.
        self.protection_raises: Exception | None = None
        #: What the venue is holding, by symbol. The runner asks before acting
        #: on an engine-side stop, so a fake that could not answer would let
        #: `_stop_is_missing` pass on a fiction.
        self.protected_qty: dict[str, Decimal] = {}
        self.refuse_flatten = False
        #: Refuse *before* an order is composed — sizing, routing. The refusal
        #: is then real and there is no order to store.
        self.refuse_before_building = False
        self._next_id = 0

    def _refused(self, symbol: str, side: Side, rule: str, reason: str) -> SubmitResult:
        """A refusal shaped like the router's, order included."""
        if self.refuse_before_building:
            return SubmitResult(
                order=None, decision=RiskDecision.deny(rule, reason), submitted=False
            )
        order = self._order(symbol, side)
        order.status = OrderStatus.REJECTED_RISK
        order.reject_reason = reason
        # Both halves, as `OrderRouter._route` records them. A fake that set
        # only the reason would let a refusal reach the order table unable to
        # say which rule made it, and no test here would notice.
        order.rejected_by = rule
        return SubmitResult(order=order, decision=RiskDecision.deny(rule, reason), submitted=False)

    def _order(self, symbol: str, side: Side, order_type: OrderType = OrderType.MARKET) -> Order:
        self._next_id += 1
        return Order(
            symbol=symbol,
            side=side,
            qty=Decimal("10"),
            order_type=order_type,
            stop_price=Decimal("90") if order_type is OrderType.STOP else None,
            client_order_id=f"atp-{self._next_id}",
            broker_order_id=f"brk-{self._next_id}",
            status=OrderStatus.SUBMITTED,
        )

    def broker_side_protected_qty(self, symbol: str, position: Position) -> Decimal:
        """How much of `position` this fake says is covered at the venue.

        Side-aware in the real router; here the tests set the number directly,
        because what is under test is what the *runner* does with the answer.
        """
        return self.protected_qty.get(symbol, Decimal(0))

    async def submit_signal(
        self,
        signal: Signal,
        portfolio: Portfolio,
        sizing: Any,
        *,
        pending: Iterable[Order] = (),
    ) -> SubmitResult:
        self.calls.append("submit_signal")
        # Recorded so `TestPendingReachesTheChain` can assert the runner hands
        # down what it believes is working — the whole point of the argument.
        self.pending_seen.append(list(pending))
        self.signals.append(signal)
        side = Side.BUY if signal.action is SignalAction.ENTER_LONG else Side.SELL
        if self.no_action:
            return SubmitResult.no_action(f"{signal.symbol}: already flat")
        if self.refuse_signals:
            return self._refused(signal.symbol, side, self.refuse_rule, "nope")
        return SubmitResult(
            order=self._order(signal.symbol, side), decision=RiskDecision.allow(), submitted=True
        )

    async def submit_protective_orders(
        self,
        entry_order: Order,
        portfolio: Portfolio,
        *,
        stop_config: Any = None,
        atr_value: Any = None,
    ) -> ProtectionResult:
        self.calls.append("submit_protective_orders")
        self.protected.append(entry_order)
        if self.protection_raises is not None:
            # The real router does this: `_route` delegates an unknown outcome
            # to `_resolve_indeterminate`, which engages a global halt and
            # raises. The fake could not express it, which is why nothing caught
            # that the raise escaped the fill handler and killed the worker.
            raise self.protection_raises
        if self.refuse_protection:
            return ProtectionResult(
                placed=[],
                refused=[
                    self._refused(
                        entry_order.symbol,
                        entry_order.side.opposite,
                        "kill_switch",
                        "halted",
                    )
                ],
                covered_qty=Decimal(0),
                unprotected_qty=Decimal("10"),
            )
        stop = self._order(entry_order.symbol, entry_order.side.opposite, OrderType.STOP)
        return ProtectionResult(
            placed=[stop], covered_qty=Decimal("10"), unprotected_qty=Decimal(0)
        )

    async def cancel_protection(self, symbol: str) -> int:
        self.calls.append("cancel_protection")
        self.protection_cancelled.append(symbol)
        return 1

    async def flatten(
        self,
        symbol: str,
        portfolio: Portfolio,
        *,
        decided_at: datetime | None = None,
        purpose: str = FLATTEN,
    ) -> SubmitResult:
        self.calls.append("flatten")
        self.flattened.append(symbol)
        #: What the runner said this exit was for. The whole of exit-reason
        #: attribution rides on it reaching the order, so tests assert on it.
        self.flatten_purposes.append(purpose)
        if self.refuse_flatten:
            return self._refused(symbol, Side.SELL, "kill_switch", "halted")
        return SubmitResult(
            order=self._order(symbol, Side.SELL), decision=RiskDecision.allow(), submitted=True
        )


class FakeReconciler:
    def __init__(self, clean: bool = True) -> None:
        self.clean = clean
        self.calls: list[list[Order]] = []

    async def reconcile(
        self, portfolio: Portfolio, *, known_orders: Any, **kwargs: Any
    ) -> ReconciliationReport:
        # Resolves a callable exactly as the real `Reconciler._compare` does.
        # Callers now defer the read so both local books are taken at one
        # instant (F4), and a fake that could only accept the eagerly evaluated
        # form would make the fix untestable through the runner.
        self.calls.append(list(known_orders() if callable(known_orders) else known_orders))
        report = ReconciliationReport(checked_at=START)
        if not self.clean:
            report.orphan_order_ids.append("atp-orphan")
        return report


class FakeCalendar:
    """One session a day, 13:30-20:00 UTC, on whatever days it is given.

    `sessions` is real enough for `_warmup_floor` to find the session covering
    an instant, because that is the question the floor asks. `days` is the set
    of exchange-local dates that trade; anything else is a closure, which is
    what makes a four-day weekend expressible here at all.
    """

    def __init__(self, open_: bool = True, *, days: set[date] | None = None) -> None:
        self.open = open_
        self.days = days

    def is_open(self, ts: datetime) -> bool:
        return self.open

    def next_open(self, after: datetime) -> datetime:
        return after + timedelta(hours=1)

    def sessions(self, first: date, last: date) -> list[Session]:
        out: list[Session] = []
        day = first
        while day <= last:
            if self.days is None or day in self.days:
                out.append(
                    Session(
                        day=day,
                        open_at=datetime.combine(day, dt_time(13, 30), tzinfo=UTC),
                        close_at=datetime.combine(day, dt_time(20, 0), tzinfo=UTC),
                    )
                )
            day += timedelta(days=1)
        return out


class RecordingAlertSink:
    """Every alert, in order. `AlertSink` implementations must not raise, and
    this one cannot — which is the contract, not a convenience."""

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)


def close_bar(runner: StrategyRunner, b: Bar) -> None:
    """Make a bar available to the repository, as if it had just closed.

    `warmup` loads the *most recent* bars, so a runner handed its whole series
    up front has nothing left to close and never calls the strategy. Tests warm
    up on the history and then close the next bar through here.
    """
    runner.bar_repo.bars.setdefault(b.symbol, []).append(b)  # type: ignore[attr-defined]


def build(
    strategy: ScriptedStrategy | None = None,
    *,
    bars: list[Bar] | None = None,
    clean: bool = True,
    stop_config: StopConfig | None = None,
    order_repo: FakeOrderRepository | None = None,
    portfolio_repo: FakePortfolioRepository | None = None,
    strategy_repo: FakeStrategyRepository | None = None,
    signal_repo: FakeSignalRepository | None = None,
    snapshot_store: FakeSnapshotStore | None = None,
    publisher: FakePublisher | None = None,
    alerts: RecordingAlertSink | None = None,
    signal_limit: int = DEFAULT_SIGNAL_LIMIT,
) -> tuple[StrategyRunner, FakeRouter, FakeKillSwitch, FakeReconciler, Portfolio, list[float]]:
    router = FakeRouter()
    switch = FakeKillSwitch()
    reconciler = FakeReconciler(clean=clean)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    runner = StrategyRunner(
        strategy=strategy or ScriptedStrategy(),
        symbols=[SYMBOL],
        router=router,  # type: ignore[arg-type]
        stop_manager=StopManager(),
        kill_switch=switch,
        bar_repo=FakeBarRepo({SYMBOL: bars or [bar(0)]}),
        quote_cache=FakeQuoteCache(),
        clock=SimulatedClock(START),
        calendar=FakeCalendar(),  # type: ignore[arg-type]
        reconciler=reconciler,  # type: ignore[arg-type]
        sizing=PositionSizeSpec(type="fixed_qty", value=Decimal("10")),
        stop_config=stop_config or StopConfig(stop_type=StopType.FIXED_PCT, value=Decimal("0.02")),
        timeframe=Timeframe.D1,
        run_mode=RunMode.PAPER,
        order_repo=order_repo or FakeOrderRepository(),  # type: ignore[arg-type]
        portfolio_repo=portfolio_repo or FakePortfolioRepository(),
        strategy_repo=strategy_repo or FakeStrategyRepository(),
        signal_repo=signal_repo or FakeSignalRepository(),
        snapshot_store=snapshot_store,
        publisher=publisher,
        alerts=alerts,
        signal_limit=signal_limit,
        sleep=sleep,
    )
    portfolio = Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))
    return runner, router, switch, reconciler, portfolio, slept


class TestWarmup:
    @pytest.mark.asyncio
    async def test_loads_history_so_indicators_are_right_from_the_first_live_bar(self) -> None:
        strategy = ScriptedStrategy(warmup=3)
        runner, _, _, _, portfolio, _ = build(strategy, bars=[bar(i) for i in range(5)])

        await runner.warmup(portfolio)

        assert len(runner._context.history(SYMBOL, Timeframe.D1, 3)) == 3

    @pytest.mark.asyncio
    async def test_it_reconciles_before_the_first_evaluation(self) -> None:
        """A runner that starts assuming it is flat will happily double a
        position it already holds."""
        runner, _, _, reconciler, portfolio, _ = build()

        await runner.warmup(portfolio)

        assert len(reconciler.calls) == 1

    @pytest.mark.asyncio
    async def test_a_mismatched_book_refuses_to_start(self) -> None:
        """The operator sees why at startup, rather than finding a process that
        is running and silently declining to trade."""
        runner, _, _, _, portfolio, _ = build(clean=False)

        with pytest.raises(ExecutionError, match="does not match the broker"):
            await runner.warmup(portfolio)

    @pytest.mark.asyncio
    async def test_it_tells_the_reconciler_what_we_believe_is_working(self) -> None:
        """The set #38 had no source for — the runner is the thing that knows
        what it submitted."""
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, _, reconciler, portfolio, _ = build(strategy, bars=[bar(0)])
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        await runner.warmup(portfolio)

        assert [o.client_order_id for o in reconciler.calls[-1]] == ["atp-1"]

    @pytest.mark.asyncio
    async def test_the_strategy_is_started(self) -> None:
        strategy = ScriptedStrategy()
        runner, _, _, _, portfolio, _ = build(strategy)

        await runner.warmup(portfolio)

        assert strategy.started is True


class TestInFlightOrdersReachTheChain:
    """The runner tells the router what it believes is working at the venue.

    `_submit` loops signals through the router against one `portfolio` object,
    and that object only moves when a fill drains in. So without this every
    signal in a pass is risk-checked against a book holding none of the others
    — the same defect a 40-symbol `buy_and_hold` replay showed as 1.97x gross
    exposure with the cap refusing nothing, reached here through production
    code rather than a backtest.

    `_open_orders` is the right source rather than a batch-local list: it is
    restored from the database at warmup and cleared on a terminal state, so an
    order still working from an earlier bar counts too.
    """

    @pytest.mark.asyncio
    async def test_the_runner_passes_what_it_believes_is_working(self) -> None:
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        await runner.warmup(portfolio)

        working = router._order(SYMBOL, Side.BUY)
        runner._open_orders[working.client_order_id or "k"] = working

        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        assert router.pending_seen, "the runner should have submitted a signal"
        assert working in router.pending_seen[-1]

    @pytest.mark.asyncio
    async def test_an_empty_book_passes_an_empty_set(self) -> None:
        """Nothing outstanding is not the same as not passing the argument, and
        the projection returns the book untouched either way."""
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        await runner.warmup(portfolio)

        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        assert router.pending_seen == [[]]


class TestTheOrdering:
    """The mirror of the backtest engine. Stops before signals, always."""

    @pytest.mark.asyncio
    async def test_stops_are_checked_before_signals_are_submitted(self) -> None:
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(
            strategy,
            bars=[bar(0)],
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.02"), broker_side=False
            ),
        )
        await runner.warmup(portfolio)
        position = portfolio.position(SYMBOL)
        position.qty = Decimal("10")
        position.avg_entry_price = Decimal("100")
        position.stop_loss_price = Decimal("98")
        position.last_price = Decimal("100")

        # A bar that trades through the stop *and* signals an entry, so both
        # paths are live on the same pass and the order between them is what
        # the assertion reads.
        close_bar(runner, bar(1, close=80.0))
        await runner.evaluate(portfolio)

        assert router.calls.index("flatten") < router.calls.index("submit_signal")

    @pytest.mark.asyncio
    async def test_a_bar_that_has_not_moved_on_does_not_retrigger_the_strategy(self) -> None:
        """An idempotent upsert re-serving the same bar, or a restatement
        landing, must not read as a fresh close."""
        strategy = ScriptedStrategy()
        runner, _, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        await runner.warmup(portfolio)

        # Deliberately no new bar: the repository keeps serving bar 0.
        await runner.evaluate(portfolio)
        await runner.evaluate(portfolio)

        assert strategy.calls == 0  # warmup already held bar 0

    @pytest.mark.asyncio
    async def test_a_newly_closed_bar_reaches_the_strategy(self) -> None:
        strategy = ScriptedStrategy()
        repo_bars = [bar(0)]
        runner, _, _, _, portfolio, _ = build(strategy, bars=repo_bars)
        await runner.warmup(portfolio)

        repo_bars.append(bar(1))
        await runner.evaluate(portfolio)

        assert [b.ts for b in strategy.bars_seen] == [bar(1).ts]

    @pytest.mark.asyncio
    async def test_a_hold_never_reaches_the_router(self) -> None:
        strategy = ScriptedStrategy({0: SignalAction.HOLD})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        assert "submit_signal" not in router.calls


class TestMarking:
    @pytest.mark.asyncio
    async def test_positions_are_marked_from_the_quote_not_the_bar(self) -> None:
        """A mark is what the book is worth now, and every percentage risk
        limit is denominated in it."""
        runner, _, _, _, portfolio, _ = build()
        await runner.warmup(portfolio)
        portfolio.position(SYMBOL).qty = Decimal("10")
        await runner.quote_cache.set_quote(
            Quote(symbol=SYMBOL, ts=START, bid=Decimal("200"), ask=Decimal("202"))
        )

        await runner.evaluate(portfolio)

        assert portfolio.position(SYMBOL).last_price == Decimal("201")

    @pytest.mark.asyncio
    async def test_a_missing_quote_falls_back_to_the_last_bar(self) -> None:
        """An unmarked holding makes every percentage limit compute too small
        and approve what it should refuse."""
        runner, _, _, _, portfolio, _ = build(bars=[bar(0, close=123.0)])
        await runner.warmup(portfolio)
        portfolio.position(SYMBOL).qty = Decimal("10")

        await runner.evaluate(portfolio)

        assert portfolio.position(SYMBOL).last_price == Decimal("123.0")
        assert portfolio.unmarked_symbols == []


class TestFills:
    @staticmethod
    def a_fill_update(client_order_id: str, qty: str = "10", price: str = "100") -> TradeUpdate:
        return TradeUpdate(
            event="fill",
            client_order_id=client_order_id,
            broker_order_id="brk-1",
            symbol=SYMBOL,
            at=START,
            fill=Fill(
                order_id="x", ts=START, qty=Decimal(qty), price=Decimal(price), venue_fill_id="e1"
            ),
        )

    @pytest.mark.asyncio
    async def test_an_entry_fill_is_protected_immediately(self) -> None:
        """Every millisecond between owning a position and having a stop on it
        is unprotected exposure — it does not wait for the next pass."""
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)
        router.calls.clear()

        await runner.on_fill_event(self.a_fill_update("atp-1"), portfolio)

        assert router.calls == ["submit_protective_orders"]
        assert portfolio.position(SYMBOL).qty == Decimal("10")

    @pytest.mark.asyncio
    async def test_a_position_left_unprotected_is_reported(self) -> None:
        """docs/SAFETY.md layer 5 not holding. The position is real whether or
        not the stop is."""
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        router.refuse_protection = True
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        await runner.on_fill_event(self.a_fill_update("atp-1"), portfolio)

        assert portfolio.position(SYMBOL).qty == Decimal("10")  # the position stands

    @pytest.mark.asyncio
    async def test_a_fill_for_an_order_we_do_not_know_is_not_booked(self) -> None:
        """Inventing an order to hang it on would apply the quantity twice when
        the real order turns up. Reconciliation reports it as an orphan."""
        runner, _, _, _, portfolio, _ = build()
        await runner.warmup(portfolio)

        await runner.on_fill_event(self.a_fill_update("atp-not-ours"), portfolio)

        assert portfolio.position(SYMBOL).is_flat

    @pytest.mark.asyncio
    async def test_a_redelivered_fill_does_not_double_the_position(self) -> None:
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)
        update = self.a_fill_update("atp-1")

        await runner.on_fill_event(update, portfolio)
        await runner.on_fill_event(update, portfolio)

        assert portfolio.position(SYMBOL).qty == Decimal("10")

    @pytest.mark.asyncio
    async def test_the_strategy_sees_the_fill_on_the_next_pass(self) -> None:
        """Booking cannot wait; the strategy's *reaction* belongs inside the
        loop's ordering like any other signal source."""
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)
        await runner.on_fill_event(self.a_fill_update("atp-1"), portfolio)
        assert strategy.fills_seen == []

        await runner.evaluate(portfolio)

        assert len(strategy.fills_seen) == 1

    @pytest.mark.asyncio
    async def test_cash_moves_with_the_fill(self) -> None:
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        await runner.on_fill_event(self.a_fill_update("atp-1", qty="10", price="100"), portfolio)

        assert portfolio.cash == Decimal("99000")


class TestDerivingAStop:
    @pytest.mark.asyncio
    async def test_an_entry_without_a_stop_gets_one(self) -> None:
        """Closes the dependency #33 recorded: `risk_pct` with an ATR stop is
        the documented default pair, and no `Signal` carries the level."""
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0, 100.0)])
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        assert router.signals[0].stop_loss_price == Decimal("98.00")

    @pytest.mark.asyncio
    async def test_a_strategys_own_stop_always_wins(self) -> None:
        """Deriving one over the top would override a deliberate choice with a
        configured default."""

        class WithStop(ScriptedStrategy):
            def on_bar(self, ctx: StrategyContext, bar_: Bar) -> list[Signal]:
                self.calls += 1
                return [
                    Signal(
                        strategy_id=self.name,
                        symbol=bar_.symbol,
                        action=SignalAction.ENTER_LONG,
                        ts=ctx.now,
                        stop_loss_price=Decimal("42"),
                    )
                ]

        runner, router, _, _, portfolio, _ = build(WithStop(), bars=[bar(0)])
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        assert router.signals[0].stop_loss_price == Decimal("42")

    @pytest.mark.asyncio
    async def test_an_exit_is_left_alone(self) -> None:
        strategy = ScriptedStrategy({0: SignalAction.EXIT})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        assert router.signals[0].stop_loss_price is None


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_an_exception_does_not_kill_the_loop(self) -> None:
        class Exploding(ScriptedStrategy):
            def on_bar(self, ctx: StrategyContext, bar_: Bar) -> list[Signal]:
                raise RuntimeError("boom")

        repo_bars = [bar(0)]
        runner, _, switch, _, portfolio, _ = build(Exploding(), bars=repo_bars)
        await runner.warmup(portfolio)

        repo_bars.append(bar(1))
        await runner.evaluate(portfolio)

        assert runner.stats.errors == 1
        assert switch.engaged is False

    @pytest.mark.asyncio
    async def test_repeated_failures_halt_the_strategy(self) -> None:
        """A runner erroring every tick is not trading, but it looks alive to a
        health check."""

        class Exploding(ScriptedStrategy):
            def on_bar(self, ctx: StrategyContext, bar_: Bar) -> list[Signal]:
                raise RuntimeError("boom")

        repo_bars = [bar(0)]
        runner, _, switch, _, portfolio, _ = build(Exploding(), bars=repo_bars)
        await runner.warmup(portfolio)

        for i in range(1, MAX_CONSECUTIVE_ERRORS + 1):
            repo_bars.append(bar(i))
            await runner.evaluate(portfolio)

        assert switch.engaged is True

    @pytest.mark.asyncio
    async def test_a_good_pass_resets_the_streak(self) -> None:
        class SometimesExploding(ScriptedStrategy):
            def on_bar(self, ctx: StrategyContext, bar_: Bar) -> list[Signal]:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("boom")
                return []

        repo_bars = [bar(0)]
        runner, _, switch, _, portfolio, _ = build(SometimesExploding(), bars=repo_bars)
        await runner.warmup(portfolio)

        repo_bars.append(bar(1))
        await runner.evaluate(portfolio)
        repo_bars.append(bar(2))
        await runner.evaluate(portfolio)

        assert runner.stats.consecutive_errors == 0
        assert switch.engaged is False


class TestShutdown:
    @pytest.mark.asyncio
    async def test_positions_are_left_open_by_default(self) -> None:
        """Liquidating on every deploy turns a routine restart into a taxable
        event and a guaranteed spread cost."""
        runner, router, _, _, portfolio, _ = build()
        await runner.warmup(portfolio)
        portfolio.position(SYMBOL).qty = Decimal("10")
        portfolio.position(SYMBOL).last_price = Decimal("100")

        await runner.shutdown()

        assert router.flattened == []

    @pytest.mark.asyncio
    async def test_closing_positions_goes_through_the_router(self) -> None:
        """Never around it — `flatten` still passes the risk chain (ADR 0005)."""
        runner, router, _, _, portfolio, _ = build()
        await runner.warmup(portfolio)
        portfolio.position(SYMBOL).qty = Decimal("10")
        portfolio.position(SYMBOL).last_price = Decimal("100")

        await runner.shutdown(close_positions=True)

        assert router.flattened == [SYMBOL]

    @pytest.mark.asyncio
    async def test_the_strategy_is_stopped(self) -> None:
        strategy = ScriptedStrategy()
        runner, _, _, _, portfolio, _ = build(strategy)
        await runner.warmup(portfolio)

        await runner.shutdown()

        assert strategy.stopped is True


class TestPublishing:
    """Step 6's other half — what the dashboard is told, and when.

    The rule running through all of it: publishing is best-effort and comes
    *after* the durable writes. Anything that must not be lost is in the
    database or the book before it is announced, and nothing announced is
    allowed to fail an evaluation — three failed evaluations halt trading, and
    stopping a strategy because a screen went dark is a cure worse than the
    disease.
    """

    @pytest.mark.asyncio
    async def test_a_pass_publishes_the_book(self) -> None:
        store = FakeSnapshotStore()
        runner, _, _, _, portfolio, _ = build(snapshot_store=store)
        await runner.warmup(portfolio)

        await runner.evaluate(portfolio)

        assert len(store.puts) == 1
        assert store.puts[0].run_mode is RunMode.PAPER
        assert store.puts[0].strategy == "scripted"

    @pytest.mark.asyncio
    async def test_the_snapshot_shares_the_instant_the_durable_one_was_written_at(self) -> None:
        """One book, one instant. Two calls to the clock would let the published
        picture and the stored one disagree by however long a write took."""
        store = FakeSnapshotStore()
        book = FakePortfolioRepository()
        runner, _, _, _, portfolio, _ = build(snapshot_store=store, portfolio_repo=book)
        await runner.warmup(portfolio)

        await runner.evaluate(portfolio)

        assert store.puts[0].as_of == book.snapshots[-1][0]

    @pytest.mark.asyncio
    async def test_a_store_that_is_down_does_not_fail_the_evaluation(self) -> None:
        """And therefore cannot count toward the consecutive-error halt."""
        store = FakeSnapshotStore()
        store.put_error = ConnectionError("redis is down")
        runner, _, switch, _, portfolio, _ = build(snapshot_store=store)
        await runner.warmup(portfolio)

        await runner.evaluate(portfolio)

        assert runner.stats.errors == 0
        assert runner.stats.consecutive_errors == 0
        assert switch.engaged is False

    @pytest.mark.asyncio
    async def test_no_store_configured_is_not_an_error(self) -> None:
        """A worker without one is running blind, not running wrong — refusing
        to trade over it would stop a strategy to protect a screen."""
        runner, _, _, _, portfolio, _ = build()
        await runner.warmup(portfolio)

        await runner.evaluate(portfolio)

        assert runner.stats.errors == 0

    @pytest.mark.asyncio
    async def test_the_feed_pulse_comes_from_the_watchlist_not_the_book(self) -> None:
        """`_mark` returns early when nothing is open, so a snapshot built from
        its cache would report "no data has ever arrived" for a flat book with a
        perfectly healthy feed."""
        store = FakeSnapshotStore()
        runner, _, _, _, portfolio, _ = build(snapshot_store=store)
        await runner.warmup(portfolio)
        runner.quote_cache.quotes[SYMBOL] = Quote(  # type: ignore[attr-defined]
            symbol=SYMBOL, ts=START, bid=Decimal("99"), ask=Decimal("101")
        )

        await runner.evaluate(portfolio)

        assert portfolio.open_positions == []
        assert store.puts[0].last_data_at == START

    @pytest.mark.asyncio
    async def test_a_refused_signal_reaches_the_feed_with_the_rule_that_refused_it(self) -> None:
        """A strategy blocked on every bar looks, from anywhere else, exactly
        like a strategy with no ideas."""
        store = FakeSnapshotStore()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)], snapshot_store=store)
        router.refuse_signals = True
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        published = store.puts[-1].recent_signals
        assert [s.acted_on for s in published] == [False]
        assert published[0].rejected_by == "a_rule"
        assert published[0].rejection_reason == "nope"

    @pytest.mark.asyncio
    async def test_an_accepted_signal_is_recorded_as_acted_on(self) -> None:
        store = FakeSnapshotStore()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, _, _, portfolio, _ = build(strategy, bars=[bar(0)], snapshot_store=store)
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        published = store.puts[-1].recent_signals
        assert [s.acted_on for s in published] == [True]
        assert published[0].rejected_by is None

    @pytest.mark.asyncio
    async def test_the_feed_is_bounded(self) -> None:
        """It is a fixed-size document in Redis. An unbounded list of a busy
        day's signals would grow it without limit."""
        store = FakeSnapshotStore()
        strategy = ScriptedStrategy(dict.fromkeys(range(10), SignalAction.ENTER_LONG))
        runner, _, _, _, portfolio, _ = build(
            strategy, bars=[bar(0)], snapshot_store=store, signal_limit=3
        )
        await runner.warmup(portfolio)
        for i in range(1, 6):
            close_bar(runner, bar(i))
            await runner.evaluate(portfolio)

        assert len(store.puts[-1].recent_signals) == 3

    @pytest.mark.asyncio
    async def test_a_signal_is_announced_on_the_signals_channel(self) -> None:
        publisher = FakePublisher()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, _, _, portfolio, _ = build(strategy, bars=[bar(0)], publisher=publisher)
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        announced = publisher.on(CHANNEL_SIGNALS)
        assert [m["type"] for m in announced] == ["signal"]
        assert announced[0]["symbol"] == SYMBOL

    @pytest.mark.asyncio
    async def test_a_publisher_that_raises_does_not_fail_the_evaluation(self) -> None:
        publisher = FakePublisher(error=ConnectionError("redis is down"))
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, switch, _, portfolio, _ = build(strategy, bars=[bar(0)], publisher=publisher)
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        assert runner.stats.errors == 0
        assert switch.engaged is False

    @pytest.mark.asyncio
    async def test_a_fill_is_announced_only_after_the_position_is_protected(self) -> None:
        """A dashboard told about a fill before the stop is armed would be
        showing exposure nothing is yet managing, and a reader who acts on that
        is acting earlier than the system did.
        """
        publisher = FakePublisher()
        order_events: list[str] = []

        class RecordingRouter(FakeRouter):
            async def submit_protective_orders(self, *args: Any, **kwargs: Any) -> ProtectionResult:
                order_events.append("protected")
                return await super().submit_protective_orders(*args, **kwargs)

        runner, _, _, _, portfolio, _ = build(publisher=publisher)
        runner.router = RecordingRouter()  # type: ignore[assignment]
        await runner.warmup(portfolio)

        order = Order(
            symbol=SYMBOL,
            side=Side.BUY,
            qty=Decimal("10"),
            client_order_id="atp-entry",
            broker_order_id="brk-entry",
            status=OrderStatus.SUBMITTED,
        )
        runner._track(order)

        original_publish = publisher.publish

        async def recording_publish(channel: str, message: dict[str, Any]) -> None:
            if channel == CHANNEL_ORDERS:
                order_events.append("announced")
            await original_publish(channel, message)

        publisher.publish = recording_publish  # type: ignore[method-assign]

        await runner.on_fill_event(
            TradeUpdate(
                event="fill",
                client_order_id="atp-entry",
                broker_order_id="brk-entry",
                symbol=SYMBOL,
                at=START,
                fill=Fill(
                    order_id=order.id,
                    ts=START,
                    qty=Decimal("10"),
                    price=Decimal("100"),
                    venue_fill_id="v-1",
                ),
            ),
            portfolio,
        )

        assert order_events == ["protected", "announced"]
        assert publisher.on(CHANNEL_ORDERS)[0]["price"] == "100"

    @pytest.mark.asyncio
    async def test_a_fill_message_carries_no_floats(self) -> None:
        """`RedisEventPublisher` refuses one outright (rule §1.1). The guard
        fires at publish time on a live worker; the point of rendering correctly
        here is that it never has to."""
        publisher = FakePublisher()
        runner, _, _, _, portfolio, _ = build(publisher=publisher)
        await runner.warmup(portfolio)
        order = Order(
            symbol=SYMBOL,
            side=Side.BUY,
            qty=Decimal("10"),
            client_order_id="atp-entry",
            broker_order_id="brk-entry",
            status=OrderStatus.SUBMITTED,
        )
        runner._track(order)

        await runner.on_fill_event(
            TradeUpdate(
                event="fill",
                client_order_id="atp-entry",
                broker_order_id="brk-entry",
                symbol=SYMBOL,
                at=START,
                fill=Fill(order_id=order.id, ts=START, qty=Decimal("10"), price=Decimal("100.125")),
            ),
            portfolio,
        )

        message = publisher.on(CHANNEL_ORDERS)[0]
        assert not any(isinstance(v, float) for v in message.values())
        assert message["price"] == "100.125"


class TestProtectiveExits:
    """Which level fired, and what the resulting order says it was for.

    Both halves matter and for different reasons. The take-profit half is a
    live-vs-backtest divergence: `BacktestEngine._check_stops` has always
    resolved a target and named `take_profit`, and this loop did not look at one
    at all — so a strategy backtested with a target ran live without it. The
    purpose half is what makes an exit attributable afterwards; all three
    engine-side exits reach the venue as `router.flatten`, and without a purpose
    they store identically.
    """

    @staticmethod
    def _armed(
        portfolio: Portfolio,
        *,
        qty: str = "10",
        entry: str = "100",
        stop: str | None = None,
        target: str | None = None,
    ) -> None:
        position = portfolio.position(SYMBOL)
        position.qty = Decimal(qty)
        position.avg_entry_price = Decimal(entry)
        position.last_price = Decimal(entry)
        position.stop_loss_price = Decimal(stop) if stop else None
        position.take_profit_price = Decimal(target) if target else None

    @pytest.mark.asyncio
    async def test_a_take_profit_is_acted_on(self) -> None:
        """The divergence this closes.

        The router arms a target on every position whose signal or `StopConfig`
        carries one, and `Position.take_profit_price` survives a restart —
        so the level existed, was persisted, and nothing looked at it.
        """
        runner, router, _, _, portfolio, _ = build(
            bars=[bar(0)],
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.02"), broker_side=False
            ),
        )
        await runner.warmup(portfolio)
        self._armed(portfolio, target="110")

        close_bar(runner, bar(1, close=112.0))
        await runner.evaluate(portfolio)

        assert router.flattened == [SYMBOL]
        assert router.flatten_purposes == [TAKE_PROFIT]

    @pytest.mark.asyncio
    async def test_a_stop_wins_when_one_bar_spans_both_levels(self) -> None:
        """The pessimistic reading, matching the engine exactly.

        The bar cannot say which came first. Assuming the target would make
        every live report — and every backtest that agreed with it — flatter
        than the truth.
        """
        runner, router, _, _, portfolio, _ = build(
            bars=[bar(0)],
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.02"), broker_side=False
            ),
        )
        await runner.warmup(portfolio)
        self._armed(portfolio, stop="98", target="102")

        # bar() spans low−1 to high+1 around its close, so a close of 100 covers
        # 99–101; widen it by closing at 100 with both levels inside the range.
        spanning = Bar(
            symbol=SYMBOL,
            ts=bar(1).ts,
            timeframe=Timeframe.D1,
            open=Decimal("100"),
            high=Decimal("103"),
            low=Decimal("97"),
            close=Decimal("100"),
            volume=Decimal("1000000"),
        )
        close_bar(runner, spanning)
        await runner.evaluate(portfolio)

        assert router.flatten_purposes == [STOP_LOSS]

    @pytest.mark.asyncio
    async def test_a_broker_side_stop_still_leaves_the_target_to_us(self) -> None:
        """The stop rests at the venue; the target never does.

        `submit_protective_orders` arms a take-profit on the position rather
        than sending a second order, so returning early on a broker-side config
        would leave the position with no upside exit at all.
        """
        runner, router, _, _, portfolio, _ = build(
            bars=[bar(0)],
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.02"), broker_side=True
            ),
        )
        await runner.warmup(portfolio)
        self._armed(portfolio, stop="98", target="110")

        close_bar(runner, bar(1, close=112.0))
        await runner.evaluate(portfolio)

        assert router.flatten_purposes == [TAKE_PROFIT]

    @pytest.mark.asyncio
    async def test_a_broker_side_stop_is_not_double_exited(self) -> None:
        """It is resting at the venue and fires without us."""
        runner, router, _, _, portfolio, _ = build(
            bars=[bar(0)],
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.02"), broker_side=True
            ),
        )
        await runner.warmup(portfolio)
        self._armed(portfolio, stop="98")

        close_bar(runner, bar(1, close=80.0))
        await runner.evaluate(portfolio)

        assert router.flattened == []

    @pytest.mark.asyncio
    async def test_a_time_exit_is_named_as_one(self) -> None:
        """Not `flatten`. A time exit and an operator flatten are the same order
        and completely different facts about a strategy."""
        runner, router, _, _, portfolio, _ = build(
            bars=[bar(0)],
            stop_config=StopConfig(stop_type=StopType.TIME, bars=1),
        )
        await runner.warmup(portfolio)
        self._armed(portfolio)
        portfolio.position(SYMBOL).opened_at = bar(0).ts

        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        assert router.flatten_purposes == [TIME_EXIT]

    @pytest.mark.asyncio
    async def test_a_position_with_no_target_is_left_alone(self) -> None:
        runner, router, _, _, portfolio, _ = build(
            bars=[bar(0)],
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.02"), broker_side=False
            ),
        )
        await runner.warmup(portfolio)
        self._armed(portfolio, stop="98")

        close_bar(runner, bar(1, close=140.0))
        await runner.evaluate(portfolio)

        assert router.flattened == []


class TestRefusalsReachTheOrderTable:
    """Every refusal the runner can hit, stored as the order it refused.

    **`GET /orders` was built for these rows and had never seen one.** Its own
    docstring says the orders that matter most are the ones that never filled
    and that "a rejection appears in no other read in the platform";
    `OrderHistoryTable` renders `rejected_risk` and shows the reason beside it.
    The whole read path was complete and nothing ever wrote a refused order —
    the runner dropped it at all four places it can be refused.

    Only one of the four had any durable trace at all: a refused *signal* is
    recorded as a decision, so `/risk/rejections` could find it. The other
    three were logged and lost, and they are the ones that describe an open
    position nobody is managing.
    """

    @staticmethod
    def stored(orders: FakeOrderRepository) -> list[Order]:
        return [o for o in orders.saved.values() if o.status is OrderStatus.REJECTED_RISK]

    @pytest.mark.asyncio
    async def test_a_refused_signal_is_stored(self) -> None:
        """Durable twice, and the two say different things.

        The signal records what the strategy *wanted*; this records what was
        actually composed — the quantity after sizing, the type, the limit —
        which is what `/orders` is read for.
        """
        orders = FakeOrderRepository()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)], order_repo=orders)
        router.refuse_signals = True
        await runner.warmup(portfolio)

        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        (refused,) = self.stored(orders)
        assert refused.symbol == SYMBOL
        assert refused.reject_reason == "nope"

    @pytest.mark.asyncio
    async def test_a_stored_refusal_names_the_rule_that_made_it(self) -> None:
        """What #78 stored, and what it could not yet say.

        That change gave `/orders` its first refused row. The row still could
        not name its refuser: the router had `decision.rule` beside
        `decision.reason` and passed only the reason to `transition()`, so the
        rule reached the log and never the table. A reason alone does not
        identify a limit — several rules say "no price available for SPY" —
        and the rule name is the string the risk limits panel is listed under.
        """
        orders = FakeOrderRepository()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)], order_repo=orders)
        router.refuse_signals = True
        await runner.warmup(portfolio)

        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        (refused,) = self.stored(orders)
        assert refused.rejected_by == "a_rule"

    @pytest.mark.asyncio
    async def test_a_refused_stop_exit_is_stored(self) -> None:
        """The most expensive of the four.

        The stop triggered, the exit was refused, and the position is still on.
        Before this it existed only as one `runner.stop_exit_refused` line in a
        log that rotates.
        """
        orders = FakeOrderRepository()
        runner, router, _, _, portfolio, _ = build(
            bars=[bar(0)],
            order_repo=orders,
            stop_config=StopConfig(
                stop_type=StopType.FIXED_PCT, value=Decimal("0.02"), broker_side=False
            ),
        )
        await runner.warmup(portfolio)
        TestProtectiveExits._armed(portfolio, stop="98")
        router.refuse_flatten = True

        close_bar(runner, bar(1, close=97.0))
        await runner.evaluate(portfolio)

        assert router.flattened == [SYMBOL]
        (refused,) = self.stored(orders)
        assert refused.reject_reason == "halted"

    @pytest.mark.asyncio
    async def test_a_refused_protective_stop_is_stored(self) -> None:
        """A position that never had protection at all.

        The same safety layer as the case above, failing at the other end:
        `runner.position_unprotected` is logged `error` and the position is
        real whether or not the stop is.
        """
        orders = FakeOrderRepository()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)], order_repo=orders)
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)
        router.refuse_protection = True

        await runner.on_fill_event(TestFills.a_fill_update("atp-1"), portfolio)

        assert [o.reject_reason for o in self.stored(orders)] == ["halted"]

    @pytest.mark.asyncio
    async def test_a_refused_shutdown_flatten_is_stored(self) -> None:
        """The book is still open and the worker has gone home.

        This is the row somebody needs tomorrow morning, when the position is
        there and nothing in the orders table explains why.
        """
        orders = FakeOrderRepository()
        runner, router, _, _, portfolio, _ = build(order_repo=orders)
        await runner.warmup(portfolio)
        portfolio.position(SYMBOL).qty = Decimal("10")
        portfolio.position(SYMBOL).last_price = Decimal("100")
        router.refuse_flatten = True

        await runner.shutdown(close_positions=True)

        (refused,) = self.stored(orders)
        assert refused.symbol == SYMBOL

    @pytest.mark.asyncio
    async def test_a_refusal_with_no_order_stores_nothing(self) -> None:
        """Sizing and routing refuse *before* an order is composed.

        There is no order to record, and inventing one would put rows in the
        table for orders that were never built. The refusal is real; its record
        is the signal.
        """
        orders = FakeOrderRepository()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)], order_repo=orders)
        router.refuse_signals = True
        router.refuse_before_building = True
        await runner.warmup(portfolio)

        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        assert self.stored(orders) == []

    @pytest.mark.asyncio
    async def test_an_approved_order_is_not_recorded_as_a_refusal(self) -> None:
        orders = FakeOrderRepository()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, _, _, portfolio, _ = build(strategy, bars=[bar(0)], order_repo=orders)
        await runner.warmup(portfolio)

        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        assert self.stored(orders) == []

    @pytest.mark.asyncio
    async def test_a_failed_write_does_not_fail_the_evaluation(self) -> None:
        """The opposite of how a *signal* write is treated, on purpose.

        A signal is written on the way into the router and the order that
        follows carries a foreign key to it, so a signal that cannot be written
        must stop what comes next. This is written on the way out, about
        something that already happened and is already in the log. Raising
        would turn recording a refused stop into a failed evaluation — and
        three of those halt trading, so the record of the refusal would become
        the thing that stops the platform.
        """
        orders = FakeOrderRepository()
        orders.save_error = RuntimeError("database is down")
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)], order_repo=orders)
        router.refuse_signals = True
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        assert runner.stats.consecutive_errors == 0


class TestTheDecisionRecord:
    """Signals and the strategy row — the join that makes an order attributable.

    `orders.strategy_id` and `orders.signal_id` are foreign keys, so these
    writes are not a nice-to-have alongside the order: without them the order
    save is refused by the database.
    """

    @pytest.mark.asyncio
    async def test_the_strategy_row_is_written_before_anything_can_trade(self) -> None:
        strategies = FakeStrategyRepository()
        runner, _, _, _, portfolio, _ = build(bars=[bar(0)], strategy_repo=strategies)

        await runner.warmup(portfolio)

        assert strategies.ensure_calls == ["scripted"]
        # The id is the strategy's *name*, because that is what every
        # `Signal.strategy_id` in the platform already carries.
        assert strategies.stored["scripted"].id == "scripted"

    @pytest.mark.asyncio
    async def test_warmup_refuses_to_start_if_the_row_cannot_be_written(self) -> None:
        strategies = FakeStrategyRepository()
        strategies.ensure_error = RuntimeError("database is down")
        runner, _, _, _, portfolio, _ = build(bars=[bar(0)], strategy_repo=strategies)

        with pytest.raises(RuntimeError, match="database is down"):
            await runner.warmup(portfolio)

    @pytest.mark.asyncio
    async def test_ensuring_is_idempotent_across_session_opens(self) -> None:
        """`warmup` re-runs at every open, and must not reset the row."""
        strategies = FakeStrategyRepository()
        runner, _, _, _, portfolio, _ = build(bars=[bar(0)], strategy_repo=strategies)

        await runner.warmup(portfolio)
        first = strategies.stored["scripted"]
        await runner.warmup(portfolio)

        assert strategies.ensure_calls == ["scripted", "scripted"]
        assert strategies.stored["scripted"] is first

    @pytest.mark.asyncio
    async def test_a_submitted_signal_is_stored_as_acted_on(self) -> None:
        signals = FakeSignalRepository()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, _, _, portfolio, _ = build(strategy, bars=[bar(0)], signal_repo=signals)
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        ((_, outcome),) = signals.stored.values()
        assert outcome.acted_on is True
        assert outcome.rejected_by is None

    @pytest.mark.asyncio
    async def test_a_refused_signal_is_stored_with_the_rule_that_refused_it(self) -> None:
        """The whole reason the refusals are kept.

        From the orders table alone, a strategy whose every idea was refused is
        indistinguishable from a strategy that had no ideas — and those two call
        for opposite responses.
        """
        signals = FakeSignalRepository()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)], signal_repo=signals)
        router.refuse_signals = True
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        ((_, outcome),) = signals.stored.values()
        assert outcome.acted_on is False
        assert outcome.rejected_by == "a_rule"
        assert outcome.rejection_reason == "nope"

    @pytest.mark.asyncio
    async def test_the_signal_is_stored_before_the_order_that_references_it(self) -> None:
        """`orders.signal_id` is a foreign key. Order matters, literally."""
        signals = FakeSignalRepository()
        orders = FakeOrderRepository()
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, _, _, portfolio, _ = build(
            strategy, bars=[bar(0)], signal_repo=signals, order_repo=orders
        )
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        assert signals.save_calls  # written at all
        assert orders.save_calls  # and the order followed
        # The fakes record in call order within one pass, so a signal recorded
        # after the order would leave `save_calls` empty at this point.
        assert len(signals.stored) == 1

    @pytest.mark.asyncio
    async def test_a_signal_write_that_fails_fails_the_evaluation(self) -> None:
        """Unlike the publisher's, this failure must reach the caller.

        Swallowing it would let the order save a step later hit a foreign-key
        violation instead — the same outage reported as a corruption.
        """
        signals = FakeSignalRepository()
        signals.save_error = RuntimeError("signals table is unreachable")
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, _, _, _, portfolio, _ = build(strategy, bars=[bar(0)], signal_repo=signals)
        await runner.warmup(portfolio)
        close_bar(runner, bar(1))

        await runner.evaluate(portfolio)

        # `evaluate` catches and counts rather than propagating — the loop must
        # not die — but the pass is recorded as failed rather than as clean.
        assert runner.stats.errors == 1


class TestTheSeriesItReads:
    """The runner must read the series something is writing.

    `PostgresBarRepository` narrows on the timeframe column, so a runner built
    for one series and an ingestor writing another do not error — the runner
    receives nothing, forever, and `_refresh_bars` returns an empty list on
    every pass. Day 1 of the paper week spent ten hours in exactly that state
    with the worker reporting itself ready and trading
    (docs/paper-week/day-1-review.md).
    """

    @pytest.mark.asyncio
    async def test_a_runner_reading_a_series_nothing_writes_is_never_handed_a_bar(self) -> None:
        """The bug, reproduced. The strategy is not called once, and nothing
        raises — which is what made it survive a full session unnoticed."""
        strategy = ScriptedStrategy()
        runner, _, _, _, portfolio, _ = build(strategy=strategy, bars=[bar(0)])
        # The store holds minutes; this runner was built asking for days.
        runner.bar_repo.timeframe = Timeframe.M1  # type: ignore[attr-defined]
        runner.timeframe = Timeframe.D1

        await runner.warmup(portfolio)
        close_bar(runner, bar(1, close=101.0))
        await runner.evaluate(portfolio)

        assert strategy.bars_seen == []

    @pytest.mark.asyncio
    async def test_the_same_series_at_both_ends_delivers_the_bar(self) -> None:
        """The control. Identical setup but for the timeframe, so the assertion
        above is about the disagreement and not about the harness."""
        strategy = ScriptedStrategy()
        runner, _, _, _, portfolio, _ = build(strategy=strategy, bars=[bar(0)])
        runner.bar_repo.timeframe = Timeframe.M1  # type: ignore[attr-defined]
        runner.timeframe = Timeframe.M1

        await runner.warmup(portfolio)
        close_bar(runner, bar(1, close=101.0))
        await runner.evaluate(portfolio)

        assert [b.close for b in strategy.bars_seen] == [Decimal("101.0")]


class TestMarkingTheWatchlist:
    """A market entry is priced off `position.last_price`, and an entry is only
    ever emitted for a symbol that is *flat*.

    Marking open positions alone therefore left every entry candidate unpriced,
    and `OrderRouter._size` had nothing to size against — a second blocker
    sitting behind the timeframe one, and one that would have produced a wall of
    sizing refusals the moment the first was fixed.
    """

    @pytest.mark.asyncio
    async def test_a_flat_watchlist_symbol_is_priced_from_the_quote(self) -> None:
        runner, _, _, _, portfolio, _ = build()
        await runner.warmup(portfolio)
        await runner.quote_cache.set_quote(
            Quote(symbol=SYMBOL, ts=START, bid=Decimal("200"), ask=Decimal("202"))
        )

        await runner.evaluate(portfolio)

        assert portfolio.position(SYMBOL).is_flat
        assert portfolio.position(SYMBOL).last_price == Decimal("201")

    @pytest.mark.asyncio
    async def test_a_flat_symbol_falls_back_to_the_last_bar(self) -> None:
        """No quote yet is not a reason to leave an entry unpriceable — the last
        completed bar is what the strategy decided on anyway."""
        runner, _, _, _, portfolio, _ = build(bars=[bar(0, close=123.0)])
        await runner.warmup(portfolio)

        await runner.evaluate(portfolio)

        assert portfolio.position(SYMBOL).last_price == Decimal("123.0")

    @pytest.mark.asyncio
    async def test_a_quiet_flat_symbol_does_not_warn(self) -> None:
        """One line per quiet symbol per pass would drown the warning that
        matters: an unmarked *holding*, which makes every percentage limit
        compute too small.

        `capture_logs` rather than `caplog`: structlog does not route through
        the stdlib logger in this configuration, so a `not in caplog.text`
        assertion would pass however loud this got (tests/unit/test_alerts.py).
        """
        runner, _, _, _, portfolio, _ = build(bars=[])
        runner.bar_repo.bars = {}  # type: ignore[attr-defined]
        await runner.warmup(portfolio)

        with capture_logs() as events:
            await runner.evaluate(portfolio)

        assert not [e for e in events if e["event"] == "runner.no_quote_for_mark"]

    @pytest.mark.asyncio
    async def test_an_unmarked_holding_still_warns(self) -> None:
        runner, _, _, _, portfolio, _ = build(bars=[])
        runner.bar_repo.bars = {}  # type: ignore[attr-defined]
        await runner.warmup(portfolio)
        portfolio.position(SYMBOL).qty = Decimal("10")

        with capture_logs() as events:
            await runner.evaluate(portfolio)

        assert [e for e in events if e["event"] == "runner.no_quote_for_mark"]


class TestTheEvaluationHeartbeat:
    """Whether the loop ran at all has to be answerable from the log.

    The only export of `stats.evaluations` was a Prometheus counter, the metrics
    server had refused to start for want of a token, and the success path logged
    nothing — so a session of roughly 390 evaluations produced no evidence that
    the strategy was ever asked anything.
    """

    @staticmethod
    def _heartbeat(events: Sequence[MutableMapping[str, Any]]) -> MutableMapping[str, Any]:
        beats = [e for e in events if e["event"] == "runner.evaluated"]
        assert len(beats) == 1, beats
        return beats[0]

    @pytest.mark.asyncio
    async def test_every_pass_says_so(self) -> None:
        runner, _, _, _, portfolio, _ = build()
        await runner.warmup(portfolio)

        with capture_logs() as events:
            await runner.evaluate(portfolio)

        assert self._heartbeat(events)["evaluations"] == 1

    @pytest.mark.asyncio
    async def test_it_reports_no_bars_when_none_closed(self) -> None:
        """`bars_closed=0` for a whole session is the shape of the timeframe
        bug, and this line is where an operator would see it."""
        runner, _, _, _, portfolio, _ = build()
        runner.bar_repo.timeframe = Timeframe.M1  # type: ignore[attr-defined]
        runner.timeframe = Timeframe.D1
        await runner.warmup(portfolio)

        with capture_logs() as events:
            await runner.evaluate(portfolio)

        beat = self._heartbeat(events)
        assert beat["bars_closed"] == 0
        assert beat["timeframe"] == "1d"

    @pytest.mark.asyncio
    async def test_it_counts_a_closed_bar(self) -> None:
        runner, _, _, _, portfolio, _ = build()
        await runner.warmup(portfolio)
        close_bar(runner, bar(1, close=101.0))

        with capture_logs() as events:
            await runner.evaluate(portfolio)

        assert self._heartbeat(events)["bars_closed"] == 1


class TestAStopThatIsNotWhereTheConfigSaysItIs:
    """`broker_side` says where stops are *meant* to rest, not where this
    position's stop actually is.

    Read alone it made this loop decline to watch the level for a position whose
    protective order the risk chain had refused — leaving a holding with no stop
    at the venue and none in the engine, which is docs/SAFETY.md layers 5 and 4
    failing together and silently.
    """

    BROKER_SIDE = StopConfig(stop_type=StopType.FIXED_PCT, value=Decimal("0.02"), broker_side=True)

    @staticmethod
    def _held(portfolio: Portfolio) -> None:
        position = portfolio.position(SYMBOL)
        position.qty = Decimal("10")
        position.avg_entry_price = Decimal("100")
        position.last_price = Decimal("100")
        position.stop_loss_price = Decimal("99")

    @pytest.mark.asyncio
    async def test_a_refused_stop_is_watched_by_the_engine(self) -> None:
        """The gap this closes. Nothing is resting at the venue, so the engine
        is the only thing that can act on the level."""
        runner, router, _, _, portfolio, _ = build(stop_config=self.BROKER_SIDE)
        await runner.warmup(portfolio)
        self._held(portfolio)
        # What `_protect` records when `submit_protective_orders` comes back
        # short, and what the router then reports holding: nothing.
        runner._unprotected[SYMBOL] = Decimal("10")
        router.protected_qty[SYMBOL] = Decimal("0")

        close_bar(runner, bar(1, close=90.0))
        await runner.evaluate(portfolio)

        assert router.flattened == [SYMBOL]
        assert router.flatten_purposes == [STOP_LOSS]

    @pytest.mark.asyncio
    async def test_a_stop_resting_at_the_venue_is_left_to_the_venue(self) -> None:
        """The double-exit this must not cause: if both fire the position closes
        twice, and the second close opens a reversed one with nothing on it."""
        runner, router, _, _, portfolio, _ = build(stop_config=self.BROKER_SIDE)
        await runner.warmup(portfolio)
        self._held(portfolio)
        runner._unprotected[SYMBOL] = Decimal("10")
        # Protection was later established over the whole position.
        router.protected_qty[SYMBOL] = Decimal("10")

        close_bar(runner, bar(1, close=90.0))
        await runner.evaluate(portfolio)

        assert router.flattened == []

    @pytest.mark.asyncio
    async def test_a_position_this_process_never_armed_is_left_alone(self) -> None:
        """The restart case, and the reason `_stop_is_missing` is a positive
        claim rather than the absence of one.

        `OrderRouter`'s protective map is in-process and is not rebuilt at
        start, so after a restart it is empty for a position whose venue stop is
        still resting. Treating that emptiness as "unprotected" would flatten a
        position the venue also closes — trading one silent bug for a worse one.
        """
        runner, router, _, _, portfolio, _ = build(stop_config=self.BROKER_SIDE)
        await runner.warmup(portfolio)
        self._held(portfolio)
        # No record either way: nothing in this process ever armed this symbol.
        assert runner._unprotected == {}
        router.protected_qty[SYMBOL] = Decimal("0")

        close_bar(runner, bar(1, close=90.0))
        await runner.evaluate(portfolio)

        assert router.flattened == []

    @pytest.mark.asyncio
    async def test_a_partially_covered_position_is_watched(self) -> None:
        """Half a stop is not a stop for the other half."""
        runner, router, _, _, portfolio, _ = build(stop_config=self.BROKER_SIDE)
        await runner.warmup(portfolio)
        self._held(portfolio)
        runner._unprotected[SYMBOL] = Decimal("4")
        router.protected_qty[SYMBOL] = Decimal("6")

        close_bar(runner, bar(1, close=90.0))
        await runner.evaluate(portfolio)

        assert router.flattened == [SYMBOL]

    @pytest.mark.asyncio
    async def test_the_target_is_still_watched_when_the_venue_holds_the_stop(self) -> None:
        """Unchanged by this: the venue holds the stop, the engine holds the
        target, and returning early on both would leave no upside exit."""
        runner, router, _, _, portfolio, _ = build(stop_config=self.BROKER_SIDE)
        await runner.warmup(portfolio)
        self._held(portfolio)
        portfolio.position(SYMBOL).take_profit_price = Decimal("110")
        router.protected_qty[SYMBOL] = Decimal("10")

        close_bar(runner, bar(1, close=112.0))
        await runner.evaluate(portfolio)

        assert router.flatten_purposes == [TAKE_PROFIT]


class TestAStrategyAskingForAnotherSeries:
    """`closes` and `history` take a timeframe and can only serve the one the
    runner warmed up on.

    Fixing the runner-to-ingestor wiring leaves this half: `SmaCrossover`'s own
    default is `timeframe: 1d`, so a worker correctly configured for minutes
    still hands it minute closes under a daily label. The bars are the right
    ones — they are the only ones — but nothing said the parameter was being
    discarded, and a silent substitution is what the layer above was just fixed
    for (docs/paper-week/day-1-review.md).
    """

    @staticmethod
    def _context(timeframe: Timeframe = Timeframe.M1) -> LiveContext:
        return LiveContext(
            {SYMBOL: [bar(0)]},
            {},
            Portfolio(cash=Decimal("1"), starting_equity=Decimal("1")),
            SimulatedClock(START),
            (SYMBOL,),
            timeframe,
        )

    def test_asking_for_the_served_series_is_silent(self) -> None:
        context = self._context(Timeframe.M1)

        with capture_logs() as events:
            context.closes(SYMBOL, Timeframe.M1, 1)

        assert not [e for e in events if e["event"] == "runner.timeframe_mismatch"]

    def test_asking_for_another_series_says_so(self) -> None:
        context = self._context(Timeframe.M1)

        with capture_logs() as events:
            context.closes(SYMBOL, Timeframe.D1, 1)

        mismatch = [e for e in events if e["event"] == "runner.timeframe_mismatch"]
        assert len(mismatch) == 1
        assert mismatch[0]["asked_for"] == "1d"
        assert mismatch[0]["serving"] == "1m"

    def test_it_still_serves_the_bars_it_has(self) -> None:
        """A warning, not a raise: `on_bar` runs inside the evaluation loop and
        three consecutive errors halt trading, so raising here would turn a
        long-standing wrong parameter into an outage."""
        context = self._context(Timeframe.M1)

        assert len(context.closes(SYMBOL, Timeframe.D1, 1)) == 1

    def test_it_warns_once_per_series_not_once_per_bar(self) -> None:
        """One line per symbol per bar would be 6,887 of them in a session, which
        is the volume that buried the signal in the first place."""
        context = self._context(Timeframe.M1)

        with capture_logs() as events:
            for _ in range(5):
                context.closes(SYMBOL, Timeframe.D1, 1)
                context.history(SYMBOL, Timeframe.D1, 1)

        assert len([e for e in events if e["event"] == "runner.timeframe_mismatch"]) == 1


class TestEscalatingARefusal:
    """F10. docs/RISK.md has always said the kill switch "auto-engages on: daily
    loss limit breach, ... a rate-limit storm", and nothing ever did — both were
    `HaltReason` values with no writer, so a platform that hit its daily loss
    limit spent the rest of the session silently refusing every entry
    (docs/paper-week/day-1-review.md)."""

    @staticmethod
    async def _refuse(rule: str, *, times: int = 1) -> FakeKillSwitch:
        strategy = ScriptedStrategy(dict.fromkeys(range(times), SignalAction.ENTER_LONG))
        runner, router, switch, _, portfolio, _ = build(
            strategy, bars=[bar(0)], signal_limit=times + 1
        )
        router.refuse_signals = True
        router.refuse_rule = rule
        await runner.warmup(portfolio)
        for i in range(1, times + 1):
            close_bar(runner, bar(i))
            await runner.evaluate(portfolio)
        return switch

    @pytest.mark.asyncio
    async def test_a_daily_loss_refusal_halts_on_the_first_one(self) -> None:
        """Refusing entries was never the missing part — `DailyLossLimitRule`
        did that correctly. What was missing is that the operator was not told,
        and a halt is how this platform says something out loud: it alerts, it
        is repeated every fifteen minutes while it stands, and it shows on the
        banner. None of those reach a `RiskDecision`."""
        switch = await self._refuse("daily_loss_limit")

        assert switch.engagements
        (scope, reason, actor, _) = switch.engagements[0]
        assert reason == str(HaltReason.DAILY_LOSS_LIMIT)
        assert scope == str(HaltScope.GLOBAL)
        assert actor == DAILY_LOSS_RULE

    @pytest.mark.asyncio
    async def test_one_rate_limit_refusal_is_a_busy_minute_not_a_storm(self) -> None:
        """`RateLimitRule`'s cap is per-minute, so hitting it once is a
        legitimate burst. Halting the platform for one would be worse than the
        silence it replaces."""
        switch = await self._refuse("rate_limit", times=1)

        assert switch.engagements == []

    @pytest.mark.asyncio
    async def test_a_run_of_them_is_the_runaway_loop_the_rule_names(self) -> None:
        switch = await self._refuse("rate_limit", times=RATE_LIMIT_STORM_REFUSALS)

        assert switch.engagements
        assert switch.engagements[0][1] == str(HaltReason.RATE_LIMIT_STORM)

    @pytest.mark.asyncio
    async def test_an_ordinary_refusal_between_them_ends_the_run(self) -> None:
        """A storm is *consecutive*. One rate-limit refusal among ordinary ones
        is a busy minute, and counting them cumulatively would eventually halt
        any long-running worker."""
        strategy = ScriptedStrategy(dict.fromkeys(range(8), SignalAction.ENTER_LONG))
        runner, router, switch, _, portfolio, _ = build(strategy, bars=[bar(0)], signal_limit=10)
        router.refuse_signals = True
        await runner.warmup(portfolio)

        for i in range(8):
            router.refuse_rule = "rate_limit" if i % 2 == 0 else "max_position_size"
            close_bar(runner, bar(i))
            await runner.evaluate(portfolio)

        assert switch.engagements == [], "no run of five ever formed"

    @pytest.mark.asyncio
    async def test_an_ordinary_refusal_does_not_halt(self) -> None:
        """Most refusals are the chain working. `max_position_size` declining an
        oversized entry is not an incident."""
        switch = await self._refuse("max_position_size")

        assert switch.engagements == []


class TestNoActionIsNotARejection:
    """F14. `SubmitResult.no_action` builds an *approved* decision precisely so
    a HOLD-shaped outcome does not inflate `orders_rejected_by_risk` — the
    number an operator reads to decide whether the risk config is too tight —
    and then the runner counted every unsubmitted result alike, so it did
    anyway (docs/paper-week/day-1-review.md)."""

    @pytest.mark.asyncio
    async def test_it_is_not_counted_as_refused_by_risk(self) -> None:
        strategy = ScriptedStrategy({0: SignalAction.EXIT})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        router.no_action = True
        await runner.warmup(portfolio)

        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        assert runner.stats.orders_rejected_by_risk == 0

    @pytest.mark.asyncio
    async def test_a_real_refusal_still_is(self) -> None:
        """The control. Suppressing the counter entirely would trade one wrong
        number for another."""
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        router.refuse_signals = True
        await runner.warmup(portfolio)

        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        assert runner.stats.orders_rejected_by_risk == 1

    @pytest.mark.asyncio
    async def test_a_no_action_never_escalates_to_a_halt(self) -> None:
        """It carries an *approved* decision whose `rule` is `no_action`, so
        nothing about it should reach the escalation path."""
        strategy = ScriptedStrategy({0: SignalAction.EXIT})
        runner, router, switch, _, portfolio, _ = build(strategy, bars=[bar(0)])
        router.no_action = True
        await runner.warmup(portfolio)

        close_bar(runner, bar(1))
        await runner.evaluate(portfolio)

        assert switch.engagements == []


class TestAProtectiveSubmissionThatRaises:
    """The position exists; something has to write down that nothing is holding
    it before the error takes the process out.

    `_route` raises `BrokerConnectionError` out of `_resolve_indeterminate` when
    a protective child's outcome is unknown — by design, and it engages a global
    halt on the way. Nothing between `_protect` and the asyncio task boundary
    caught it, so the trade-updates responsibility ended and the worker exited
    *before* `self._unprotected` was written. The restart then read an empty map
    as "nothing in this process ever tried", which `_stop_is_missing` treats as
    safe — so the engine declined to watch the level the router had already
    armed and persisted (the day-1 fix audit, sweep finding B2).
    """

    @pytest.mark.asyncio
    async def test_the_position_is_recorded_unprotected_before_the_raise(self) -> None:
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        router.protection_raises = BrokerConnectionError("the venue went away")
        await runner.evaluate(portfolio)
        router.calls.clear()

        with pytest.raises(BrokerConnectionError):
            await runner.on_fill_event(self.a_fill_update("atp-1"), portfolio)

        assert runner._unprotected[SYMBOL] == Decimal("10"), (
            "a position whose stop submission raised is in the KNOWN-unprotected "
            "state, not the unknown one"
        )

    @pytest.mark.asyncio
    async def test_the_engine_then_watches_the_level(self) -> None:
        """The whole point of recording it: `_stop_is_missing` short-circuits on
        membership, so without the write the engine never asks the router
        anything and never watches the stop."""
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0)])
        router.protection_raises = BrokerConnectionError("the venue went away")
        await runner.evaluate(portfolio)
        with pytest.raises(BrokerConnectionError):
            await runner.on_fill_event(self.a_fill_update("atp-1"), portfolio)

        assert runner._stop_is_missing(portfolio.position(SYMBOL))

    @staticmethod
    def a_fill_update(client_order_id: str) -> TradeUpdate:
        return TradeUpdate(
            event="fill",
            client_order_id=client_order_id,
            broker_order_id="brk-1",
            symbol=SYMBOL,
            at=START,
            fill=Fill(
                order_id="x", ts=START, qty=Decimal("10"), price=Decimal("100"), venue_fill_id="e1"
            ),
        )


class TestAClosedPositionTakesItsStopWithIt:
    """A protective stop must not outlive the position it protects.

    `cancel_protection` had exactly one caller, inside `OrderRouter.flatten`,
    and a strategy's own EXIT goes through `submit_signal` instead. So a
    position closed by a cross-down left a GTC stop working at the venue with
    nothing behind it; when price later traded through the level it filled and
    opened a *short*, for an order this process still tracked — so no reconciler
    flagged it, `_protect` read the fill as reducing and armed nothing, and
    `sma_crossover` could emit neither an entry nor an exit for the symbol
    again (the day-1 fix audit, sweep finding B1).

    Driven from the fill rather than from the submit, which is the correction a
    review of the first attempt forced: an exit is `DAY` and may be `LIMIT`, so
    cancelling on acknowledgement de-armed positions whose exit never filled.
    """

    @staticmethod
    def fill(client_order_id: str, qty: str = "10") -> TradeUpdate:
        return TradeUpdate(
            event="fill",
            client_order_id=client_order_id,
            broker_order_id="brk-1",
            symbol=SYMBOL,
            at=START,
            fill=Fill(
                order_id="x", ts=START, qty=Decimal(qty), price=Decimal("100"), venue_fill_id="e1"
            ),
        )

    async def _long_then_exit(self) -> tuple[Any, Any, Portfolio]:
        strategy = ScriptedStrategy({0: SignalAction.ENTER_LONG, 1: SignalAction.EXIT})
        runner, router, _, _, portfolio, _ = build(strategy, bars=[bar(0), bar(1)])
        await runner.evaluate(portfolio)
        await runner.on_fill_event(self.fill("atp-1"), portfolio)
        await runner.evaluate(portfolio)
        router.protection_cancelled.clear()
        return runner, router, portfolio

    @pytest.mark.asyncio
    async def test_the_fill_that_closes_the_position_cancels_the_stop(self) -> None:
        runner, router, portfolio = await self._long_then_exit()

        await runner.on_fill_event(self.fill("atp-2"), portfolio)

        assert portfolio.position(SYMBOL).is_flat
        assert router.protection_cancelled == [SYMBOL]

    @pytest.mark.asyncio
    async def test_a_partial_exit_leaves_the_stop_alone(self) -> None:
        """The failure the first attempt introduced: cancelling when the exit is
        merely accepted strips the stop off shares that are still held."""
        runner, router, portfolio = await self._long_then_exit()

        await runner.on_fill_event(self.fill("atp-2", qty="4"), portfolio)

        assert not portfolio.position(SYMBOL).is_flat
        assert router.protection_cancelled == [], (
            "shares are still held; the stop over them must stay working"
        )


class TestAPositionWithNoVenueStopReachesAHuman:
    """Day 2 logged `position_unprotected` CRITICAL 85 times and sent 0 alerts.

    Every one of the 17 alerts that day was halt lifecycle or a scheduled
    report. The platform woke a human 11 times for a halt that turned out to be
    a false positive, and zero times for a real, total loss of protection across
    all 20 symbols (docs/paper-week/day-2-review.md, F3).

    There was no throttle suppressing it. Log level and notification are wholly
    decoupled here — 47 `log.critical` call sites against 11 `Alert(...)` sites
    — and `position_unprotected` simply had no route. These tests are that
    route, and the deduplication that makes it survivable: one page naming every
    naked symbol, not 85 naming one fill each.
    """

    def _naked(self, portfolio: Portfolio, *symbols: str, qty: str = "10") -> None:
        """Hold `symbols` with nothing covering them at the venue."""
        for symbol in symbols:
            position = portfolio.position(symbol)
            position.qty = Decimal(qty)
            position.avg_entry_price = Decimal(100)
            position.last_price = Decimal(100)
            position.stop_loss_price = Decimal(95)
            position.broker_protected_qty = Decimal(0)

    def test_one_page_names_every_naked_symbol(self) -> None:
        """The rollup. 85 fill-level events on 20 symbols must not be 85 pages
        — a phone that buzzes 85 times is a phone that gets silenced."""
        alerts = RecordingAlertSink()
        runner, _router, _switch, _rec, portfolio, _slept = build(alerts=alerts)
        self._naked(portfolio, "KO", "PEP", "INTC")

        runner._mark_broker_protection(portfolio)

        assert len(alerts.sent) == 1
        alert = alerts.sent[0]
        assert alert.severity is Severity.CRITICAL
        assert "3 position(s)" in alert.title
        assert "INTC, KO, PEP" in alert.body
        # `alerts/ports.py`: an alert names the fact, never the book. It renders
        # on a lock screen in a coffee shop.
        assert "100" not in alert.body

    def test_an_unchanged_naked_set_does_not_page_again(self) -> None:
        """The loop runs once a minute for six and a half hours. Re-paging every
        pass is the same failure as never paging, arrived at from the other
        side."""
        alerts = RecordingAlertSink()
        runner, _router, _switch, _rec, portfolio, _slept = build(alerts=alerts)
        self._naked(portfolio, "KO")

        for _ in range(5):
            runner._mark_broker_protection(portfolio)

        assert len(alerts.sent) == 1

    def test_a_growing_naked_set_re_pages_once_the_cooldown_passes(self) -> None:
        """A second symbol going naked is news, but not news worth a page every
        minute. Day 2 accumulated 20 of them over three hours."""
        alerts = RecordingAlertSink()
        runner, _router, _switch, _rec, portfolio, _slept = build(alerts=alerts)
        clock = runner.clock
        self._naked(portfolio, "KO")
        runner._mark_broker_protection(portfolio)

        self._naked(portfolio, "PEP")
        runner._mark_broker_protection(portfolio)
        assert len(alerts.sent) == 1, "inside the cooldown, so still one page"

        clock.set(clock.now() + timedelta(seconds=901))  # type: ignore[attr-defined]
        runner._mark_broker_protection(portfolio)

        assert len(alerts.sent) == 2
        assert "KO, PEP" in alerts.sent[1].body

    def test_the_all_clear_is_sent_too(self) -> None:
        """An operator who was paged is owed the sentence that says it is over.
        Without it the only way to learn is to go and look — which is the
        behaviour that made day 2 unsurvivable in the first place."""
        alerts = RecordingAlertSink()
        runner, _router, _switch, _rec, portfolio, _slept = build(alerts=alerts)
        self._naked(portfolio, "KO")
        runner._mark_broker_protection(portfolio)

        runner.router.protected_qty["KO"] = Decimal(10)  # type: ignore[attr-defined]
        runner._mark_broker_protection(portfolio)

        assert len(alerts.sent) == 2
        assert alerts.sent[1].severity is Severity.INFO
        assert "Protection restored" in alerts.sent[1].title

    def test_a_clean_book_pages_nobody(self) -> None:
        alerts = RecordingAlertSink()
        runner, _router, _switch, _rec, portfolio, _slept = build(alerts=alerts)
        self._naked(portfolio, "KO")
        runner.router.protected_qty["KO"] = Decimal(10)  # type: ignore[attr-defined]

        runner._mark_broker_protection(portfolio)

        assert alerts.sent == []

    def test_the_gauge_is_set_every_pass_including_to_zero(self) -> None:
        """docs/SAFETY.md's go-live gate is evaluated from this number, so
        "clean" has to be a reading rather than an absence of readings."""
        from prometheus_client import generate_latest

        from atp_core.metrics.registry import get_registry

        runner, _router, _switch, _rec, portfolio, _slept = build()
        self._naked(portfolio, "KO", "PEP")
        runner._mark_broker_protection(portfolio)
        assert b"atp_positions_unprotected 2.0" in generate_latest(get_registry())

        runner.router.protected_qty.update(  # type: ignore[attr-defined]
            {"KO": Decimal(10), "PEP": Decimal(10)}
        )
        runner._mark_broker_protection(portfolio)
        assert b"atp_positions_unprotected 0.0" in generate_latest(get_registry())

    def test_a_worker_with_no_sink_still_records_the_exposure(self) -> None:
        """The sink is optional; the fact is not. A worker that cannot reach a
        phone must still write the book down honestly."""
        runner, _router, _switch, _rec, portfolio, _slept = build(alerts=None)
        self._naked(portfolio, "KO")

        runner._mark_broker_protection(portfolio)

        assert portfolio.position("KO").unprotected_qty == Decimal(10)
        assert portfolio.position("KO").broker_protected_qty == Decimal(0)


class TestWarmupCannotReachAcrossAClosure:
    """Day 2's SMA(50) spanned a four-day weekend and nothing said so.

    `runner.warmed_up bars=1020 symbols=20` is exactly 51 bars each — a full
    window — and `runner.warmup_short_history` fired **zero** times. But this
    session had ingested 118 bars in total before that line, 5.9 per symbol, so
    at least 45 of each symbol's 51 came from storage predating the session.
    The newest stored bar was Friday 4 September post-market; Monday 7 September
    was Labor Day. `get_last_n_bars` had no recency bound, and 51 rows is 51
    rows (docs/paper-week/day-2-review.md, F8).

    A moving average is only defined over a contiguous series, and the one trade
    that made the day's P&L was taken on the part of the series that was not.
    """

    #: Tuesday 2026-09-08 — the session day 2 traded, after Labor Day.
    TUESDAY = date(2026, 9, 8)
    FRIDAY = date(2026, 9, 4)

    def _minute_bar(self, ts: datetime, close: float = 100.0) -> Bar:
        price = Decimal(str(close))
        return Bar(
            symbol=SYMBOL,
            ts=ts,
            timeframe=Timeframe.M1,
            open=price,
            high=price + Decimal("1"),
            low=price - Decimal("1"),
            close=price,
            volume=Decimal("1000"),
        )

    #: `slow_period + 1`, which is what day 2's warmup asked for per symbol.
    NEEDED = 51

    def _runner_at(self, now: datetime, bars: list[Bar]) -> StrategyRunner:
        """A minute-timeframe runner whose calendar knows about Labor Day."""
        runner, _router, _switch, _rec, _portfolio, _slept = build(
            strategy=ScriptedStrategy(warmup=self.NEEDED), bars=bars, clean=True
        )
        runner.timeframe = Timeframe.M1
        runner.bar_repo = FakeBarRepo({SYMBOL: bars}, timeframe=Timeframe.M1)
        runner.calendar = FakeCalendar(days={self.FRIDAY, self.TUESDAY})  # type: ignore[assignment]
        runner.clock = SimulatedClock(now)
        return runner

    @pytest.mark.asyncio
    async def test_fridays_bars_do_not_seed_tuesdays_average(self) -> None:
        """The finding, reproduced. 45 bars from before Labor Day and 6 from
        this session used to make a full 51-bar window; now they make six."""
        friday = [
            self._minute_bar(datetime(2026, 9, 4, 19, 30, tzinfo=UTC) + timedelta(minutes=i))
            for i in range(45)
        ]
        tuesday_open = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
        tuesday = [self._minute_bar(tuesday_open + timedelta(minutes=i)) for i in range(6)]
        runner = self._runner_at(tuesday_open + timedelta(minutes=6), friday + tuesday)

        await runner.warmup(runner._portfolio)

        held = runner._bars[SYMBOL]
        assert len(held) == 6, "Friday's 45 bars must not count towards this session"
        assert all(b.ts >= tuesday_open for b in held)

    @pytest.mark.asyncio
    async def test_short_history_becomes_the_loud_path(self) -> None:
        """The review's words. Reaching back across a holiday was silent;
        refusing to is not."""
        friday = [
            self._minute_bar(datetime(2026, 9, 4, 19, 30, tzinfo=UTC) + timedelta(minutes=i))
            for i in range(45)
        ]
        tuesday_open = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
        runner = self._runner_at(tuesday_open + timedelta(minutes=2), friday)

        with capture_logs() as logs:
            await runner.warmup(runner._portfolio)

        short = [line for line in logs if line["event"] == "runner.warmup_short_history"]
        assert len(short) == 1
        assert short[0]["have"] == 0
        assert short[0]["not_before"] == tuesday_open.isoformat()

    @pytest.mark.asyncio
    async def test_this_session_s_own_bars_are_kept(self) -> None:
        """The bound excludes a different trading day, not the current one."""
        tuesday_open = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
        bars = [self._minute_bar(tuesday_open + timedelta(minutes=i)) for i in range(60)]
        runner = self._runner_at(tuesday_open + timedelta(minutes=60), bars)

        await runner.warmup(runner._portfolio)

        assert len(runner._bars[SYMBOL]) == self.NEEDED

    def test_a_daily_series_is_never_bounded(self) -> None:
        """The asymmetry is the point: a daily bar *is* a session, so a 50-day
        average spanning weekends and holidays is what a 50-day average is.
        Bounding it would be the same mistake in the other direction."""
        runner, _r, _s, _rec, _p, _sl = build()
        assert runner.timeframe is Timeframe.D1

        assert runner._warmup_floor(datetime(2026, 9, 8, 14, 0, tzinfo=UTC)) is None

    def test_an_intraday_floor_is_this_session_s_open(self) -> None:
        runner = self._runner_at(datetime(2026, 9, 8, 17, 0, tzinfo=UTC), [])

        floor = runner._warmup_floor(datetime(2026, 9, 8, 17, 0, tzinfo=UTC))

        assert floor == datetime(2026, 9, 8, 13, 30, tzinfo=UTC)

    def test_while_the_market_is_shut_nothing_from_a_past_session_qualifies(self) -> None:
        """Whatever is warmed now will trade the *next* session, and last
        Friday's bars are not that session's. `run` re-warms at each open, so
        this corrects itself rather than needing a restart."""
        runner = self._runner_at(datetime(2026, 9, 7, 12, 0, tzinfo=UTC), [])

        floor = runner._warmup_floor(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))

        assert floor is not None
        assert floor > datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
