"""Order endpoints."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/orders", tags=["orders"])


class ManualOrderRequest(BaseModel):
    symbol: str
    side: str
    qty: Decimal
    order_type: str = "market"
    limit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    reason: str            # required: a manual order with no stated reason is
                           # unreviewable a week later


@router.get("")
async def list_orders(
    status: str | None = None,
    symbol: str | None = None,
    strategy_id: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    raise NotImplementedError


@router.post("", status_code=202)
async def submit_manual_order(payload: ManualOrderRequest, actor: str) -> dict[str, object]:
    """Human-initiated order.

    Goes through the SAME `OrderRouter` and risk engine as a strategy order
    (rule §1.5) — a manual order is not a reason to skip the limits, it is the
    single most common reason to need them. Audit-logged with `actor`.
    """
    raise NotImplementedError


@router.delete("/{order_id}")
async def cancel_order(order_id: str, actor: str) -> dict[str, object]:
    raise NotImplementedError


@router.post("/cancel-all")
async def cancel_all_orders(symbol: str | None = None, actor: str = "") -> dict[str, int]:
    raise NotImplementedError
