"""Position accounting.

The most important tests in the suite: every P&L number the platform reports is
computed from this, and an error here is invisible — nothing crashes, the
numbers are simply wrong.
"""

from __future__ import annotations

import pytest


class TestApplyFill:
    def test_open_long(self) -> None:
        """Flat + buy → long at the fill price, no realised P&L."""
        pytest.skip("TODO: implement Position.apply_fill")

    def test_add_to_long_reaverages_basis(self) -> None:
        """100 @ $50 then 100 @ $60 → 200 @ $55."""
        pytest.skip("TODO")

    def test_partial_reduce_realises_pnl_and_keeps_basis(self) -> None:
        """THE case people get wrong.

        200 @ $50, sell 100 @ $60 → realised $1,000, remaining 100 still at a
        $50 basis. Re-averaging on a reduction silently misstates every
        subsequent trade in that symbol.
        """
        pytest.skip("TODO")

    def test_full_close_flattens(self) -> None:
        pytest.skip("TODO")

    def test_flip_through_zero(self) -> None:
        """Long 100 @ $50, sell 300 @ $60 → realised $1,000 on the closed 100,
        then short 200 with a $60 basis. Not a single re-average."""
        pytest.skip("TODO")

    def test_short_side_signs(self) -> None:
        """Shorts profit when price falls. Sign errors here are silent."""
        pytest.skip("TODO")

    def test_fees_reduce_total_pnl(self) -> None:
        pytest.skip("TODO")


class TestInvariants:
    """Property tests — see docs/TESTING.md."""

    def test_realised_plus_unrealised_equals_total(self) -> None:
        """Over any sequence of fills."""
        pytest.skip("TODO: hypothesis")

    def test_round_trip_pnl_is_path_independent(self) -> None:
        """Buy at X, sell at Y: total P&L is the same however it was scaled
        in and out."""
        pytest.skip("TODO: hypothesis")
