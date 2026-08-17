"""In-memory test doubles.

`FakeBroker` is the only broker the test suite may talk to (CLAUDE.md §1.7). It
is deliberately not `SimulatedBroker`: that one is a fill *simulator* whose
realism bounds every backtest, and it is a Phase 4 item of its own. This is a
controllable stand-in for a venue, built to be told to fail.

The failures it can be told to produce are the ones that matter:

- a partial fill, so a position update meets a fill *sequence* (CLAUDE.md §5);
- a venue rejection, which is an ordinary outcome and not an exception path;
- a submit that times out **after** the venue accepted the order, which is the
  case that makes a blind resubmit create a second position;
- a submit that times out having never reached the venue;
- a broker that is unreachable for reads as well as writes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.brokers.ports import AccountSnapshot
from atp_core.domain import Fill, Order, OrderStatus, OrderType, Position
from atp_core.errors import BrokerConnectionError, OrderRejectedError

if TYPE_CHECKING:
    from atp_core.domain import Side


class FakeBroker:
    """A `BrokerPort` you can make misbehave on demand."""

    def __init__(self, *, equity: Decimal = Decimal("100000")) -> None:
        self.equity = equity
        #: client_order_id → the venue's copy of the order. A *copy*: an adapter
        #: that handed back the caller's own object would make adoption look
        #: like it worked when nothing was adopted.
        self.accepted: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}
        self.cancelled: list[str] = []
        self.submit_calls: list[str] = []
        self.market_open = True

        # ── the levers ──────────────────────────────────────────────────────
        #: Reject the next submit with this reason (a venue refusal).
        self.reject_next: str | None = None
        #: Time out the next submit. `accept_on_timeout` decides whether the
        #: venue got it anyway — the difference between a lost order and a
        #: hidden one, and the whole reason not to resubmit blind.
        self.timeout_next = False
        self.accept_on_timeout = False
        #: Reads fail too — an adapter cannot look up what it cannot reach.
        self.reads_fail = False

        self._next_id = 0

    # ── BrokerPort ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "fake"

    @property
    def supports_fractional(self) -> bool:
        return True

    async def get_account(self) -> AccountSnapshot:
        self._guard_reads()
        return AccountSnapshot(
            account_id="fake-account",
            equity=self.equity,
            cash=self.equity,
            buying_power=self.equity,
            maintenance_margin=Decimal(0),
            is_pattern_day_trader=False,
            trading_blocked=False,
            as_of=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
        )

    async def submit_order(self, order: Order) -> Order:
        self.submit_calls.append(order.client_order_id)

        if self.reject_next is not None:
            reason, self.reject_next = self.reject_next, None
            raise OrderRejectedError(reason)

        if self.timeout_next:
            self.timeout_next = False
            if self.accept_on_timeout:
                self._accept(order)
            raise BrokerConnectionError("connection reset by peer")

        # Idempotent on client_order_id, as the port requires: a resubmit of a
        # key the venue already holds returns what it holds rather than opening
        # a second order.
        if order.client_order_id in self.accepted:
            return self.accepted[order.client_order_id]
        return self._accept(order)

    async def cancel_order(self, broker_order_id: str) -> None:
        self._guard_reads()
        self.cancelled.append(broker_order_id)
        for held in self.accepted.values():
            if held.broker_order_id == broker_order_id and not held.is_complete:
                held.status = OrderStatus.CANCELLED

    async def get_order(self, broker_order_id: str) -> Order | None:
        self._guard_reads()
        return next(
            (o for o in self.accepted.values() if o.broker_order_id == broker_order_id), None
        )

    async def get_open_orders(self) -> list[Order]:
        self._guard_reads()
        return [o for o in self.accepted.values() if not o.is_complete]

    async def get_positions(self) -> list[Position]:
        self._guard_reads()
        return [p for p in self.positions.values() if not p.is_flat]

    async def close_position(self, symbol: str) -> Order:
        raise NotImplementedError("the router flattens through submit(), never through here")

    async def close_all_positions(self) -> list[Order]:
        raise NotImplementedError("emergency flatten is a runbook path, not a router path")

    async def is_market_open(self) -> bool:
        self._guard_reads()
        return self.market_open

    # ── the levers, operated ────────────────────────────────────────────────

    def fill(
        self,
        client_order_id: str,
        qty: Decimal,
        price: Decimal,
        *,
        fee: Decimal = Decimal(0),
        at: datetime | None = None,
    ) -> Fill:
        """Fill part or all of an accepted order, venue-side."""
        held = self.accepted[client_order_id]
        fill = Fill(
            order_id=held.id,
            ts=at or datetime(2024, 6, 3, 14, 31, tzinfo=UTC),
            qty=qty,
            price=price,
            fee=fee,
        )
        held.apply_fill(fill)
        return fill

    def hold(self, symbol: str, qty: Decimal, avg_entry_price: Decimal) -> None:
        """Give the venue a position, for reconciliation-shaped assertions."""
        self.positions[symbol] = Position(
            symbol=symbol, qty=qty, avg_entry_price=avg_entry_price, last_price=avg_entry_price
        )

    def order_for(self, client_order_id: str) -> Order:
        return self.accepted[client_order_id]

    def open_order_count(
        self,
        symbol: str | None = None,
        side: Side | None = None,
        order_type: OrderType | None = None,
    ) -> int:
        return sum(
            1
            for o in self.accepted.values()
            if not o.is_complete
            and (symbol is None or o.symbol == symbol)
            and (side is None or o.side is side)
            and (order_type is None or o.order_type is order_type)
        )

    def open_stops(self, symbol: str | None = None, side: Side | None = None) -> int:
        """Protective orders only — an entry resting at the venue is not one."""
        return self.open_order_count(symbol, side, OrderType.STOP)

    # ── internals ───────────────────────────────────────────────────────────

    def _accept(self, order: Order) -> Order:
        self._next_id += 1
        held = replace(
            order,
            broker_order_id=f"brk-{self._next_id}",
            status=OrderStatus.SUBMITTED,
            fills=[],
        )
        self.accepted[order.client_order_id] = held
        return held

    def _guard_reads(self) -> None:
        if self.reads_fail:
            raise BrokerConnectionError("broker unreachable")


class FakeKillSwitch:
    """Records halts instead of reaching Redis."""

    def __init__(self, engaged: bool = False) -> None:
        self.engaged = engaged
        self.engagements: list[tuple[str, str, str, str]] = []

    def is_engaged(self, strategy_id: str | None = None, symbol: str | None = None) -> bool:
        return self.engaged

    def engage(
        self,
        scope: object,
        reason: object,
        engaged_by: str,
        detail: str = "",
        target: str | None = None,
    ) -> object:
        self.engaged = True
        self.engagements.append((str(scope), str(reason), engaged_by, detail))
        return None

    def clear(self, scope: object, cleared_by: str, target: str | None = None) -> None:
        self.engaged = False

    def active_halts(self) -> list[object]:
        return []
