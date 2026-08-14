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
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.backtest.costs import CostModel
    from atp_core.domain import Bar, Order, Portfolio, Signal, Timeframe
    from atp_core.risk.engine import RiskEngine
    from atp_core.strategy.base import Strategy


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
        raise NotImplementedError

    def to_report(self) -> dict[str, object]:
        """Serialisable summary for the API and the dashboard."""
        raise NotImplementedError


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
    """

    def __init__(
        self,
        strategy: Strategy,
        config: BacktestConfig,
        cost_model: CostModel,
        risk_engine: RiskEngine,
    ) -> None:
        self.strategy = strategy
        self.config = config
        self.cost_model = cost_model
        self.risk_engine = risk_engine

    def run(self, bars: dict[str, list[Bar]]) -> BacktestResult:
        """Execute the backtest.

        `bars` is symbol → chronologically sorted bars. Validate before starting:
        gaps, duplicate timestamps and unsorted input each produce plausible but
        wrong results, so fail loudly (`DataGapError`) rather than proceeding.
        """
        raise NotImplementedError("see docs/BACKTESTING.md")

    def _fill_pending(self, bar: Bar) -> list[Order]:
        """Fill resting orders against this bar.

        Market → next open plus slippage. Limit → only if the bar's range
        actually reached the price. Stop → triggers on the extreme, fills with
        slippage past it, because a stop becomes a market order in a moving
        market and the fill is routinely worse than the trigger.
        """
        raise NotImplementedError
