"""Performance metrics.

Expected values are worked out from the definitions, and several use
`periods_per_year=4` so the annualisation factor is √4 = 2 and the arithmetic
stays checkable by hand rather than by rerunning the code.

These numbers are how a strategy gets approved or killed, and the same
functions run over live results (`analytics/performance.py`) so that "did it
hold up out of sample?" is a fair comparison. A metric that is quietly
generous is the same class of failure as a backtest that fills at the wrong
price: nothing errors, the strategy just looks better than it is.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import numpy as np
import pytest

from atp_core.backtest.metrics import (
    PerformanceMetrics,
    calmar_ratio,
    compute_all,
    expectancy,
    max_drawdown,
    profit_factor,
    returns_from_equity,
    sharpe_ratio,
    sortino_ratio,
)

START = datetime(2024, 1, 2, tzinfo=UTC)


def curve(values: list[float], *, step_days: int = 1) -> list[tuple[object, Decimal]]:
    return [(START + timedelta(days=i * step_days), Decimal(str(v))) for i, v in enumerate(values)]


class TestReturns:
    def test_simple_period_returns(self) -> None:
        got = returns_from_equity(np.array([100.0, 110.0, 99.0]))
        assert got == pytest.approx([0.10, -0.10])

    def test_one_point_has_no_returns(self) -> None:
        assert len(returns_from_equity(np.array([100.0]))) == 0

    def test_zero_equity_yields_zero_not_infinity(self) -> None:
        """A blown-up account has no defined return. An inf here would poison
        every statistic computed downstream of it."""
        got = returns_from_equity(np.array([100.0, 0.0, 50.0]))
        assert got[0] == pytest.approx(-1.0)
        assert got[1] == 0.0


class TestSharpe:
    def test_hand_computed(self) -> None:
        """returns [0.02, 0.00], quarterly (√4 = 2).

        mean = 0.01;  sample sd = √(((0.01)² + (0.01)²) / 1) = 0.0141421
        sharpe = 0.01 / 0.0141421 × 2 = √2
        """
        got = sharpe_ratio(np.array([0.02, 0.00]), periods_per_year=4)
        assert got == pytest.approx(math.sqrt(2))

    def test_zero_mean_is_zero_sharpe(self) -> None:
        assert sharpe_ratio(np.array([0.01, -0.01, 0.01, -0.01])) == pytest.approx(0.0)

    def test_flat_returns_score_zero_not_infinity(self) -> None:
        """A strategy that never traded has no risk to be compensated for.
        Reporting infinity would put it at the top of every ranking."""
        assert sharpe_ratio(np.array([0.0, 0.0, 0.0])) == 0.0

    def test_losing_strategy_is_negative(self) -> None:
        assert sharpe_ratio(np.array([-0.01, -0.02, -0.01])) < 0

    def test_risk_free_rate_is_annual_and_reduces_sharpe(self) -> None:
        returns = np.array([0.02, 0.00])
        assert sharpe_ratio(returns, risk_free_rate=0.04, periods_per_year=4) < sharpe_ratio(
            returns, periods_per_year=4
        )

    def test_too_short_a_sample_scores_zero(self) -> None:
        assert sharpe_ratio(np.array([0.05])) == 0.0


class TestSortino:
    def test_hand_computed(self) -> None:
        """returns [0.02, -0.01], quarterly (√4 = 2).

        mean = 0.005
        downside deviation = √(mean(0², 0.01²)) = √0.00005 = 0.00707107
        sortino = 0.005 / 0.00707107 × 2 = √2
        """
        got = sortino_ratio(np.array([0.02, -0.01]), periods_per_year=4)
        assert got == pytest.approx(math.sqrt(2))

    def test_upside_volatility_is_not_penalised(self) -> None:
        """The whole point: two series with the same downside but different
        upside score the same under Sortino and differently under Sharpe."""
        calm = np.array([0.01, -0.01, 0.01, -0.01])
        spiky = np.array([0.05, -0.01, 0.05, -0.01])
        assert sortino_ratio(spiky) > sortino_ratio(calm)
        assert sortino_ratio(spiky) > sharpe_ratio(spiky)

    def test_no_downside_is_infinite(self) -> None:
        assert sortino_ratio(np.array([0.01, 0.02, 0.03])) == float("inf")

    def test_no_downside_and_no_gain_is_zero(self) -> None:
        assert sortino_ratio(np.array([0.0, 0.0, 0.0])) == 0.0


class TestDrawdown:
    def test_hand_computed_peak_and_trough(self) -> None:
        """[100, 120, 90, 110, 80, 100] — worst fall is 120 → 80 = -1/3."""
        worst, peak, trough = max_drawdown(np.array([100.0, 120.0, 90.0, 110.0, 80.0, 100.0]))
        assert worst == pytest.approx(-1 / 3)
        assert (peak, trough) == (1, 4)

    def test_peak_is_the_one_the_worst_trough_fell_from(self) -> None:
        """Not the highest peak overall. Here 130 is the high, but the worst
        fall is from it to 60 — and a naive 'highest peak' would still be
        right, so the case that matters is the reverse: a deeper trough after a
        lower peak."""
        worst, peak, trough = max_drawdown(np.array([100.0, 130.0, 120.0, 60.0, 125.0]))
        assert worst == pytest.approx((60 - 130) / 130)
        assert (peak, trough) == (1, 3)

    def test_monotonic_rise_has_no_drawdown(self) -> None:
        assert max_drawdown(np.array([100.0, 110.0, 120.0])) == (0.0, 0, 0)

    def test_empty_curve_is_zero(self) -> None:
        assert max_drawdown(np.array([])) == (0.0, 0, 0)


class TestRatiosOverTrades:
    PNLS: ClassVar[list[Decimal]] = [Decimal(100), Decimal(-50), Decimal(200), Decimal(-50)]

    def test_profit_factor(self) -> None:
        """gross profit 300 ÷ gross loss 100 = 3.0"""
        assert profit_factor(self.PNLS) == pytest.approx(3.0)

    def test_profit_factor_is_infinite_without_losses(self) -> None:
        """Which nearly always means too few trades, not perfection."""
        assert profit_factor([Decimal(10), Decimal(20)]) == float("inf")

    def test_profit_factor_of_nothing_is_zero(self) -> None:
        assert profit_factor([]) == 0.0

    def test_expectancy(self) -> None:
        """(100 - 50 + 200 - 50) / 4 = 50"""
        assert expectancy(self.PNLS) == pytest.approx(50.0)

    def test_expectancy_can_be_negative_with_a_high_win_rate(self) -> None:
        """Nine small wins and one large loss. Win rate 90%, expectancy < 0 —
        the exact case docs/GLOSSARY.md warns about."""
        pnls = [Decimal(10)] * 9 + [Decimal(-200)]
        assert expectancy(pnls) < 0

    def test_calmar(self) -> None:
        assert calmar_ratio(0.2, -0.1) == pytest.approx(2.0)

    def test_calmar_without_drawdown_is_infinite(self) -> None:
        assert calmar_ratio(0.2, 0.0) == float("inf")
        assert calmar_ratio(0.0, 0.0) == 0.0


class TestComputeAll:
    def test_hand_computed_full_set(self) -> None:
        """Equity 100 → 200 over exactly one year at 4 periods per year.

        total_return = 1.0;  years = 4 returns / 4 = 1;  cagr = 2^(1/1) - 1 = 1.0
        """
        metrics = compute_all(
            curve([100, 120, 140, 170, 200]),
            [Decimal(100), Decimal(-50), Decimal(200), Decimal(-50)],
            periods_per_year=4,
        )
        assert metrics.total_return == pytest.approx(1.0)
        assert metrics.cagr == pytest.approx(1.0)
        assert metrics.max_drawdown == 0.0
        assert metrics.num_trades == 4
        assert metrics.win_rate == pytest.approx(0.5)
        assert metrics.profit_factor == pytest.approx(3.0)
        assert metrics.expectancy == pytest.approx(50.0)
        assert metrics.avg_win == pytest.approx(150.0)
        assert metrics.avg_loss == pytest.approx(-50.0)
        assert metrics.largest_win == pytest.approx(200.0)
        assert metrics.largest_loss == pytest.approx(-50.0)

    def test_drawdown_duration_uses_real_dates(self) -> None:
        """Peak on day 1, trough on day 4 → 3 days."""
        metrics = compute_all(curve([100, 120, 90, 110, 80, 100]), [], periods_per_year=4)
        assert metrics.max_drawdown == pytest.approx(-1 / 3)
        assert metrics.max_drawdown_duration_days == 3

    def test_drawdown_duration_falls_back_without_dates(self) -> None:
        """The curve is typed as carrying opaque timestamps, so this has to
        survive them not being dates at all."""
        opaque: list[tuple[object, Decimal]] = [
            (object(), Decimal(str(v))) for v in [100, 120, 90, 110, 80, 100]
        ]
        # At 252 periods a year the fallback converts periods to trading days
        # one for one, so it lands on the same answer the dates would give.
        metrics = compute_all(opaque, [], periods_per_year=252)
        assert metrics.max_drawdown_duration_days == 3

    def test_cagr_is_not_extrapolated_from_a_short_sample(self) -> None:
        """Five daily bars up 10% is not a 900%-a-year strategy. Annualising a
        fortnight is how a backtest reports an impossible number."""
        metrics = compute_all(curve([100, 102, 104, 106, 110]), [])
        assert metrics.total_return == pytest.approx(0.10)
        assert metrics.cagr > 0  # annualised, but from a real elapsed fraction
        assert metrics.cagr == pytest.approx((1.10) ** (252 / 4) - 1)

    def test_wiped_out_account_reports_no_growth_rate(self) -> None:
        """Ending at zero has no annualisable growth rate — and (0)^(1/y) would
        be 0, quietly reporting -100% a year as though it were a rate."""
        metrics = compute_all(curve([100, 50, 0]), [Decimal(-100)])
        assert metrics.total_return == pytest.approx(-1.0)
        assert metrics.cagr == 0.0
        assert metrics.max_drawdown == pytest.approx(-1.0)

    def test_empty_inputs_do_not_raise(self) -> None:
        metrics = compute_all([], [])
        assert metrics.num_trades == 0
        assert metrics.total_return == 0.0
        assert metrics.sharpe == 0.0

    def test_supplied_trade_shape_is_passed_through(self) -> None:
        metrics = compute_all(
            curve([100, 110]),
            [Decimal(10)],
            avg_holding_period_hours=48.0,
            exposure_pct=0.25,
            turnover=1.5,
        )
        assert metrics.avg_holding_period_hours == 48.0
        assert metrics.exposure_pct == 0.25
        assert metrics.turnover == 1.5

    def test_unsupplied_trade_shape_is_zero(self) -> None:
        """0.0 means "not supplied" here, which is why the caller that has the
        trades is the one that passes them."""
        metrics = compute_all(curve([100, 110]), [Decimal(10)])
        assert metrics.avg_holding_period_hours == 0.0
        assert metrics.exposure_pct == 0.0
        assert metrics.turnover == 0.0

    def test_to_dict_round_trips_every_field(self) -> None:
        metrics = compute_all(curve([100, 110]), [Decimal(10)])
        as_dict = metrics.to_dict()
        assert set(as_dict) == set(PerformanceMetrics.__slots__)
        assert as_dict["num_trades"] == 1
