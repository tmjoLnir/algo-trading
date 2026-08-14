"""Backtest endpoints — requirement #2.

Backtests are queued to the worker, not run inline: a multi-year minute-bar run
takes minutes and would block an API worker for the duration.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/backtests", tags=["backtests"])


class BacktestRequest(BaseModel):
    strategy_id: str
    symbols: list[str]
    start: datetime
    end: datetime
    timeframe: str = "1d"
    starting_cash: Decimal = Decimal("100000")
    cost_model: str = "alpaca_equities"   # never default to zero-cost


class BacktestOut(BaseModel):
    id: str
    strategy_id: str
    status: str
    metrics: dict[str, float] | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


@router.post("", response_model=BacktestOut, status_code=202)
async def run_backtest(payload: BacktestRequest) -> BacktestOut:
    """Queue a run. Returns immediately with status `queued`.

    Validate data coverage BEFORE queueing — telling the user up front that
    history is missing beats a job that fails four minutes in.
    """
    raise NotImplementedError


@router.get("", response_model=list[BacktestOut])
async def list_backtests(strategy_id: str | None = None, limit: int = 50) -> list[BacktestOut]:
    raise NotImplementedError


@router.get("/{run_id}", response_model=BacktestOut)
async def get_backtest(run_id: str) -> BacktestOut:
    raise NotImplementedError


@router.get("/{run_id}/trades")
async def get_backtest_trades(run_id: str) -> list[dict[str, Any]]:
    """Every simulated trade. Inspecting individual trades is how you catch a
    backtest that is "profitable" because of one impossible fill."""
    raise NotImplementedError


@router.get("/{run_id}/equity-curve")
async def get_backtest_equity_curve(run_id: str) -> dict[str, Any]:
    raise NotImplementedError


@router.post("/compare")
async def compare_backtests(run_ids: list[str]) -> dict[str, Any]:
    """Metrics side by side.

    Beware: comparing many variants and picking the best is how overfitting
    happens. The winner of a 200-way parameter sweep is usually the luckiest
    parameter set, not the best one. See docs/BACKTESTING.md.
    """
    raise NotImplementedError
