"""Order endpoints.

The read and both cancels are built; `POST /orders` is not, and the line between
them is the one rule §1.5 draws. Cancelling *withdraws* an intent: it consults
no risk rule, because there is no order to judge — `OrderRouter.cancel_all` asks
the venue what is open and retracts it. `POST /orders` *places* one, and there is
exactly one path from an intent to a venue — `RiskEngine.validate` then
`OrderRouter.submit`. That path now exists in this process (`atp_api.execution`)
and `POST /positions/{symbol}/close` is the first handler to use it, so what is
left here is not the wiring: it is a manual order's own decisions — what sizing
a hand-typed quantity is checked against, and what a stop attached to it means
when no strategy owns the position afterwards. Nothing on a screen asks for one
yet, and it is not made smaller by being started halfway.

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

from atp_api.deps import (
    CurrentUser,
    get_audit_sink,
    get_broker,
    get_calendar,
    get_clock,
    get_effective_risk_limits,
    get_kill_switch,
    get_order_repository,
)
from atp_api.execution import build_router
from atp_core.audit.ports import Action, AuditEntry, AuditSink
from atp_core.brokers import BrokerPort
from atp_core.clock import Clock, TradingCalendar
from atp_core.config import Settings, get_settings
from atp_core.domain import Order, OrderStatus
from atp_core.errors import ATPError
from atp_core.execution.ports import OrderRepository
from atp_core.risk.killswitch import KillSwitch
from atp_core.risk.limits import RiskLimits

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

    `reject_reason` is the field this whole endpoint is for, and `rejected_by`
    is its other half — why, and who. `purpose` is the second: it says whether
    an order was an entry, a stop or a target, and it is the only thing that can
    distinguish two exits that agree on everything else.

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
    #: *Who* refused it, where `reject_reason` is *why* — the risk rule that
    #: said no (`max_gross_exposure`), the pre-rule stage `routing`, or the
    #: broker's name when the venue refused. `status` says which of the two it
    #: is: `rejected_risk` names one of ours, `rejected` names the venue.
    #:
    #: Null on a refusal stored before the column existed, which is not the
    #: same fact as null on an order nothing refused. The screen distinguishes
    #: them, because a rule name is the string that gets a reader from a
    #: refusal to the limit that predicted it — the cross-reference the risk
    #: limits panel is built around.
    rejected_by: str | None
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
        rejected_by=order.rejected_by,
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
async def cancel_order(
    order_id: str,
    actor: CurrentUser,
    broker: Annotated[BrokerPort, Depends(get_broker)],
    audit: Annotated[AuditSink, Depends(get_audit_sink)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> dict[str, object]:
    """Cancel one working order.

    `order_id` is matched against the venue's id **or** our `client_order_id`,
    because both are things a human legitimately has in front of them: the
    order table shows ours, an Alpaca screen shows theirs, and refusing the one
    someone is holding is a way to make an operator retype an id during an
    incident.

    Resolved from `BrokerPort.get_open_orders()` rather than from our own table,
    which is the argument `OrderRouter.cancel_all` makes and it applies here
    with more force: the broker is the truth and our state a cache
    (docs/ARCHITECTURE.md), and after a restart the orders missing from the
    cache are exactly the ones most likely to still be working.

    404 when nothing working matches. That covers both "no such order" and
    "already terminal", and the two are deliberately not distinguished: an order
    that filled while the operator was deciding is not an error to explain, and
    the honest answer to "cancel this" is that there is nothing to cancel.
    """
    try:
        working = await broker.get_open_orders()
    except ATPError as exc:
        # Distinct from the refusal below and worth its own reply: we never
        # established what is working, so nothing can be said about this order
        # at all — including that it does not exist.
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"could not read the venue's open orders: {exc}. Nothing was cancelled, "
                "and whether this order is still working is unknown."
            ),
        ) from exc

    target = next(
        (o for o in working if order_id in {o.broker_order_id, o.client_order_id}),
        None,
    )
    if target is None or target.broker_order_id is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=(
                f"no working order at the venue matches {order_id!r} — it may have "
                "filled, been cancelled already, or never reached the broker"
            ),
        )

    try:
        await broker.cancel_order(target.broker_order_id)
    except ATPError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"the venue refused to cancel {target.symbol} order "
                f"{target.broker_order_id}: {exc}. It is still working."
            ),
        ) from exc

    # After the venue confirms, never before: a row claiming a cancel that did
    # not take has a reader stop looking for an order that is still live.
    await audit.record(
        AuditEntry(
            at=clock.now(),
            actor=actor,
            action=Action.ORDER_CANCELLED,
            target=target.symbol,
            detail={
                "broker_order_id": target.broker_order_id,
                "client_order_id": target.client_order_id,
                "qty": str(target.qty),
                "filled_qty": str(target.filled_qty),
                "purpose": target.purpose,
            },
        )
    )
    return {
        "cancelled": True,
        "symbol": target.symbol,
        "broker_order_id": target.broker_order_id,
        "client_order_id": target.client_order_id,
    }


@router.post("/cancel-all")
async def cancel_all_orders(
    actor: CurrentUser,
    broker: Annotated[BrokerPort, Depends(get_broker)],
    kill_switch: Annotated[KillSwitch, Depends(get_kill_switch)],
    clock: Annotated[Clock, Depends(get_clock)],
    calendar: Annotated[TradingCalendar, Depends(get_calendar)],
    limits: Annotated[RiskLimits, Depends(get_effective_risk_limits)],
    audit: Annotated[AuditSink, Depends(get_audit_sink)],
    symbol: str | None = None,
) -> dict[str, int]:
    """Cancel every working order, or every one for `symbol`.

    Through `OrderRouter.cancel_all` rather than a loop written here. That
    method is the tested home for the two things this must get right — asking
    the venue what is open instead of trusting our cache, and attempting every
    order before reporting a failure, so one stubborn cancel does not abandon
    the other nine.

    The router is built with **no quotes**, which would deny every order on
    `StaleDataRule` if the chain were consulted. It is not: cancelling places
    nothing, so `RiskEngine.validate` is never reached. Passing an empty map
    rather than reading the cache says that at the call site instead of leaving
    a reader to work out which rules a cancel runs.

    **This does not close positions.** Cancelling a protective stop leaves the
    position it was protecting naked, which is why the runbook's emergency path
    is `POST /risk/flatten-all` — that one cancels *and* closes, in that order.
    """
    router_ = build_router(
        broker=broker,
        kill_switch=kill_switch,
        clock=clock,
        calendar=calendar,
        limits=limits,
        quotes={},
    )
    try:
        cancelled = await router_.cancel_all(symbol)
    except ATPError as exc:
        # `cancel_all` raises only once it has attempted every order, so some
        # may well have gone through. 502 with the detail, rather than a count
        # that would imply the rest are still working when they are not.
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=f"{exc} — re-read the order book before assuming anything is still working",
        ) from exc

    await audit.record(
        AuditEntry(
            at=clock.now(),
            actor=actor,
            action=Action.ORDER_CANCELLED,
            target=symbol,
            detail={"scope": symbol or "all", "cancelled": cancelled},
        )
    )
    return {"cancelled": cancelled}
