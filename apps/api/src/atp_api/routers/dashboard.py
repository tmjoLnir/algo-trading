"""Dashboard endpoints — requirement #7.

The dashboard reads `GET /api/v1/dashboard/live` when its reader asks it to — a
browser reload, or the button on the screen (ADR 0022). That endpoint returns
everything the main view needs in ONE response: account, open positions, recent
signals, working orders, and any active halt.

One aggregate endpoint rather than six parallel requests, for a reason worth
stating: six independent fetches produce a screen assembled from six different
instants. On a fast-moving position, a P&L figure computed from one snapshot and
a price from another simply disagree, and the human reading it cannot tell which
number to trust. One query, one consistent picture.

Live prices still arrive over the WebSocket between reads, and a fill or a halt
prompts the client to re-read on its own. What was removed is the clock, not the
paths that exist because the book changed.

## Where each number comes from, and why it is not all one place

The response has two halves and the split is deliberate.

**The book** — account, positions, signals, working orders — is published by the
worker at the end of every evaluation (`atp_core.dashboard`) and served here
verbatim. It is not recomputed. The API can reach the order table and the quote
cache and could assemble its own version, and that version would be computed at
a different instant from the one the trading loop just acted on. Two answers to
"what is my equity" is exactly what the single aggregate endpoint exists to
prevent, so the book is computed once, where it is authoritative.

**Everything else** — the run mode, whether the market is open, and the active
halts — is answered here, now, from configuration, the exchange calendar and the
kill switch. Each of those must still be correct when the worker is dead, and a
halt banner sourced from a snapshot published by a process that has stopped
publishing would say "not halted" at exactly the moment that matters most.

So the response carries two timestamps rather than one. `as_of` is when the API
assembled it; `book_as_of` is when the worker built the book half. That is more
honest than one, not less: the concern behind "one `as_of`" is six *parts of the
book* from six instants, and the book still has exactly one.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from atp_api.deps import (
    get_calendar,
    get_clock,
    get_kill_switch,
    get_portfolio_repository,
    get_snapshot_store,
)
from atp_core.clock import Clock, TradingCalendar
from atp_core.config import Settings, get_settings
from atp_core.dashboard import AccountSummary, LiveSnapshot, SnapshotStore
from atp_core.dashboard.curve import (
    RESOLUTIONS,
    default_resolution_for,
    downsample,
    resolve,
)
from atp_core.dashboard.snapshot import DEFAULT_SIGNAL_LIMIT, RATIO_PLACES
from atp_core.domain import RunMode
from atp_core.execution.ports import PortfolioRepository
from atp_core.logging import get_logger
from atp_core.risk.killswitch import HaltRecord, KillSwitch

log = get_logger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

#: The longest window the equity chart may ask for in one request. A year of
#: minute snapshots is roughly 130k rows; the chart draws a few hundred points.
#: Bounded here rather than trusted, because an unbounded `days` is one URL away
#: from a table scan that blocks every other dashboard read behind it.
MAX_CURVE_DAYS = 365

#: How far back to look for the session whose open anchors day P&L. A week
#: covers the longest run of consecutive closures a US exchange produces
#: (a Friday holiday either side of a weekend), so the search always finds one.
DAY_ANCHOR_LOOKBACK_DAYS = 7


class PositionView(BaseModel):
    """One holding.

    Every mark-dependent figure is nullable, and that is not defensive typing:
    an unmarked position is not a position worth nothing. `market_value`
    reported as 0 would put a real holding at the bottom of the exposure column
    and make a breached limit look compliant. `unmarked_symbols` on the account
    says which ones, so the reader is told rather than left to notice.
    """

    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    last_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pnl_pct: Decimal | None
    realized_pnl: Decimal
    fees_paid: Decimal
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    #: How much of the entry-to-stop distance is left, as a fraction: 1.0 at the
    #: entry, 0.0 at the stop, above 1.0 in profit. The single most useful
    #: number on the screen — it says how close this position is to being
    #: closed, without arithmetic in the reader's head. **Negative means price
    #: is already through the stop and the exit has not happened.**
    distance_to_stop_pct: Decimal | None
    opened_at: datetime | None


class SignalView(BaseModel):
    """A recent decision, with its reasoning — requirement #7's "trades selected
    based on preset rules".

    `indicators` values are strings, not numbers. An indicator value is usually
    a price — an SMA of closes is denominated in dollars — and rule §1.1's
    exemption for indicator maths covers computing one, not putting it on a wire
    whose only numeric type is a binary float.
    """

    id: str
    ts: datetime
    strategy_id: str
    symbol: str
    action: str
    reason: str
    indicators: dict[str, str] = Field(default_factory=dict)
    acted_on: bool
    #: Which rule refused it. Also set for `no_action` — a signal that needed no
    #: order, such as an exit for a position already flat — which is why it is
    #: separate from `rejection_reason`: a client can tell a risk refusal from
    #: nothing-to-do without parsing English.
    rejected_by: str | None
    rejection_reason: str | None


class OrderView(BaseModel):
    id: str
    client_order_id: str
    ts: datetime | None
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    filled_qty: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    avg_fill_price: Decimal | None
    status: str
    strategy_id: str | None


class AccountView(BaseModel):
    """Account-level figures, all from one book at one instant.

    No `buying_power`: that is the venue's number and reading it costs a broker
    call per dashboard read, on the same rate limit the trading process is
    placing orders against. `BuyingPowerRule` constrains against `cash`, so cash
    is the number that actually decides whether an order is approved here.
    """

    equity: Decimal
    cash: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    #: None when equity is zero. Leverage against no capital is undefined, and
    #: rendering it as 0.0 would read as "unlevered".
    leverage: Decimal | None
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    #: Change in equity since the first snapshot of the current session. None
    #: when there is no anchor to measure from — a first-ever run, or a database
    #: that could not answer. Never zero, which is a value a reader acts on.
    day_pnl: Decimal | None
    day_pnl_pct: Decimal | None
    open_position_count: int
    #: Open positions carrying no mark. Non-empty means every figure above
    #: understates exposure and equity.
    unmarked_symbols: list[str] = Field(default_factory=list)


class HaltView(BaseModel):
    scope: str
    reason: str
    engaged_at: datetime
    engaged_by: str
    detail: str
    target: str | None


class LiveDashboard(BaseModel):
    """One screen, assembled from two sources — see the module docstring."""

    #: When the API assembled this response.
    as_of: datetime
    run_mode: str  # drives the "PAPER"/"LIVE" banner — never let this be ambiguous
    market_open: bool
    #: Read live from the kill switch on every request, never from the book.
    #: A halt must be visible whether or not the worker is publishing.
    active_halts: list[HaltView] = Field(default_factory=list)
    #: How old a reading may be before the client calls it stale. Echoed so that
    #: judgement follows the server's config rather than a hardcoded browser
    #: constant that drifts out of sync. It is not a cadence — nothing polls.
    stale_after_seconds: int

    # ── the worker's half, all null when nothing has been published ─────────
    #: When the worker built the book below. None means it has published
    #: nothing: it is not trading, or it has only just started. That is an
    #: ordinary state and is reported as itself rather than as an empty book,
    #: because "you hold nothing" and "nobody has said what you hold" are
    #: different sentences and only one of them is safe to act on.
    book_as_of: datetime | None
    #: How far behind `as_of` the book is. Sent rather than left to the client
    #: so that the one number deciding whether the screen is trustworthy is not
    #: computed from two timestamps in a browser.
    book_age_seconds: int | None
    strategy: str | None
    account: AccountView | None
    positions: list[PositionView] = Field(default_factory=list)
    recent_signals: list[SignalView] = Field(default_factory=list)
    working_orders: list[OrderView] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    #: The newest market-data timestamp the worker has seen.
    last_data_at: datetime | None
    #: Whether that age is a problem, judged against the same freshness budget
    #: `StaleDataRule` refuses orders on. None when there is no book to judge —
    #: unknown, rather than a guess in either direction.
    data_feed_healthy: bool | None


class EquityPointView(BaseModel):
    ts: datetime
    equity: Decimal
    cash: Decimal
    gross_exposure: Decimal


class EquityCurveView(BaseModel):
    """The account's equity over time, thinned to one point per bucket."""

    run_mode: str
    start: datetime
    end: datetime
    resolution: str
    points: list[EquityPointView] = Field(default_factory=list)


