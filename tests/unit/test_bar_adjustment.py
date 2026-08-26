"""`Bar.adjusted()` — moving a candle into split/dividend-adjusted space.

The conversion CLAUDE.md §5 is about. Raw closes are the prices as traded, so a
corporate action lands in them as a discontinuity: GE's 1:8 reverse split
octupled its price overnight on 2021-08-02, and a backtest holding a fixed share
count through that books a 700% gain that never happened. These pin the
arithmetic that removes it, and the invariants a caller is entitled to assume —
above all that the whole candle moves together, because a run that marks at an
adjusted close and fills at a raw open is wrong by the split factor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from atp_core.domain import Bar, Timeframe

TS = datetime(2021, 7, 30, tzinfo=UTC)


def bar(
    *,
    open_: str = "12.40",
    high: str = "12.90",
    low: str = "12.30",
    close: str = "12.80",
    volume: str = "80000000",
    adj_close: str | None = "102.40",
    vwap: str | None = None,
) -> Bar:
    """GE on the last session before its 1:8 reverse split, near enough.

    Raw close 12.80; the adjusted close is eight times that, because every
    later bar is quoted on the post-split basis and the series has to be
    continuous across the action to be a return series at all.
    """
    return Bar(
        symbol="GE",
        ts=TS,
        timeframe=Timeframe.D1,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        adj_close=None if adj_close is None else Decimal(adj_close),
        vwap=None if vwap is None else Decimal(vwap),
    )


class TestTheWholeCandleMoves:
    def test_a_reverse_split_scales_every_price_by_the_same_factor(self) -> None:
        """Factor 102.40 / 12.80 = 8, applied to open, high, low and close.

        Hand-computed rather than derived from the code: 12.40 → 99.20,
        12.90 → 103.20, 12.30 → 98.40, 12.80 → 102.40.
        """
        adjusted = bar().adjusted()

        assert adjusted.open == Decimal("99.20")
        assert adjusted.high == Decimal("103.20")
        assert adjusted.low == Decimal("98.40")
        assert adjusted.close == Decimal("102.40")

    def test_a_forward_split_scales_them_down(self) -> None:
        """AAPL's 4:1, the mirror case. Raw 400 with an adjusted close of 100 is
        factor 0.25, and the candle divides by four rather than crashing 75%."""
        adjusted = bar(open_="404", high="408", low="396", close="400", adj_close="100").adjusted()

        assert adjusted.open == Decimal("101")
        assert adjusted.high == Decimal("102")
        assert adjusted.low == Decimal("99")
        assert adjusted.close == Decimal("100")

    def test_volume_moves_the_other_way_so_notional_is_unchanged(self) -> None:
        """A 1:8 reverse split leaves an eighth of the shares at eight times the
        price. Dividing volume by the factor keeps the bar's traded notional
        fixed, which is what the engine's participation cap is a fraction of."""
        original = bar()
        adjusted = original.adjusted()

        assert adjusted.volume == Decimal("10000000")  # 80,000,000 / 8
        assert adjusted.close * adjusted.volume == original.close * original.volume

    def test_vwap_is_a_price_and_scales_with_the_rest(self) -> None:
        assert bar(vwap="12.60").adjusted().vwap == Decimal("100.80")

    def test_no_vwap_stays_absent(self) -> None:
        assert bar(vwap=None).adjusted().vwap is None

    def test_identity_fields_are_untouched(self) -> None:
        adjusted = bar().adjusted()
        assert adjusted.symbol == "GE"
        assert adjusted.ts == TS
        assert adjusted.timeframe is Timeframe.D1


