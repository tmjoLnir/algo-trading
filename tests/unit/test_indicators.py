"""Indicator maths.

Expected values here are hand-computed from the definition, not captured from a
previous run of this code — a test that records whatever the implementation
produced would pass just as happily on a wrong implementation.

The warmup assertions matter as much as the values. An indicator that returns a
partial average instead of `nan` before it is defined is how a backtest opens
with a burst of trades taken on an SMA(200) computed from six bars
(`Strategy.warmup_bars`, docs/BACKTESTING.md).
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from atp_core.indicators import ta

PRICES = st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)


# ── SMA ─────────────────────────────────────────────────────────────────────


def test_sma_averages_the_last_period_values() -> None:
    assert ta.sma(np.array([1.0, 2.0, 3.0, 4.0]), 2) == 3.5  # (3+4)/2, not all four


def test_sma_series_is_nan_until_the_window_is_full() -> None:
    out = ta.sma_series(np.array([1.0, 2.0, 3.0, 4.0]), 3)
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(2.0)  # (1+2+3)/3
    assert out[3] == pytest.approx(3.0)  # (2+3+4)/3


def test_sma_series_shorter_than_period_is_all_nan() -> None:
    assert np.isnan(ta.sma_series(np.array([1.0, 2.0]), 5)).all()


# ── EMA ─────────────────────────────────────────────────────────────────────


def test_ema_seeds_with_the_sma_of_the_first_period_values() -> None:
    """[1,2,3,4,5], period 3, alpha = 2/(3+1) = 0.5.

    seed = mean(1,2,3) = 2  →  0.5*4 + 0.5*2 = 3  →  0.5*5 + 0.5*3 = 4
    """
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ta.ema_series(values, 3)
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)
    assert ta.ema(values, 3) == pytest.approx(4.0)


def test_ema_of_a_flat_series_is_that_value() -> None:
    assert ta.ema(np.full(50, 7.5), 10) == pytest.approx(7.5)


def test_ema_matches_the_recurrence_written_out_independently() -> None:
    """Guards the vectorised seed, not the recurrence — spelled out by hand."""
    rng = np.random.default_rng(1234)
    values = rng.uniform(50.0, 150.0, size=60)
    period, alpha = 12, 2.0 / 13
    expected = float(np.mean(values[:period]))
    for value in values[period:]:
        expected = alpha * value + (1 - alpha) * expected
    assert ta.ema(values, period) == pytest.approx(expected)


def test_ema_rejects_a_series_shorter_than_its_period() -> None:
    with pytest.raises(ValueError, match="needs 5 values"):
        ta.ema(np.array([1.0, 2.0]), 5)


# ── RSI ─────────────────────────────────────────────────────────────────────


def test_rsi_hand_computed_with_wilder_smoothing() -> None:
    """[10, 11, 10, 12], period 2.

    deltas = [+1, -1, +2] → gains [1,0,2], losses [0,1,0]
    seed avg_gain = mean(1,0) = 0.5   seed avg_loss = mean(0,1) = 0.5
      → first RSI = 100 - 100/(1 + 0.5/0.5) = 50
    next avg_gain = (0.5*1 + 2)/2 = 1.25   avg_loss = (0.5*1 + 0)/2 = 0.25
      → RS = 5 → RSI = 100 - 100/6 = 83.333...
    """
    values = np.array([10.0, 11.0, 10.0, 12.0])
    out = ta.rsi_series(values, 2)
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(50.0)
    assert out[3] == pytest.approx(100.0 - 100.0 / 6.0)
    assert ta.rsi(values, 2) == pytest.approx(100.0 - 100.0 / 6.0)


def test_rsi_is_100_when_nothing_fell() -> None:
    assert ta.rsi(np.arange(1.0, 30.0), 14) == pytest.approx(100.0)


def test_rsi_is_0_when_nothing_rose() -> None:
    assert ta.rsi(np.arange(30.0, 1.0, -1.0), 14) == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_100_not_nan() -> None:
    """A deliberate convention, shared with the reference implementations: the
    guard is `avg_loss == 0`, and a flat window trips it. Worth pinning — the
    alternative is a nan that silently disables every rule reading RSI."""
    assert ta.rsi(np.full(30, 42.0), 14) == pytest.approx(100.0)


def test_rsi_needs_one_more_value_than_its_period() -> None:
    """`period` deltas need `period + 1` prices."""
    assert np.isnan(ta.rsi_series(np.arange(14.0), 14)).all()
    with pytest.raises(ValueError, match="needs 15 values"):
        ta.rsi(np.arange(14.0), 14)
    assert not math.isnan(ta.rsi(np.arange(15.0), 14))


# ── True range / ATR ────────────────────────────────────────────────────────


def test_true_range_first_bar_is_nan_having_no_previous_close() -> None:
    tr = ta.true_range(np.array([10.0, 11.0]), np.array([9.0, 10.0]), np.array([9.5, 10.5]))
    assert np.isnan(tr[0])
    assert tr[1] == pytest.approx(1.5)  # max(11-10, |11-9.5|, |10-9.5|)


def test_true_range_uses_the_gap_when_it_exceeds_the_bar() -> None:
    """The whole reason the gap terms exist: a bar that opens far from
    yesterday's close has a range H−L misses entirely."""
    tr = ta.true_range(np.array([10.0, 20.0]), np.array([9.0, 19.0]), np.array([9.5, 19.5]))
    assert tr[1] == pytest.approx(10.5)  # |20 - 9.5|, not H-L = 20-19 = 1


