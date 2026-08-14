"""Market-data endpoints — charts and symbol search."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

router = APIRouter(prefix="/market-data", tags=["market-data"])


@router.get("/bars/{symbol}")
async def get_bars(
    symbol: str, timeframe: str = "1d", start: datetime | None = None, end: datetime | None = None
) -> dict[str, object]:
    """Historical bars for charting. Served from our own store, not the vendor —
    charts must not consume the broker's rate limit."""
    raise NotImplementedError


@router.get("/quote/{symbol}")
async def get_quote(symbol: str) -> dict[str, object]:
    """Latest cached quote. Include `age_seconds` in the response so the client
    can grey out a stale price rather than displaying it as current."""
    raise NotImplementedError


@router.get("/search")
async def search_symbols(q: str, limit: int = 20) -> list[dict[str, str]]:
    raise NotImplementedError


@router.get("/calendar")
async def get_market_calendar(
    start: datetime | None = None, end: datetime | None = None
) -> list[dict[str, object]]:
    """Sessions, holidays and early closes."""
    raise NotImplementedError