def _halt_view(record: HaltRecord) -> HaltView:
    return HaltView(
        scope=record.scope.value,
        reason=record.reason.value,
        engaged_at=record.engaged_at,
        engaged_by=record.engaged_by,
        detail=record.detail,
        target=record.target,
    )


async def _active_halts(kill_switch: KillSwitch) -> list[HaltView]:
    """Every active halt, oldest first.

    Off the event loop because the kill switch is synchronous — it has to be,
    since the risk chain that consults it is (`persistence.redis_client`) — and
    a blocking Redis round trip on every dashboard read from every open tab is
    exactly the sort of thing that makes an event loop stutter.

    A failure here is a 503 rather than an empty list. `active_halts` already
    refuses to swallow the error for this reason: "nothing is halted" is the
    worst possible thing to show a human when the truth is unknown, and the
    client keeps its last good data on screen instead.
    """
    try:
        records = await asyncio.to_thread(kill_switch.active_halts)
    except Exception as exc:
        log.error("dashboard.halts_unreadable", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "cannot read the halt state — refusing to report trading as permitted "
                f"when that is unknown: {exc}"
            ),
        ) from exc
    return [_halt_view(r) for r in sorted(records, key=lambda r: r.engaged_at)]


async def _read_snapshot(store: SnapshotStore, run_mode: RunMode) -> LiveSnapshot | None:
    """The worker's published book, or None if it has published nothing.

    None is ordinary. An *unreadable* store is not, and it raises rather than
    reading as None — a dashboard that rendered "no positions" because Redis
    blinked would be telling its reader they are flat.
    """
    try:
        return await store.get(run_mode)
    except Exception as exc:
        log.error("dashboard.snapshot_unreadable", run_mode=run_mode.value, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"cannot read the published book: {exc}",
        ) from exc


