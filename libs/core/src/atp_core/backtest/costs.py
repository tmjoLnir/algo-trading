"""Commission and slippage models.

A frictionless backtest is a lie, and the cheaper the strategy's edge per trade
the bigger the lie. A model trading 20 times a day at 5bps of unmodelled cost
gives up ~250% a year to reality. Model costs pessimistically: if a strategy
only works with zero costs, it does not work.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from typing import TYPE_CHECKING, Protocol

from atp_core.domain.enums import Side

if TYPE_CHECKING:
    from atp_core.domain import Bar, Order

#: Money is charged in whole cents. Kept here rather than inline so the two
#: models round the same way.
_CENT = Decimal("0.01")


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
    taf_cap: Decimal = Decimal("8.30")  # FINRA's per-order ceiling

    def commission(self, order: Order, fill_price: Decimal, fill_qty: Decimal) -> Decimal:
        """Cost of one fill.

        Both the minimum and the TAF cap are properties of the *order*, but this
        is called once per fill. Charging either per fill would make an order
        that filled in three pieces cost more than the same order filled in one
        — and the backtest engine's volume cap makes partial fills ordinary
        rather than exotic.

        So each is charged as the difference between what the order owes in
        total after this fill and what it owed before. `order.filled_qty` is the
        quantity already filled, because the engine computes the fee before
        applying the fill. A 200-share order at $0.005 with a $1.00 minimum then
        costs $1.00 whether it arrives whole or in halves, and TAF simply stops
        contributing once the cap is reached.
        """
        if fill_qty <= 0:
            return Decimal(0)

        filled = order.filled_qty
        broker = self._broker_total(filled + fill_qty) - self._broker_total(filled)

        fees = Decimal(0)
        if order.side is Side.SELL:
            # Regulatory fees are charged on sells only. They are small, and on
            # a high-turnover strategy they are also relentless.
            notional = fill_price * fill_qty
            # Section 31 fees round UP to the penny, by the rule's convention.
            fees += (self.sec_fee_rate * notional).quantize(_CENT, rounding=ROUND_UP)
            fees += self._taf_total(filled + fill_qty) - self._taf_total(filled)

        return (broker + fees).quantize(_CENT, rounding=ROUND_HALF_UP)

    def _broker_total(self, qty: Decimal) -> Decimal:
        """What an order of `qty` shares owes in commission, in total."""
        if qty <= 0:
            return Decimal(0)
        return max(self.per_share * qty, self.minimum)

    def _taf_total(self, qty: Decimal) -> Decimal:
        """What an order of `qty` shares owes in TAF, in total, capped."""
        if qty <= 0:
            return Decimal(0)
        return min(self.taf_per_share * qty, self.taf_cap)

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
        """Adverse by construction — sign follows the side, never favours us.

        Two terms, as a fraction of the intended price:

            half_spread_bps / 10,000  +  impact_coefficient × √(qty / volume)

        The square root is the point. Impact is not linear in size: doubling an
        order does not double the cost, but nor does it leave it unchanged, and
        a constant-slippage model looks fine at 100 shares while
        underestimating catastrophically at 100,000.

        Sized on `remaining_qty` — what we are trying to execute — rather than
        on what ends up filling. That is the pessimistic reading, and it is the
        right one: the impact of working an order is paid for attempting it,
        not only for the part that completes.

        A zero-volume bar leaves only the spread term. Nothing traded, so there
        is no participation to measure; the engine's volume cap independently
        refuses to fill against it at all.
        """
        fraction = self.half_spread_bps / Decimal(10_000)
        if bar.volume > 0:
            participation = order.remaining_qty / bar.volume
            fraction += self.impact_coefficient * participation.sqrt()
        return intended_price * fraction * self._sign(order.side)

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
