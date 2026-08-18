"""Position endpoints."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter

from atp_api.deps import CurrentUser

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("")
async def list_positions(open_only: bool = True) -> list[dict[str, object]]:
    raise NotImplementedError


@router.get("/{symbol}")
async def get_position(symbol: str) -> dict[str, object]:
    raise NotImplementedError


@router.post("/{symbol}/close")
async def close_position(symbol: str, actor: CurrentUser) -> dict[str, object]:
    """Flatten one position at market. Audit-logged."""
    raise NotImplementedError


@router.patch("/{symbol}/stop")
async def update_stop(
    symbol: str, stop_loss_price: Decimal, actor: CurrentUser
) -> dict[str, object]:
    """Adjust a protective stop.

    Widening a stop (moving it away from price) requires an explicit override
    flag and is audit-logged prominently. It is the most common way a
    disciplined system becomes an undisciplined one, usually at the exact moment
    discipline was needed.
    """
    raise NotImplementedError