def _feed_healthy(last_data_at: datetime | None, *, now: datetime, budget: int) -> bool:
    """Is market data still arriving?

    Judged against `RISK_MAX_QUOTE_AGE_SECONDS` — the same budget `StaleDataRule`
    refuses to price an order against, and the one `scripts/status.py` prints.
    Using a second number here would let the dashboard call a feed healthy while
    every order against it is being refused for staleness, which is the pair of
    observations an operator would spend an afternoon reconciling.

    Callers only ask while the market is open. A quiet feed at 02:00 on a Sunday
    is correct, and answering that question here as well would make this
    function need a calendar it has no other use for.
    """
    if last_data_at is None:
        return False
    return (now - last_data_at).total_seconds() <= budget


async def _session_open_before(calendar: TradingCalendar, now: datetime) -> datetime | None:
    """The open of the session `now` falls in, or of the last one before it.

    "The current session" on a Saturday is the Friday that just finished, which
    is what makes day P&L still show Friday's move over a weekend rather than
    resetting to nothing the moment the bell goes.

    Off the event loop for the same reason `/market-data/calendar` does it: the
    first question about a year materialises it, which is tens of milliseconds
    of pandas, and it is not worth holding every other request behind.
    """
    today = now.astimezone(calendar.tz).date()
    first = today - timedelta(days=DAY_ANCHOR_LOOKBACK_DAYS)
    sessions = await asyncio.to_thread(calendar.sessions, first, today)
    opens = [s.open_at for s in sessions if s.open_at <= now]
    return opens[-1] if opens else None


