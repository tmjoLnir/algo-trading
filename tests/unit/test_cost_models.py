"""Commission and slippage.

Expected values are worked out from the published rates in the docstrings, not
captured from a run. A cost model that is quietly too cheap is the same failure
as a backtest that fills at the wrong price: it does not error, it just makes a
strategy look like it works (docs/BACKTESTING.md §5).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from atp_core.backtest.costs import (
    CompositeCostModel,
    PerShareCostModel,
    SpreadSlippageModel,
    ZeroCostModel,
    alpaca_equities_default,
)
from atp_core.domain import Bar, Fill, Order, Side, Timeframe

TS = datetime(2024, 1, 2, tzinfo=UTC)


def order(side: Side = Side.BUY, qty: int = 100) -> Order:
    return Order(symbol="TEST", side=side, qty=Decimal(qty))


def a_bar(volume: float = 1_000_000, close: float = 50.0) -> Bar:
    return Bar(
        symbol="TEST",
        ts=TS,
        timeframe=Timeframe.D1,
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
    )


def fill_to(o: Order, qty: int, price: float = 50.0) -> None:
    """Advance an order's filled quantity, so the next fee sees a real total."""
    o.apply_fill(Fill(order_id=o.id, ts=TS, qty=Decimal(qty), price=Decimal(str(price))))


class TestPerShareCommission:
    def test_minimum_applies_when_per_share_is_below_it(self) -> None:
        """100 × $0.005 = $0.50, floored to the $1.00 minimum."""
        assert PerShareCostModel().commission(order(), Decimal(50), Decimal(100)) == Decimal("1.00")

    def test_per_share_applies_once_it_exceeds_the_minimum(self) -> None:
        """1,000 × $0.005 = $5.00."""
        model = PerShareCostModel()
        assert model.commission(order(qty=1000), Decimal(50), Decimal(1000)) == Decimal("5.00")

    def test_buys_pay_no_regulatory_fees(self) -> None:
        """SEC and TAF are sell-side only. A buy pays commission alone."""
        model = PerShareCostModel()
        buy = model.commission(order(Side.BUY, 1000), Decimal(50), Decimal(1000))
        sell = model.commission(order(Side.SELL, 1000), Decimal(50), Decimal(1000))
        assert buy == Decimal("5.00")
        assert sell > buy

    def test_sell_adds_sec_and_taf(self) -> None:
        """1,000 shares at $50, notional $50,000:

        commission  1,000 × 0.005              = 5.000
        SEC         50,000 × 0.0000278 = 1.39  = 1.390
        TAF         1,000 × 0.000166           = 0.166
                                                 ------
                                                 6.556  → 6.56
        """
        model = PerShareCostModel()
        assert model.commission(order(Side.SELL, 1000), Decimal(50), Decimal(1000)) == Decimal(
            "6.56"
        )

    def test_sec_fee_rounds_up_to_the_penny(self) -> None:
        """100 × $50 = $5,000 notional → 0.0000278 × 5,000 = 0.139, up to 0.14.

        commission  max(0.50, 1.00) = 1.00
        SEC                           0.14
        TAF         100 × 0.000166  = 0.0166
                                      ------
                                      1.1566 → 1.16
        """
        model = PerShareCostModel()
        assert model.commission(order(Side.SELL), Decimal(50), Decimal(100)) == Decimal("1.16")

    def test_taf_is_capped_per_order(self) -> None:
        """100,000 × 0.000166 = $16.60, over the $8.30 ceiling.

        commission  100,000 × 0.005                 = 500.00
        SEC         5,000,000 × 0.0000278           = 139.00
        TAF         capped                          =   8.30
                                                      -------
                                                      647.30
        """
        model = PerShareCostModel()
        got = model.commission(order(Side.SELL, 100_000), Decimal(50), Decimal(100_000))
        assert got == Decimal("647.30")

    def test_partial_fills_cost_the_same_as_one_fill(self) -> None:
        """The minimum belongs to the order, not to each piece of it. The
        engine's volume cap makes this the ordinary case, not a corner."""
        model = PerShareCostModel()

        whole = model.commission(order(qty=200), Decimal(50), Decimal(200))

        split = order(qty=200)
        first = model.commission(split, Decimal(50), Decimal(100))
        fill_to(split, 100)
        second = model.commission(split, Decimal(50), Decimal(100))

        assert whole == Decimal("1.00")
        assert first + second == whole

    def test_taf_cap_survives_partial_fills(self) -> None:
        """Once the cap is reached, later fills add no more TAF."""
        model = PerShareCostModel(per_share=Decimal(0), minimum=Decimal(0))

        whole = model.commission(order(Side.SELL, 100_000), Decimal(50), Decimal(100_000))

        split = order(Side.SELL, 100_000)
        total = Decimal(0)
        for _ in range(4):
            total += model.commission(split, Decimal(50), Decimal(25_000))
            fill_to(split, 25_000)

        # SEC rounds up per execution, so four fills can round up to four times;
        # the difference is bounded by three pennies and nothing else moves.
        assert abs(total - whole) <= Decimal("0.03")

    def test_alpaca_default_is_commission_free_but_still_pays_regulators(self) -> None:
        """Alpaca charges no commission. The SEC and FINRA still do."""
        model = alpaca_equities_default()
        assert model.commission(order(Side.BUY, 1000), Decimal(50), Decimal(1000)) == Decimal("0")
        assert model.commission(order(Side.SELL, 1000), Decimal(50), Decimal(1000)) > 0

    def test_zero_quantity_is_free(self) -> None:
        assert PerShareCostModel().commission(order(), Decimal(50), Decimal(0)) == Decimal(0)


