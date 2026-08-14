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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.clock import Clock, TradingCalendar
    from atp_core.data.ports import BarRepository, QuoteCache
    from atp_core.domain import Portfolio
    from atp_core.execution.router import OrderRouter
    from atp_core.risk.killswitch import KillSwitch
    from atp_core.risk.stops import StopManager
    from atp_core.strategy.base import Strategy


@dataclass(slots=True)
class RunnerStats:
    started_at: datetime | None = None
    evaluations: int = 0
    signals_generated: int = 0
    orders_submitted: int = 0
    orders_rejected_by_risk: int = 0
    last_evaluation_at: datetime | None = None
    errors: int = 0


class StrategyRunner:
    """Runs one strategy against live (or paper) market data."""

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
        self.stats = RunnerStats()

    async def warmup(self, portfolio: Portfolio) -> None:
        """Load history and rebuild state before the first evaluation.

        Two things must happen here, and skipping either produces a runner that
        appears to work:

        1. Load `strategy.warmup_bars` of history, so indicators are correct
           from the first live bar rather than from bar 50.
        2. Reconcile against the broker (`execution.reconciliation`). We may
           have restarted holding positions we no longer know about — a runner
           that starts assuming it is flat will happily double a position.
        """
        raise NotImplementedError

    async def run(self, portfolio: Portfolio) -> None:
        """The main loop. Runs until cancelled.

        Between sessions, sleep until `calendar.next_open()` rather than
        spinning — and re-run `warmup()` at each open, because overnight
        corporate actions and after-hours fills change the picture.
        """
        raise NotImplementedError

    async def evaluate(self, portfolio: Portfolio) -> None:
        """One pass of the ordering documented at the top of this module.

        Wrap in a try/except: an unhandled exception here must not kill the
        loop silently. Log it, increment `stats.errors`, and engage the kill
        switch after N consecutive failures — a runner erroring every tick is
        not trading, but it looks alive to a health check.
        """
        raise NotImplementedError

    async def on_fill_event(self, event: dict[str, object], portfolio: Portfolio) -> None:
        """Handle a broker fill.

        On an entry fill, place protective orders IMMEDIATELY (`router
        .submit_protective_orders`). Every millisecond between owning a position
        and having a stop on it is unprotected exposure.
        """
        raise NotImplementedError

    async def shutdown(self, close_positions: bool = False) -> None:
        """Stop cleanly.

        Default is to leave positions open with their broker-side stops intact.
        Liquidating on every deploy would turn a routine restart into a taxable
        event and a guaranteed spread cost — the stops are there precisely so
        the position can survive us not running.
        """
        raise NotImplementedError
