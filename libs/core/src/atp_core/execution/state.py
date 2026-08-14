"""Order state machine.

Orders arrive from two directions — our own submissions and broker events — and
events arrive out of order after a reconnect. An explicit transition table means
a late "submitted" event cannot overwrite a "filled" status, which is the class
of bug that makes a position appear to vanish.
"""

from __future__ import annotations

from atp_core.domain.enums import OrderStatus
from atp_core.errors import InvalidStateTransitionError

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
