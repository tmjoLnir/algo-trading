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


def _scope_target_error(scope: HaltScope, target: str | None, *, verb: str) -> str | None:
    """The scope/target pairing, in one place because both ends need it.

    A halt is keyed on (scope, target) and is cleared by that same pair, so the
    two requests have to agree about which pairs exist. Written twice they would
    drift, and the direction that drift goes is the dangerous one: a resume that
    accepted a combination `/halt` refuses could only ever be asking to clear a
    halt that cannot exist, and would answer "nothing was halted" — which reads
    exactly like "trading is fine" to whoever is looking at it.
    """
    if scope is HaltScope.GLOBAL and target is not None:
        return f"target is meaningless with scope 'global' — it {verb} everything"
    if scope is not HaltScope.GLOBAL and not target:
        return f"scope '{scope.value}' needs a target (a strategy id or symbol)"
    return None


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
        problem = _scope_target_error(self.scope, self.target, verb="halts")
        if problem is not None:
            raise ValueError(problem)
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
    """Clearing a halt, with the password that proves someone is still there.

    `scope` is the domain enum and not a bare string, for the reason
    `HaltRequest` gives and one more that is specific to this end: the handler
    has to hand a `HaltScope` to the kill switch, so a string would be converted
    somewhere — and converting it inside the handler turns a typo into a 500
    with no useful body, where the enum makes it a 422 that names the three
    scopes that exist. An operator clearing a halt is not in a position to guess
    which of those two an error page meant.
    """

    scope: HaltScope = HaltScope.GLOBAL
    target: str | None = None
    #: Re-presented for this one act. In the body and never a query parameter:
    #: a query string is written to nginx's access log verbatim.
    password: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _scope_and_target_agree(self) -> ResumeRequest:
        problem = _scope_target_error(self.scope, self.target, verb="clears")
        if problem is not None:
            raise ValueError(problem)
        return self


class ResumedView(BaseModel):
    """What this call did, and whether it did anything at all.

    `was_halted` is the field to read first. `clear` is deliberately not an
    error when nothing was engaged — an operator clearing defensively should not
    get an exception for being early — so "resumed" and "there was nothing to
    resume" are both successes, and only this tells them apart.

    The halt fields describe **what was removed**, so they are null when
    `was_halted` is false. They are worth returning rather than dropping: the
    thing an operator most wants confirmed after resuming is that the halt they
    cleared is the halt they meant, and `reason` is what says so.

    Deliberately silent about what is *still* halted. Clearing the global halt
    while a symbol halt stands leaves trading partly stopped, which matters — but
    answering it here means a second read of the store on a path whose first
    write has already landed, so a failed read would report failure for a resume
    that actually happened. The banner re-reads every halt on the next poll and
    stays up if any remain; that is the honest place for the question.
    """

    scope: str
    target: str | None
    was_halted: bool
    cleared_by: str
    reason: str | None = None
    engaged_at: datetime | None = None
    engaged_by: str | None = None
    detail: str | None = None


class FlattenAllRequest(BaseModel):
    """Liquidating the book. Two proofs, because it cannot be undone."""

    confirm: str
    password: str = Field(min_length=1, max_length=1024)


