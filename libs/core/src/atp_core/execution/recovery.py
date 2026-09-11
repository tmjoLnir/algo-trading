"""What the venue did to our orders while nobody was listening.

`trade_updates.py` folds the venue's *push* stream into our copy of an order.
This module answers the question that arises when that stream was not running:
a fill that landed while the worker was restarting, or inside a WebSocket gap,
exists only at the venue. Alpaca does not replay it.

`brokers.ports.TradeUpdatesReconnected` has always said so in as many words:

    The catch-up is not optional and it is not a backfill. Alpaca does not
    replay trade updates, so the only way to learn what happened during the gap
    is to re-read the open orders over REST.

**Nothing did that re-read.** Both consumers went straight to
`Reconciler.reconcile`, which compares *positions and cash* and holds no
opinion about orders — so a missed fill was never booked, it was **reported**,
and the report is a global halt that no restart can clear.

Day 3 of the paper week is the whole demonstration. A market BUY of 10 MSFT
filled at the venue at 14:44 while the trade-updates consumer was dead. The
worker then booted 131 times; every boot logged
`runner.restored_open_orders count=1` for an order that no longer existed (F9),
and every boot quarantined on the same two discrepancies (F3):

    missing_position MSFT: ours 0, theirs 10
    cash            account: ours 100090.05, theirs 95157.73

Those are one event described twice. 10 × 493.232 = 4,932.32, which is the cash
difference to the cent — the book's *equity* was right to two cents all day and
every constituent of it was wrong. The platform had the order, had its venue
id, and never asked.

**This is not adoption, and it must not become adoption.**
`Reconciler.adopt_broker_state` overwrites our book with the venue's and is
deliberately manual: a book that silently agrees with the venue hides the bug
that made it disagree. What happens here is narrower by construction — we ask
the venue about orders *we submitted*, addressed by the id the venue gave us
for our own `client_order_id` (rule §1.4), and we accept only the arithmetic of
what that specific order did. Everything else still halts, unchanged:

- a venue position with no order of ours behind it — `missing_position`
- cash that moved for a reason no order of ours explains — `cash`
- an order working at the venue we do not know — `orphan_order`
- a venue that reports *less* filled than we have already booked — refused
  here, reported by the reconciler, because code cannot tell which side is
  wrong and guessing picks the direction that keeps trading

The reconciler is not relaxed by any of this. It runs immediately afterwards
with the same tolerance and the same rules; what changes is that it no longer
runs with a known-missing input.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.brokers.ports import TradeUpdate
from atp_core.domain.enums import OrderStatus
from atp_core.domain.order import Fill
from atp_core.errors import BrokerError
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from atp_core.brokers.ports import BrokerPort
    from atp_core.domain import Order

log = get_logger(__name__)

#: The `TradeUpdate.event` every update synthesised here carries.
#:
#: `event` is documented as "the venue's event name, normalised to lower case",
#: and this is deliberately not one of them: these updates were reconstructed
#: from a REST snapshot rather than delivered, and a reader of the log — or of
#: `apply_trade_update`'s refusal messages, the field's other consumer — should
#: be able to tell the difference between a print the venue pushed and a
#: difference we inferred.
RECOVERED = "rest_recovery"

#: Statuses `Order.apply_fill` owns and no status event may set.
#:
#: The same rule `AlpacaBroker._from_alpaca_order` applies to the same payload,
#: and for the same reason: only the arithmetic knows whether a print completed
#: an order, so a status derived from the venue's running totals would be
#: asserting something the fill has already decided.
_FILL_OWNED = frozenset({OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED})


def missed_updates(
    ours: Order,
    theirs: Order,
    *,
    now: datetime,
    broker_name: str | None = None,
) -> list[TradeUpdate]:
    """The events that would have carried `ours` to the venue's `theirs`.

    At most two, and **in the order they must be applied**: the fill first,
    then any status the fill does not itself imply. A partial that was then
    cancelled is exactly that sequence, and applying the cancel first would
    strand the shares — `state.transition` refuses a fill from `CANCELLED`,
    which is `trade_updates.py`'s guard doing its job against an ordering
    mistake of ours.

    Pure: it reads two orders and returns events. Nothing is mutated here, and
    in particular nothing touches a `Portfolio` — booking a fill moves cash,
    moves a position and arms a protective stop, and those belong to the one
    component that already does them for a pushed fill.

    ── The fill's price is reconstructed, not read ──────────────────────────

    REST reports running totals — `filled_qty` and `filled_avg_price` — while
    the stream carries individual prints (`TradeUpdate`'s docstring). So the
    difference between the two books is one tranche of unknown internal
    composition, and the only price that keeps the arithmetic honest is the one
    that makes *our* average come out equal to the venue's:

        price = (their_notional − our_notional) / missing_qty

    For the common case — an order we have booked nothing against — that
    reduces to the venue's own average, exactly. For a partial it is a
    `Decimal` division at the context's 28 significant digits, so the residue
    is some twenty orders of magnitude below the smallest unit of any currency;
    it is not float error accumulating in a ledger (rule §1.1), and the
    reconciler's $1.00 cash tolerance is not being leaned on to absorb it.
    """
    updates: list[TradeUpdate] = []
    at = theirs.filled_at or now
    broker_order_id = theirs.broker_order_id or ours.broker_order_id or ""

    missing = theirs.filled_qty - ours.filled_qty
    if missing < 0:
        # Our book claims more filled than the venue admits to. One of the two
        # is wrong and nothing here can tell which — and of the two possible
        # guesses, the one that "corrects" our book downwards would release
        # buying power against a position that may really exist. Left alone for
        # the reconciler, which is the component whose answer to an unresolvable
        # disagreement is to stop.
        log.error(
            "execution.recovery.venue_filled_less",
            client_order_id=ours.client_order_id,
            ours=str(ours.filled_qty),
            theirs=str(theirs.filled_qty),
            detail="our book has more filled than the venue reports — not resolvable here",
        )
        # No status either, for the reason the refused-fill branch below gives:
        # a terminal status would retire the order and with it the only handle
        # on a quantity the two books already disagree about.
        return []
    elif missing > 0:
        fill = _reconstruct_fill(ours, theirs, missing_qty=missing, at=at)
        if fill is None:
            # The quantity is real and we could not price it. Returning the
            # venue's *status* alone would be the worst of both: the order goes
            # terminal, leaves the working set, and takes with it the only
            # record of what the unbooked shares belong to — so the next boot
            # finds a `missing_position` with nothing behind it and no longer
            # even tries. It stays working instead, and a later pass gets
            # another chance at the same question.
            return []
        updates.append(
            TradeUpdate(
                event=RECOVERED,
                client_order_id=ours.client_order_id,
                broker_order_id=broker_order_id,
                symbol=ours.symbol,
                at=at,
                # None, not FILLED: `Order.apply_fill` sets the status from the
                # arithmetic, and `_apply_fill` would never reach the branch
                # that sets one anyway.
                status=None,
                fill=fill,
                broker=broker_name,
            )
        )

    if theirs.status not in _FILL_OWNED and theirs.status is not ours.status:
        updates.append(
            TradeUpdate(
                event=RECOVERED,
                client_order_id=ours.client_order_id,
                broker_order_id=broker_order_id,
                symbol=ours.symbol,
                at=at,
                status=theirs.status,
                reason=theirs.reject_reason,
                broker=broker_name,
            )
        )
    return updates


def _reconstruct_fill(
    ours: Order, theirs: Order, *, missing_qty: Decimal, at: datetime
) -> Fill | None:
    """One aggregate `Fill` for everything the venue filled and we did not.

    `None` — and a loud line — rather than a guess whenever the venue's numbers
    cannot produce a price we would be willing to put in a ledger. A fill
    refused here leaves the divergence in place, which the reconciler then
    halts on; a fill invented here would be a wrong number in the P&L that
    nothing downstream could distinguish from a real one.

    `order_id` is **ours**. `theirs` is a freshly parsed order object with its
    own generated id, and a fill hung on that id would be written against a row
    that does not exist.
    """
    if theirs.avg_fill_price is None:
        log.error(
            "execution.recovery.no_average_price",
            client_order_id=ours.client_order_id,
            filled_qty=str(theirs.filled_qty),
            detail="the venue reports a filled quantity with no average price",
        )
        return None

    their_notional = theirs.avg_fill_price * theirs.filled_qty
    our_notional = (ours.avg_fill_price or Decimal(0)) * ours.filled_qty
    price = (their_notional - our_notional) / missing_qty
    if price <= 0:
        log.error(
            "execution.recovery.implausible_price",
            client_order_id=ours.client_order_id,
            price=str(price),
            detail="the tranche we are missing prices at or below zero — refusing to book it",
        )
        return None

    return Fill(
        order_id=ours.id,
        ts=at,
        qty=missing_qty,
        price=price,
        # Zero for the same reason `AlpacaBroker` sets it to zero on this
        # endpoint: regulatory fees are booked on the account activity feed,
        # not on the order, and `execution.fees.settle_broker_fees` is what
        # moves cash for them. A guessed fee is a wrong ledger.
        fee=Decimal(0),
        # Deterministic, and keyed on the venue's *cumulative* total rather
        # than on the tranche. Two catch-ups that see the same totals produce
        # the same id and the second is discarded by `apply_trade_update`'s
        # duplicate guard; a catch-up that sees more filled since produces a
        # different one and is booked. That is what makes this safe to run on
        # every boot and every reconnect.
        venue_fill_id=f"{RECOVERED}:{theirs.broker_order_id}:{theirs.filled_qty}",
    )


async def read_missed_updates(
    broker: BrokerPort, orders: Iterable[Order], *, now: datetime
) -> list[TradeUpdate]:
    """Ask the venue about every order we believe is working.

    Returns what should be applied, oldest order first, and applies nothing.

    Each order is addressed by `broker_order_id` — the venue's own id for the
    `client_order_id` we minted — so this can only ever learn about orders we
    submitted. It never reads the venue's order list and adopts what it finds
    there; an order working at the venue that we do not know about is an
    `orphan_order`, and it stays one.

    A `BrokerError` is swallowed *per order* rather than raised. The caller's
    next move is `Reconciler.reconcile`, whose three reads hit the same venue
    and whose `BrokerError` branch halts on `BROKER_UNREACHABLE` — which is the
    right outcome and the one place that should decide it. Raising here would
    produce a second, differently-worded outage for the same cause.
    """
    updates: list[TradeUpdate] = []
    for order in orders:
        if order.broker_order_id is None:
            # Submitted but never acknowledged, or never submitted at all. The
            # venue may still hold it under our `client_order_id`, which is
            # what rule §1.4 exists for — but resolving that is the router's
            # `_resolve_indeterminate`, on the submit path, with the retry
            # semantics that belong to it. Reported here so the gap is visible
            # rather than assumed absent.
            log.warning(
                "execution.recovery.unacknowledged",
                client_order_id=order.client_order_id,
                status=order.status.value,
                detail="no venue id to ask about — left for reconciliation",
            )
            continue

        try:
            theirs = await broker.get_order(order.broker_order_id)
        except BrokerError as exc:
            log.warning(
                "execution.recovery.unreadable",
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                error=str(exc),
                detail="the reconcile that follows decides what an unreachable venue means",
            )
            continue

        if theirs is None:
            # The venue has no such order. Not something to act on: our record
            # says we submitted it and got an id back, so an id the venue now
            # disowns is a disagreement, not a cancellation.
            log.warning(
                "execution.recovery.not_at_venue",
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
            )
            continue

        if theirs.client_order_id != order.client_order_id:
            # We asked about one order and were handed another. Applying its
            # fill would move a position the order never touched, which is the
            # exact failure `apply_trade_update`'s first guard refuses — caught
            # a layer earlier, where the wrong order is still identifiable.
            log.error(
                "execution.recovery.identity_mismatch",
                asked_for=order.client_order_id,
                got=theirs.client_order_id,
                broker_order_id=order.broker_order_id,
            )
            continue

        updates.extend(missed_updates(order, theirs, now=now, broker_name=broker.name))

    if updates:
        log.warning(
            "execution.recovery.missed_events",
            count=len(updates),
            client_order_ids=sorted({u.client_order_id for u in updates}),
            fills=sum(1 for u in updates if u.fill is not None),
            detail="the venue moved these orders while we were not listening — booking them now",
        )
    return updates
