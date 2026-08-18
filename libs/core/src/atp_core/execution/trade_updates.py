"""Folding a venue's trade-update stream into our copy of an order.

`execution/state.py` names the one hole in the order state machine and says
where the guard belongs:

    a fill applied to an order in a status the table would not have allowed to
    fill — a `PENDING_RISK` order, say — is accepted. [...] the guard belongs
    with whatever consumes the trade-updates stream, which is where an event of
    unknown provenance first meets an order.

This is that consumer, and this module is that guard. It is venue-agnostic by
construction: the adapter has already translated Alpaca's spelling into a
`TradeUpdate` (CLAUDE.md — nothing outside `brokers/` knows a venue's
vocabulary), so what is left here is the part that would be identical for any
broker.

Three failure modes it exists to stop, all of which are silent in the version
that just calls `apply_fill`:

- **A redelivered fill counted twice.** Every fill carries the venue's own
  execution id. A fill whose id we already hold is discarded, which is what
  makes a stream that re-sends safe rather than a position that doubles.
- **A fill against an order our book thinks is dead.** Applying it resurrects a
  cancelled order or overfills a complete one; dropping it leaves our position
  disagreeing with the venue's. Neither is recoverable in code, so it raises
  and a human reconciles.
- **An order that fills before we recorded it as submitted.** Here the event is
  the evidence: the venue plainly has the order, so the order is moved through
  `SUBMITTED` first rather than the fill being refused on a technicality.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atp_core.domain.enums import OrderStatus
from atp_core.errors import ReconciliationError
from atp_core.execution import state
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from atp_core.brokers.ports import TradeUpdate
    from atp_core.domain import Order

log = get_logger(__name__)

#: Statuses an order can be in and still legally receive a fill. Derived from
#: the transition table rather than restated, so the two cannot drift.
_CAN_FILL = frozenset(
    status for status in OrderStatus if state.can_transition(status, OrderStatus.PARTIALLY_FILLED)
)

#: Statuses that mean "we have not yet heard the venue acknowledge this", but
#: from which a fill event is proof that it did. Moved through `SUBMITTED`
#: rather than refused — see the module docstring.
_PRE_ACK = (OrderStatus.PENDING_RISK, OrderStatus.PENDING_SUBMIT)


def apply_trade_update(order: Order, update: TradeUpdate) -> bool:
    """Fold `update` into `order`. True if it changed anything.

    The order must be the one the update names; a caller that looks orders up
    by the wrong key would otherwise apply a fill to somebody else's position,
    so the mismatch is refused rather than trusted.

    Returns False for an event that was correctly ignored — a duplicate fill, a
    replayed status, an event carrying neither. Raises `ReconciliationError`
    for the cases where our book and the venue's have genuinely diverged, which
    is a page-a-human outcome rather than a branch to handle (docs/RUNBOOK.md).
    """
    if update.client_order_id != order.client_order_id:
        raise ReconciliationError(
            f"trade update for {update.client_order_id} applied to order "
            f"{order.client_order_id} — refusing to fill the wrong order"
        )

    if order.broker_order_id is None:
        # First hard evidence of the venue's id for this order. Recorded so a
        # later cancel has something to address.
        order.broker_order_id = update.broker_order_id

    return _apply_fill(order, update) if update.fill is not None else _apply_status(order, update)


def _apply_fill(order: Order, update: TradeUpdate) -> bool:
    fill = update.fill
    assert fill is not None  # guarded by the caller

    if _already_seen(order, update):
        log.info(
            "execution.trade_update.duplicate_fill_discarded",
            order_id=order.id,
            client_order_id=order.client_order_id,
            venue_fill_id=fill.venue_fill_id,
        )
        return False

    if order.status in _PRE_ACK:
        # The venue filled it, so the venue has it. Our status simply lags —
        # walk it forward rather than refusing the fill, which would leave a
        # real position unrecorded.
        log.warning(
            "execution.trade_update.fill_before_ack",
            order_id=order.id,
            client_order_id=order.client_order_id,
            status=order.status.value,
        )
        if order.status is OrderStatus.PENDING_RISK:
            state.transition(order, OrderStatus.PENDING_SUBMIT)
        state.transition(order, OrderStatus.SUBMITTED, at=update.at)

    if order.status not in _CAN_FILL:
        # The guard `state.py` asked for. Our book says this order can no
        # longer fill and the venue says it just did; one of them is wrong and
        # code cannot tell which.
        raise ReconciliationError(
            f"venue filled {fill.qty} of {order.client_order_id} while our book has it "
            f"{order.status.value} — a fill cannot be applied from that status"
        )

    if fill.qty > order.remaining_qty:
        # `apply_fill` would raise anyway; this says which side is wrong.
        raise ReconciliationError(
            f"venue filled {fill.qty} of {order.client_order_id} but only "
            f"{order.remaining_qty} was outstanding — our book and the venue's disagree"
        )

    order.apply_fill(fill)
    log.info(
        "execution.trade_update.filled",
        order_id=order.id,
        symbol=order.symbol,
        qty=str(fill.qty),
        price=str(fill.price),
        filled_qty=str(order.filled_qty),
        status=order.status.value,
    )
    return True


def _apply_status(order: Order, update: TradeUpdate) -> bool:
    """A status-only event, moved through the transition table.

    `state.transition` already discards a stale event against a terminal order
    and refuses an illegal move, which is exactly the behaviour wanted here —
    an event arriving out of order after a reconnect is ordinary, and must not
    overwrite a status the order has legitimately moved past.
    """
    if update.status is None:
        # `venue_event` rather than `event`: structlog takes the event name as
        # its first positional argument, so an `event=` keyword collides with it.
        log.debug("execution.trade_update.no_op", venue_event=update.event, order_id=order.id)
        return False
    return state.transition(order, update.status, at=update.at, reason=update.reason)


def _already_seen(order: Order, update: TradeUpdate) -> bool:
    """Has this exact execution already been applied?

    Keyed on the venue's execution id, which is the only identifier that
    survives a redelivery. Falls back to False when the venue sent none: two
    genuine prints of the same size at the same price are ordinary, so
    treating an id-less fill as a duplicate would silently drop real volume.
    """
    fill = update.fill
    if fill is None or fill.venue_fill_id is None:
        return False
    return any(seen.venue_fill_id == fill.venue_fill_id for seen in order.fills)
