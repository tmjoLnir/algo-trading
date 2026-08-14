"""Risk and kill-switch endpoints — requirement #3.

These are the emergency controls. They must work when everything else is
degraded: no heavy queries, no dependency on the worker being alive, minimal
code between the request and the Redis write.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/risk", tags=["risk"])


class HaltRequest(BaseModel):
    scope: str = "global"
    reason: str = "manual"
    detail: str = ""
    target: str | None = None


@router.get("/limits")
async def get_risk_limits() -> dict[str, object]:
    raise NotImplementedError


@router.get("/status")
async def get_risk_status() -> dict[str, object]:
    """Current usage against every limit: exposure, daily P&L, order rate,
    open position count. What a human checks before promoting to live."""
    raise NotImplementedError


@router.post("/halt")
async def engage_kill_switch(payload: HaltRequest, actor: str) -> dict[str, object]:
    """STOP TRADING. Takes effect immediately for all processes.

    No confirmation step by design — hesitation is the expensive part. Clearing
    it is the deliberate action (see below).
    """
    raise NotImplementedError


@router.post("/resume")
async def clear_kill_switch(scope: str, actor: str, target: str | None = None) -> dict[str, object]:
    """Resume trading. Requires a named human and is audit-logged.

    Deliberately asymmetric with `/halt`: stopping is reflexive, restarting is
    a decision.
    """
    raise NotImplementedError


@router.post("/flatten-all")
async def flatten_all(actor: str, confirm: str) -> dict[str, object]:
    """Liquidate everything at market.

    Requires `confirm` to equal the literal string "FLATTEN ALL POSITIONS".
    Irreversible: it realises every open P&L at whatever the market offers.
    """
    raise NotImplementedError


@router.get("/rejections")
async def list_rejections(limit: int = 100) -> list[dict[str, object]]:
    """Recently blocked orders and why.

    A strategy silently doing nothing because a limit rejects it every time
    looks identical to a strategy with no signals — this endpoint is how you
    tell the difference.
    """
    raise NotImplementedError
