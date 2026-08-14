"""Analytics and reporting endpoints — requirement #6."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/performance")
async def get_performance(
    start: date | None = None, end: date | None = None, strategy_id: str | None = None
) -> dict[str, object]:
    """Full metric set over a period."""
    raise NotImplementedError


@router.get("/trades")
async def list_trades(
    start: date | None = None,
    end: date | None = None,
    strategy_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, object]]:
    """Completed round trips with MAE/MFE."""
    raise NotImplementedError


@router.get("/attribution")
async def get_attribution(
    by: str = "strategy", start: date | None = None, end: date | None = None
) -> list[dict[str, object]]:
    """P&L grouped by strategy | symbol | weekday | hour | exit_reason."""
    raise NotImplementedError


@router.get("/live-vs-backtest/{strategy_id}")
async def live_vs_backtest(strategy_id: str) -> dict[str, object]:
    """Is live performing as the backtest promised?

    The most important report here. Persistent negative divergence means the
    backtest was wrong — overfitting, unmodelled costs, or unachievable fills.
    """
    raise NotImplementedError


@router.get("/reports/daily")
async def daily_report(day: date | None = None, output_format: str = "json") -> dict[str, object]:
    """End-of-day summary: P&L, trades, rejections, halts, feed incidents."""
    raise NotImplementedError