async def _require_step_up(
    password: str,
    actor: str,
    settings: Settings,
    audit: AuditSink,
    clock: Clock,
    target: str,
) -> None:
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

    **A failure is recorded before it is raised.** `Action.FORBIDDEN` has always
    described itself as covering "a read-only session attempting a write, or a
    failed step-up (ADR 0009)", and `deps.require_write_scope` wrote the first
    of those from the start; this end wrote nothing, so the record has been
    claiming a coverage it did not have. The gap matters more here than there:
    a wrong password against `/resume` or `/flatten-all` is either the operator
    mistyping or somebody working through guesses with a stolen cookie, those
    look identical at the moment of refusal, and `rate_limited` only ever
    counted attempts at the *login* form. Without this row the second case
    leaves no trace anywhere.
    """
    if authenticate(actor, password, settings) is not None:
        return

    await audit.record(
        AuditEntry(
            at=clock.now(),
            actor=actor,
            action=Action.FORBIDDEN,
            target=target,
            # Named so this is distinguishable from a read-only session's
            # refusal, which shares the verb and would otherwise be indistinct
            # on the audit screen — one is a session in the wrong mode, the
            # other is a credential that did not check out.
            detail={"reason": "step_up_failed"},
        )
    )
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
    kill_switch: Annotated[KillSwitch, Depends(get_kill_switch)],
    audit: Annotated[AuditSink, Depends(get_audit_sink)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> ResumedView:
    """Resume trading. Requires a named human and is audit-logged.

    Deliberately asymmetric with `/halt`: stopping is reflexive, restarting is
    a decision. The password is where that asymmetry stops being a comment and
    starts being enforced — `/halt` asks for nothing at all, and a read-only
    session may call it; this one asks again and a read-only session may not.
    That second half needs no code here: `deps.READ_ONLY_MAY_CALL` names `/halt`
    and nothing else, so `require_write_scope` refuses this route by default.

    `cleared_by` is the session's user, exactly as `engaged_by` is on the way
    in. It is the answer to the only question anyone asks after an incident —
    who decided it was safe to trade again — and a field the request could fill
    in would not be an answer at all (ADR 0008).

    Off the event loop for the reason `/halt` is: the switch is synchronous
    because the risk chain consulting it is, and one Redis round trip must not
    block every other request.

    **A failure here is a 503, and it means the opposite of the one on `/halt`.**
    Nothing was cleared, so the halt is still in force and nothing is trading —
    the safe direction, and worth saying plainly because an operator who has
    just been refused will otherwise be left wondering whether they are now half
    resumed. There is no partial state to recover from: `clear` is a single
    DELETE, so it either happened or it did not.
    """
    await _require_step_up(payload.password, actor, settings, audit, clock, "/api/v1/risk/resume")

    try:
        cleared = await asyncio.to_thread(
            kill_switch.clear,
            payload.scope,
            actor,
            payload.target,
        )
    except Exception as exc:
        log.critical(
            "risk.resume_failed",
            error=str(exc),
            actor=actor,
            scope=payload.scope.value,
            target=payload.target,
            effect="the halt still stands — nothing is trading",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "trading was NOT resumed: "
                f"{exc}. The halt is still in force and the switch fails closed, "
                "so nothing is trading — this failed safe. Try again once the "
                "store is reachable, and confirm with `scripts/halt.py status`."
            ),
        ) from exc

    # After the clear, never before — the mirror of the halt row's ordering and
    # for the inverse reason. A row claiming trading resumed when the delete did
    # not land would have whoever reads it stop looking for the thing that is
    # still stopping the platform.
    #
    # Written whether or not anything was removed. A clear that found nothing is
    # not a no-op worth dropping: someone with the password decided the platform
    # should be trading, and that decision is the record's business even when it
    # turned out to be unnecessary.
    await audit.record(
        AuditEntry(
            at=clock.now(),
            actor=actor,
            action=Action.HALT_CLEARED,
            target=payload.target,
            detail={
                "scope": payload.scope.value,
                "was_halted": cleared is not None,
                # Who this operator overrode, when it was not themselves. An
                # automated halt cleared by a human is the case worth being able
                # to find afterwards: the risk layer stopped trading for a
                # reason it had, and somebody decided that reason no longer
                # applied.
                "original_reason": cleared.reason.value if cleared is not None else None,
                "originally_engaged_by": cleared.engaged_by if cleared is not None else None,
            },
        )
    )

    return ResumedView(
        scope=payload.scope.value,
        target=payload.target,
        was_halted=cleared is not None,
        cleared_by=actor,
        reason=cleared.reason.value if cleared is not None else None,
        engaged_at=cleared.engaged_at if cleared is not None else None,
        engaged_by=cleared.engaged_by if cleared is not None else None,
        detail=cleared.detail if cleared is not None else None,
    )


@router.post("/flatten-all")
async def flatten_all(
    payload: FlattenAllRequest,
    actor: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditSink, Depends(get_audit_sink)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> dict[str, object]:
    """Liquidate everything at market.

    Requires `confirm` to equal the literal string "FLATTEN ALL POSITIONS", and
    the account password with it. Irreversible: it realises every open P&L at
    whatever the market offers.

    Both proofs, not either. The phrase shows the caller knows what this does;
    the password shows they are the person entitled to do it. A copied session
    cookie satisfies neither on its own.
    """
    await _require_step_up(
        payload.password, actor, settings, audit, clock, "/api/v1/risk/flatten-all"
    )
    raise NotImplementedError


@router.get("/rejections")
async def list_rejections(limit: int = 100) -> list[dict[str, object]]:
    """Recently blocked orders and why.

    A strategy silently doing nothing because a limit rejects it every time
    looks identical to a strategy with no signals — this endpoint is how you
    tell the difference.
    """
    raise NotImplementedError