def test_true_range_rejects_ragged_inputs() -> None:
    with pytest.raises(ValueError, match="equal-length"):
        ta.true_range(np.array([1.0, 2.0]), np.array([1.0]), np.array([1.0, 2.0]))


def test_atr_hand_computed() -> None:
    """TR = [nan, 1.5, 1.5]; period 2 → seed = mean(1.5, 1.5) = 1.5."""
    high = np.array([10.0, 11.0, 12.0])
    low = np.array([9.0, 10.0, 11.0])
    close = np.array([9.5, 10.5, 11.5])
    assert ta.atr(high, low, close, 2) == pytest.approx(1.5)


def test_atr_smooths_wilder_style_after_the_seed() -> None:
    """TR = [nan, 1.5, 9.5, 1.5]; period 2.

    seed = mean(1.5, 9.5) = 5.5  →  (5.5*1 + 1.5)/2 = 3.5
    """
    high = np.array([10.0, 11.0, 20.0, 21.0])
    low = np.array([9.0, 10.0, 19.0, 20.0])
    close = np.array([9.5, 10.5, 19.5, 20.5])
    out = ta.atr_series(high, low, close, 2)
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(5.5)
    assert out[3] == pytest.approx(3.5)
    assert ta.atr(high, low, close, 2) == pytest.approx(3.5)


def test_atr_needs_one_more_bar_than_its_period() -> None:
    with pytest.raises(ValueError, match="needs 15 values"):
        ta.atr(np.arange(14.0), np.arange(14.0), np.arange(14.0), 14)


# ── Bollinger / stddev ──────────────────────────────────────────────────────


def test_stddev_is_the_population_one() -> None:
    """[1..5] has population sd sqrt(2); the sample sd would be sqrt(2.5)."""
    assert ta.stddev(np.array([1.0, 2.0, 3.0, 4.0, 5.0]), 5) == pytest.approx(math.sqrt(2.0))


def test_stddev_of_a_flat_window_is_zero_not_nan() -> None:
    """Cancellation in E[x²]−E[x]² can leave a tiny negative; sqrt of that is
    nan, which would propagate into both Bollinger bands."""
    out = ta.stddev_series(np.full(10, 1234.5), 5)
    assert out[4:] == pytest.approx(0.0)


def test_bollinger_bands_sit_num_std_either_side_of_the_mean() -> None:
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    lower, middle, upper = ta.bollinger(values, 5, 2.0)
    assert middle == pytest.approx(3.0)
    assert lower == pytest.approx(3.0 - 2 * math.sqrt(2.0))
    assert upper == pytest.approx(3.0 + 2 * math.sqrt(2.0))


def test_bollinger_series_warmup_is_nan_on_all_three_bands() -> None:
    lower, middle, upper = ta.bollinger_series(np.arange(10.0), 5)
    for band in (lower, middle, upper):
        assert np.isnan(band[:4]).all()
        assert not np.isnan(band[4:]).any()


# ── MACD ────────────────────────────────────────────────────────────────────


def test_macd_of_a_flat_series_is_all_zero() -> None:
    macd_line, signal_line, histogram = ta.macd(np.full(60, 100.0))
    assert macd_line == pytest.approx(0.0)
    assert signal_line == pytest.approx(0.0)
    assert histogram == pytest.approx(0.0)


