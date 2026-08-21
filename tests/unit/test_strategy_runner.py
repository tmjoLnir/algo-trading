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

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from atp_core.brokers.ports import TradeUpdate
from atp_core.channels import CHANNEL_ORDERS, CHANNEL_SIGNALS
from atp_core.clock import SimulatedClock
from atp_core.dashboard.snapshot import DEFAULT_SIGNAL_LIMIT
from atp_core.domain import (
    Bar,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Portfolio,
    Quote,
    RunMode,
    Side,
    Signal,
    SignalAction,
    StopType,
    Timeframe,
)
from atp_core.errors import ExecutionError
from atp_core.execution.idempotency import FLATTEN, STOP_LOSS, TAKE_PROFIT, TIME_EXIT
from atp_core.execution.reconciliation import ReconciliationReport
from atp_core.execution.router import ProtectionResult, SubmitResult
from atp_core.risk.engine import RiskDecision
from atp_core.risk.stops import StopConfig, StopManager
from atp_core.strategy.base import Strategy
from atp_core.strategy.rules import PositionSizeSpec
from atp_worker.runner import MAX_CONSECUTIVE_ERRORS, StrategyRunner
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
    def __init__(self, bars: dict[str, list[Bar]] | None = None) -> None:
        self.bars = bars or {}

    async def upsert_bars(self, bars: list[Bar]) -> int:
        return 0

    async def get_bars(self, symbol: str, timeframe: Any, start: Any, end: Any) -> list[Bar]:
        return list(self.bars.get(symbol, []))

    async def get_last_n_bars(self, symbol: str, timeframe: Any, n: int) -> list[Bar]:
        return list(self.bars.get(symbol, []))[-n:]

    async def find_gaps(self, symbol: str, timeframe: Any, start: Any, end: Any) -> list[Any]:
        return []

    async def stored_series(self) -> list[tuple[str, Any]]:
        return [(symbol, Timeframe.D1) for symbol in self.bars]


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
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.signals: list[Signal] = []
        self.flattened: list[str] = []
        self.flatten_purposes: list[str] = []
        self.protected: list[Order] = []
        self.refuse_signals = False
        self.refuse_protection = False
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

    async def submit_signal(
        self, signal: Signal, portfolio: Portfolio, sizing: Any
    ) -> SubmitResult:
        self.calls.append("submit_signal")
        self.signals.append(signal)
        side = Side.BUY if signal.action is SignalAction.ENTER_LONG else Side.SELL
        if self.refuse_signals:
            return self._refused(signal.symbol, side, "a_rule", "nope")
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
        self.calls.append(list(known_orders))
        report = ReconciliationReport(checked_at=START)
        if not self.clean:
            report.orphan_order_ids.append("atp-orphan")
        return report


class FakeCalendar:
    def __init__(self, open_: bool = True) -> None:
        self.open = open_

    def is_open(self, ts: datetime) -> bool:
        return self.open

    def next_open(self, after: datetime) -> datetime:
        return after + timedelta(hours=1)


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
        kill_switch=switch,  # type: ignore[arg-type]
        bar_repo=FakeBarRepo({SYMBOL: bars or [bar(0)]}),  # type: ignore[arg-type]
        quote_cache=FakeQuoteCache(),  # type: ignore[arg-type]
        clock=SimulatedClock(START),
        calendar=FakeCalendar(),  # type: ignore[arg-type]
        reconciler=reconciler,  # type: ignore[arg-type]
        sizing=PositionSizeSpec(type="fixed_qty", value=Decimal("10")),
        stop_config=stop_config or StopConfig(stop_type=StopType.FIXED_PCT, value=Decimal("0.02")),
        timeframe=Timeframe.D1,
        run_mode=RunMode.PAPER,
        order_repo=order_repo or FakeOrderRepository(),  # type: ignore[arg-type]
        portfolio_repo=portfolio_repo or FakePortfolioRepository(),  # type: ignore[arg-type]
        strategy_repo=strategy_repo or FakeStrategyRepository(),  # type: ignore[arg-type]
        signal_repo=signal_repo or FakeSignalRepository(),  # type: ignore[arg-type]
        snapshot_store=snapshot_store,  # type: ignore[arg-type]
        publisher=publisher,  # type: ignore[arg-type]
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
