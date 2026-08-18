"""Reconciliation — does our book match the broker's?

Run at startup, on a schedule, and after any reconnect. Our position and order
state is a cache; the broker is the truth. They drift for ordinary reasons: a
fill arrived while we were restarting, a WebSocket dropped, a stop we did not
place fired, a corporate action changed a share count overnight.

**A mismatch halts trading.** Not "logs a warning" — halts. Continuing to size
orders against a position we believe is 100 shares when it is actually 1,000 is
how a small bug becomes a large loss, and it compounds with every subsequent
order. Stopping is cheap; being wrong about the book is not.

This is docs/SAFETY.md's layer 7, and that table names its failure mode as
"reconciliation itself is not running". So a broker this cannot reach is not a
reason to skip the check and carry on — it is the check failing, and it halts
on `BROKER_UNREACHABLE` for the same reason the kill switch fails closed when
Redis is unreachable. An unverified book and a book known to be wrong are the
same thing to anyone sizing an order against it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.errors import BrokerError
from atp_core.logging import get_logger
from atp_core.risk.killswitch import HaltReason, HaltScope

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable
    from datetime import datetime

    from atp_core.brokers.ports import BrokerPort
    from atp_core.clock import Clock
    from atp_core.domain import Order, Portfolio, Position
    from atp_core.risk.killswitch import KillSwitch

log = get_logger(__name__)

#: How far cash may drift before it is a discrepancy rather than arithmetic.
#: Fees settle late, interest accrues daily and a fractional share leaves
#: rounding behind, so an exact match is not the thing to demand — the
#: docstring on the check below is explicit that halting on a cent would make
#: layer 7 fire constantly and get switched off, which is worse than a loose
#: tolerance.
DEFAULT_CASH_TOLERANCE = Decimal("1.00")

#: An account-level discrepancy belongs to no instrument. Empty rather than a
#: sentinel like "CASH", because `symbol` is an uppercase ticker everywhere
#: else in this codebase and a fake one would sort and group with real ones.
_NO_SYMBOL = ""


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: str  # "position_qty" | "missing_position" | "unknown_position" | "orphan_order" | "cash"
    symbol: str
    ours: Decimal | None
    theirs: Decimal | None
    detail: str = ""


@dataclass(slots=True)
class ReconciliationReport:
    checked_at: datetime
    discrepancies: list[Discrepancy] = field(default_factory=list)
    orphan_order_ids: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies and not self.orphan_order_ids

    def summary(self) -> str:
        """One line for a log or a page. Names the symbols, not just a count —
        "3 discrepancies" sends a human to a dashboard; naming SPY sends them
        to the position."""
        if self.is_clean:
            return "clean"
        by_kind: dict[str, list[str]] = {}
        for item in self.discrepancies:
            by_kind.setdefault(item.kind, []).append(item.symbol or "account")
        parts = [
            f"{kind}: {', '.join(sorted(symbols))}" for kind, symbols in sorted(by_kind.items())
        ]
        if self.orphan_order_ids:
            parts.append(f"orphan orders: {', '.join(sorted(self.orphan_order_ids))}")
        return " | ".join(parts)


class Reconciler:
    """Compare our book against the venue's, and halt when they disagree.

    Takes a `Clock` rather than reading the wall clock, so `checked_at` means
    the same thing in a backtest as in production (rule §1.2). It is required
    rather than defaulted for the same reason `default_rules()` requires its
    dependencies: a default that quietly worked would be a default nobody chose.
    """

    def __init__(self, broker: BrokerPort, kill_switch: KillSwitch, clock: Clock) -> None:
        self.broker = broker
        self.kill_switch = kill_switch
        self.clock = clock

    async def reconcile(
        self,
        portfolio: Portfolio,
        *,
        known_orders: Collection[Order],
        halt_on_mismatch: bool = True,
        cash_tolerance: Decimal = DEFAULT_CASH_TOLERANCE,
    ) -> ReconciliationReport:
        """Compare our state with the broker's.

        Checks:
        1. Every broker position exists locally with the same signed quantity.
        2. No local position the broker does not have.
        3. Every open broker order is one we know about ("orphan" otherwise —
           usually a stop we placed before a restart, and cancelling it blindly
           would leave the position naked; report, do not auto-cancel).
        4. Cash and equity within tolerance (fees and interest cause small,
           legitimate drift — do not halt on a cent).

        On any discrepancy with `halt_on_mismatch`, engage the kill switch and
        page a human. See docs/RUNBOOK.md.

        `known_orders` is what the caller believes is working at the venue, and
        it has no default on purpose. Defaulting it to empty would report every
        real order as an orphan and halt on the first healthy run; defaulting it
        to "skip check 3" would silently disable a documented safety check.
        Neither is a decision this class may make for a caller — the caller is
        the only thing that knows what it submitted.
        """
        try:
            broker_positions = await self.broker.get_positions()
            broker_orders = await self.broker.get_open_orders()
            account = await self.broker.get_account()
        except BrokerError as exc:
            # Layer 7's own failure mode. We cannot verify the book, which is
            # indistinguishable from knowing it is wrong for anyone about to
            # size an order against it.
            log.error("execution.reconcile.broker_unreachable", error=str(exc))
            if halt_on_mismatch:
                self.kill_switch.engage(
                    HaltScope.GLOBAL,
                    HaltReason.BROKER_UNREACHABLE,
                    engaged_by="reconciler",
                    detail=f"could not read the broker's book: {exc}",
                )
            raise

        report = ReconciliationReport(checked_at=self.clock.now())
        report.discrepancies.extend(self._position_discrepancies(portfolio, broker_positions))
        report.discrepancies.extend(
            self._cash_discrepancies(portfolio, account.cash, cash_tolerance)
        )

        known_ids = {order.client_order_id for order in known_orders}
        for order in broker_orders:
            if order.client_order_id in known_ids:
                continue
            # Reported, never cancelled. An orphan is most often a protective
            # stop we placed before a restart, and cancelling it blindly leaves
            # the position it guards naked — which is a worse state than the
            # one being reported.
            report.orphan_order_ids.append(order.client_order_id)
            report.discrepancies.append(
                Discrepancy(
                    kind="orphan_order",
                    symbol=order.symbol,
                    ours=None,
                    theirs=order.qty,
                    detail=(
                        f"{order.side.value} {order.qty} {order.symbol} is working at the venue "
                        "and is not an order we know about"
                    ),
                )
            )

        if report.is_clean:
            log.info(
                "execution.reconcile.clean",
                positions=len(broker_positions),
                open_orders=len(broker_orders),
            )
            return report

        log.error("execution.reconcile.mismatch", summary=report.summary())
        if halt_on_mismatch:
            self.kill_switch.engage(
                HaltScope.GLOBAL,
                HaltReason.RECONCILIATION_MISMATCH,
                engaged_by="reconciler",
                detail=report.summary(),
            )
        return report

    def _position_discrepancies(
        self, portfolio: Portfolio, broker_positions: Iterable[Position]
    ) -> list[Discrepancy]:
        """Checks 1 and 2, in both directions.

        Compared on *signed* quantity, so a long we believe is a short is a
        discrepancy rather than a match on magnitude — which is the one
        disagreement that doubles the loss when it is acted on.
        """
        found: list[Discrepancy] = []
        theirs = {position.symbol: position.qty for position in broker_positions}
        # Read without `Portfolio.position()`, which creates on access and would
        # add an entry for every broker symbol while we are deciding whether one
        # is missing.
        ours = {
            symbol: position.qty
            for symbol, position in portfolio.positions.items()
            if not position.is_flat
        }

        for symbol in sorted(theirs.keys() | ours.keys()):
            their_qty = theirs.get(symbol, Decimal(0))
            our_qty = ours.get(symbol, Decimal(0))
            if their_qty == our_qty:
                continue

            if our_qty == 0:
                kind, detail = "missing_position", "the broker holds a position we do not"
            elif their_qty == 0:
                kind, detail = "unknown_position", "we hold a position the broker does not"
            else:
                kind, detail = "position_qty", "the same symbol, a different quantity"

            found.append(
                Discrepancy(kind=kind, symbol=symbol, ours=our_qty, theirs=their_qty, detail=detail)
            )
        return found

    @staticmethod
    def _cash_discrepancies(
        portfolio: Portfolio, broker_cash: Decimal, tolerance: Decimal
    ) -> list[Discrepancy]:
        """Check 4. Cash only — equity is not compared.

        Equity is cash plus marks, and our marks come from our data feed while
        the broker's come from theirs. Two feeds disagreeing by a tick on an
        open position is not a book discrepancy, and reporting it as one would
        make this fire on every volatile day. Cash is arithmetic on fills, so a
        drift beyond the tolerance means a fill one of us does not know about —
        which is exactly what this exists to catch.
        """
        drift = portfolio.cash - broker_cash
        if abs(drift) <= tolerance:
            return []
        return [
            Discrepancy(
                kind="cash",
                symbol=_NO_SYMBOL,
                ours=portfolio.cash,
                theirs=broker_cash,
                detail=f"cash differs by {drift}, beyond the {tolerance} tolerance",
            )
        ]

    async def adopt_broker_state(self, portfolio: Portfolio) -> None:
        """Overwrite local state with the broker's.

        The recovery action after a human has reviewed a mismatch. Deliberately
        NOT automatic: silently adopting hides the bug that caused the drift,
        and if the cause is a duplicate-submission bug, adopting is how you end
        up doing it again tomorrow.

        Protective levels are **not** carried over. The broker knows a position
        exists; it does not know the stop we intended for it, and inventing one
        from the venue's average entry price would arm a level no strategy
        chose. A position adopted here is unprotected until something re-arms
        it, which is the honest state and is why docs/RUNBOOK.md has the
        operator reconcile before clearing the halt rather than after.
        """
        account = await self.broker.get_account()
        broker_positions = await self.broker.get_positions()

        portfolio.positions.clear()
        for position in broker_positions:
            if position.is_flat:
                continue
            portfolio.positions[position.symbol] = position
        portfolio.cash = account.cash

        log.warning(
            "execution.reconcile.adopted_broker_state",
            positions=len(portfolio.positions),
            cash=str(account.cash),
        )
