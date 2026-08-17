"""Performance metrics — shared by backtests and live analytics (#2 and #6).

One implementation for both, so a paper-trading Sharpe is directly comparable to
the backtested one. Two of them computed differently would make the comparison
that matters most — did this hold up out of sample? — meaningless.

Convention: returns are simple (not log) period returns; `periods_per_year`
annualises (252 for daily bars, 252×390 for US equity minute bars).

Two more conventions, stated because reasonable implementations differ and a
number that disagrees with the reader's own arithmetic reads as a bug:

- Standard deviation is the **sample** one (`ddof=1`). A return series is a
  sample, not a population, and every reference implementation of Sharpe treats
  it that way.
- `risk_free_rate` and `target` are quoted **annually** and divided down by
  `periods_per_year`, matching how anyone actually quotes a risk-free rate.

Ratios return `inf` where the denominator is legitimately zero — no losing
trade, no downside deviation — rather than a sentinel. That is the honest value,
and per docs/BACKTESTING.md it nearly always means too few trades rather than a
perfect strategy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence

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
        return asdict(self)


def returns_from_equity(equity: np.ndarray) -> np.ndarray:
    """Period-over-period simple returns.

    One shorter than the input. A period that starts from zero or negative
    equity has no defined return — the account was already gone — and yields 0
    rather than an infinity that would poison every statistic downstream.
    """
    equity = np.asarray(equity, dtype=float)
    if len(equity) < 2:
        return np.empty(0)
    previous, current = equity[:-1], equity[1:]
    out: np.ndarray = np.zeros(len(previous))
    np.divide(current - previous, previous, out=out, where=previous > 0)
    return out


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
    returns = np.asarray(returns, dtype=float)
    if len(returns) < 2:
        return 0.0
    excess = returns - risk_free_rate / periods_per_year
    deviation = float(np.std(excess, ddof=1))
    if deviation == 0:
        # A perfectly flat return stream has no risk to be compensated for.
        # Reporting infinity here would put a strategy that never traded at the
        # top of every ranking.
        return 0.0
    return float(np.mean(excess) / deviation * np.sqrt(periods_per_year))


def sortino_ratio(
    returns: np.ndarray, target: float = 0.0, periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> float:
    """Like Sharpe but penalises only downside deviation. Usually the fairer
    measure — upside volatility is not a risk anyone objects to.

    Downside deviation is the root mean square of the shortfalls over *every*
    period, not only the losing ones: dividing by the count of losses instead
    would reward a strategy for having few of them twice over.
    """
    returns = np.asarray(returns, dtype=float)
    if len(returns) < 2:
        return 0.0
    excess = returns - target / periods_per_year
    shortfall = np.minimum(excess, 0.0)
    downside = float(np.sqrt(np.mean(shortfall**2)))
    mean = float(np.mean(excess))
    if downside == 0:
        # Never below target. Genuinely unbounded, and per the module docstring
        # that is a statement about the sample size, not about the strategy.
        return float("inf") if mean > 0 else 0.0
    return float(mean / downside * np.sqrt(periods_per_year))


def max_drawdown(equity: np.ndarray) -> tuple[float, int, int]:
    """(max drawdown as a negative fraction, peak index, trough index).

    The number a human actually feels. A 60% drawdown needs a 150% gain to
    recover, which is why it, not volatility, is what makes people abandon a
    strategy at the worst moment.

    The peak returned is the one the worst trough fell from, not the highest
    peak overall — those differ whenever a later, shallower peak precedes a
    deeper trough, and it is the fall that is being measured.
    """
    equity = np.asarray(equity, dtype=float)
    if len(equity) == 0:
        return 0.0, 0, 0

    running_peak = np.maximum.accumulate(equity)
    drawdowns = np.divide(
        equity - running_peak,
        running_peak,
        out=np.zeros(len(equity)),
        where=running_peak > 0,
    )
    trough = int(np.argmin(drawdowns))
    worst = float(drawdowns[trough])
    if worst == 0:
        return 0.0, 0, 0
    peak = int(np.argmax(equity[: trough + 1]))
    return worst, peak, trough


def calmar_ratio(cagr: float, max_dd: float) -> float:
    """CAGR ÷ |max drawdown| — return per unit of worst-case pain."""
    if max_dd == 0:
        return float("inf") if cagr > 0 else 0.0
    return cagr / abs(max_dd)


def profit_factor(pnls: list[Decimal]) -> float:
    """Gross profit ÷ gross loss. Below 1.0 the strategy loses money.

    Infinite when there are no losses — which nearly always means too few
    trades, not a perfect strategy.
    """
    gross_profit = float(sum((p for p in pnls if p > 0), Decimal(0)))
    gross_loss = float(abs(sum((p for p in pnls if p < 0), Decimal(0))))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def expectancy(pnls: list[Decimal]) -> float:
    """Average P&L per trade. The one number that answers "should I run this?"

    A 30%-win-rate strategy with positive expectancy beats a 70% one with
    negative expectancy. Win rate alone tells you nothing.
    """
    if not pnls:
        return 0.0
    return float(sum(pnls, Decimal(0)) / len(pnls))


def _duration_days(
    timestamps: Sequence[object], peak: int, trough: int, periods_per_year: int
) -> int:
    """Calendar days from peak to trough, or trading days if there are no dates.

    The equity curve is typed as carrying opaque timestamps because no other
    metric needs them. This one does, so it uses real dates when they are real
    dates and falls back to converting the period count otherwise — which for
    daily bars is the same number anyway.
    """
    if peak == trough:
        return 0
    start, end = timestamps[peak], timestamps[trough]
    if isinstance(start, datetime) and isinstance(end, datetime):
        return (end - start).days
    periods = trough - peak
    return round(periods * TRADING_DAYS_PER_YEAR / periods_per_year)


def compute_all(
    equity_curve: list[tuple[object, Decimal]],
    trade_pnls: list[Decimal],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    *,
    avg_holding_period_hours: float = 0.0,
    exposure_pct: float = 0.0,
    turnover: float = 0.0,
) -> PerformanceMetrics:
    """Compute the full set in one pass.

    The three keyword arguments are the metrics an equity curve cannot answer:
    how long positions were held, how much of the time the book was in the
    market, and how much notional was traded. They need trade shape, which
    lives with the caller — the backtest engine has it from its fills, and the
    analytics layer has it from `TradeRecord`s. They default to 0.0, so a
    caller that does not supply them reports 0.0 rather than a wrong number;
    that is a gap in the report, not a measurement.
    """
    equity = np.array([float(e) for _, e in equity_curve], dtype=float)
    timestamps = [ts for ts, _ in equity_curve]
    returns = returns_from_equity(equity)

    total_return = float((equity[-1] - equity[0]) / equity[0]) if len(equity) and equity[0] else 0.0

    years = len(returns) / periods_per_year if periods_per_year else 0.0
    if years > 0 and equity[0] > 0 and equity[-1] > 0:
        cagr = float((equity[-1] / equity[0]) ** (1 / years) - 1)
    else:
        # A sub-period sample, or an account that ended at or below zero. There
        # is no growth rate to annualise; extrapolating one from a fortnight is
        # how a backtest reports 400% a year.
        cagr = 0.0

    worst_dd, peak_i, trough_i = max_drawdown(equity)
    volatility = (
        float(np.std(returns, ddof=1) * np.sqrt(periods_per_year)) if len(returns) > 1 else 0.0
    )

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]

    return PerformanceMetrics(
        total_return=total_return,
        cagr=cagr,
        sharpe=sharpe_ratio(returns, periods_per_year=periods_per_year),
        sortino=sortino_ratio(returns, periods_per_year=periods_per_year),
        calmar=calmar_ratio(cagr, worst_dd),
        max_drawdown=worst_dd,
        max_drawdown_duration_days=_duration_days(timestamps, peak_i, trough_i, periods_per_year),
        volatility=volatility,
        win_rate=len(wins) / len(trade_pnls) if trade_pnls else 0.0,
        profit_factor=profit_factor(trade_pnls),
        expectancy=expectancy(trade_pnls),
        avg_win=float(sum(wins, Decimal(0)) / len(wins)) if wins else 0.0,
        avg_loss=float(sum(losses, Decimal(0)) / len(losses)) if losses else 0.0,
        largest_win=float(max(wins)) if wins else 0.0,
        largest_loss=float(min(losses)) if losses else 0.0,
        num_trades=len(trade_pnls),
        avg_holding_period_hours=avg_holding_period_hours,
        exposure_pct=exposure_pct,
        turnover=turnover,
    )
