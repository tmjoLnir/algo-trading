"""Technical indicators — pure functions over float arrays.

Floats, not Decimals: these feed statistics, not a ledger (CLAUDE.md §1.1).

Each returns the value for the LAST element of the input. Vectorised variants
returning full series live alongside for the backtest engine, which computes
once per symbol rather than once per bar. They are named `<indicator>_series`
and are the primitive: EMA, RSI and ATR are recursive, so their last value is
only definable by running the whole recursion. The scalar form is the last
element of that series, not a second implementation of the same maths — two
implementations of Wilder's smoothing is two chances to get it subtly wrong.

**Warmup is `nan`, never a partial value.** A series is the same length as its
input, with `nan` in the leading positions where the indicator is not yet
defined. Returning a partial average there instead is how a backtest opens with
a burst of trades taken on an SMA(200) computed from six bars.

Scalar callers pay for the whole recursion on every call, which is O(n) per
bar and O(n²) over a run. That is deliberate: it keeps the scalar form honest
for a strategy hook, and anything hot should either use the `_series` variant
or `StrategyContext.indicator`, which caches.

Conventions worth stating, because reasonable implementations differ and a
number that disagrees with the user's chart reads as a bug:

- EMA seeds with the SMA of the first `period` values.
- RSI and ATR use Wilder's smoothing (alpha = 1/period), seeded with a simple
  average of the first `period` observations.
- Standard deviation is the population one (`ddof=0`), which is what charting
  packages plot Bollinger bands from.
"""

from __future__ import annotations

import numpy as np


def _require_period(name: str, period: int) -> None:
    if period < 1:
        raise ValueError(f"{name} needs period >= 1, got {period}")


def _require_length(name: str, needed: int, got: int) -> None:
    if got < needed:
        raise ValueError(f"{name} needs {needed} values, got {got}")


def sma(values: np.ndarray, period: int) -> float:
    """Simple moving average of the last `period` values."""
    if len(values) < period:
        raise ValueError(f"sma({period}) needs {period} values, got {len(values)}")
    return float(np.mean(values[-period:]))


def sma_series(values: np.ndarray, period: int) -> np.ndarray:
    """Rolling simple moving average; `nan` until `period` values exist."""
    _require_period(f"sma({period})", period)
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out
    # Cumulative sums give every window in one pass rather than one mean per
    # window, which matters on a five-year minute series.
    cumulative = np.cumsum(np.insert(values, 0, 0.0))
    out[period - 1 :] = (cumulative[period:] - cumulative[:-period]) / period
    return out


def ema_series(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average over the whole input; `nan` during warmup."""
    _require_period(f"ema({period})", period)
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out

    alpha = 2.0 / (period + 1)
    out[period - 1] = float(np.mean(values[:period]))
    # A plain recurrence rather than a cumulative-product trick: the closed form
    # multiplies by (1 - alpha)**n, which underflows to zero on a long series
    # and silently stops being an average.
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def ema(values: np.ndarray, period: int) -> float:
    """Exponential moving average.

    Seeded with the SMA of the first `period` values, which is the convention
    charting packages use — seeding with the first value instead produces
    numbers that disagree with what a user sees on their broker's chart.
    """
    _require_period(f"ema({period})", period)
    _require_length(f"ema({period})", period, len(values))
    return float(ema_series(values, period)[-1])


def _wilder_smooth(seed: float, rest: np.ndarray, period: int) -> np.ndarray:
    """Wilder's running average, applied to `rest` after `seed`.

    `avg_t = (avg_{t-1} * (period - 1) + x_t) / period`, which is an EMA with
    alpha = 1/period. Wilder's own notation, kept because RSI and ATR are both
    defined in it and reading them side by side should not require translating.
    """
    out = np.empty(len(rest) + 1)
    out[0] = seed
    for i, value in enumerate(rest, start=1):
        out[i] = (out[i - 1] * (period - 1) + value) / period
    return out


def rsi_series(values: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI over the whole input; `nan` until `period + 1` values exist."""
    _require_period(f"rsi({period})", period)
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < period + 1:
        return out

    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = _wilder_smooth(float(np.mean(gains[:period])), gains[period:], period)
    avg_loss = _wilder_smooth(float(np.mean(losses[:period])), losses[period:], period)

    # A window with no losses has no finite RS. It is not an error and not a
    # missing value — it is the top of the scale, and clamping to 100 is what
    # every reference implementation does. Same reasoning for a window with
    # neither gains nor losses: a flat series is not oversold.
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0, np.inf, avg_gain / np.where(avg_loss == 0, 1.0, avg_loss))
    rsi_values = np.where(np.isinf(rs), 100.0, 100.0 - 100.0 / (1.0 + rs))

    # `deltas` is one shorter than `values`, and the first RSI lands on the bar
    # that closes the first full window — index `period`, not `period - 1`.
    out[period:] = rsi_values
    return out


