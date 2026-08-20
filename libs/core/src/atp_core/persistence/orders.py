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
from atp_core.execution.idempotency import UNKNOWN_PURPOSE
from atp_core.logging import get_logger
from atp_core.persistence.db import session_scope
from atp_core.persistence.models import FillRow, OrderRow

if TYPE_CHECKING:
    from datetime import datetime

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

    async def filled_orders(
        self, run_mode: RunMode, *, until: datetime, strategy_id: str | None = None
    ) -> list[Order]:
        """Every order with at least one fill up to `until`, oldest first.

        Filtered on the stored `filled_qty` rather than on status, because both
        `FILLED` and `PARTIALLY_FILLED` moved quantity and so did a `CANCELLED`
        order that filled half before the cancel landed. Selecting on status
        would drop that last one, and a partial fill nobody accounted for is a
        position the reconstruction believes closed when it did not.

        Ordered by `created_at`, which is the decision instant rather than the
        fill instant. That is the order the FIFO matcher wants: an entry decided
        before an exit is the entry that exit closes, even on the rare occasion
        the venue prints them out of sequence.
        """
        query = (
            select(OrderRow)
            .where(
                OrderRow.run_mode == run_mode.value,
                OrderRow.filled_qty > 0,
                OrderRow.created_at <= until,
            )
            .order_by(OrderRow.created_at, OrderRow.id)
        )
        if strategy_id is not None:
            query = query.where(OrderRow.strategy_id == strategy_id)

        async with session_scope(self._session_factory) as session:
            rows = (await session.execute(query)).scalars().all()
            fills_by_order = await self._fills_for(session, [row.id for row in rows])

        return [self._to_order(row, fills_by_order.get(row.id, [])) for row in rows]

    async def recent_orders(
        self,
        run_mode: RunMode,
        *,
        status: OrderStatus | None = None,
        symbol: str | None = None,
        strategy_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[Order]:
        """Orders for display, newest first — see the port for why it is bounded.

        Ordered by `created_at` descending with the id as a tie-break, so a page
        is stable: two orders decided in the same microsecond would otherwise
        swap places between two reads of the same data, and the row a person is
        looking at would move under them.

        `created_at` rather than `filled_at` or `submitted_at`, because those are
        null on exactly the orders this read exists to surface. Sorting on either
        would drop every rejection to the bottom of the list, or off the end of
        it — a screen that hides refusals is the failure this is here to prevent.

        The filters compose, and every one of them is applied in SQL rather than
        after the limit: filtering the page would return the last hundred orders
        that happen to be rejections, which is a different question from the
        last hundred rejections and reads identically.
        """
        query = select(OrderRow).where(OrderRow.run_mode == run_mode.value)
        if status is not None:
            query = query.where(OrderRow.status == status.value)
        if symbol is not None:
            # Uppercase because a symbol is always an uppercase ticker
            # (CLAUDE.md §4) and a filter typed in lower case finding nothing
            # reads as "no such orders" rather than as "no such spelling".
            query = query.where(OrderRow.symbol == symbol.upper())
        if strategy_id is not None:
            query = query.where(OrderRow.strategy_id == strategy_id)
        if since is not None:
            query = query.where(OrderRow.created_at >= since)
        query = query.order_by(OrderRow.created_at.desc(), OrderRow.id.desc()).limit(limit)

        async with session_scope(self._session_factory) as session:
            rows = (await session.execute(query)).scalars().all()
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
            # Both were hardcoded `None` with a note that a strategies row would
            # have to exist first. `PostgresStrategyRepository.ensure` and
            # `PostgresSignalRepository.save` are that row and that signal, and
            # the runner calls them before it saves an order — so these now
            # carry the join that makes an order attributable to the decision
            # behind it. A caller that skips those writes gets a foreign-key
            # violation here, which is the intended outcome: a null was how this
            # gap stayed invisible.
            "strategy_id": order.strategy_id,
            "signal_id": order.signal_id,
            "purpose": order.purpose,
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
            strategy_id=row.strategy_id,
            signal_id=row.signal_id,
            parent_order_id=row.parent_order_id,
            # Rows written before the column existed have none. `Order` refuses
            # an empty purpose, so the fallback is the default rather than "":
            # a historical order genuinely does not know, and the analytics
            # layer reports that as unattributable rather than as an entry.
            purpose=row.purpose or UNKNOWN_PURPOSE,
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
