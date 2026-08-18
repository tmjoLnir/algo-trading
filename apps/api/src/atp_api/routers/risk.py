"""Risk and kill-switch endpoints — requirement #3.

These are the emergency controls. They must work when everything else is
degraded: no heavy queries, no dependency on the worker being alive, minimal
code between the request and the Redis write.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from atp_api.auth import authenticate
from atp_api.deps import CurrentUser
from atp_core.config import Settings, get_settings

router = APIRouter(prefix="/risk", tags=["risk"])


class HaltRequest(BaseModel):
    scope: str = "global"
    reason: str = "manual"
    detail: str = ""
    target: str | None = None


class ResumeRequest(BaseModel):
    """Clearing a halt, with the password that proves someone is still there."""

    scope: str = "global"
    target: str | None = None
    #: Re-presented for this one act. In the body and never a query parameter:
    #: a query string is written to nginx's access log verbatim.
    password: str = Field(min_length=1, max_length=1024)


class FlattenAllRequest(BaseModel):
    """Liquidating the book. Two proofs, because it cannot be undone."""

    confirm: str
    password: str = Field(min_length=1, max_length=1024)


def _require_step_up(password: str, actor: str, settings: Settings) -> None:
    """Demand the password again, for an act that cannot be taken back.

    This is what finally enforces docs/RISK.md's "clearing requires a named
    human". A session cookie proves someone logged in at some point in the last
    twelve hours; it does not prove anyone is at the keyboard now. For halting
    that distinction does not matter — hesitation is the expensive part, and
    `/halt` deliberately asks for nothing. For clearing a halt and for
    liquidating the book it is the whole point.

    Deliberately no elevation window. A "recently authenticated" period would be
    a stretch of minutes during which a walked-away laptop can flatten the book,
    which is the exact situation this exists to prevent. The proof travels with
    the act instead.

    403 rather than 401: the session is valid and stays valid. Answering 401
    would send the dashboard to a login screen, which is not what went wrong.
    """
    if authenticate(actor, password, settings) is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="password required for this action",
        )


@router.get("/limits")
async def get_risk_limits() -> dict[str, object]:
    raise NotImplementedError


@router.get("/status")
async def get_risk_status() -> dict[str, object]:
    """Current usage against every limit: exposure, daily P&L, order rate,
    open position count. What a human checks before promoting to live."""
    raise NotImplementedError


@router.post("/halt")
async def engage_kill_switch(payload: HaltRequest, actor: CurrentUser) -> dict[str, object]:
    """STOP TRADING. Takes effect immediately for all processes.

    No confirmation step by design — hesitation is the expensive part. Clearing
    it is the deliberate action (see below).
    """
    raise NotImplementedError


@router.post("/resume")
async def clear_kill_switch(
    payload: ResumeRequest,
    actor: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Resume trading. Requires a named human and is audit-logged.

    Deliberately asymmetric with `/halt`: stopping is reflexive, restarting is
    a decision. The password is where that asymmetry stops being a comment and
    starts being enforced — `/halt` asks for nothing at all, and a read-only
    session may call it; this one asks again and a read-only session may not.
    """
    _require_step_up(payload.password, actor, settings)
    raise NotImplementedError


@router.post("/flatten-all")
async def flatten_all(
    payload: FlattenAllRequest,
    actor: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Liquidate everything at market.

    Requires `confirm` to equal the literal string "FLATTEN ALL POSITIONS", and
    the account password with it. Irreversible: it realises every open P&L at
    whatever the market offers.

    Both proofs, not either. The phrase shows the caller knows what this does;
    the password shows they are the person entitled to do it. A copied session
    cookie satisfies neither on its own.
    """
    _require_step_up(payload.password, actor, settings)
    raise NotImplementedError


@router.get("/rejections")
async def list_rejections(limit: int = 100) -> list[dict[str, object]]:
    """Recently blocked orders and why.

    A strategy silently doing nothing because a limit rejects it every time
    looks identical to a strategy with no signals — this endpoint is how you
    tell the difference.
    """
    raise NotImplementedError
