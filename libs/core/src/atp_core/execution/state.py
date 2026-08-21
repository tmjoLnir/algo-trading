"""Order state machine.

Orders arrive from two directions — our own submissions and broker events — and
events arrive out of order after a reconnect. An explicit transition table means
a late "submitted" event cannot overwrite a "filled" status, which is the class
of bug that makes a position appear to vanish.

`transition()` is how a status is set. The table is a guarantee only where
something consults it, and a plain `order.status = ...` consults nothing.

**One status change deliberately does not come through here.**
`Order.apply_fill` sets `PARTIALLY_FILLED` / `FILLED` itself, because `domain/`
imports nothing from its siblings (CLAUDE.md §2) and a fill is an accounting
event that has to stay in the entity that accumulates it. That left one gap
worth naming rather than papering over: a fill applied to an order in a status
the table would not have allowed to fill — a `PENDING_RISK` order, say — is
accepted here. Closing it *in this module* would mean either `domain` importing
`execution` or this module reimplementing fill accounting, and both are worse
than the gap.

It is closed where it belongs instead. `execution.trade_updates
.apply_trade_update` is the consumer of the venue's push stream, which is where
an event of unknown provenance first meets an order, and it refuses a fill from
a status this table would not have allowed to fill — deriving that set from
`TRANSITIONS` rather than restating it, so the two cannot drift. It also
discards a fill the venue has already sent us once, which is the other half of
the same problem: `apply_fill` is arithmetic and has no way to know it is being
told the same thing twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atp_core.domain.enums import OrderStatus
from atp_core.errors import InvalidStateTransitionError
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.domain import Order

log = get_logger(__name__)

#: Legal transitions. Anything absent is rejected.
TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_RISK: frozenset({OrderStatus.PENDING_SUBMIT, OrderStatus.REJECTED_RISK}),
    OrderStatus.PENDING_SUBMIT: frozenset(
        {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    # Terminal states go nowhere.
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.REJECTED_RISK: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in TRANSITIONS[current]


def assert_transition(current: OrderStatus, target: OrderStatus) -> None:
    """Raise `InvalidStateTransitionError` if the move is illegal.

    Note PARTIALLY_FILLED → PARTIALLY_FILLED is legal: an order fills in many
    pieces and each is a real event.
    """
    if not can_transition(current, target):
        raise InvalidStateTransitionError(current.value, target.value)


def is_stale_event(current: OrderStatus, incoming: OrderStatus) -> bool:
    """True when `incoming` is an out-of-order event for an already-terminal
    order — discard it rather than raising. Common after a WS reconnect replays
    history, and not an error worth waking anyone for.
    """
    return current.is_terminal and incoming is not current


def transition(
    order: Order,
    target: OrderStatus,
    *,
    at: datetime | None = None,
    reason: str | None = None,
    rejected_by: str | None = None,
) -> bool:
    """Move an order to `target`, through the table rather than around it.

    Returns True if the order moved and False if the event was discarded as
    stale. Raises `InvalidStateTransitionError` on a move that is neither.

    The table above is only a guarantee if something consults it, and until now
    nothing did: every status in the codebase was assigned with `order.status =
    ...`, which is exactly how a replayed "submitted" ends up overwriting a
    "filled". This is the one place that assignment should happen for a status
    that came from outside the order — our own submission path included, since
    a bug in it looks identical to a bad broker event.

    A repeat of the status the order already holds is a no-op rather than an
    error. Brokers re-send, and `PARTIALLY_FILLED → PARTIALLY_FILLED` is only
    meaningful when a *fill* comes with it — which is `Order.apply_fill`'s job,
    not this one.

    `at`, `reason` and `rejected_by` fill the fields that belong to the move
    itself, so a caller cannot record the transition and forget the timestamp
    that explains it. `filled_at` is deliberately absent: it is set by
    `apply_fill` from the fill's own timestamp, because a status carries no
    execution time.

    `rejected_by` is *who* refused and `reason` is *why*. They are taken at one
    call for the reason the pair exists at all: both are computed together —
    `RiskDecision` carries `rule` beside `reason` — and recorded separately they
    drift, which is how the rule came to be logged while only the reason was
    stored. Either may still arrive alone, because a venue can refuse without
    saying why: the broker's name is known and the reason is not.
    """
    current = order.status
    if current is target:
        return False
    if is_stale_event(current, target):
        log.info(
            "order.stale_event_discarded",
            order_id=order.id,
            client_order_id=order.client_order_id,
            current=current.value,
            incoming=target.value,
        )
        return False

    assert_transition(current, target)
    order.status = target
    if target is OrderStatus.SUBMITTED and at is not None:
        order.submitted_at = at
    # Only on a refusal, and each guarded separately so a caller that knows one
    # half does not blank the other — `_adopt` names the broker it submitted to
    # whether or not the venue's copy carried a reason.
    if target in (OrderStatus.REJECTED, OrderStatus.REJECTED_RISK):
        if reason is not None:
            order.reject_reason = reason
        if rejected_by is not None:
            order.rejected_by = rejected_by
    return True
