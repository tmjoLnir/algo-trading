"""Commission and slippage models.

A frictionless backtest is a lie, and the cheaper the strategy's edge per trade
the bigger the lie. A model trading 20 times a day at 5bps of unmodelled cost
gives up ~250% a year to reality. Model costs pessimistically: if a strategy
only works with zero costs, it does not work.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from atp_core.domain.enums import Side

if TYPE_CHECKING:
    from atp_core.domain import Bar, Order


class CostModel(Protocol):
    def commission(self, order: Order, fill_price: Decimal, fill_qty: Decimal) -> Decimal: ...

    def slippage(self, order: Order, bar: Bar, intended_price: Decimal) -> Decimal:
        """Signed price adjustment. Always adverse: positive for buys."""
        ...


@dataclass(frozen=True, slots=True)
class ZeroCostModel:
    """No costs. For unit-testing engine mechanics only — never for evaluating
    a strategy."""

    def commission(self, order: Order, fill_price: Decimal, fill_qty: Decimal) -> Decimal:
        return Decimal(0)

    def slippage(self, order: Order, bar: Bar, intended_price: Decimal) -> Decimal:
        return Decimal(0)


@dataclass(frozen=True, slots=True)
class PerShareCostModel:
    """US equities: per-share commission with a minimum, plus regulatory fees.

    SEC and TAF fees apply to SELLS only and are small but real for a
    high-turnover strategy. Alpaca is commission-free on equities, so
    `per_share=0` there — but keep the regulatory fees, they are still charged.
    """

    per_share: Decimal = Decimal("0.005")
    minimum: Decimal = Decimal("1.00")
    sec_fee_rate: Decimal = Decimal("0.0000278")  # per $ of sell notional
    taf_per_share: Decimal = Decimal("0.000166")  # sells only, capped per order

    def commission(self, order: Order, fill_price: Decimal, fill_qty: Decimal) -> Decimal:
        raise NotImplementedError

    def slippage(self, order: Order, bar: Bar, intended_price: Decimal) -> Decimal:
        return Decimal(0)


@dataclass(frozen=True, slots=True)
class SpreadSlippageModel:
    """Cross half the spread, plus an impact term that scales with size.

    The impact term matters: cost is not linear in size. A model that assumes
    constant slippage looks fine at 100 shares and catastrophically
    underestimates at 100,000.
    """

    half_spread_bps: Decimal = Decimal("2")
    impact_coefficient: Decimal = Decimal("0.1")  # × sqrt(order_qty / bar_volume)

    def commission(self, order: Order, fill_price: Decimal, fill_qty: Decimal) -> Decimal:
        return Decimal(0)

    def slippage(self, order: Order, bar: Bar, intended_price: Decimal) -> Decimal:
        """Adverse by construction — sign follows the side, never favours us."""
        raise NotImplementedError

    @staticmethod
    def _sign(side: Side) -> int:
        return 1 if side is Side.BUY else -1


@dataclass(frozen=True, slots=True)
class CompositeCostModel:
    """Combine a commission model and a slippage model."""

    commission_model: CostModel
    slippage_model: CostModel

    def commission(self, order: Order, fill_price: Decimal, fill_qty: Decimal) -> Decimal:
        return self.commission_model.commission(order, fill_price, fill_qty)

    def slippage(self, order: Order, bar: Bar, intended_price: Decimal) -> Decimal:
        return self.slippage_model.slippage(order, bar, intended_price)


def alpaca_equities_default() -> CostModel:
    """Realistic defaults for Alpaca US equities."""
    return CompositeCostModel(
        commission_model=PerShareCostModel(per_share=Decimal(0), minimum=Decimal(0)),
        slippage_model=SpreadSlippageModel(),
    )
