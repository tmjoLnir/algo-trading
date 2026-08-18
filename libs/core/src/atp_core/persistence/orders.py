"""Order storage — the `OrderRepository` port over PostgreSQL.

An `Order` is not a value object. It accumulates fills over its life, so this
is written for repeated saves of the same order rather than one insert: an
upsert on the order's id, and an append of whichever fills are not stored yet.

Two identities do the work, and they are different on purpose.

`client_order_id` carries a unique constraint in the schema — that is the
database-level half of rule §1.4, and a duplicate submit fails loudly there
rather than double-filling. But it is not what this upserts on, because the
same key legitimately arrives many times as one order fills in pieces. The
primary key is.

A `Fill` has no id of its own, so one is derived deterministically from the
order and the fill's position in its sequence. Fills are only ever appended, so
that index is stable, and re-saving an order therefore recomputes the same ids
and inserts nothing new. `venue_fill_id` is stored alongside and is unique in
the schema, so a venue that redelivers an execution collides there too.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from atp_core.domain import Fill, Order, OrderStatus, OrderType, Side, TimeInForce
from atp_core.logging import get_logger
from atp_core.persistence.db import session_scope
from atp_core.persistence.models import FillRow, OrderRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from atp_core.domain import RunMode
    from atp_core.execution.ports import OrderRepository

log = get_logger(__name__)

#: Namespace for deriving a fill's row id. Fixed, so the same fill computes the
#: same id in every process and on every re-save — which is what makes storing
#: an order twice a no-op rather than a duplicated position.
_FILL_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _fill_id(order_id: str, index: int) -> str:
    return str(uuid.uuid5(_FILL_NAMESPACE, f"{order_id}:{index}"))


class PostgresOrderRepository:
    """`OrderRepository` over PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, order: Order, *, run_mode: RunMode) -> None:
        """Upsert the order, then append any fills not already stored."""
        async with session_scope(self._session_factory) as session:
            await session.execute(
                pg_insert(OrderRow)
                .values(**self._order_values(order, run_mode))
                .on_conflict_do_update(
                    index_elements=[OrderRow.id],
                    # Deliberately partial. The immutable half of an order —
                    # symbol, side, quantity, type — is not updated, because a
                    # row whose side changed under the same id would be a bug
                    # this write should not be able to express.
                    set_={
                        "broker_order_id": order.broker_order_id,
                        "status": order.status.value,
                        "filled_qty": order.filled_qty,
                        "avg_fill_price": order.avg_fill_price,
                        "reject_reason": order.reject_reason,
                        "submitted_at": order.submitted_at,
                        "filled_at": order.filled_at,
                    },
                )
            )

            for index, fill in enumerate(order.fills):
                await session.execute(
                    pg_insert(FillRow)
                    .values(
                        id=_fill_id(order.id, index),
                        order_id=order.id,
                        venue_fill_id=fill.venue_fill_id,
                        ts=fill.ts,
                        qty=fill.qty,
                        price=fill.price,
                        fee=fill.fee,
                    )
                    # Nothing to update: a fill is immutable once printed. A
                    # second write of the same one is a re-save, not news.
                    .on_conflict_do_nothing(index_elements=[FillRow.id])
                )

    async def open_orders(self, run_mode: RunMode) -> list[Order]:
        """Every non-terminal order for this run mode, oldest first."""
        terminal = [s.value for s in OrderStatus if s.is_terminal]
        async with session_scope(self._session_factory) as session:
            rows = (
                (
                    await session.execute(
                        select(OrderRow)
                        .where(
                            OrderRow.run_mode == run_mode.value,
                            OrderRow.status.notin_(terminal),
                        )
                        .order_by(OrderRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            fills_by_order = await self._fills_for(session, [row.id for row in rows])

        return [self._to_order(row, fills_by_order.get(row.id, [])) for row in rows]

    @staticmethod
    async def _fills_for(session: AsyncSession, order_ids: list[str]) -> dict[str, list[FillRow]]:
        if not order_ids:
            return {}
        rows = (
            (await session.execute(select(FillRow).where(FillRow.order_id.in_(order_ids))))
            .scalars()
            .all()
        )
        grouped: dict[str, list[FillRow]] = {}
        for row in rows:
            grouped.setdefault(row.order_id, []).append(row)
        # Chronological, because `Order.apply_fill` recomputes a volume-weighted
        # average incrementally and replaying prints out of order would produce
        # a different one.
        for fills in grouped.values():
            fills.sort(key=lambda f: f.ts)
        return grouped

    @staticmethod
    def _order_values(order: Order, run_mode: RunMode) -> dict[str, object]:
        return {
            "id": order.id,
            "client_order_id": order.client_order_id,
            "broker_order_id": order.broker_order_id,
            "strategy_id": None,  # set once strategies are rows; FK would fail otherwise
            "signal_id": None,
            "parent_order_id": order.parent_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "time_in_force": order.time_in_force.value,
            "qty": order.qty,
            "limit_price": order.limit_price,
            "stop_price": order.stop_price,
            "status": order.status.value,
            "filled_qty": order.filled_qty,
            "avg_fill_price": order.avg_fill_price,
            "reject_reason": order.reject_reason,
            "run_mode": run_mode.value,
            "created_at": order.created_at,
            "submitted_at": order.submitted_at,
            "filled_at": order.filled_at,
        }

    @staticmethod
    def _to_order(row: OrderRow, fills: list[FillRow]) -> Order:
        """Rebuild an order, replaying its fills through `apply_fill`.

        Replayed rather than assigned: `filled_qty` and `avg_fill_price` are
        the output of the same arithmetic every other fill in the platform goes
        through, and restoring them by assignment would let a stored row that
        disagrees with its own fills survive as though it did not.
        """
        order = Order(
            symbol=row.symbol,
            side=Side(row.side),
            qty=row.qty,
            order_type=OrderType(row.order_type),
            time_in_force=TimeInForce(row.time_in_force),
            limit_price=row.limit_price,
            stop_price=row.stop_price,
            id=row.id,
            client_order_id=row.client_order_id,
            broker_order_id=row.broker_order_id,
            parent_order_id=row.parent_order_id,
            created_at=row.created_at,
            submitted_at=row.submitted_at,
        )
        for stored in fills:
            order.apply_fill(
                Fill(
                    order_id=order.id,
                    ts=stored.ts,
                    qty=stored.qty,
                    price=stored.price,
                    fee=stored.fee,
                    venue_fill_id=stored.venue_fill_id,
                )
            )
        # `apply_fill` sets FILLED / PARTIALLY_FILLED from the arithmetic. Any
        # other stored status is restored as it was — a cancelled order is
        # cancelled whatever its fills say.
        stored_status = OrderStatus(row.status)
        if stored_status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            order.status = stored_status
        order.reject_reason = row.reject_reason
        return order


def _typecheck(repo: PostgresOrderRepository) -> OrderRepository:
    """Structural conformance, checked by mypy rather than asserted at runtime."""
    return repo
