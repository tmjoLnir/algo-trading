"""Strategy CRUD and lifecycle — requirement #1.

Promotion is a one-way ratchet: draft → backtest → paper → live. A strategy
cannot skip a stage. The API enforces it because the discipline is worth more
than the convenience, and because "just this once" is how untested code reaches
a live account.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/strategies", tags=["strategies"])


class StrategyCreate(BaseModel):
    name: str
    description: str = ""
    kind: str  # "coded" (registered class) | "ruleset" (declarative)
    class_name: str | None = None
    params: dict[str, Any] = {}
    ruleset: dict[str, Any] | None = None   # validated against RuleSet
    universe: list[str]
    timeframe: str = "1d"
    risk_config: dict[str, Any] = {}


class StrategyOut(StrategyCreate):
    id: str
    state: str
    created_at: datetime
    updated_at: datetime


@router.get("", response_model=list[StrategyOut])
async def list_strategies(state: str | None = None) -> list[StrategyOut]:
    raise NotImplementedError


@router.post("", response_model=StrategyOut, status_code=201)
async def create_strategy(payload: StrategyCreate) -> StrategyOut:
    """Validate before storing.

    A `ruleset` is parsed through `RuleSet` here so a malformed rule fails at
    creation with a clear message, not at 09:31 next Tuesday inside the worker.
    """
    raise NotImplementedError


@router.get("/available")
async def list_available_strategy_classes() -> list[dict[str, Any]]:
    """Registered strategy classes with their params JSON Schema — the frontend
    renders its configuration form from this."""
    raise NotImplementedError


@router.get("/{strategy_id}", response_model=StrategyOut)
async def get_strategy(strategy_id: str) -> StrategyOut:
    raise NotImplementedError


@router.patch("/{strategy_id}", response_model=StrategyOut)
async def update_strategy(strategy_id: str, payload: dict[str, Any]) -> StrategyOut:
    """Editing a strategy that is live requires pausing it first — swapping
    parameters underneath open positions leaves them orphaned from the logic
    that opened them."""
    raise NotImplementedError


@router.post("/{strategy_id}/promote")
async def promote_strategy(strategy_id: str, target_state: str, confirmed_by: str) -> StrategyOut:
    """Advance a stage.

    Promotion to `live` additionally requires: a completed backtest on record,
    a minimum paper-trading period, `ATP_ALLOW_LIVE_TRADING=true`, and an audit
    entry naming a human. See docs/SAFETY.md.
    """
    raise NotImplementedError


@router.post("/{strategy_id}/pause")
async def pause_strategy(strategy_id: str, close_positions: bool = False) -> StrategyOut:
    """Stop generating signals. Existing positions keep their stops unless
    `close_positions` — pausing must not silently strip protection."""
    raise NotImplementedError