class TestSpreadSlippage:
    def test_half_spread_is_paid_on_every_fill(self) -> None:
        """2bps of $100 = $0.02. Impact is switched off so the spread term is
        the only thing this can be measuring."""
        model = SpreadSlippageModel(impact_coefficient=Decimal(0))
        got = model.slippage(order(), a_bar(), Decimal(100))
        assert got == Decimal("100") * (Decimal(2) / Decimal(10_000))

    def test_slippage_is_adverse_on_both_sides(self) -> None:
        """Positive for a buy, negative for a sell. Never in our favour — that
        is the whole point of the sign convention."""
        model = SpreadSlippageModel()
        buy = model.slippage(order(Side.BUY), a_bar(), Decimal(100))
        sell = model.slippage(order(Side.SELL), a_bar(), Decimal(100))
        assert buy > 0
        assert sell < 0
        assert buy == -sell

    def test_impact_scales_with_the_square_root_of_participation(self) -> None:
        """1% of volume: 0.1 × √0.01 = 0.01, plus 2bps spread.

        On $100 that is $1.0200 — a full percent of impact for one percent of
        the day's volume.
        """
        model = SpreadSlippageModel()
        got = model.slippage(order(qty=10_000), a_bar(volume=1_000_000), Decimal(100))
        expected = Decimal(100) * (Decimal("0.0002") + Decimal("0.1") * Decimal("0.01").sqrt())
        assert got == expected
        assert float(got) == pytest.approx(1.02, abs=1e-9)

    def test_impact_is_not_linear_in_size(self) -> None:
        """Quadrupling the order doubles the impact, not quadruples it. A
        constant-slippage model looks fine at 100 shares and underestimates
        catastrophically at 100,000."""
        model = SpreadSlippageModel(half_spread_bps=Decimal(0))
        small = model.slippage(order(qty=1_000), a_bar(), Decimal(100))
        large = model.slippage(order(qty=4_000), a_bar(), Decimal(100))
        assert float(large / small) == pytest.approx(2.0, abs=1e-9)

    def test_impact_matches_the_closed_form(self) -> None:
        model = SpreadSlippageModel(half_spread_bps=Decimal(5), impact_coefficient=Decimal("0.25"))
        got = model.slippage(order(qty=2_500), a_bar(volume=250_000), Decimal("37.50"))
        expected = 37.50 * (5 / 10_000 + 0.25 * math.sqrt(2_500 / 250_000))
        assert float(got) == pytest.approx(expected, abs=1e-9)

    def test_zero_volume_leaves_only_the_spread(self) -> None:
        """Nothing traded, so there is no participation to measure — and the
        engine's volume cap refuses to fill against such a bar anyway."""
        model = SpreadSlippageModel()
        got = model.slippage(order(), a_bar(volume=0), Decimal(100))
        assert got == Decimal("100") * (Decimal(2) / Decimal(10_000))

    def test_slippage_model_charges_no_commission(self) -> None:
        assert SpreadSlippageModel().commission(order(), Decimal(50), Decimal(100)) == Decimal(0)


class TestComposite:
    def test_composite_takes_each_from_its_own_model(self) -> None:
        model = CompositeCostModel(
            commission_model=PerShareCostModel(),
            slippage_model=SpreadSlippageModel(),
        )
        assert model.commission(order(), Decimal(50), Decimal(100)) == Decimal("1.00")
        assert model.slippage(order(), a_bar(), Decimal(100)) > 0

    def test_zero_cost_model_is_free_in_both_directions(self) -> None:
        """It exists for engine mechanics only — never for evaluating a
        strategy (docs/BACKTESTING.md §5)."""
        model = ZeroCostModel()
        assert model.commission(order(), Decimal(50), Decimal(100)) == Decimal(0)
        assert model.slippage(order(), a_bar(), Decimal(100)) == Decimal(0)