class TestIdempotence:
    """Adjusting twice must not scale twice.

    The engine converts a whole series at the top of `run`, and a caller that
    has already done so — or a symbol that never had a corporate action, whose
    stored `adj_close` equals its close — has to pass through unchanged. Without
    this the second pass would multiply by a factor of one only if the first
    pass happened to land exactly on `adj_close`, which `Decimal` division does
    not guarantee.
    """

    def test_an_adjusted_bar_reports_itself_as_one(self) -> None:
        assert not bar().is_adjusted
        assert bar().adjusted().is_adjusted

    def test_adjusting_an_adjusted_bar_is_a_no_op(self) -> None:
        once = bar().adjusted()
        assert once.adjusted() == once

    def test_a_symbol_with_no_corporate_actions_is_returned_as_is(self) -> None:
        """`adj_close == close` is the ordinary case for most symbols most of
        the time, and it must not churn the candle through a factor of one."""
        untouched = bar(adj_close="12.80")
        assert untouched.is_adjusted
        assert untouched.adjusted() is untouched

    def test_a_bar_with_no_adj_close_is_not_adjusted(self) -> None:
        assert not bar(adj_close=None).is_adjusted


class TestItRefusesRatherThanGuesses:
    def test_a_missing_adj_close_raises_and_names_the_fix(self) -> None:
        """Defaulting to the raw close is the silent fallback this method exists
        to remove — it completes, and the only trace is a fictional return."""
        with pytest.raises(ValueError, match="no adj_close"):
            bar(adj_close=None).adjusted()

    def test_the_message_says_how_to_get_the_data(self) -> None:
        with pytest.raises(ValueError, match="raw-only"):
            bar(adj_close=None).adjusted()

    @pytest.mark.parametrize("close", ["0", "-1.50"])
    def test_a_non_positive_close_has_no_factor(self, close: str) -> None:
        with pytest.raises(ValueError, match="adjustment factor is undefined"):
            bar(open_=close, high=close, low=close, close=close).adjusted()


class TestTheResultIsAlwaysAValidBar:
    """`__post_init__` enforces `low <= open,close <= high`, so a conversion
    that broke the ordering would raise from inside the engine's first loop.

    Multiplying by a positive constant preserves order in exact arithmetic; the
    claim worth testing is that it survives `Decimal`'s rounding to 28
    significant digits at prices and factors that do not divide cleanly.
    """

    @settings(max_examples=300)
    @given(
        low=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5000"), places=4),
        span=st.decimals(min_value=Decimal("0"), max_value=Decimal("500"), places=4),
        within=st.decimals(min_value=Decimal("0"), max_value=Decimal("1"), places=6),
        factor=st.decimals(min_value=Decimal("0.001"), max_value=Decimal("1000"), places=6),
    )
    def test_ordering_survives_any_factor(
        self, low: Decimal, span: Decimal, within: Decimal, factor: Decimal
    ) -> None:
        high = low + span
        inside = low + span * within
        source = Bar(
            symbol="TEST",
            ts=TS,
            timeframe=Timeframe.D1,
            open=inside,
            high=high,
            low=low,
            close=inside,
            volume=Decimal("1000"),
            adj_close=inside * factor,
        )

        adjusted = source.adjusted()  # raises from __post_init__ if order broke

        assert adjusted.low <= adjusted.open <= adjusted.high
        assert adjusted.low <= adjusted.close <= adjusted.high

    @settings(max_examples=200)
    @given(
        close=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5000"), places=4),
        factor=st.decimals(min_value=Decimal("0.001"), max_value=Decimal("1000"), places=6),
    )
    def test_the_adjusted_close_is_the_vendors_figure(
        self, close: Decimal, factor: Decimal
    ) -> None:
        """To within `Decimal`'s precision. The result carries its own close as
        `adj_close` rather than the vendor's, so that `is_adjusted` holds
        exactly; the two must still agree to far more places than money needs.
        """
        target = close * factor
        adjusted = bar(
            open_=str(close),
            high=str(close),
            low=str(close),
            close=str(close),
            adj_close=str(target),
        ).adjusted()

        assert adjusted.is_adjusted
        assert abs(adjusted.close - target) <= abs(target) * Decimal("1e-25")