async def day_pnl_since_open(
    repo: PortfolioRepository,
    calendar: TradingCalendar,
    *,
    run_mode: RunMode,
    equity: Decimal,
    now: datetime,
) -> tuple[Decimal | None, Decimal | None]:
    """Equity change since the session opened, and the same as a fraction.

    The anchor is the **first equity snapshot at or after the session open**,
    not `Portfolio.starting_equity`. That field is inception-to-date and
    `PostgresPortfolioRepository.latest` resets it to the reload point on a
    restart, so measuring a day against it would report a number that changes
    meaning every time the worker is bounced.

    A database that cannot answer costs this one figure and nothing else. The
    alternative — failing the whole response — would take the halt banner and
    the position list down with it, and those are the parts a person needs when
    something is already wrong.

    Public because `/risk/status` reports the day's move against
    `RISK_MAX_DAILY_LOSS_PCT` and has to arrive at the same number this does.
    Two implementations of "the day's move" would eventually disagree, and the
    screen they disagree on is the one an operator consults to decide whether
    the platform is safe to keep running.
    """
    session_open = await _session_open_before(calendar, now)
    if session_open is None:
        return None, None

    try:
        history = await repo.equity_history(run_mode, start=session_open, end=now)
    except Exception as exc:
        log.warning("dashboard.day_pnl_unavailable", error=str(exc))
        return None, None

    if not history:
        return None, None

    anchor = history[0].equity
    day_pnl = equity - anchor
    pct = None if anchor == 0 else (day_pnl / anchor).quantize(RATIO_PLACES)
    return day_pnl, pct


def account_view(
    account: AccountSummary, day_pnl: Decimal | None, day_pnl_pct: Decimal | None
) -> AccountView:
    """`AccountSummary` as the wire model.

    Takes the summary rather than the whole snapshot, so `/positions` can build
    the same account block from the *stored* book. Day P&L stays a parameter
    because it is not a property of any single book — it is this equity against
    the session's first recorded one — and the two callers answer it
    differently: the live screen computes it, the stored one passes None rather
    than a zero a reader would act on.
    """
    return AccountView(
        equity=account.equity,
        cash=account.cash,
        gross_exposure=account.gross_exposure,
        net_exposure=account.net_exposure,
        leverage=account.leverage,
        realized_pnl=account.realized_pnl,
        unrealized_pnl=account.unrealized_pnl,
        day_pnl=day_pnl,
        day_pnl_pct=day_pnl_pct,
        open_position_count=account.open_position_count,
        unmarked_symbols=list(account.unmarked_symbols),
    )


