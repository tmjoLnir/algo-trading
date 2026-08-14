"""Performance metrics — shared by backtests and live analytics (#2 and #6).

One implementation for both, so a paper-trading Sharpe is directly comparable to
the backtested one. Two of them computed differently would make the comparison
that matters most — did this hold up out of sample? — meaningless.

Convention: returns are simple (not log) period returns; `periods_per_year`
annualises (252 for daily bars, 252×390 for US equity minute bars).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

    import numpy as np

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    max_drawdown_duration_days: int
    volatility: float
    win_rate: float
    profit_factor: float
    expectancy: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    num_trades: int
    avg_holding_period_hours: float
    exposure_pct: float
    turnover: float

    def to_dict(self) -> dict[str, float | int]:
        raise NotImplementedError


def returns_from_equity(equity: np.ndarray) -> np.ndarray:
    """Period-over-period simple returns."""
    raise NotImplementedError


def sharpe_ratio(
    returns: np.ndarray, risk_free_rate: float = 0.0, periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> float:
    """Excess return per unit of total volatility, annualised.

    Annualise by √periods_per_year. Two caveats worth remembering before
    trusting a big number: Sharpe assumes roughly normal returns, so a strategy
    that sells options — tiny steady gains, rare huge losses — shows a
    spectacular Sharpe right up until it doesn't; and it is unstable over short
    samples. Under ~100 observations, treat it as a hint, not a result.
    """
    raise NotImplementedError


def sortino_ratio(
    returns: np.ndarray, target: float = 0.0, periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> float:
    """Like Sharpe but penalises only downside deviation. Usually the fairer
    measure — upside volatility is not a risk anyone objects to."""
    raise NotImplementedError


def max_drawdown(equity: np.ndarray) -> tuple[float, int, int]:
    """(max drawdown as a negative fraction, peak index, trough index).

    The number a human actually feels. A 60% drawdown needs a 150% gain to
    recover, which is why it, not volatility, is what makes people abandon a
    strategy at the worst moment.
    """
    raise NotImplementedError


def calmar_ratio(cagr: float, max_dd: float) -> float:
    """CAGR ÷ |max drawdown| — return per unit of worst-case pain."""
    raise NotImplementedError


def profit_factor(pnls: list[Decimal]) -> float:
    """Gross profit ÷ gross loss. Below 1.0 the strategy loses money.

    Infinite when there are no losses — which nearly always means too few
    trades, not a perfect strategy.
    """
    raise NotImplementedError


def expectancy(pnls: list[Decimal]) -> float:
    """Average P&L per trade. The one number that answers "should I run this?"

    A 30%-win-rate strategy with positive expectancy beats a 70% one with
    negative expectancy. Win rate alone tells you nothing.
    """
    raise NotImplementedError


def compute_all(
    equity_curve: list[tuple[object, Decimal]],
    trade_pnls: list[Decimal],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> PerformanceMetrics:
    """Compute the full set in one pass."""
    raise NotImplementedError
