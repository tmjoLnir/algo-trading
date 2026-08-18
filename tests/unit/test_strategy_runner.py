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
from atp_core.clock import SimulatedClock
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
from atp_core.execution.reconciliation import ReconciliationReport
from atp_core.execution.router import ProtectionResult, SubmitResult
from atp_core.risk.engine import RiskDecision
from atp_core.risk.stops import StopConfig, StopManager
from atp_core.strategy.base import Strategy
from atp_core.strategy.rules import PositionSizeSpec
from atp_worker.runner import MAX_CONSECUTIVE_ERRORS, StrategyRunner
from tests.fakes import FakeKillSwitch, FakeOrderRepository, FakePortfolioRepository

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
    """Records what the runner asked for, in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.signals: list[Signal] = []
        self.flattened: list[str] = []
        self.protected: list[Order] = []
        self.refuse_signals = False
        self.refuse_protection = False
        self.refuse_flatten = False
        self._next_id = 0

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
        if self.refuse_signals:
            return SubmitResult(
                order=None, decision=RiskDecision.deny("a_rule", "nope"), submitted=False
            )
        side = Side.BUY if signal.action is SignalAction.ENTER_LONG else Side.SELL
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
                    SubmitResult(
                        order=None,
                        decision=RiskDecision.deny("kill_switch", "halted"),
                        submitted=False,
                    )
                ],
                covered_qty=Decimal(0),
                unprotected_qty=Decimal("10"),
            )
        stop = self._order(entry_order.symbol, entry_order.side.opposite, OrderType.STOP)
        return ProtectionResult(
            placed=[stop], covered_qty=Decimal("10"), unprotected_qty=Decimal(0)
        )

    async def flatten(self, symbol: str, portfolio: Portfolio) -> SubmitResult:
        self.calls.append("flatten")
        self.flattened.append(symbol)
        if self.refuse_flatten:
            return SubmitResult(
                order=None, decision=RiskDecision.deny("kill_switch", "halted"), submitted=False
            )
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
