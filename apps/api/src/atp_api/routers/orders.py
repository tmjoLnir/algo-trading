"""Order endpoints.

The read is built; the writes are not, and the split is worth stating because
it is not arbitrary. `GET` serves the order table, which is a display of
something the runner already decided and stored. Every other handler here
*places* something, and there is exactly one path from an intent to a venue —
`RiskEngine.validate` then `execution.router.OrderRouter.submit` (rule §1.5).
Wiring that path through this process is its own piece of work with its own
failure paths and its own audit trail (ADR 0010), and it is not made smaller by
being started halfway.

**Why this read exists when the dashboard already shows working orders.** The
live dashboard shows what is working *now*, from the snapshot the worker
published. This shows what happened, terminal orders included — and the ones
that matter most are the orders that never filled at all. A rejection appears in
no other read in the platform: not in the book, not in a reconstructed round
trip, not on the equity curve. `filled_orders` excludes it by design, because it
moved no quantity. So a strategy whose every order is refused looks, from
everywhere else, exactly like a strategy that never placed one.
"""

from __future__ import annotations

#: Imported at runtime, not behind `if TYPE_CHECKING`: FastAPI resolves a
#: handler's annotations when it wires the graph, and a name that exists only for
#: the type checker raises `NameError` on the first request.
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel

from atp_api.deps import CurrentUser, get_order_repository
from atp_core.config import Settings, get_settings
from atp_core.domain import Order, OrderStatus
from atp_core.execution.ports import OrderRepository

router = APIRouter(prefix="/orders", tags=["orders"])

#: Hard ceiling on one page. This is a screen, not an export: the cost of a
#: larger number is paid by the database and by the browser rendering it, and
#: neither has a reader who wanted five thousand rows.
MAX_LIMIT = 500


class ManualOrderRequest(BaseModel):
    symbol: str
    side: str
    qty: Decimal
    order_type: str = "market"
    limit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    reason: str  # required: a manual order with no stated reason is
    # unreviewable a week later


class OrderHistoryView(BaseModel):
    """One order, as the history screen reads it.

    Deliberately **not** `dashboard.OrderView`, which is the same noun from a
    different source: that one is built from the book the worker published and
    describes an order still working. This is read from the order table and has
    to describe a finished one — which needs the three fields that one has no
    use for and would be null on every row it serves.

    `reject_reason` is the field this whole endpoint is for. `purpose` is the
    second: it says whether an order was an entry, a stop or a target, and it is
    the only thing that can distinguish two exits that agree on everything else.

    Every monetary field is a `Decimal` and reaches the browser as a string
    (docs/DASHBOARD.md).
    """

    id: str
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: str
    order_type: str
    time_in_force: str
    qty: Decimal
    filled_qty: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    avg_fill_price: Decimal | None
    status: str
    #: Why this order exists — entry, stop_loss, take_profit, time_exit,
    #: flatten, manual. Null for orders stored before the column existed, and
    #: left null rather than defaulted: labelling a historical exit an "entry"
    #: is worse than admitting the record does not say.
    purpose: str | None
    #: Null unless something refused it. Non-null is the row a reader came for.
    reject_reason: str | None
    strategy_id: str | None
    signal_id: str | None
    #: When the decision was made. Never null on a stored order, and the field
    #: the list is ordered by — `submitted_at` and `filled_at` are null on
    #: exactly the refusals this endpoint exists to show.
    created_at: datetime | None
    submitted_at: datetime | None
    filled_at: datetime | None


class OrdersResponse(BaseModel):
    orders: list[OrderHistoryView]
    #: True when the page came back full, so there may be older orders that did
    #: not fit. Stated rather than inferred: a list that stops at exactly the
    #: limit looks identical to a list that ended, and only one of them means
    #: "this is everything".
    limit_reached: bool
    #: What the run mode was scoped to. Paper and live share a table, and a
    #: screen that did not say which it was showing would be unreadable on a
    #: machine that has run both.
    run_mode: str


def _to_view(order: Order) -> OrderHistoryView:
    return OrderHistoryView(
        id=order.id,
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        symbol=order.symbol,
        side=order.side.value,
        order_type=order.order_type.value,
        time_in_force=order.time_in_force.value,
        qty=order.qty,
        filled_qty=order.filled_qty,
        limit_price=order.limit_price,
        stop_price=order.stop_price,
        avg_fill_price=order.avg_fill_price,
        status=order.status.value,
        purpose=order.purpose,
        reject_reason=order.reject_reason,
        strategy_id=order.strategy_id,
        signal_id=order.signal_id,
        created_at=order.created_at,
        submitted_at=order.submitted_at,
        filled_at=order.filled_at,
    )


def _parse_status(raw: str | None) -> OrderStatus | None:
    """An unknown status is a 422 naming the ones that exist.

    The same refusal `/analytics/attribution` makes for an unknown dimension,
    for the same reason: an empty list is how "you asked for something that does
    not exist" comes to read as "nothing happened". On this screen that reading
    is the worse of the two — somebody filtering for rejections and being told
    there are none would conclude the opposite of the truth.
    """
    if raw is None:
        return None
    try:
        return OrderStatus(raw)
    except ValueError:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"unknown order status {raw!r}; known statuses are "
                f"{', '.join(s.value for s in OrderStatus)}"
            ),
        ) from None


@router.get("", response_model=OrdersResponse)
async def list_orders(
    order_repo: Annotated[OrderRepository, Depends(get_order_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    status: str | None = None,
    symbol: str | None = None,
    strategy_id: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
) -> OrdersResponse:
    """The order table, newest first.

    Read straight from storage rather than from the worker's published book, and
    that is the right source here even though ADR 0007 says the opposite for the
    live screen. That ADR is about a quantity still moving: two processes
    computing "what do we hold" at two instants disagree. A stored order is a
    record of something that already happened, and reading it in this process
    cannot disagree with anything the runner is doing — the same reasoning
    `analytics.py` gives for reconstructing round trips here (ADR 0015).

    Scoped to the configured run mode. The response says which, because paper
    and live orders share a table.
    """
    orders = await order_repo.recent_orders(
        settings.run_mode,
        status=_parse_status(status),
        symbol=symbol,
        strategy_id=strategy_id,
        since=since,
        limit=limit,
    )
    return OrdersResponse(
        orders=[_to_view(order) for order in orders],
        limit_reached=len(orders) == limit,
        run_mode=settings.run_mode.value,
    )


@router.post("", status_code=202)
async def submit_manual_order(payload: ManualOrderRequest, actor: CurrentUser) -> dict[str, object]:
    """Human-initiated order.

    Goes through the SAME `OrderRouter` and risk engine as a strategy order
    (rule §1.5) — a manual order is not a reason to skip the limits, it is the
    single most common reason to need them. Audit-logged with `actor`.
    """
    raise NotImplementedError


@router.delete("/{order_id}")
async def cancel_order(order_id: str, actor: CurrentUser) -> dict[str, object]:
    raise NotImplementedError


@router.post("/cancel-all")
async def cancel_all_orders(actor: CurrentUser, symbol: str | None = None) -> dict[str, int]:
    raise NotImplementedError