def test_macd_line_is_the_difference_of_the_two_emas() -> None:
    rng = np.random.default_rng(7)
    values = rng.uniform(50.0, 150.0, size=80)
    macd_line, _, _ = ta.macd(values, 12, 26, 9)
    assert macd_line == pytest.approx(ta.ema(values, 12) - ta.ema(values, 26))


def test_macd_signal_is_seeded_from_where_the_macd_line_becomes_defined() -> None:
    """Running the signal EMA across the nan warmup would poison everything
    after it, so the first signal value lands at `slow + signal - 2`."""
    values = np.random.default_rng(3).uniform(50.0, 150.0, size=60)
    _, signal_line, _ = ta.macd_series(values, 12, 26, 9)
    first = 26 + 9 - 2
    assert np.isnan(signal_line[:first]).all()
    assert not np.isnan(signal_line[first:]).any()


def test_macd_histogram_is_line_minus_signal() -> None:
    values = np.random.default_rng(11).uniform(50.0, 150.0, size=90)
    macd_line, signal_line, histogram = ta.macd(values)
    assert histogram == pytest.approx(macd_line - signal_line)


def test_macd_needs_slow_plus_signal_minus_one_values() -> None:
    with pytest.raises(ValueError, match="needs 34 values"):
        ta.macd(np.arange(33.0), 12, 26, 9)


def test_macd_rejects_a_fast_period_that_is_not_faster() -> None:
    with pytest.raises(ValueError, match="fast < slow"):
        ta.macd(np.arange(100.0), 26, 12, 9)


# ── shared contracts ────────────────────────────────────────────────────────


@pytest.mark.parametrize("period", [0, -1])
def test_indicators_reject_a_nonsense_period(period: int) -> None:
    values = np.arange(100.0)
    for call in (
        lambda: ta.ema(values, period),
        lambda: ta.rsi(values, period),
        lambda: ta.stddev(values, period),
        lambda: ta.atr(values, values, values, period),
    ):
        with pytest.raises(ValueError, match="period >= 1"):
            call()


def test_scalar_forms_agree_with_the_last_element_of_their_series() -> None:
    """They are meant to be the same computation, not two of them."""
    rng = np.random.default_rng(99)
    values = rng.uniform(50.0, 150.0, size=120)
    high, low = values + 1.0, values - 1.0
    assert ta.ema(values, 12) == pytest.approx(ta.ema_series(values, 12)[-1])
    assert ta.rsi(values, 14) == pytest.approx(ta.rsi_series(values, 14)[-1])
    assert ta.stddev(values, 20) == pytest.approx(ta.stddev_series(values, 20)[-1])
    assert ta.atr(high, low, values, 14) == pytest.approx(ta.atr_series(high, low, values, 14)[-1])


def test_crossed_above_is_an_edge_not_a_level() -> None:
    """Already-above is not a crossing; a strategy that treats it as one buys
    the same signal on every bar of a trend."""
    assert ta.crossed_above(np.array([1.0, 3.0]), np.array([2.0, 2.0]))
    assert not ta.crossed_above(np.array([3.0, 4.0]), np.array([2.0, 2.0]))
    assert not ta.crossed_above(np.array([3.0]), np.array([2.0]))


# ── properties ──────────────────────────────────────────────────────────────


@settings(max_examples=100)
@given(st.lists(PRICES, min_size=15, max_size=120))
def test_rsi_always_lands_on_the_0_100_scale(values: list[float]) -> None:
    assert 0.0 <= ta.rsi(np.array(values), 14) <= 100.0


@settings(max_examples=100)
@given(st.lists(PRICES, min_size=12, max_size=120))
def test_ema_never_leaves_the_range_of_its_input(values: list[float]) -> None:
    """An average that escapes its own inputs is a seeding or recurrence bug."""
    array = np.array(values)
    assert min(values) - 1e-9 <= ta.ema(array, 12) <= max(values) + 1e-9


@settings(max_examples=100)
@given(st.lists(PRICES, min_size=20, max_size=120))
def test_bollinger_bands_never_cross_their_midline(values: list[float]) -> None:
    lower, middle, upper = ta.bollinger(np.array(values), 20)
    assert lower <= middle <= upper


@settings(max_examples=100)
@given(st.lists(PRICES, min_size=16, max_size=120))
def test_atr_is_never_negative(values: list[float]) -> None:
    close = np.array(values)
    assert ta.atr(close + 1.0, close - 1.0, close, 14) >= 0.0
