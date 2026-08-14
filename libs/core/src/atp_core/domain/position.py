"""Positions and portfolio state.

The portfolio is the single source of truth for exposure. The risk engine reads
it before approving anything; the analytics layer reads its equity curve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.domain.order import Fill


@dataclass(slots=True)
class Position:
    """A holding in one symbol.

    `qty` is signed: positive long, negative short. `avg_entry_price` is the
    cost basis of the *currently open* quantity — it is unchanged when you
    reduce a position (that realises P&L instead) and re-averaged when you add.
    Getting this wrong silently corrupts every downstream P&L number.
    """

    symbol: str
    qty: Decimal = Decimal(0)
    avg_entry_price: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    opened_at: datetime | None = None
    last_price: Decimal | None = None

    # Protective levels, maintained by risk.stops.StopManager.
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    high_water_mark: Decimal | None = None  # for trailing stops

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def market_value(self) -> Decimal:
        """Signed mark-to-market value. Negative for shorts."""
        if self.last_price is None:
            return Decimal(0)
        return self.qty * self.last_price

    @property
    def exposure(self) -> Decimal:
        """Absolute notional at risk — what exposure limits are measured against."""
        return abs(self.market_value)

    @property
    def unrealized_pnl(self) -> Decimal:
        if self.last_price is None or self.is_flat:
            return Decimal(0)
        return (self.last_price - self.avg_entry_price) * self.qty

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl - self.fees_paid

    def apply_fill(self, fill: Fill, signed_qty: Decimal) -> Decimal:
        """Fold a fill into this position; return the P&L realised by it.

        Three cases, and the reducing case is the one worth reading twice:
        opening/adding re-averages the cost basis; reducing realises P&L against
        the existing basis and leaves the basis alone; a flip does both, closing
        the old side before opening the new at the fill price.
        """
        raise NotImplementedError(
            "Implement with care — see docs/RISK.md 'Position accounting'. "
            "Must handle: add to existing, partial reduce, full close, and flip "
            "through zero. Property-tested in tests/unit/test_position.py."
        )


@dataclass(slots=True)
class Portfolio:
    """Account-level state: cash, positions, and the equity curve."""

    cash: Decimal
    starting_equity: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)

    @property
    def total_market_value(self) -> Decimal:
        return sum((p.market_value for p in self.positions.values()), Decimal(0))

    @property
    def equity(self) -> Decimal:
        """Cash plus mark-to-market. The number every percentage risk limit
        and every performance metric is denominated in."""
        return self.cash + self.total_market_value

    @property
    def gross_exposure(self) -> Decimal:
        """Sum of |notional| across positions. Longs and shorts ADD here —
        a market-neutral book still consumes buying power."""
        return sum((p.exposure for p in self.positions.values()), Decimal(0))

    @property
    def net_exposure(self) -> Decimal:
        """Signed sum — directional market exposure."""
        return self.total_market_value

    @property
    def leverage(self) -> Decimal:
        eq = self.equity
        return self.gross_exposure / eq if eq else Decimal(0)

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if not p.is_flat]

    def position(self, symbol: str) -> Position:
        """Get or create — an absent position is a flat position, not an error."""
        return self.positions.setdefault(symbol, Position(symbol=symbol))

    def mark(self, symbol: str, price: Decimal, ts: datetime) -> None:
        """Update the mark for one symbol and append to the equity curve."""
        self.position(symbol).last_price = price
        self.equity_curve.append((ts, self.equity))
