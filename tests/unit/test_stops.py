"""Stop management.

Three things get tested here because all three are silent when wrong.

**Sign.** A long's stop sits below entry and a short's above. Invert it and the
position stops out instantly or never — neither raises, and both look like a
strategy problem rather than an arithmetic one. Every level test runs both sides.

**Monotonicity.** A trailing stop must never move away from price. Widening one
converts a planned small loss into an unplanned large one, and it always feels
justified at the time (docs/RISK.md).

**Which price triggers it.** A bar that dipped to the stop and recovered did hit
it. Comparing against the close pretends otherwise and inflates every backtest
that uses stops.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from atp_core.domain import Bar, Position, Side, Timeframe
from atp_core.domain.enums import StopType
from atp_core.risk.stops import StopConfig, StopManager

TS = datetime(2024, 1, 2, tzinfo=UTC)
D = Decimal


def bar(high: float, low: float, close: float | None = None, index: int = 0) -> Bar:
    body = close if close is not None else (high + low) / 2
    return Bar(
        symbol="TEST",
        ts=TS + timedelta(days=index),
        timeframe=Timeframe.D1,
        open=D(str(body)),
        high=D(str(high)),
        low=D(str(low)),
        close=D(str(body)),
        volume=D(1_000_000),
    )


def position(qty: float = 100, entry: float = 100, stop: float | None = None) -> Position:
    return Position(
        symbol="TEST",
        qty=D(str(qty)),
        avg_entry_price=D(str(entry)),
        stop_loss_price=D(str(stop)) if stop is not None else None,
    )


manager = StopManager()


class TestInitialLevels:
    @pytest.mark.parametrize(
        ("config", "atr", "long_level", "short_level"),
        [
            (StopConfig(StopType.FIXED_PCT, value=D("0.02")), None, D("98.00"), D("102.00")),
            (StopConfig(StopType.FIXED_AMOUNT, value=D(5)), None, D(95), D(105)),
            (StopConfig(StopType.ATR, multiplier=D(2)), D(3), D(94), D(106)),
            (StopConfig(StopType.CHANDELIER, multiplier=D(2)), D(3), D(94), D(106)),
            (StopConfig(StopType.TRAILING_PCT, value=D("0.05")), None, D("95.00"), D("105.00")),
        ],
    )
    def test_long_below_entry_short_above(
        self, config: StopConfig, atr: Decimal | None, long_level: Decimal, short_level: Decimal
    ) -> None:
        assert manager.initial_stop(D(100), Side.BUY, config, atr) == long_level
        assert manager.initial_stop(D(100), Side.SELL, config, atr) == short_level

    def test_chandelier_starts_where_an_atr_stop_does(self) -> None:
        """Its anchor is the highest high since entry, which at entry is the
        entry bar. They diverge on the first ratchet, not before."""
        atr_stop = manager.initial_stop(
            D(100), Side.BUY, StopConfig(StopType.ATR, multiplier=D(3)), D(2)
        )
        chandelier = manager.initial_stop(
            D(100), Side.BUY, StopConfig(StopType.CHANDELIER, multiplier=D(3)), D(2)
        )
        assert atr_stop == chandelier == D(94)

    def test_a_time_stop_has_no_price(self) -> None:
        with pytest.raises(ValueError, match="no price level"):
            manager.initial_stop(D(100), Side.BUY, StopConfig(StopType.TIME, bars=5))

    def test_a_percentage_over_one_would_put_the_stop_below_zero(self) -> None:
        with pytest.raises(ValueError, match="not a price"):
            manager.initial_stop(D(100), Side.BUY, StopConfig(StopType.FIXED_PCT, value=D("1.5")))

    def test_a_multiplier_wide_enough_to_swamp_the_entry_is_refused(self) -> None:
        """2 x an ATR of 60 on a $100 entry lands at -20. A stop below zero can
        never be reached, so the position is unprotected while looking guarded —
        which is the whole failure mode worth erroring on."""
        with pytest.raises(ValueError, match="not a price"):
            manager.initial_stop(D(100), Side.BUY, StopConfig(StopType.ATR, multiplier=D(2)), D(60))

    @pytest.mark.parametrize(
        ("config", "atr", "match"),
        [
            (StopConfig(StopType.FIXED_PCT), None, "need `value`"),
            (StopConfig(StopType.ATR), D(3), "need `multiplier`"),
            (StopConfig(StopType.ATR, multiplier=D(2)), None, "positive ATR"),
            (StopConfig(StopType.ATR, multiplier=D(2)), D(0), "positive ATR"),
            (StopConfig(StopType.FIXED_PCT, value=D(0)), None, "must be positive"),
        ],
    )
    def test_missing_or_degenerate_inputs_raise(
        self, config: StopConfig, atr: Decimal | None, match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            manager.initial_stop(D(100), Side.BUY, config, atr)


class TestTrailing:
    CONFIG = StopConfig(StopType.TRAILING_PCT, value=D("0.10"))

    def test_ratchets_up_on_a_new_high(self) -> None:
        pos = position(entry=100, stop=90)
        assert manager.update_trailing(pos, bar(high=120, low=110), self.CONFIG) == D(108)
        assert pos.stop_loss_price == D(108)
        assert pos.high_water_mark == D(120)

    def test_never_moves_down(self) -> None:
        """The invariant. A lower high after a higher one leaves the stop where
        it was — returning None rather than a level, so a caller cannot assign
        a widened stop by accident."""
        pos = position(entry=100, stop=90)
        manager.update_trailing(pos, bar(high=120, low=110), self.CONFIG)
        assert manager.update_trailing(pos, bar(high=112, low=105), self.CONFIG) is None
        assert pos.stop_loss_price == D(108)

    def test_tracks_the_bar_high_not_the_close(self) -> None:
        """A spike that closed back down still ratcheted the stop. Tracking
        closes would leave that gain unprotected."""
        pos = position(entry=100, stop=90)
        level = manager.update_trailing(pos, bar(high=150, low=95, close=100), self.CONFIG)
        assert level == D(135)  # 150 x 0.9, not 100 x 0.9

    def test_a_short_ratchets_downward(self) -> None:
        pos = position(qty=-100, entry=100, stop=110)
        assert manager.update_trailing(pos, bar(high=95, low=80), self.CONFIG) == D(88)
        assert pos.high_water_mark == D(80)
        # And never back up.
        assert manager.update_trailing(pos, bar(high=92, low=85), self.CONFIG) is None
        assert pos.stop_loss_price == D(88)

    def test_chandelier_hangs_off_the_highest_high(self) -> None:
        """120 highest-high, 3 x ATR(4) = 12 → 108."""
        pos = position(entry=100, stop=90)
        config = StopConfig(StopType.CHANDELIER, multiplier=D(3))
        assert manager.update_trailing(pos, bar(high=120, low=110), config, D(4)) == D(108)

    def test_chandelier_needs_an_atr(self) -> None:
        with pytest.raises(ValueError, match="positive ATR"):
            manager.update_trailing(
                position(stop=90),
                bar(high=120, low=110),
                StopConfig(StopType.CHANDELIER, multiplier=D(3)),
            )

    @pytest.mark.parametrize("stop_type", [StopType.FIXED_PCT, StopType.FIXED_AMOUNT, StopType.ATR])
    def test_a_stop_that_does_not_trail_is_left_alone(self, stop_type: StopType) -> None:
        """Not broken — just not the kind of stop that moves. Ratcheting one
        would be second-guessing a level the strategy chose deliberately."""
        pos = position(entry=100, stop=95)
        config = StopConfig(stop_type, value=D("0.05"), multiplier=D(2))
        assert manager.update_trailing(pos, bar(high=150, low=140), config, D(3)) is None
        assert pos.stop_loss_price == D(95)

    def test_a_flat_position_has_nothing_to_trail(self) -> None:
        assert manager.update_trailing(position(qty=0), bar(high=120, low=110), self.CONFIG) is None

    @settings(max_examples=200)
    @given(
        st.lists(
            st.tuples(
                st.decimals(min_value=1, max_value=500, places=2),
                st.decimals(min_value=1, max_value=500, places=2),
            ),
            min_size=1,
            max_size=40,
        )
    )
    def test_a_long_trailing_stop_never_decreases_over_any_price_path(
        self, path: list[tuple[Decimal, Decimal]]
    ) -> None:
        """docs/TESTING.md states this as a property, and it is the one worth
        stating that way: no sequence of bars, in any order, may lower it."""
        pos = position(entry=100, stop=90)
        levels: list[Decimal] = []
        for index, (a, b) in enumerate(path):
            high, low = max(a, b), min(a, b)
            manager.update_trailing(pos, bar(float(high), float(low), index=index), self.CONFIG)
            assert pos.stop_loss_price is not None
            levels.append(pos.stop_loss_price)

        assert levels == sorted(levels)
        assert min(levels) >= D(90)


class TestTriggering:
    def test_a_long_is_stopped_by_the_low_even_if_the_close_recovered(self) -> None:
        """The bar dipped through the stop. In reality that filled; using the
        close pretends you were never stopped out."""
        pos = position(entry=100, stop=95)
        assert manager.should_trigger(pos, bar(high=105, low=94, close=104))

    def test_a_long_survives_a_bar_that_stayed_above(self) -> None:
        assert not manager.should_trigger(position(entry=100, stop=95), bar(high=105, low=96))

    def test_a_short_is_stopped_by_the_high(self) -> None:
        pos = position(qty=-100, entry=100, stop=105)
        assert manager.should_trigger(pos, bar(high=106, low=99, close=100))
        assert not manager.should_trigger(pos, bar(high=104, low=99))

    def test_touching_the_stop_exactly_counts(self) -> None:
        assert manager.should_trigger(position(entry=100, stop=95), bar(high=105, low=95))

    def test_no_stop_no_trigger(self) -> None:
        assert not manager.should_trigger(position(entry=100), bar(high=105, low=1))

    def test_a_flat_position_cannot_be_stopped(self) -> None:
        assert not manager.should_trigger(position(qty=0, stop=95), bar(high=105, low=1))


class TestTakeProfit:
    def test_mirrors_the_entry_on_the_opposite_side_from_the_stop(self) -> None:
        config = StopConfig(StopType.FIXED_PCT, value=D("0.06"))
        assert manager.take_profit_level(D(100), Side.BUY, config) == D("106.00")
        assert manager.take_profit_level(D(100), Side.SELL, config) == D("94.00")

    def test_fixed_amount(self) -> None:
        config = StopConfig(StopType.FIXED_AMOUNT, value=D(8))
        assert manager.take_profit_level(D(100), Side.BUY, config) == D(108)
        assert manager.take_profit_level(D(100), Side.SELL, config) == D(92)

    @pytest.mark.parametrize(
        "stop_type", [StopType.ATR, StopType.TRAILING_PCT, StopType.CHANDELIER, StopType.TIME]
    )
    def test_a_target_that_is_not_a_distance_from_entry_is_refused(
        self, stop_type: StopType
    ) -> None:
        """Refused rather than quietly None: a take-profit that does not exist
        is a position with no upside exit, and silence is how that ships."""
        with pytest.raises(ValueError, match="fixed distance from entry"):
            manager.take_profit_level(
                D(100), Side.BUY, StopConfig(stop_type, value=D("0.05"), multiplier=D(2), bars=5)
            )

    def test_a_short_target_below_zero_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a price"):
            manager.take_profit_level(
                D(100), Side.SELL, StopConfig(StopType.FIXED_PCT, value=D("1.5"))
            )


class TestTimeStop:
    def test_exits_once_the_bars_have_elapsed(self) -> None:
        config = StopConfig(StopType.TIME, bars=5)
        assert not manager.time_exit_due(4, config)
        assert manager.time_exit_due(5, config)
        assert manager.time_exit_due(9, config)

    def test_other_stop_types_never_time_out(self) -> None:
        assert not manager.time_exit_due(1_000, StopConfig(StopType.FIXED_PCT, value=D("0.02")))

    def test_a_time_stop_without_bars_is_a_configuration_error(self) -> None:
        with pytest.raises(ValueError, match="positive `bars`"):
            manager.time_exit_due(3, StopConfig(StopType.TIME))
