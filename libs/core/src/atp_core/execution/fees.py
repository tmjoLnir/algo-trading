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
account-level charge back to the fill that caused it. We apply what the venue
says it charged, once each, and let reconciliation keep judging the result.
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

    `total` is what left cash. Zero with an empty `applied` is the steady
    state — the venue has charged nothing since the last pass — and is not
    worth a log line above DEBUG.
    """

    total: Decimal = Decimal(0)
    applied: list[FeeActivity] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.applied


async def settle_broker_fees(
    portfolio: Portfolio,
    *,
    broker: BrokerPort,
    ledger: FeeLedger,
    run_mode: RunMode,
    today: date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> FeeSettlement:
    """Take the venue's unapplied fees out of `portfolio.cash`. Idempotent.

    The idempotency is the ledger's, not this function's: `record_unseen`
    durably records what it returns, in one atomic statement, so a charge is
    handed back exactly once no matter how often the feed is re-read. That
    matters because this runs on every reconcile — every five minutes, and
    twice more whenever a disagreement is re-read.

    **Recorded before it is applied.** If the process dies between the two, the
    fee is lost from our cash and reconciliation reports it as drift: visible,
    and the thing an operator is already equipped to read. The other ordering
    loses the *record* and re-applies the charge on the next pass, which
    silently walks our cash below the venue's — a wrong book that looks like a
    right one.

    Failure is not fatal. A venue that will not answer is a fee we cannot
    apply, and refusing to reconcile because of it would turn a fee-feed
    outage into a halt. The unapplied charge stays visible as drift, so the
    worst case degrades to exactly the behaviour that existed before this
    module.

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
            msg="could not read the venue's fee feed; unapplied fees stay visible "
            "to reconciliation as cash drift",
        )
        return FeeSettlement()

    if not charged:
        return FeeSettlement()

    unseen = await ledger.record_unseen(charged, run_mode=run_mode)
    if not unseen:
        log.debug("execution.fees.nothing_new", offered=len(charged))
        return FeeSettlement()

    total = sum((item.amount for item in unseen), Decimal(0))
    portfolio.cash -= total

    log.info(
        "execution.fees.settled",
        count=len(unseen),
        total=str(total),
        cash=str(portfolio.cash),
        fees=[
            {
                "id": item.activity_id,
                "date": item.booked_on.isoformat(),
                "sub_type": item.sub_type,
                "amount": str(item.amount),
            }
            for item in unseen
        ],
    )
    return FeeSettlement(total=total, applied=list(unseen))


def _lookback_floor(today: date, lookback_days: int) -> date:
    """The oldest day to ask the venue about.

    Clamped at zero rather than trusting the caller: a negative lookback would
    ask for fees booked in the future, get none, and read as a venue that
    charges nothing — the exact silent-success this path exists to remove.
    """
    return today - timedelta(days=max(lookback_days, 0))
