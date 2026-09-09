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

    #: How much of this position has a stop **working at the venue**, as
    #: opposed to a level this platform has merely armed.
    #:
    #: The distinction is the whole point of the field, and it is not
    #: pedantic. `stop_loss_price` above is *intent*: the router arms it before
    #: submitting the child order, deliberately, so that a refused stop still
    #: leaves the engine-side fallback something to watch. That makes it a
    #: number which is present whether or not any order exists — and on day 2
    #: of the paper week it was present on all 38 positions while all 85
    #: protective orders were rejected. Every screen read the armed level, and
    #: every screen said "protected".
    #:
    #: A quantity rather than a boolean, for the reason
    #: `OrderRouter.broker_side_protected_qty` gives: a boolean reports a
    #: partly covered position as protected and hides the naked remainder.
    #:
    #: Unsigned, unlike `qty` — it is an amount of cover, not a direction. Zero
    #: means nothing is resting at the venue, which is also the honest value
    #: for a position this process has not yet asked about.
    broker_protected_qty: Decimal = Decimal(0)

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
    def unprotected_qty(self) -> Decimal:
        """How many shares are exposed with no stop resting at the venue.

        docs/SAFETY.md's go-live gate is *"there are no unprotected
        positions"*, and until this existed the only way to evaluate it was
        `docker compose logs worker | grep runner.position_unprotected` — which
        is how the day-2 review found the failure, a day late. Riding the
        position into the snapshot makes the gate a query.

        Floored at zero: over-cover is a different defect (a stop that would
        flip the position when it triggers) and reporting it as negative
        exposure here would net it against a genuinely naked position
        elsewhere.
        """
        return max(Decimal(0), abs(self.qty) - self.broker_protected_qty)

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

        `signed_qty` carries the direction that a `Fill` does not: positive adds
        to the position, negative reduces it. Its magnitude must equal
        `fill.qty`, which is always unsigned — passing the two separately is
        what lets a fill be applied without the position knowing about `Side`.

        Returns realised P&L *gross of fees*. Fees accumulate into `fees_paid`
        instead, because `total_pnl` already subtracts that; netting them here
        as well would charge every fee twice.
        """
        if fill.qty <= 0:
            raise ValueError(f"fill qty must be positive, got {fill.qty}")
        if signed_qty == 0:
            raise ValueError("signed_qty must be non-zero — a fill always moves the position")
        if abs(signed_qty) != fill.qty:
            raise ValueError(
                f"signed_qty magnitude {abs(signed_qty)} does not match fill qty {fill.qty}"
            )

        old_qty = self.qty
        price = fill.price
        realized = Decimal(0)

        if old_qty == 0:
            # Opening from flat: the fill price *is* the basis.
            self.avg_entry_price = price
            self.opened_at = fill.ts
        elif (old_qty > 0) == (signed_qty > 0):
            # Adding to the side we already hold: re-average over both legs.
            old_abs, add_abs = abs(old_qty), abs(signed_qty)
            self.avg_entry_price = (self.avg_entry_price * old_abs + price * add_abs) / (
                old_abs + add_abs
            )
        else:
            # Reducing, closing, or flipping. P&L is realised against the OLD
            # basis and the basis does NOT move — docs/RISK.md case 2. A short
            # realises the mirror image, hence the direction factor rather than
            # a bare (price - basis).
            direction = Decimal(1) if old_qty > 0 else Decimal(-1)
            closed_qty = min(abs(signed_qty), abs(old_qty))
            realized = closed_qty * (price - self.avg_entry_price) * direction
            self.realized_pnl += realized

            if abs(signed_qty) > abs(old_qty):
                # Flip through zero: the new side opens at the fill price. It is
                # emphatically not re-averaged against the side just closed.
                self.avg_entry_price = price
                self.opened_at = fill.ts

        self.qty = old_qty + signed_qty
        self.fees_paid += fill.fee
        # A fill is an observed print. Marking to it keeps `unrealized_pnl`
        # honest between explicit `Portfolio.mark()` calls; without it a freshly
        # opened position reports zero market value until the next tick arrives.
        self.last_price = price

        if self.qty == 0:
            # Flat: no basis, and no protective levels. A stop left behind here
            # would arm itself against whatever position opens next in this
            # symbol — a live order at a price that means nothing to it.
            self.avg_entry_price = Decimal(0)
            self.opened_at = None
            self.stop_loss_price = None
            self.take_profit_price = None
            self.high_water_mark = None
            # Cover follows the position it covered. Left standing, the next
            # position opened in this symbol would inherit a coverage figure
            # earned by shares that no longer exist, and read as protected
            # before anything had been placed for it.
            self.broker_protected_qty = Decimal(0)

        return realized


@dataclass(slots=True)
class Portfolio:
    """Account-level state: cash, positions, and the equity curve."""

    cash: Decimal
    starting_equity: Decimal
    #: How much of the venue's own fee charges is already reflected in `cash`.
    #:
    #: A running total, not a flag, and it lives here rather than in the fee
    #: ledger for one reason: it is persisted by the same statement that
    #: persists `cash`. A ledger row saying "applied" and a cash balance that
    #: does not reflect it are two writes that can disagree, and on 2026-09-09
    #: they did — the charges committed, the corrected cash never reached a
    #: snapshot, and $4.14 was lost permanently while the worker crash-looped
    #: on the drift it explained (ADR 0031).
    #:
    #: Because both numbers travel together, the correction owed at any moment
    #: is `total fees seen − fees_settled`, which is re-derivable from durable
    #: state after a crash at any point. That is a stronger property than
    #: applying each charge exactly once: it self-heals rather than merely
    #: avoiding a double-count.
    fees_settled: Decimal = Decimal(0)
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

    @property
    def unmarked_symbols(self) -> list[str]:
        """Open positions carrying no mark, so their value is unknown.

        `market_value` returns 0 for an unmarked position, which means
        `gross_exposure` and `equity` both silently *under*-report — and a risk
        rule reading them would compute a smaller number and therefore approve
        where it should refuse. That is the exact inversion the engine's
        default-closed posture exists to prevent, so rules that price the book
        consult this first and deny while it is non-empty.

        Sorted, because it ends up in a rejection reason a human reads.
        """
        return sorted(
            p.symbol for p in self.positions.values() if not p.is_flat and p.last_price is None
        )

    def position(self, symbol: str) -> Position:
        """Get or create — an absent position is a flat position, not an error."""
        return self.positions.setdefault(symbol, Position(symbol=symbol))

    def mark(self, symbol: str, price: Decimal, ts: datetime) -> None:
        """Update the mark for one symbol and append to the equity curve."""
        self.position(symbol).last_price = price
        self.equity_curve.append((ts, self.equity))
