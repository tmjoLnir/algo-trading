"""Settling the venue's fees against our cash.

`Reconciler` compares our cash with the broker's and its own docstring states
the assumption underneath that check: *"cash is arithmetic on fills, so a drift
beyond the tolerance means a fill one of us does not know about"*. That holds
for a venue which charges nothing else. Alpaca charges regulatory fees — CAT,
REG and TAF — and books them as account activities rather than on the fill, so
`Fill.fee` arrives as zero and our cash is a fills-only total against a venue
cash that is not one.

The difference is not noise that settles. It is a ratchet: every session adds
its fee take and nothing ever gives it back. One session (2026-09-08) cost
$4.14 against a $1.00 tolerance, which halted the worker at `warmup` the next
morning and would have halted it again within a day of any `adopt_broker_state`
run to clear it.

So this module makes the fee a first-class movement of cash rather than
something the tolerance is asked to absorb. It is deliberately *not* an
estimate: nothing here models a fee schedule, and nothing attributes an
account-level charge back to the fill that caused it.

**The correction is derived on every pass rather than applied once** (ADR
0031). `Portfolio.fees_settled` says how much fee the book already reflects and
is persisted by the same statement as the cash; the ledger says what the venue
has charged in total. The difference is what is owed, and both operands survive
a crash, so an interrupted settlement is recomputed rather than lost. The
previous design recorded each charge as applied and then adjusted an in-memory
balance that reached the database minutes later, which cost $4.14 and a second
crash-loop on the morning of 2026-09-09.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.logging import get_logger

if TYPE_CHECKING:
    from datetime import date

    from atp_core.brokers.ports import BrokerPort, FeeActivity
    from atp_core.domain import Portfolio, RunMode
    from atp_core.execution.ports import FeeLedger

log = get_logger(__name__)

#: How far back to ask for fees.
#:
#: Generous on purpose, and it costs nothing to be: the ledger discards every
#: charge it has already applied, so a wide window is a slightly larger response
#: and no other consequence. A narrow one silently loses a fee the venue booked
#: late — Alpaca stamped 2026-09-08's fees with `created_at` just after
#: midnight UTC on the 9th, so even "yesterday" is not a safe floor after a
#: weekend or a holiday.
DEFAULT_LOOKBACK_DAYS = 30


@dataclass(frozen=True, slots=True)
class FeeSettlement:
    """What one settling pass moved, for a log line and for a test.

    `total` is the correction that left cash — the *derived* amount owed, which
    is not the same as the sum of `applied`. On a book that is level they are
    both effectively nothing; on a book recovering from an interrupted
    settlement `total` is what was still owed while `applied` is every charge
    the venue reported in the window.

    Negative is legitimate and means money came back: the ledger shrank, so the
    book had settled more than the venue now says it charged.
    """

    total: Decimal = Decimal(0)
    applied: list[FeeActivity] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Whether this pass moved any money. Keyed on `total` and not on
        `applied`, because a sweep can return a hundred charges and owe nothing
        — which is the steady state, not an empty one."""
        return self.total == 0


async def settle_broker_fees(
    portfolio: Portfolio,
    *,
    broker: BrokerPort,
    ledger: FeeLedger,
    run_mode: RunMode,
    today: date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> FeeSettlement:
    """Bring `portfolio.cash` up to date with the venue's fee charges.

    **The correction is derived, not applied once.** Every call computes

        owed = (total fees the venue has told us about) - portfolio.fees_settled

    and moves both numbers together. Both operands are durable — the total is
    the ledger's, the settled figure rides on the same snapshot row as the cash
    — so a crash at any point leaves the next run able to derive exactly the
    same answer. Nothing is marked done before the thing it describes is true.

    That is the property ADR 0030 lacked. It recorded each charge as applied and
    then subtracted it from an in-memory balance that reached the database only
    at the next scheduled snapshot. A worker that did the first and not the
    second left a ledger insisting three charges were applied to a book that had
    never seen them, and no later run could tell — every one of them read a
    stale balance, found nothing new to apply, and halted on the $4.14 it was
    supposed to have settled (2026-09-09).

    Self-healing follows from the same arithmetic and is worth stating
    separately: a book that is behind by any amount of fee, for any reason
    including that bug, is brought level by the next call. Nothing has to be
    deleted by hand.

    `owed` of zero is the steady state and does nothing. A negative `owed` is
    possible and is applied as a credit — it means the ledger shrank, which
    happens when a charge is reversed or an operator removes a row, and in both
    cases returning the money is the honest response rather than a refusal.

    Failure is not fatal. A venue that will not answer is a fee we cannot see,
    and refusing to reconcile because of it would turn a fee-feed outage into a
    halt. The unsettled charge stays visible as drift, so the worst case
    degrades to exactly the behaviour that existed before this module.

    Fees are charged to cash only, never to `Position.fees_paid`: an
    account-level debit belongs to no position, and dividing it across the
    session's symbols would put a number nobody chose into per-position P&L.
    """
    since = _lookback_floor(today, lookback_days)
    try:
        charged = await broker.get_fee_activities(since)
    except Exception as exc:
        log.warning(
            "execution.fees.unavailable",
            error=str(exc),
            since=since.isoformat(),
            msg="could not read the venue's fee feed; unsettled fees stay visible "
            "to reconciliation as cash drift",
        )
        return FeeSettlement()

    # Called even when the sweep returned nothing. The ledger may already hold
    # charges this book has not settled — which is exactly the state 2026-09-09
    # left behind — and skipping the call on an empty feed would leave the book
    # stuck there forever, waiting for a fee that has already been recorded.
    total_seen = await ledger.record_seen(charged, run_mode=run_mode)
    owed = total_seen - portfolio.fees_settled

    if owed == 0:
        log.info(
            "execution.fees.level",
            offered=len(charged),
            total_seen=str(total_seen),
            msg="the book already reflects every fee the venue has charged",
        )
        return FeeSettlement()

    portfolio.cash -= owed
    portfolio.fees_settled = total_seen

    log.info(
        "execution.fees.settled",
        owed=str(owed),
        total_seen=str(total_seen),
        offered=len(charged),
        cash=str(portfolio.cash),
        msg="cash and fees_settled move together, and persist together",
    )
    return FeeSettlement(total=owed, applied=list(charged))


def _lookback_floor(today: date, lookback_days: int) -> date:
    """The oldest day to ask the venue about.

    Clamped at zero rather than trusting the caller: a negative lookback would
    ask for fees booked in the future, get none, and read as a venue that
    charges nothing — the exact silent-success this path exists to remove.
    """
    return today - timedelta(days=max(lookback_days, 0))
