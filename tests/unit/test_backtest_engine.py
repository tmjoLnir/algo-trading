"""Backtest engine — the lookahead guarantees.

If these pass and the engine is still wrong, every backtest the platform
produces is fiction. Treat a failure here as a correctness emergency, never as
a test to relax.
"""

from __future__ import annotations

import pytest


class TestNoLookahead:
    def test_context_cannot_see_future_bars(self) -> None:
        """`ctx.history()` never returns a bar closing after the current one."""
        pytest.skip("TODO")

    def test_signal_fills_at_next_bar_open(self) -> None:
        """Not at the signal bar's close — you cannot trade at a price you only
        know once the bar is over. This single rule separates a real backtest
        from a flattering one."""
        pytest.skip("TODO")

    def test_warmup_signals_discarded(self) -> None:
        pytest.skip("TODO")


class TestFills:
    def test_limit_fills_only_if_range_reached_it(self) -> None:
        pytest.skip("TODO")

    def test_stop_fills_worse_than_trigger(self) -> None:
        pytest.skip("TODO")

    def test_volume_participation_capped(self) -> None:
        """Cannot buy 10x the bar's volume."""
        pytest.skip("TODO")

    def test_stop_assumed_first_when_bar_spans_stop_and_target(self) -> None:
        """The pessimistic reading is the only honest one at bar resolution."""
        pytest.skip("TODO")


class TestOrdering:
    def test_stops_checked_before_new_signals(self) -> None:
        """Mirrors the live runner. Reordering lets a strategy exit at a price
        it could not have obtained."""
        pytest.skip("TODO")


class TestAgainstKnownFixture:
    def test_hand_computed_20_bar_scenario(self) -> None:
        """A fixture whose expected P&L was worked out by hand. The only real
        defence against an engine that is self-consistently wrong."""
        pytest.skip("TODO")
