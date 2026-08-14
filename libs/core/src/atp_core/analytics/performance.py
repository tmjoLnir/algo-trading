"""Analytics and reporting — requirement #6.

Runs over live/paper trading history using the same metric functions as the
backtest engine (`backtest/metrics.py`). Deliberately shared: the question worth
answering is "is live performing like the backtest said it would?", and two
separate implementations would make that comparison meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime
    from decimal import Decimal

    from atp_core.backtest.metrics import PerformanceMetrics
    from atp_core.domain import Order


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """A completed round trip — entry through exit.

    Distinct from an `Order`: one trade may span several orders (scaling in,
    partial exits). Trade-level statistics are what a human reasons about;
    order-level ones are noise.
    """

    trade_id: str
    strategy_id: str
    symbol: str
    side: str
    entry_ts: datetime
    exit_ts: datetime | None
    entry_price: Decimal
    exit_price: Decimal | None
    qty: Decimal
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    holding_period_hours: float
    exit_reason: str  # "signal" | "stop_loss" | "take_profit" | "time" | "manual"
    max_favorable_excursion: Decimal | None = None
    max_adverse_excursion: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AttributionRow:
    """P&L broken down by a dimension (strategy, symbol, hour, day of week)."""

    key: str
    net_pnl: Decimal
    num_trades: int
    win_rate: float
    avg_pnl: Decimal
    contribution_pct: float


class PerformanceAnalyzer:
    def build_trades(self, orders: list[Order]) -> list[TradeRecord]:
        """Fold fills into round trips.

        The subtle part is partial exits and scale-ins: matching must be
        consistent (FIFO by default) and the choice affects reported per-trade
        P&L, though never the total. Document which one you used — comparing
        FIFO numbers to LIFO numbers across periods is a silent error.

        MAE/MFE need bar data over the holding period; they are worth the extra
        query, because "how far underwater did this go before it worked?" is
        what tells you whether your stops are placed sensibly.
        """
        raise NotImplementedError

    def metrics(
        self, trades: list[TradeRecord], equity_curve: list[tuple[datetime, Decimal]]
    ) -> PerformanceMetrics:
        raise NotImplementedError

    def attribution(self, trades: list[TradeRecord], by: str) -> list[AttributionRow]:
        """Group P&L by `by` ∈ {strategy, symbol, hour, weekday, exit_reason}.

        `exit_reason` is the most actionable: a strategy whose profit comes
        entirely from take-profits while stops bleed it has a stop-placement
        problem, not a signal problem.
        """
        raise NotImplementedError

    def daily_returns(self, equity_curve: list[tuple[datetime, Decimal]]) -> dict[date, Decimal]:
        raise NotImplementedError

    def compare_to_backtest(
        self, live: PerformanceMetrics, backtest: PerformanceMetrics
    ) -> dict[str, float]:
        """Live vs backtest, metric by metric.

        Large negative divergence usually means one of: overfitting, unmodelled
        costs, or a strategy whose backtested fills were unachievable. Surfacing
        it is the single most valuable report this platform produces.
        """
        raise NotImplementedError
