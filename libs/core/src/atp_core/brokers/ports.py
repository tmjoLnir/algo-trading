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

    from atp_core.domain import Order, Position


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
