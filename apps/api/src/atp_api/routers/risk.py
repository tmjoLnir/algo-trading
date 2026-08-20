"""Risk and kill-switch endpoints — requirement #3.

These are the emergency controls. They must work when everything else is
degraded: no heavy queries, no dependency on the worker being alive, minimal
code between the request and the Redis write.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from atp_api.auth import authenticate
from atp_api.deps import CurrentUser, get_audit_sink, get_clock, get_kill_switch
from atp_core.audit.ports import Action, AuditEntry, AuditSink
from atp_core.clock import Clock
from atp_core.config import Settings, get_settings
from atp_core.logging import get_logger
from atp_core.risk.killswitch import HaltReason, HaltScope, KillSwitch

log = get_logger(__name__)

router = APIRouter(prefix="/risk", tags=["risk"])


class HaltRequest(BaseModel):
    """What to stop. Everything, by default.

    `scope` and `reason` are the domain enums rather than bare strings, so an
    unrecognised value is a 422 naming the ones that exist instead of a halt
    recorded under a reason nothing will ever query for.

    The defaults are the point of the whole endpoint: a client that knows only
    "stop" sends `{}` and stops everything.
    """

    scope: HaltScope = HaltScope.GLOBAL
    reason: HaltReason = HaltReason.MANUAL
    detail: str = ""
    target: str | None = None

    @model_validator(mode="after")
    def _scope_and_target_agree(self) -> HaltRequest:
        """A narrowed scope needs something to narrow to, and global needs none.

        The same pairing `scope/halt.py` enforces on the command line, and it is
        worth refusing rather than interpreting. `{"scope": "symbol"}` with no
        target would key a halt on the literal string `None`, which halts
        nothing and reads on the banner as though it halted something.
        """
        if self.scope is HaltScope.GLOBAL and self.target is not None:
            raise ValueError("target is meaningless with scope 'global' — it halts everything")
        if self.scope is not HaltScope.GLOBAL and not self.target:
            raise ValueError(f"scope '{self.scope.value}' needs a target (a strategy id or symbol)")
        return self


class HaltEngagedView(BaseModel):
    """The halt that is now in force — not necessarily the one just requested.

    `engage` is idempotent and returns the *original* record when a halt is
    already active, so these fields can name an earlier person and an earlier
    time. That is the answer, not a bug in it: if `engaged_by` is not you, your
    request changed nothing because trading was already stopped, and the record
    of who stopped it first is the one worth keeping.

    Deliberately not `dashboard.HaltView`, which is a row in an aggregate
    describing the world. This answers one question about one request.

    `datetime` is imported at runtime rather than behind `TYPE_CHECKING` because
    FastAPI resolves these annotations when it builds the schema — one that
    existed only to the type checker would import cleanly and fail on the first
    request (`test_api_contract.py::test_openapi_schema_generates`).
    """

    scope: str
    reason: str
    engaged_at: datetime
    engaged_by: str
    detail: str
    target: str | None


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
async def engage_kill_switch(
    payload: HaltRequest,
    actor: CurrentUser,
    kill_switch: Annotated[KillSwitch, Depends(get_kill_switch)],
    audit: Annotated[AuditSink, Depends(get_audit_sink)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> HaltEngagedView:
    """STOP TRADING. Takes effect immediately for all processes.

    No confirmation step by design — hesitation is the expensive part. Clearing
    it is the deliberate action (see below), and a read-only session may still
    call this one: `deps.READ_ONLY_MAY_CALL` names it, because the person
    watching the book from a phone is exactly who most needs to stop it.

    `engaged_by` is the session's user and never a field the caller supplies.
    An actor a request can name is not an audit trail (`deps.get_current_user`).

    Off the event loop, because the switch is synchronous — it has to be, since
    the risk chain that consults it is — and this handler must not block every
    other request on one Redis round trip. The dashboard's halt read does the
    same for the same reason.

    **A failure here is a 503 that says trading did not stay stopped**, which is
    the opposite of what an operator would assume from a red error on a halt
    button. `RedisKillSwitch.engage` deliberately does not swallow its
    exceptions, and the reason the message has to be explicit is the interaction
    with `is_engaged`, which fails *closed*: while Redis is unreachable nothing
    trades, so the moment of the failure is genuinely safe. But nothing was
    written, so trading resumes the instant Redis comes back. Reporting only
    "could not halt" would leave a reader to guess which of those two states
    they are in.
    """
    try:
        record = await asyncio.to_thread(
            kill_switch.engage,
            payload.scope,
            payload.reason,
            actor,
            payload.detail,
            payload.target,
        )
    except Exception as exc:
        log.critical(
            "risk.halt_failed",
            error=str(exc),
            actor=actor,
            scope=payload.scope.value,
            effect="nothing was written — trading resumes when the store recovers",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "the halt was NOT recorded: "
                f"{exc}. Orders are being refused for as long as the store is "
                "unreachable, because the switch fails closed — but nothing was "
                "written, so trading resumes on its own when it recovers. Stop "
                "the worker, or re-halt once the store is back, and confirm with "
                "`scripts/halt.py status`."
            ),
        ) from exc

    # Written after the engage, never before: a row claiming a halt that did not
    # take is read as "we stopped" by whoever reviews the incident. The write
    # cannot fail the request — the sink never raises and never refuses the
    # action (atp_core.audit.ports), because a platform that declined to stop
    # trading over an unreachable Postgres would have its failure modes exactly
    # inverted.
    #
    # `reason` and `detail` come from the **request**, not from the record that
    # came back, and the difference only shows when a halt was already active.
    # This row is an account of what a person did, so it has to say what they
    # asked for: an operator pressing the button during an automated
    # `data_feed_lost` halt acted for their own reasons, and copying the
    # automation's onto their row would attribute a machine's diagnosis to a
    # human. `scope` and `target` cannot diverge — they are the key `engage`
    # looked the existing halt up by — so they are the same either way.
    await audit.record(
        AuditEntry(
            at=clock.now(),
            actor=actor,
            action=Action.HALT_ENGAGED,
            target=payload.target,
            detail={
                "scope": payload.scope.value,
                "reason": payload.reason.value,
                "detail": payload.detail,
                # Whether this request is what stopped trading, or found it
                # already stopped. Derived from the record rather than from a
                # read-then-write, which would race: `engage` returns the
                # original record untouched when a halt is already active, so a
                # name that is not this caller's is proof it was already halted.
                # The converse is not proof — the same operator halting twice
                # looks identical — so the field says what it can stand behind.
                "already_halted_by_another": record.engaged_by != actor,
            },
        )
    )

    return HaltEngagedView(
        scope=record.scope.value,
        reason=record.reason.value,
        engaged_at=record.engaged_at,
        engaged_by=record.engaged_by,
        detail=record.detail,
        target=record.target,
    )


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
