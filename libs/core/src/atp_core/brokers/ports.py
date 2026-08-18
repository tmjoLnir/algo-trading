"""The broker port.

Every venue sits behind this interface. Adding a broker means writing one
adapter; nothing in `strategy/`, `risk/` or `backtest/` changes.

Three implementations ship:

    AlpacaBroker(mode=live)   real money
    AlpacaBroker(mode=paper)  Alpaca's paper endpoint — live data, fake money
    SimulatedBroker           our own fill simulator, for backtests and tests

Requirement #5 (paper trading) is satisfied by binding a different adapter, not
by branching inside the engine. There is no `if paper:` anywhere in core, and
there should never be one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from atp_core.domain import Fill, Order, OrderStatus, Position

    #: Everything `AlpacaBroker.stream_trade_updates()` can yield.
    #: `TradeUpdatesReconnected` is in here on purpose — see its docstring.
    type TradeUpdateEvent = TradeUpdate | TradeUpdatesReconnected


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """The broker's view of the account. Authoritative — ours is a cache."""

    account_id: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    maintenance_margin: Decimal
    is_pattern_day_trader: bool
    trading_blocked: bool
    as_of: datetime


@dataclass(frozen=True, slots=True)
class TradeUpdate:
    """One account event, in our vocabulary rather than the venue's.

    This is the *push* view of an order and it is not interchangeable with the
    REST one. `BrokerPort.get_order` reports running totals — 300 filled at an
    average of 101.4 — whereas this carries the individual print that moved it,
    which is the fill *sequence* a position update has to handle (CLAUDE.md §5)
    and the thing REST cannot reconstruct.

    `status` is the state the venue is telling us the order is now in, or None
    for an event that carries no status change (a cancel that was itself
    rejected leaves the order exactly as it was). A fill's status is
    deliberately not set here: `Order.apply_fill` owns that, because only the
    arithmetic knows whether this print completed the order.
    """

    #: The venue's event name, normalised to lower case. Kept as a string
    #: rather than an enum: it is for logs and for the applier's refusal
    #: messages, and an unrecognised one is refused at the adapter rather than
    #: silently becoming a member here.
    event: str
    client_order_id: str
    broker_order_id: str
    symbol: str
    at: datetime
    status: OrderStatus | None = None
    #: Present only on a fill or partial fill. Carries `venue_fill_id`, which
    #: is what makes a redelivered event safe to discard rather than
    #: double-count.
    fill: Fill | None = None
    #: The venue's position size after this fill. Not applied — it is a
    #: cross-check against our own arithmetic, and reconciliation is where a
    #: disagreement gets resolved.
    position_qty: Decimal | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TradeUpdatesReconnected:
    """The stream dropped and came back. Every event in between is gone.

    Carried *in* the event stream rather than handed to a callback, for the
    same reason `data.ports.FeedReconnected` is: one `async for` body runs to
    completion before the next event is delivered, so a consumer's catch-up
    provably happens before it sees the first event of the new connection. An
    out-of-band notification cannot promise that ordering.

    The catch-up is not optional and it is not a backfill. Alpaca does not
    replay trade updates, so the only way to learn what happened during the gap
    is to re-read the open orders over REST — and a missed fill means our
    position view is wrong in the direction that keeps trading (CLAUDE.md §5).
    """

    #: The last instant the order state is known good — the last event received
    #: before the drop, or the connection's open time if it never delivered one.
    gap_since: datetime
    reconnected_at: datetime
    #: Connection attempts it took to get back; 1 means it returned first try.
    attempts: int


@runtime_checkable
class BrokerPort(Protocol):
    """What the platform needs from a venue.

    Implementations must be idempotent on `submit_order` with respect to
    `client_order_id` (rule §1.4): if a submit times out, calling it again with
    the same id must not create a second order.
    """

    @property
    def name(self) -> str: ...

    @property
    def supports_fractional(self) -> bool: ...

    async def get_account(self) -> AccountSnapshot: ...

    async def submit_order(self, order: Order) -> Order:
        """Send an order; return it updated with `broker_order_id` and status.

        Raises `OrderRejectedError` on venue refusal, `BrokerConnectionError` on
        transport failure. The caller retries only the latter, and only with the
        same `client_order_id`.
        """
        ...

    async def cancel_order(self, broker_order_id: str) -> None:
        """Cancel. Cancelling an already-filled order is not an error — it is a
        race we lost, and the fill stands."""
        ...

    async def get_order(self, broker_order_id: str) -> Order | None: ...

    async def get_open_orders(self) -> list[Order]: ...

    async def get_positions(self) -> list[Position]:
        """The broker's positions. Reconciliation compares these to ours;
        any disagreement halts trading (`ReconciliationError`)."""
        ...

    async def close_position(self, symbol: str) -> Order: ...

    async def close_all_positions(self) -> list[Order]:
        """Emergency flatten. See docs/RUNBOOK.md."""
        ...

    async def is_market_open(self) -> bool: ...

    # `stream_trade_updates` is deliberately **not** on this protocol.
    #
    # A pushed order stream is a property of a venue, not of brokers in
    # general: `SimulatedBroker` fills in-process and has nothing to push, and
    # requiring it here would oblige two implementations to supply an empty
    # generator pretending to be a capability they do not have — the kind of
    # stub that reads as "no fills happened". `AlpacaBroker` exposes it
    # directly, and the types it yields live below so a consumer can be written
    # against the vocabulary rather than against Alpaca. Promote it here when a
    # second venue actually streams, which is the point at which the shape of
    # the abstraction is known rather than guessed.
