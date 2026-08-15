"""Position accounting.

The most important tests in the suite: every P&L number the platform reports is
computed from this, and an error here is invisible — nothing crashes, the
numbers are simply wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from atp_core.domain.order import Fill
from atp_core.domain.position import Position

TS = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


def fill(qty: str | Decimal, price: str | Decimal, fee: str | Decimal = "0") -> Fill:
    """A fill of unsigned `qty` at `price`. Direction is the caller's business."""
    return Fill(order_id="o-1", ts=TS, qty=Decimal(qty), price=Decimal(price), fee=Decimal(fee))


def buy(pos: Position, qty: str, price: str, fee: str = "0") -> Decimal:
    return pos.apply_fill(fill(qty, price, fee), Decimal(qty))


def sell(pos: Position, qty: str, price: str, fee: str = "0") -> Decimal:
    return pos.apply_fill(fill(qty, price, fee), -Decimal(qty))


class TestApplyFill:
    def test_open_long(self) -> None:
        """Flat + buy → long at the fill price, no realised P&L."""
        pos = Position(symbol="SPY")

        realized = buy(pos, "100", "50")

        assert realized == Decimal(0)
        assert pos.qty == Decimal(100)
        assert pos.avg_entry_price == Decimal(50)
        assert pos.realized_pnl == Decimal(0)
        assert pos.is_long
        assert pos.opened_at == TS

    def test_add_to_long_reaverages_basis(self) -> None:
        """100 @ $50 then 100 @ $60 → 200 @ $55."""
        pos = Position(symbol="SPY")

        buy(pos, "100", "50")
        realized = buy(pos, "100", "60")

        assert realized == Decimal(0)
        assert pos.qty == Decimal(200)
        assert pos.avg_entry_price == Decimal(55)
        assert pos.realized_pnl == Decimal(0)

    def test_partial_reduce_realises_pnl_and_keeps_basis(self) -> None:
        """THE case people get wrong.

        200 @ $50, sell 100 @ $60 → realised $1,000, remaining 100 still at a
        $50 basis. Re-averaging on a reduction silently misstates every
        subsequent trade in that symbol.
        """
        pos = Position(symbol="SPY")
        buy(pos, "200", "50")

        realized = sell(pos, "100", "60")

        assert realized == Decimal(1000)
        assert pos.realized_pnl == Decimal(1000)
        assert pos.qty == Decimal(100)
        assert pos.avg_entry_price == Decimal(50), "basis must not move on a reduction"

    def test_full_close_flattens(self) -> None:
        pos = Position(symbol="SPY")
        buy(pos, "100", "50")

        realized = sell(pos, "100", "60")

        assert realized == Decimal(1000)
        assert pos.realized_pnl == Decimal(1000)
        assert pos.qty == Decimal(0)
        assert pos.is_flat
        assert pos.avg_entry_price == Decimal(0)
        assert pos.unrealized_pnl == Decimal(0)
        assert pos.opened_at is None

    def test_full_close_clears_protective_levels(self) -> None:
        """A stop that outlives its position would arm against the next one."""
        pos = Position(symbol="SPY")
        buy(pos, "100", "50")
        pos.stop_loss_price = Decimal("45")
        pos.take_profit_price = Decimal("70")
        pos.high_water_mark = Decimal("62")

        sell(pos, "100", "60")

        assert pos.stop_loss_price is None
        assert pos.take_profit_price is None
        assert pos.high_water_mark is None

    def test_flip_through_zero(self) -> None:
        """Long 100 @ $50, sell 300 @ $60 → realised $1,000 on the closed 100,
        then short 200 with a $60 basis. Not a single re-average."""
        pos = Position(symbol="SPY")
        buy(pos, "100", "50")

        realized = sell(pos, "300", "60")

        assert realized == Decimal(1000), "only the closed 100 realises P&L"
        assert pos.realized_pnl == Decimal(1000)
        assert pos.qty == Decimal(-200)
        assert pos.is_short
        assert pos.avg_entry_price == Decimal(60), "new side opens at the fill price"
        assert pos.opened_at == TS

    def test_short_side_signs(self) -> None:
        """Shorts profit when price falls. Sign errors here are silent."""
        pos = Position(symbol="SPY")

        opened = sell(pos, "100", "60")
        assert opened == Decimal(0)
        assert pos.qty == Decimal(-100)
        assert pos.avg_entry_price == Decimal(60)
        assert pos.is_short

        # Price falls to 50 — a short is up $10 a share, not down.
        pos.last_price = Decimal(50)
        assert pos.unrealized_pnl == Decimal(1000)

        realized = buy(pos, "100", "50")
        assert realized == Decimal(1000)
        assert pos.realized_pnl == Decimal(1000)
        assert pos.is_flat

    def test_short_loses_when_price_rises(self) -> None:
        pos = Position(symbol="SPY")
        sell(pos, "100", "50")

        realized = buy(pos, "100", "60")

        assert realized == Decimal(-1000)
        assert pos.realized_pnl == Decimal(-1000)

    def test_add_to_short_reaverages_basis(self) -> None:
        """The mirror of the long case: -100 @ $50 then -100 @ $60 → -200 @ $55."""
        pos = Position(symbol="SPY")

        sell(pos, "100", "50")
        sell(pos, "100", "60")

        assert pos.qty == Decimal(-200)
        assert pos.avg_entry_price == Decimal(55)
        assert pos.realized_pnl == Decimal(0)

    def test_fees_reduce_total_pnl(self) -> None:
        pos = Position(symbol="SPY")
        buy(pos, "100", "50", fee="1.25")
        sell(pos, "100", "60", fee="1.75")

        assert pos.realized_pnl == Decimal(1000), "realised P&L is gross of fees"
        assert pos.fees_paid == Decimal("3.00")
        assert pos.total_pnl == Decimal("997.00")

    def test_partial_fill_sequence_closes_cleanly(self) -> None:
        """An order is not binary (CLAUDE.md §5) — a close can arrive in pieces."""
        pos = Position(symbol="SPY")
        buy(pos, "100", "50")

        first = sell(pos, "40", "60")
        second = sell(pos, "60", "55")

        assert first == Decimal(400)
        assert second == Decimal(300)
        assert pos.realized_pnl == Decimal(700)
        assert pos.is_flat


