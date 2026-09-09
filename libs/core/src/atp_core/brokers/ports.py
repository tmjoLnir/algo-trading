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
    from datetime import date, datetime
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
class FeeActivity:
    """One fee the venue charged the account, as the venue booked it.

    **Cash moves for reasons that are not fills, and this is the one that
    matters to us.** `Reconciler._cash_discrepancies` states the platform's
    working assumption — "cash is arithmetic on fills, so a drift beyond the
    tolerance means a fill one of us does not know about" — and that is true of
    a venue which charges nothing else. Alpaca charges regulatory fees (the SEC
    fee and FINRA's TAF, both sell-side) and books them on the account activity
    feed, *not* on the fill: `AlpacaBroker` sets `Fill.fee` to zero at both
    sites it builds one, with a comment at each saying so.

    The consequence is not drift, it is a ratchet. Our cash is a fills-only
    total, the venue's is not, and the gap only ever widens. The account feed
    for 2026-09-08 — one session — held exactly three rows:

        FEE / CAT  -0.01   FEE / REG  -3.82   FEE / TAF  -0.31

    which is the $4.14 that halted the worker at `warmup` the next morning,
    against a $1.00 tolerance. One session breaches it four times over, so
    `adopt_broker_state` clears this for less than a day.

    `activity_id` is the venue's own identifier and it is the idempotency key.
    A fee applied twice is a wrong ledger in the other direction, and a fee
    feed is re-read on every reconcile — so the identifier is what the ledger
    stores, not the date and not the amount.

    `amount` is **positive for money leaving the account**, which is the
    opposite sign to the `net_amount` Alpaca reports. Adapters normalise it, so
    nothing downstream has to remember a venue's convention: settling a fee is
    always `cash -= amount`.
    """

    activity_id: str
    booked_on: date
    amount: Decimal
    #: The venue's own name for the kind of fee — Alpaca books `CAT`, `REG` and
    #: `TAF` as sub-types under one `FEE` activity type. Carried because it is
    #: what an operator reconciling a session reads, and because it is the
    #: field that says whether a charge is one this platform caused.
    sub_type: str = ""
    description: str = ""


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
    #: Which venue this event came from, as `BrokerPort.name` reports it.
    #: Recorded as `Order.rejected_by` when the event refuses the order, so a
    #: rejection pushed on the stream names its refuser like the two
    #: router-side refusal paths do.
    #:
    #: On the event rather than passed alongside it: this is the *venue's* view
    #: of an order, and nothing between the adapter and the applier knows which
    #: venue that is — the runner reaches a broker only through the router
    #: (rule §1.5), so asking it to supply the name would mean handing it the
    #: one dependency that rule exists to keep away from it.
    broker: str | None = None


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

    async def get_fee_activities(self, since: date) -> list[FeeActivity]:
        """Fees the venue charged on or after `since`, oldest first.

        On the port rather than left to the Alpaca adapter because the gap it
        closes is not Alpaca's: any venue that charges a fee it does not put on
        a fill will drift our cash the same way, and the reconciler must be
        able to ask without knowing which venue it is talking to.

        `since` is a date and not an instant because that is the granularity a
        fee is booked at. Callers should ask for more history than they think
        they need — the ledger discards what it has already applied, so a wide
        window costs a larger response and nothing else, while a narrow one
        silently loses a fee booked late.

        A venue that charges nothing outside the fill returns an empty list;
        that is a real answer and not a stub. `SimulatedBroker` is such a
        venue — its cost model charges the fill itself.
        """
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