@router.get("/live", response_model=LiveDashboard)
async def get_live_dashboard(
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    calendar: Annotated[TradingCalendar, Depends(get_calendar)],
    kill_switch: Annotated[KillSwitch, Depends(get_kill_switch)],
    store: Annotated[SnapshotStore, Depends(get_snapshot_store)],
    portfolio_repo: Annotated[PortfolioRepository, Depends(get_portfolio_repository)],
    signal_limit: Annotated[
        int,
        Query(ge=1, le=DEFAULT_SIGNAL_LIMIT, description="most recent decisions to return"),
    ] = DEFAULT_SIGNAL_LIMIT,
) -> LiveDashboard:
    """Everything the dashboard needs, from one point in time.

    Fast by construction: it is read by every open browser tab, so the book is
    one Redis `GET` of a document the worker already assembled rather than a
    recomputation from fills. The two other reads — the halt keys and, only when
    a book exists, one bounded equity query for the day anchor — are both small
    and both bounded.

    Signals come back newest first, because a feed is read from the top.
    """
    now = clock.now()
    halts = await _active_halts(kill_switch)
    snapshot = await _read_snapshot(store, settings.run_mode)
    market_open = calendar.is_open(now)

    common = {
        "as_of": now,
        "run_mode": settings.run_mode.value,
        "market_open": market_open,
        "active_halts": halts,
        "stale_after_seconds": settings.dashboard_stale_after_seconds,
    }
    if snapshot is None:
        # Not an error. A worker that is up but not trading publishes nothing,
        # and so does one that has only just started — but the banners, the
        # halts and the kill switch above still have to render, which is why
        # they were never in the book to begin with.
        return LiveDashboard(
            **common,
            book_as_of=None,
            book_age_seconds=None,
            strategy=None,
            account=None,
            last_data_at=None,
            data_feed_healthy=None,
        )

    day_pnl, day_pnl_pct = await day_pnl_since_open(
        portfolio_repo,
        calendar,
        run_mode=settings.run_mode,
        equity=snapshot.account.equity,
        now=now,
    )
    return LiveDashboard(
        **common,
        book_as_of=snapshot.as_of,
        # Clamped at zero: a worker clock a second ahead of the API's would
        # otherwise render as "updated -1s ago", which reads as a bug in the
        # dashboard rather than the clock skew it is.
        book_age_seconds=max(0, int((now - snapshot.as_of).total_seconds())),
        strategy=snapshot.strategy,
        account=account_view(snapshot.account, day_pnl, day_pnl_pct),
        positions=[
            PositionView.model_validate(p, from_attributes=True) for p in snapshot.positions
        ],
        # Newest first: a feed is read from the top, and the trim takes the most
        # recent `signal_limit` rather than the oldest.
        recent_signals=[
            SignalView.model_validate(s, from_attributes=True)
            for s in reversed(snapshot.recent_signals[-signal_limit:])
        ],
        working_orders=[
            OrderView.model_validate(o, from_attributes=True) for o in snapshot.working_orders
        ],
        symbols=list(snapshot.symbols),
        last_data_at=snapshot.last_data_at,
        data_feed_healthy=(
            _feed_healthy(
                snapshot.last_data_at, now=now, budget=settings.risk.max_quote_age_seconds
            )
            if market_open
            # Out of hours a silent feed is correct, not broken. Reporting it as
            # unhealthy every evening is how a health indicator stops being read.
            else True
        ),
    )


@router.get("/equity-curve", response_model=EquityCurveView)
async def get_equity_curve(
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    portfolio_repo: Annotated[PortfolioRepository, Depends(get_portfolio_repository)],
    days: Annotated[int, Query(ge=1, le=MAX_CURVE_DAYS)] = 30,
    resolution: Annotated[
        str | None,
        Query(description=f"one of {', '.join(RESOLUTIONS)}; omitted picks one to suit `days`"),
    ] = None,
) -> EquityCurveView:
    """Equity over time, for the dashboard's headline chart.

    Thinned to one point per bucket, keeping the **last** observation in each:
    equity is a level, and averaging the minute points inside an hour produces a
    number the account never actually held (`atp_core.dashboard.curve`).

    `resolution` defaults to whatever keeps the requested window under a few
    hundred points, so a caller that has not thought about it gets a readable
    chart instead of a minute-resolution year.
    """
    now = clock.now()
    start = now - timedelta(days=days)
    chosen = resolution or default_resolution_for(days)
    try:
        every = resolve(chosen)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    history = await portfolio_repo.equity_history(settings.run_mode, start=start, end=now)
    return EquityCurveView(
        run_mode=settings.run_mode.value,
        start=start,
        end=now,
        resolution=chosen,
        points=[
            EquityPointView(
                ts=point.ts,
                equity=point.equity,
                cash=point.cash,
                gross_exposure=point.gross_exposure,
            )
            for point in downsample(history, every)
        ],
    )


@router.get("/health")
async def get_system_health() -> dict[str, object]:
    """Operational status: feed connected, broker reachable, worker heartbeat,
    DB lag, last reconciliation result.

    Deliberately still unbuilt, and the blocker is worth naming: **there is no
    worker heartbeat**. The ingestor's `IngestorStats` and the reconciler's last
    report live in the worker's memory and are published nowhere, so the only
    thing this endpoint could honestly answer today is what `/dashboard/live`
    already carries — `last_data_at`, `data_feed_healthy` and the halt list.
    Building it means giving the worker a health key to write, which is a
    producer-side change and belongs with the Phase 6 metrics item rather than
    being faked here from what the API happens to be able to reach.
    """
    raise NotImplementedError
