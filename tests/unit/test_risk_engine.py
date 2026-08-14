"""Risk engine — each rule blocks what it should and allows what it should."""

from __future__ import annotations

import pytest


class TestRiskRules:
    def test_kill_switch_blocks_everything(self) -> None:
        pytest.skip("TODO")

    def test_max_position_counts_existing_holding(self) -> None:
        """Three 4% orders must not become a 12% position."""
        pytest.skip("TODO")

    def test_gross_exposure_counts_shorts(self) -> None:
        """A market-neutral book still consumes buying power."""
        pytest.skip("TODO")

    def test_daily_loss_limit_blocks_entries(self) -> None:
        pytest.skip("TODO")

    def test_daily_loss_limit_ALLOWS_exits(self) -> None:
        """Critical: blocking an exit traps you in a losing position and turns
        a bad day into an unbounded one."""
        pytest.skip("TODO")

    def test_rate_limit_stops_runaway_loop(self) -> None:
        pytest.skip("TODO")

    def test_stale_quote_blocks_order(self) -> None:
        pytest.skip("TODO")

    def test_rule_that_cannot_evaluate_denies(self) -> None:
        """Default-closed: an unpriced position is when you least want to trade."""
        pytest.skip("TODO")


class TestPositionSizing:
    def test_risk_pct_equalises_risk_not_notional(self) -> None:
        """$100k equity, 1% risk: $50 entry/$48 stop → 500 shares;
        $50 entry/$35 stop → 66 shares. Both lose $1,000 if stopped."""
        pytest.skip("TODO")

    def test_risk_pct_without_stop_raises(self) -> None:
        """Undefined — must raise rather than silently defaulting."""
        pytest.skip("TODO")