def rsi(values: np.ndarray, period: int = 14) -> float:
    """Wilder's relative strength index, 0..100.

    Use Wilder's smoothing (alpha = 1/period), not a simple average of gains —
    the two disagree materially and every reference implementation uses Wilder's.
    """
    _require_period(f"rsi({period})", period)
    _require_length(f"rsi({period})", period + 1, len(values))
    return float(rsi_series(values, period)[-1])


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True range per bar; `nan` for the first, which has no previous close.

    `max(H−L, |H−prev_close|, |L−prev_close|)`. The gap terms are the point:
    a bar that opens far from yesterday's close has a range H−L misses
    entirely, and an ATR stop sized off H−L alone is too tight to survive one.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    if not len(high) == len(low) == len(close):
        raise ValueError(
            f"true_range needs equal-length inputs, got high={len(high)} "
            f"low={len(low)} close={len(close)}"
        )

    out = np.full(len(high), np.nan)
    if len(high) < 2:
        return out
    prev_close = close[:-1]
    out[1:] = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
    )
    return out


def atr_series(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Wilder's ATR over the whole input; `nan` until `period + 1` bars exist."""
    _require_period(f"atr({period})", period)
    ranges = true_range(high, low, close)
    out = np.full(len(ranges), np.nan)
    if len(ranges) < period + 1:
        return out

    # `ranges[0]` is nan by construction, so the first full window is
    # `ranges[1 : period + 1]` and the first ATR lands on index `period`.
    defined = ranges[1:]
    seed = float(np.mean(defined[:period]))
    out[period:] = _wilder_smooth(seed, defined[period:], period)
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    """Average true range — the volatility measure ATR stops and volatility
    position sizing are built on.

    True range is max(H−L, |H−prev_close|, |L−prev_close|); the gap terms are
    the point, since they capture overnight moves that H−L misses entirely.
    """
    _require_period(f"atr({period})", period)
    _require_length(f"atr({period})", period + 1, len(high))
    return float(atr_series(high, low, close, period)[-1])


def stddev_series(values: np.ndarray, period: int) -> np.ndarray:
    """Rolling population standard deviation; `nan` until `period` values exist."""
    _require_period(f"stddev({period})", period)
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out

    means = sma_series(values, period)
    mean_of_squares = sma_series(values * values, period)
    # E[x²] − E[x]², clipped because cancellation can leave a tiny negative
    # where the true variance is zero — a flat window must not produce nan.
    variance = np.clip(mean_of_squares - means * means, 0.0, None)
    out[period - 1 :] = np.sqrt(variance[period - 1 :])
    return out


def stddev(values: np.ndarray, period: int) -> float:
    """Population standard deviation of the last `period` values (`ddof=0`)."""
    _require_period(f"stddev({period})", period)
    _require_length(f"stddev({period})", period, len(values))
    return float(np.std(np.asarray(values, dtype=float)[-period:]))


def bollinger_series(
    values: np.ndarray, period: int = 20, num_std: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(lower, middle, upper) as full series; `nan` during warmup."""
    middle = sma_series(values, period)
    spread = num_std * stddev_series(values, period)
    return middle - spread, middle, middle + spread


def bollinger(
    values: np.ndarray, period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float]:
    """(lower, middle, upper)."""
    _require_period(f"bollinger({period})", period)
    _require_length(f"bollinger({period})", period, len(values))
    lower, middle, upper = bollinger_series(values, period, num_std)
    return float(lower[-1]), float(middle[-1]), float(upper[-1])


def macd_series(
    values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(macd_line, signal_line, histogram) as full series; `nan` during warmup."""
    for name, period in (("fast", fast), ("slow", slow), ("signal", signal)):
        _require_period(f"macd({name}={period})", period)
    if fast >= slow:
        raise ValueError(f"macd needs fast < slow, got fast={fast} slow={slow}")

    values = np.asarray(values, dtype=float)
    macd_line = ema_series(values, fast) - ema_series(values, slow)
    signal_line = np.full(len(values), np.nan)

    # The signal EMA is an EMA *of the MACD line*, so it has to be seeded from
    # where that line becomes defined — running it over the nan warmup would
    # poison every value after it.
    defined = macd_line[slow - 1 :]
    if len(defined) >= signal:
        signal_line[slow - 1 :] = ema_series(defined, signal)

    return macd_line, signal_line, macd_line - signal_line


def macd(
    values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float, float, float]:
    """(macd_line, signal_line, histogram)."""
    _require_length(f"macd({fast},{slow},{signal})", slow + signal - 1, len(values))
    macd_line, signal_line, histogram = macd_series(values, fast, slow, signal)
    return float(macd_line[-1]), float(signal_line[-1]), float(histogram[-1])


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