class TestApplyFillRejects:
    """Bad input is a caller bug and must not be absorbed silently."""

    def test_zero_signed_qty_rejected(self) -> None:
        pos = Position(symbol="SPY")
        with pytest.raises(ValueError, match="non-zero"):
            pos.apply_fill(fill("10", "50"), Decimal(0))

    def test_signed_qty_magnitude_must_match_fill(self) -> None:
        pos = Position(symbol="SPY")
        with pytest.raises(ValueError, match="does not match fill qty"):
            pos.apply_fill(fill("10", "50"), Decimal(-25))

    def test_non_positive_fill_qty_rejected(self) -> None:
        pos = Position(symbol="SPY")
        with pytest.raises(ValueError, match="fill qty must be positive"):
            pos.apply_fill(fill("-10", "50"), Decimal(-10))


class TestInvariants:
    """Property tests — see docs/TESTING.md."""

    @settings(max_examples=200)
    @given(
        legs=st.lists(
            st.tuples(
                st.booleans(),  # True → buy
                st.integers(min_value=1, max_value=500),  # qty
                st.integers(min_value=1, max_value=1000),  # price
            ),
            min_size=1,
            max_size=25,
        )
    )
    def test_realised_plus_unrealised_equals_total(self, legs: list[tuple[bool, int, int]]) -> None:
        """Over any sequence of fills.

        Computed directly, P&L is the net cash the fills moved plus the market
        value of whatever is still open. That must equal what the position
        reports, however the legs opened, reduced and flipped along the way.
        """
        pos = Position(symbol="SPY")
        cash = Decimal(0)

        for is_buy, qty, price in legs:
            signed = Decimal(qty) if is_buy else -Decimal(qty)
            pos.apply_fill(fill(Decimal(qty), Decimal(price)), signed)
            # Buying spends cash, selling raises it.
            cash -= signed * Decimal(price)

        assert pos.last_price is not None
        direct = cash + pos.qty * pos.last_price
        reported = pos.realized_pnl + pos.unrealized_pnl

        # Not exact: re-averaging the basis divides, and Decimal division rounds
        # to context precision (28 significant digits). The residual is many
        # orders of magnitude below a cent at these notionals.
        assert abs(direct - reported) < Decimal("1e-8")

    @settings(max_examples=200)
    @given(
        buy_lots=st.lists(st.integers(min_value=1, max_value=200), min_size=1, max_size=10),
        sell_split=st.lists(st.integers(min_value=1, max_value=100), min_size=1, max_size=10),
        entry=st.integers(min_value=1, max_value=1000),
        exit_=st.integers(min_value=1, max_value=1000),
    )
    def test_round_trip_pnl_is_path_independent(
        self, buy_lots: list[int], sell_split: list[int], entry: int, exit_: int
    ) -> None:
        """Buy at X, sell at Y: total P&L is the same however it was scaled
        in and out."""
        total = sum(buy_lots)
        # Rescale the sell split to close exactly the quantity bought.
        weights_total = sum(sell_split)
        sell_lots = [lot * total // weights_total for lot in sell_split]
        sell_lots[-1] += total - sum(sell_lots)
        sell_lots = [lot for lot in sell_lots if lot > 0]

        pos = Position(symbol="SPY")
        for lot in buy_lots:
            buy(pos, str(lot), str(entry))
        for lot in sell_lots:
            sell(pos, str(lot), str(exit_))

        assert pos.is_flat
        # Every buy is at the same price, so the basis is exactly `entry` no
        # matter how the lots were sliced — the round trip is exact here.
        assert pos.realized_pnl == Decimal(total) * (Decimal(exit_) - Decimal(entry))
