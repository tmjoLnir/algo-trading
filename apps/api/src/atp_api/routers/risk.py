"""Risk and kill-switch endpoints — requirement #3.

These are the emergency controls. They must work when everything else is
degraded: no heavy queries, no dependency on the worker being alive, minimal
code between the request and the Redis write.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from atp_api.auth import authenticate
from atp_api.deps import (
    CurrentUser,
    get_audit_sink,
    get_calendar,
    get_clock,
    get_kill_switch,
    get_portfolio_repository,
    get_signal_repository,
    get_snapshot_store,
)
from atp_api.routers.dashboard import day_pnl_since_open
from atp_core.audit.ports import Action, AuditEntry, AuditSink
from atp_core.clock import Clock, TradingCalendar
from atp_core.config import RiskLimits, Settings, get_settings
from atp_core.dashboard import LiveSnapshot, SnapshotStore
from atp_core.dashboard.snapshot import RATIO_PLACES
from atp_core.domain import RunMode
from atp_core.execution.ports import PortfolioRepository
from atp_core.logging import get_logger
from atp_core.risk.killswitch import HaltReason, HaltScope, KillSwitch
from atp_core.strategy.ports import SignalRepository

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


class RiskLimitsView(BaseModel):
    """The configured ceilings, as `RiskLimits` holds them.

    Field-for-field with the settings object rather than reshaped, because the
    thing an operator is checking is whether the deployment is configured the
    way they think — and a view that renamed or rounded anything would make
    that check answer a different question than the one asked.

    The fractions are `Decimal` and serialise as strings. They are not money,
    but they are multiplied by equity to produce the ceiling an order is
    measured against, and a `0.1` that arrived as a binary float would move
    that ceiling (CLAUDE.md §1.1).
    """

    max_position_pct: Decimal
    max_gross_exposure_pct: Decimal
    max_daily_loss_pct: Decimal
    max_orders_per_minute: int
    max_open_positions: int
    max_quote_age_seconds: int
    default_stop_loss_pct: Decimal
    default_take_profit_pct: Decimal


class LimitUsageView(BaseModel):
    """One limit, and where the book stands against it.

    `ceiling` and `current` are both `Decimal` even where the underlying limit
    is a count, which is the one place this deviates from `AccountView`'s
    convention of `int` for counts. The rows are heterogeneous — fractions of
    equity, position counts, orders per minute, seconds — and a column that
    changed type per row is one a table has to branch on to render. `unit` says
    how to read the pair.
    """

    #: The rule that enforces it, by its own `name` — so a refusal an operator
    #: reads on the orders screen ("refused by max_gross_exposure") names the
    #: same string as the row that predicted it.
    rule: str
    unit: str
    ceiling: Decimal
    #: None when the figure cannot be observed from here. Never zero as a
    #: stand-in for unknown: zero is a value a reader acts on.
    current: Decimal | None
    #: `current` as a fraction of `ceiling`, for a bar. None whenever `current`
    #: is. Can exceed 1.
    utilisation: Decimal | None
    #: True when the rule would now refuse. Mirrors that rule's own comparison
    #: including its boundary, which is not the same across rules.
    at_limit: bool | None
    #: False means this row is structurally unreadable from the API, not that
    #: today's value happens to be missing. `note` says why.
    observable: bool = True
    note: str | None = None


class RiskStatusView(BaseModel):
    """Usage against every limit, from one book at one instant.

    `book_published` is the field to read before any other. False means the
    worker has published nothing and every `current` below is null — which is
    ordinary (a worker that is up but not trading publishes nothing) and is not
    the same as a compliant book.
    """

    as_of: datetime
    #: When the worker built the book. None when there is none.
    book_as_of: datetime | None
    book_age_seconds: int | None
    book_published: bool
    equity: Decimal | None
    limits: list[LimitUsageView]
    #: Open positions carrying no mark. Non-empty means every exposure figure
    #: here is a *lower bound*, which is the direction that makes a breached
    #: limit look compliant — so it travels with the numbers rather than being
    #: left to be inferred from the dashboard.
    unmarked_symbols: list[str] = Field(default_factory=list)


#: Rule name → (unit, the `RiskLimits` field holding its ceiling), in the order
#: the chain checks them, which is the order an operator reads them in.
#:
#: `buying_power` and `trading_hours` are deliberately absent. Both are
#: predicates about *one prospective order* — can this account pay for it, is
#: the market open — rather than a standing quantity a book consumes, so
#: neither has a "current usage" that means anything. Inventing one would put
#: two rows on the screen that never move.
_CEILINGS: tuple[tuple[str, str, str], ...] = (
    ("max_position_size", "fraction_of_equity", "max_position_pct"),
    ("max_gross_exposure", "fraction_of_equity", "max_gross_exposure_pct"),
    ("daily_loss_limit", "fraction_of_equity", "max_daily_loss_pct"),
    ("max_open_positions", "count", "max_open_positions"),
    ("rate_limit", "orders_per_minute", "max_orders_per_minute"),
    ("stale_data", "seconds", "max_quote_age_seconds"),
)

#: Why the order rate cannot be reported from here, in the response itself.
#:
#: `RateLimitRule` keeps its window in a `deque` in the worker's own process,
#: and counts an order on the *attempt* — before the rules after it have voted.
#: The API has no access to that, and the obvious substitute is worse than
#: nothing: refused orders are never persisted (`runner._persist` walks the
#: open-order set, which a risk refusal never enters), so their record lives in
#: `signals` instead. Counting the `orders` table would therefore report a calm
#: rate for precisely the runaway this limit exists to catch — a strategy
#: looping on rejections — and understating it is the direction that makes a
#: breached limit look compliant.
_RATE_LIMIT_NOTE = (
    "not observable from the API: the rule's window lives in the worker's "
    "process and counts refused attempts, which are recorded as signals rather "
    "than orders — a count taken from the order table would read as calm during "
    "exactly the runaway this limit exists to catch"
)

_STALE_DATA_NOTE = (
    "the age of the newest tick across the whole watchlist, so it is a lower "
    "bound: StaleDataRule judges each symbol separately and one frozen symbol "
    "does not move this number"
)

_MARKET_CLOSED_NOTE = (
    "the market is closed, so this is expected — TradingHoursRule refuses "
    "first, and the feed is meant to be silent"
)


def _limits_view(limits: RiskLimits) -> RiskLimitsView:
    return RiskLimitsView(
        max_position_pct=limits.max_position_pct,
        max_gross_exposure_pct=limits.max_gross_exposure_pct,
        max_daily_loss_pct=limits.max_daily_loss_pct,
        max_orders_per_minute=limits.max_orders_per_minute,
        max_open_positions=limits.max_open_positions,
        max_quote_age_seconds=limits.max_quote_age_seconds,
        default_stop_loss_pct=limits.default_stop_loss_pct,
        default_take_profit_pct=limits.default_take_profit_pct,
    )


async def _read_book(store: SnapshotStore, run_mode: RunMode) -> LiveSnapshot | None:
    """The worker's published book, or None if it has published nothing.

    The same posture `/dashboard/live` takes, and it has to be: None is
    ordinary, an unreadable store is not. A risk screen that rendered "nothing
    is near a limit" because Redis blinked would be the most misleading page in
    the application.
    """
    try:
        return await store.get(run_mode)
    except Exception as exc:
        log.error("risk.status.book_unreadable", run_mode=run_mode.value, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"cannot read the published book, so no limit can be reported against it: {exc}"
            ),
        ) from exc


def _usage(
    rule: str,
    limits: RiskLimits,
    current: Decimal | None,
    at_limit: bool | None,
    *,
    note: str | None = None,
    observable: bool = True,
    utilisation: Decimal | None = None,
) -> LimitUsageView:
    """One row, with `utilisation` derived unless a caller knows better.

    `daily_loss_limit` is the case that needs to know better: its `current` is
    the day's signed change, so a profitable day is a *negative* fraction of a
    loss allowance, and dividing would render `+1.2%` as `-40% used`.
    """
    unit, field = next((u, f) for r, u, f in _CEILINGS if r == rule)
    ceiling = Decimal(str(getattr(limits, field)))
    if utilisation is None and current is not None and ceiling != 0:
        utilisation = (current / ceiling).quantize(RATIO_PLACES)
    return LimitUsageView(
        rule=rule,
        unit=unit,
        ceiling=ceiling,
        current=current,
        utilisation=utilisation,
        at_limit=at_limit,
        observable=observable,
        note=note,
    )


def _unreadable_rows(limits: RiskLimits) -> list[LimitUsageView]:
    """Every row, ceilings intact and every reading null.

    The ceilings are still worth serving with no book: "what is the exposure
    limit" has an answer whether or not anyone is trading, and dropping the
    rows entirely would leave a screen that renders as though the limits
    themselves had gone away.

    `rate_limit` keeps `observable=False` here rather than joining the others
    at null, because the two mean different things and the distinction survives
    the book coming back: the rest are unknown *right now*, that one is unknown
    always.
    """
    return [
        _usage(
            rule,
            limits,
            None,
            None,
            observable=rule != "rate_limit",
            note=_RATE_LIMIT_NOTE
            if rule == "rate_limit"
            else "no book published — the worker has not said what it is holding",
        )
        for rule, _unit, _field in _CEILINGS
    ]


def _status_view(
    snapshot: LiveSnapshot,
    limits: RiskLimits,
    *,
    now: datetime,
    day_pnl_pct: Decimal | None,
    market_open: bool,
) -> RiskStatusView:
    """Read one published book against every limit.

    Each comparison below is the matching rule's, boundary included. Where the
    rules differ from each other they are copied as they are rather than
    harmonised — see the endpoint docstring.
    """
    account = snapshot.account
    equity = account.equity
    unmarked = list(account.unmarked_symbols)
    #: Every fraction-of-equity row is undefined without equity to divide by.
    #: `MaxPositionSizeRule` and `MaxExposureRule` both refuse outright at
    #: `equity <= 0`, so the honest reading is "unknown", not "0%".
    priced = equity > 0

    # `MaxPositionSizeRule`: the largest single holding, gross, against equity.
    # An unmarked position contributes nothing here, which is why `unmarked`
    # travels with the answer.
    values = [abs(p.market_value) for p in snapshot.positions if p.market_value is not None]
    largest = max(values) if values else Decimal(0)
    position_pct = (largest / equity).quantize(RATIO_PLACES) if priced else None

    # `MaxExposureRule`: gross, so every leg adds.
    exposure_pct = (account.gross_exposure / equity).quantize(RATIO_PLACES) if priced else None

    # `StaleDataRule`: `last_data_at` is the newest tick the worker has seen.
    age = snapshot.last_data_at
    quote_age = Decimal(max(0, int((now - age).total_seconds()))) if age is not None else None

    open_count = Decimal(account.open_position_count)
    exposure_note = "understated: some positions are unmarked" if unmarked else None

    return RiskStatusView(
        as_of=now,
        book_as_of=snapshot.as_of,
        # Clamped at zero for the reason `/dashboard/live` clamps it: a worker
        # clock a second ahead reads as a bug in the screen rather than skew.
        book_age_seconds=max(0, int((now - snapshot.as_of).total_seconds())),
        book_published=True,
        equity=equity,
        unmarked_symbols=unmarked,
        limits=[
            _usage(
                "max_position_size",
                limits,
                position_pct,
                # `resulting > ceiling` in the rule: the ceiling is a value a
                # position may hold exactly.
                None if position_pct is None else position_pct > limits.max_position_pct,
                note=exposure_note,
            ),
            _usage(
                "max_gross_exposure",
                limits,
                exposure_pct,
                None if exposure_pct is None else exposure_pct > limits.max_gross_exposure_pct,
                note=exposure_note,
            ),
            _usage(
                "daily_loss_limit",
                limits,
                day_pnl_pct,
                # `change <= -max_daily_loss_pct` in the rule — at the limit
                # blocks, and the comparison is against the negated limit
                # because `current` here is a signed change.
                None if day_pnl_pct is None else day_pnl_pct <= -limits.max_daily_loss_pct,
                # A profitable day consumes none of a loss allowance. Dividing
                # would render it as a negative percentage used.
                utilisation=(
                    None
                    if day_pnl_pct is None or limits.max_daily_loss_pct == 0
                    else (max(Decimal(0), -day_pnl_pct) / limits.max_daily_loss_pct).quantize(
                        RATIO_PLACES
                    )
                ),
                note=(
                    None
                    if day_pnl_pct is not None
                    else "no equity snapshot at this session's open to measure from"
                ),
            ),
            _usage(
                "max_open_positions",
                limits,
                open_count,
                # `open_count >= limit` in the rule: holding the limit means no
                # *new* symbol may be opened, so at the limit already blocks.
                open_count >= limits.max_open_positions,
            ),
            _usage("rate_limit", limits, None, None, observable=False, note=_RATE_LIMIT_NOTE),
            _usage(
                "stale_data",
                limits,
                quote_age,
                # `age > max_quote_age_seconds` in the rule, judged the same way
                # whether or not the market is open — because the rule is. The
                # note carries the context instead of the verdict bending to it.
                None if quote_age is None else quote_age > limits.max_quote_age_seconds,
                note=(
                    _STALE_DATA_NOTE
                    if market_open
                    else f"{_STALE_DATA_NOTE}; {_MARKET_CLOSED_NOTE}"
                )
                if quote_age is not None
                else "the worker has seen no market data at all",
            ),
        ],
    )


@router.get("/limits", response_model=RiskLimitsView)
async def get_risk_limits(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RiskLimitsView:
    """The configured ceilings. What the rules are, not where we stand.

    Config only: no Redis, no Postgres, no broker. That is the point of it
    being separate from `/status` rather than a field on it — the moment an
    operator most wants to know what the limits are is an incident, which is
    also when the stores are least likely to answer. This route survives all of
    them being down.

    Read by a full or a read-only session alike; there is nothing here a reader
    should not see, and `.env` is not somewhere a person can look during an
    incident.
    """
    return _limits_view(settings.risk)


@router.get("/status", response_model=RiskStatusView)
async def get_risk_status(
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    calendar: Annotated[TradingCalendar, Depends(get_calendar)],
    store: Annotated[SnapshotStore, Depends(get_snapshot_store)],
    portfolio_repo: Annotated[PortfolioRepository, Depends(get_portfolio_repository)],
) -> RiskStatusView:
    """Current usage against every limit. What a human checks before promoting
    to live.

    Read from the worker's published book, which is the same book the risk
    engine evaluates orders against — the stored copy in Postgres is a lagging
    record of it and would answer a slightly different question.

    **No book published means every usage is null, never zero.** That is the
    whole safety property of this endpoint. A worker that is up but not trading
    publishes nothing, and so does one that has just started or just died; a
    screen that rendered those as "0% of your exposure limit, 0 of 20
    positions" would be telling an operator they are flat and compliant at the
    exact moment nobody knows what the book contains (ADR 0007).

    Every comparison mirrors its rule's own, including the boundary. The rules
    disagree with each other about it on purpose — `MaxOpenPositionsRule`
    refuses at `>=` because holding the limit means no new symbol may be
    opened, while `MaxExposureRule` refuses at `>` because the ceiling is a
    value exposure may reach — and a status screen that rounded those together
    would tell someone they are fine while the engine refuses their next order.
    """
    now = clock.now()
    limits = settings.risk
    snapshot = await _read_book(store, settings.run_mode)

    if snapshot is None:
        return RiskStatusView(
            as_of=now,
            book_as_of=None,
            book_age_seconds=None,
            book_published=False,
            equity=None,
            limits=_unreadable_rows(limits),
            unmarked_symbols=[],
        )

    _, day_pct = await day_pnl_since_open(
        portfolio_repo,
        calendar,
        run_mode=settings.run_mode,
        equity=snapshot.account.equity,
        now=now,
    )
    return _status_view(
        snapshot,
        limits,
        now=now,
        day_pnl_pct=day_pct,
        market_open=calendar.is_open(now),
    )


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


class RejectionView(BaseModel):
    """One decision the risk chain refused.

    A *signal*, not an order, and the distinction is the endpoint's whole
    subject: a refused signal never becomes an order, so the orders table
    cannot show it. `signal_id` is the row in `signals`, which also carries the
    indicators the strategy was looking at when it decided.
    """

    signal_id: str
    at: datetime
    strategy_id: str
    symbol: str
    action: str
    #: The rule that refused it, by its own `name` — the same string
    #: `/risk/status` puts on the limit's row and `RiskDecision.rule` carries.
    rule: str
    #: What a human reads. Null for a refusal recorded with a rule and no words,
    #: which is possible and is not worth inventing a sentence for.
    reason: str | None
    #: What the strategy was looking at, as strings. Never parsed to numbers:
    #: the column holds prices, period counts and boolean flags together, and
    #: this layer cannot tell which is which (`persistence.signals._to_signal`).
    indicators: dict[str, str] = Field(default_factory=dict)


class RejectionsResponse(BaseModel):
    """Refusals, and an honest account of which ones are missing."""

    rejections: list[RejectionView]

    #: How many of **the refusals below** each rule accounts for — not of all
    #: history. It is computed over the returned page, because counting the
    #: whole table would be a second query whose answer would not match the
    #: rows on the screen. The name a reader wants is usually the one at the
    #: top of a short list anyway: "which rule is refusing everything".
    by_rule: dict[str, int] = Field(default_factory=dict)

    #: **What this endpoint structurally cannot show**, in the payload rather
    #: than only in the docs, for the same reason `/risk/status` marks the order
    #: rate unobservable: an empty list here reads as "nothing is being
    #: refused", and a screen that renders it needs the sentence that stops a
    #: person concluding that.
    blind_spots: list[str] = Field(default_factory=list)


#: What this endpoint does not cover, said in the payload rather than only here.
#:
#: This reads `signals`, so it shows refusals of things a *strategy decided*.
#: The runner can also be refused three ways that never involve a signal — a
#: stop exit, a protective stop, and a shutdown flatten — and those are the ones
#: that describe an open position nobody is managing.
#:
#: They used to be logged and dropped, which made this list an apology. They are
#: stored as orders now, so the list is a signpost instead: the refusal is real,
#: it is durable, and it is on the Orders tab rather than this one. Pointing
#: somewhere is worth much more than confessing to nowhere, and the distinction
#: still has to be drawn — an operator reading an empty table here has *not*
#: established that nothing was refused.
BLIND_SPOTS = [
    "refusals that never involved a signal are not here: a stop exit, a "
    "protective stop or a shutdown flatten refused by the risk chain is stored "
    "as a rejected order, so `/orders` is where those appear",
    "those are the more serious refusals — a refused entry is a trade that did "
    "not happen, a refused stop exit is a position that should have closed and "
    "did not — so an empty table here does not mean nothing was refused",
    "`no_action` outcomes are excluded on purpose: a HOLD, or an exit against "
    "an already-flat position, is approved rather than refused",
]


@router.get("/rejections", response_model=RejectionsResponse)
async def list_rejections(
    signals: Annotated[SignalRepository, Depends(get_signal_repository)],
    strategy_id: str | None = None,
    rule: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> RejectionsResponse:
    """Recently blocked orders and why.

    A strategy silently doing nothing because a limit rejects it every time
    looks identical to a strategy with no signals — this endpoint is how you
    tell the difference.

    Read from `signals`, which is where refusals live. They are deliberately
    **not** in the orders table: `runner.evaluate` skips `_track` when the
    router refuses, so a refused order never enters the open-order set that
    `_persist` walks, and nothing else saves it. That is why
    `/risk/status` reports the order rate as unobservable, and it is the same
    fact seen from the other side — the record exists, it is just kept as a
    decision rather than as an order.

    **The filtering happens in SQL.** Reading the newest hundred signals and
    keeping the refused ones would answer a different question: "were any of
    the last hundred decisions refused" is "no" for a strategy blocked all week
    that has since emitted one HOLD, and an empty list reads as "nothing is
    being refused" (`SignalRepository.rejections`).

    A read, so a read-only session may call it. Nothing here is a secret an
    operator watching the book should not see — and this is precisely the screen
    someone reaches for when a strategy appears to be doing nothing.
    """
    found = await signals.rejections(strategy_id=strategy_id, rule=rule, since=since, limit=limit)

    counts: dict[str, int] = {}
    views: list[RejectionView] = []
    for signal, outcome in found:
        # `rejected_by` is what the repository filtered on, so it is never None
        # here — but the type says it can be, and a view built from `or ""`
        # would put a nameless rule on the screen rather than failing loudly.
        refusing = outcome.rejected_by
        if refusing is None:  # pragma: no cover — excluded by the query above
            continue
        counts[refusing] = counts.get(refusing, 0) + 1
        views.append(
            RejectionView(
                signal_id=signal.id,
                at=signal.ts,
                strategy_id=signal.strategy_id,
                symbol=signal.symbol,
                action=signal.action.value,
                rule=refusing,
                reason=outcome.rejection_reason,
                indicators={k: str(v) for k, v in signal.indicators.items()},
            )
        )

    return RejectionsResponse(
        rejections=views,
        by_rule=dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        blind_spots=list(BLIND_SPOTS),
    )
