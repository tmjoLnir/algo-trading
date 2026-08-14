"""Technical indicators — pure functions over float arrays.

Floats, not Decimals: these feed statistics, not a ledger (CLAUDE.md §1.1).

Each returns the value for the LAST element of the input. Vectorised variants
returning full series live alongside for the backtest engine, which computes
once per symbol rather than once per bar.
"""

from __future__ import annotations

import numpy as np


def sma(values: np.ndarray, period: int) -> float:
    """Simple moving average of the last `period` values."""
    if len(values) < period:
        raise ValueError(f"sma({period}) needs {period} values, got {len(values)}")
    return float(np.mean(values[-period:]))


def ema(values: np.ndarray, period: int) -> float:
    """Exponential moving average.

    Seeded with the SMA of the first `period` values, which is the convention
    charting packages use — seeding with the first value instead produces
    numbers that disagree with what a user sees on their broker's chart.
    """
    raise NotImplementedError


def rsi(values: np.ndarray, period: int = 14) -> float:
    """Wilder's relative strength index, 0..100.

    Use Wilder's smoothing (alpha = 1/period), not a simple average of gains —
    the two disagree materially and every reference implementation uses Wilder's.
    """
    raise NotImplementedError


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    """Average true range — the volatility measure ATR stops and volatility
    position sizing are built on.

    True range is max(H−L, |H−prev_close|, |L−prev_close|); the gap terms are
    the point, since they capture overnight moves that H−L misses entirely.
    """
    raise NotImplementedError


def bollinger(
    values: np.ndarray, period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float]:
    """(lower, middle, upper)."""
    raise NotImplementedError


def macd(
    values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float, float, float]:
    """(macd_line, signal_line, histogram)."""
    raise NotImplementedError


def stddev(values: np.ndarray, period: int) -> float:
    raise NotImplementedError


def crossed_above(fast: np.ndarray, slow: np.ndarray) -> bool:
    """True on the bar where `fast` transitions from ≤ to > `slow`.

    A crossover is an edge, not a level — see the note in the SMA example.
    """
    if len(fast) < 2 or len(slow) < 2:
        return False
    return bool(fast[-2] <= slow[-2] and fast[-1] > slow[-1])


def crossed_below(fast: np.ndarray, slow: np.ndarray) -> bool:
    if len(fast) < 2 or len(slow) < 2:
        return False
    return bool(fast[-2] >= slow[-2] and fast[-1] < slow[-1])
