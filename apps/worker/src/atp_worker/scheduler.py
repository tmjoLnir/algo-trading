"""Scheduled jobs.

Times are UTC (rule §1.2) but the schedule is driven by the trading calendar,
not the clock: "30 minutes after the close" is not a fixed UTC time — the US
market closes early on several days a year, and a fixed-time job would run
mid-session on those days.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from atp_core.clock import SystemClock, TradingCalendar
from atp_core.config import get_settings
from atp_core.data.backfill import GapBackfillResult, backfill_gaps
from atp_core.data.gaps import SUPPORTED_TIMEFRAMES
from atp_core.data.providers.alpaca import AlpacaHistoricalProvider
from atp_core.logging import correlation_id, get_logger
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from atp_core.clock import Clock
    from atp_core.domain import Order, Portfolio, Timeframe
    from atp_core.execution.reconciliation import Reconciler

log = get_logger(__name__)

#: How far back the nightly sweep looks. Long enough to cover a three-day
#: weekend plus the night the feed was down, short enough that the scan stays
#: cheap on a minute series. Anything older is a deliberate operator job —
#: `scripts/backfill_bars.py --verify` over the range in question — not
#: something a nightly cron should rediscover and re-fetch every single night.
NIGHTLY_LOOKBACK_DAYS = 7

#: Paces the vendor's 200/min. The nightly job runs unattended against the same
#: rate limit as everything else; being the reason a morning fetch gets a 429 is
#: not worth the few minutes it saves.
NIGHTLY_REQUESTS_PER_MINUTE = 120


@dataclass(frozen=True, slots=True)
class SessionJobs:
    """The live trading state a scheduled job needs, supplied by `main.py`.

    `portfolio` is the runner's own object, held by reference rather than
    copied. It is mutated in place as fills arrive, and a copy would be a book
    that stopped being true the moment it was taken — which is the one kind of
    staleness a reconciler must not have.

    `open_orders` is a callable rather than a list for the same reason: what is
    working at the venue changes between one five-minute check and the next, and
    a list captured at wiring time would report every order placed since as an
    orphan and halt on it.
    """

    reconciler: Reconciler
    portfolio: Portfolio
    open_orders: Callable[[], list[Order]]


async def reconcile_with_broker(session: SessionJobs) -> None:
    """Every 5 minutes during market hours. Any mismatch halts trading.

    `execution.reconciliation`'s own docstring says it runs "at startup, on a
    schedule, and after any reconnect". Two of those three were built: the
    runner reconciles in `warmup`, and `trading.consume_trade_updates` does it
    again whenever the trade-updates socket comes back. **The schedule was
    not**, so between a clean start and the next reconnect nothing checked the
    book at all — and the drift this exists to catch (a stop that fired while
    the socket was healthy, a corporate action, a fill the venue never pushed)
    announces itself at neither of those moments. docs/SAFETY.md layer 7 names
    its failure mode as "reconciliation itself is not running", which is what a
    schedule with a hole in it is.

    Session-bound rather than building its own dependencies like the nightly
    sweep below, and it has to be. The book this must check is the **runner's
    live portfolio** — the object orders are sized against — not a row read back
    from Postgres. A reload would compare the venue against a snapshot written
    up to a tick ago and report every fill in between as a discrepancy, which is
    a halt caused by reading, not by drift.

    Left to the `Reconciler`'s default, a mismatch halts. That is not this job
    softening or hardening anything: `reconcile` engages the kill switch itself,
    and passing `halt_on_mismatch=False` here would disable a documented safety
    check from the one caller that runs unattended.

    The report is logged either way. A clean run at INFO is what makes "layer 7
    is running" observable rather than assumed — the absence of a halt proves
    nothing, since a job that never ran also never halts.
    """
    report = await session.reconciler.reconcile(
        session.portfolio, known_orders=session.open_orders()
    )
    if report.is_clean:
        log.info("worker.reconcile.clean", checked_at=report.checked_at.isoformat())
        return

    # The reconciler has already engaged the kill switch. Repeated here because
    # this is an unattended path: an operator reading the worker's log should
    # find what diverged next to the halt, not have to correlate two streams.
    log.critical(
        "worker.reconcile.diverged",
        summary=report.summary(),
        discrepancies=len(report.discrepancies),
        orphan_orders=len(report.orphan_order_ids),
        msg="trading is halted — see docs/RUNBOOK.md 'Reconciliation mismatch'",
    )


async def backfill_missing_bars() -> list[GapBackfillResult]:
    """Nightly. Find gaps via `BarRepository.find_gaps` and fetch them.

    Calendar-aware, or every weekend registers as a gap and the alert becomes
    noise within a fortnight.

    Wiring only — which windows to fetch and whether they were actually filled
    is `atp_core.data.backfill.backfill_gaps`, where it is testable without a
    network or a database. What is decided here is the scope: every series the
    store already holds, over the last `NIGHTLY_LOOKBACK_DAYS`.

    Anything still missing afterwards is logged at WARNING with the symbol and
    window. That is deliberately not an exception: a symbol that had not listed
    yet leaves a permanent hole, and a nightly job that raises on it stops
    checking everything after it in the sweep.
    """
    settings = get_settings()
    now = SystemClock().now()
    start = now - timedelta(days=NIGHTLY_LOOKBACK_DAYS)

    engine = create_engine(settings.database_url)
    provider = AlpacaHistoricalProvider(
        settings, min_request_interval_seconds=60.0 / NIGHTLY_REQUESTS_PER_MINUTE
    )
    repository = PostgresBarRepository(create_session_factory(engine))
    results: list[GapBackfillResult] = []

    try:
        by_timeframe: dict[Timeframe, list[str]] = {}
        skipped: set[str] = set()
        for symbol, timeframe in await repository.stored_series():
            if timeframe in SUPPORTED_TIMEFRAMES:
                by_timeframe.setdefault(timeframe, []).append(symbol)
            else:
                # `1h`/`4h` bars do not divide a session evenly, so there is no
                # grid to check them against (docs/DATA.md 'Gaps'). Named rather
                # than dropped: a sweep that quietly covers less than it appears
                # to is how a hole survives a job that reports success.
                skipped.add(timeframe.value)

        log.info(
            "worker.backfill_missing_bars.starting",
            start=start.isoformat(),
            end=now.isoformat(),
            series=sum(len(s) for s in by_timeframe.values()),
            timeframes=sorted(t.value for t in by_timeframe),
            skipped_timeframes=sorted(skipped),
        )

        for timeframe, symbols in by_timeframe.items():
            result = await backfill_gaps(
                provider,
                repository,
                symbols=symbols,
                timeframe=timeframe,
                start=start,
                end=now,
            )
            results.append(result)
            for symbol, gap_start, gap_end in result.remaining:
                log.warning(
                    "worker.backfill_missing_bars.unfilled",
                    symbol=symbol,
                    timeframe=timeframe.value,
                    start=gap_start.isoformat(),
                    end=gap_end.isoformat(),
                    hint="the vendor has no bars there either — pre-listing, halted, or lost",
                )
    finally:
        await provider.aclose()
        await engine.dispose()

    log.info(
        "worker.backfill_missing_bars.done",
        gaps_found=sum(r.gaps_found for r in results),
        bars_written=sum(r.bars_written for r in results),
        requests=sum(r.requests for r in results),
        unfilled=sum(len(r.remaining) for r in results),
    )
    return results


async def generate_daily_report() -> None:
    """After the close: P&L, trades, risk rejections, halts, feed incidents."""
    raise NotImplementedError


async def apply_corporate_actions() -> None:
    """Pre-open. Splits and dividends change share counts and adjusted history.

    A 4:1 split that is not applied makes a position look like it lost 75%
    overnight, which will trip stops and the daily loss limit on a day when
    nothing actually happened.
    """
    raise NotImplementedError


async def rollover_daily_counters() -> None:
    """At the session open: reset the daily loss limit and rate-limit counters,
    and clear any halt that was engaged purely by the daily loss limit."""
    raise NotImplementedError


#: What runs whether or not this worker is trading. Three of the four are still
#: stubs and the driver marks each dormant after one attempt.
#:
#: **`snapshot_positions` is deliberately absent rather than unimplemented.** It
#: sat here on a one-minute market-hours interval, described as feeding the
#: dashboard's history charts and giving reconciliation a baseline after a
#: restart. `StrategyRunner._persist` already does exactly that at the end of
#: every evaluation — `portfolio_repo.snapshot` then `snapshot_store.put` — on
#: the same one-minute cadence (`ENGINE_TICK_INTERVAL_SECONDS`) inside the same
#: session window. Building it would have duplicated every equity-history point
#: and added a second writer to a store whose port says it has one by design:
#: "a second writer would be a bug to fix at its source rather than a race to
#: arbitrate here" (`dashboard/ports.py`). A worker not running a strategy has
#: no book to snapshot either, so there is no case the job would have covered.
SCHEDULE: list[dict[str, Any]] = [
    {"job": rollover_daily_counters, "trigger": "market_open", "offset_minutes": -5},
    {"job": apply_corporate_actions, "trigger": "market_open", "offset_minutes": -60},
    {"job": generate_daily_report, "trigger": "market_close", "offset_minutes": 30},
    {"job": backfill_missing_bars, "trigger": "cron", "hour": 2, "minute": 0},
]


def build_schedule(session: SessionJobs | None = None) -> list[dict[str, Any]]:
    """The schedule this worker will actually run.

    `SCHEDULE` is the part that needs nothing from the trading loop. The jobs
    that need the live book are added only when there is one: a worker with no
    strategy configured holds no portfolio and has no broker to compare it
    against, and an entry that failed every five minutes for want of either
    would be indistinguishable in the log from a venue that had gone away.
    """
    if session is None:
        return list(SCHEDULE)
    return [
        {
            "job": partial(reconcile_with_broker, session),
            "trigger": "interval",
            "minutes": 5,
            "market_hours_only": True,
        },
        *SCHEDULE,
    ]


# ── the driver ──────────────────────────────────────────────────────────────
#
# `SCHEDULE` above is a declaration and was, until now, only that: nothing read
# it, so `backfill_missing_bars` had no caller outside a test. What follows is
# the loop that runs it.
#
# Hand-rolled rather than apscheduler, which this package already depends on.
# Two of the four trigger types here are relative to a *session* — five minutes
# before the open, thirty after the close — and neither a cron nor an interval
# trigger can express either: the open shifts an hour with DST and the close
# shifts three on a half-day. In apscheduler that needs a custom trigger that
# consults `TradingCalendar` and re-registers itself after every fire, which is
# the loop below with a second scheduler underneath it.

#: How far ahead a session scan looks for the next open or close. Ten days
#: covers the longest run of closed days the NYSE produces — a holiday landing
#: either side of a weekend — while still failing loudly rather than spinning
#: if the calendar returns no sessions at all.
SESSION_SCAN_DAYS = 10

#: Longest single sleep before the due times are recomputed. A job due tomorrow
#: could be awaited in one 24-hour sleep, but then a clock stepped by NTP, a
#: laptop resumed from suspend, or a DST boundary would all be discovered a day
#: late. Re-deriving the schedule every few minutes costs nothing and is what
#: keeps "30 minutes after the close" true on the days the close moves.
MAX_SLEEP_SECONDS = 300.0


def _job_name(entry: dict[str, Any]) -> str:
    """What to call this entry in a log line or an error.

    Unwraps `functools.partial`, which carries no `__name__` of its own: the
    session-bound jobs `build_schedule` produces are partials, and a scheduler
    naming them all "partial" would name nothing.
    """
    job: Any = entry["job"]
    return str(getattr(job, "__name__", None) or job.func.__name__)


@dataclass(slots=True)
class _Job:
    """One entry from `SCHEDULE`, plus the mutable bit: when it next runs."""

    entry: dict[str, Any]
    due: datetime
    #: Set once a job turns out to be a `NotImplementedError` stub. Three of the
    #: four in `SCHEDULE` still are, and a scheduler that retried them every
    #: interval would bury its own log in the same traceback forever.
    dormant: bool = False

    @property
    def name(self) -> str:
        return _job_name(self.entry)


def next_due(entry: dict[str, Any], now: datetime, calendar: TradingCalendar) -> datetime:
    """When this entry should next run, strictly after `now`.

    Pure, so every trigger type is testable against a fixed clock and a real
    calendar without waiting for one to elapse.
    """
    trigger = entry["trigger"]
    if trigger == "cron":
        candidate = now.replace(
            hour=int(entry["hour"]), minute=int(entry["minute"]), second=0, microsecond=0
        )
        return candidate if candidate > now else candidate + timedelta(days=1)

    if trigger == "interval":
        due = now + timedelta(minutes=int(entry["minutes"]))
        # A market-hours job whose next slot falls outside a session waits for
        # the open instead of firing into a shut market and being skipped over
        # and over until it reopens.
        if entry.get("market_hours_only") and not calendar.is_open(due):
            return calendar.next_open(due)
        return due

    if trigger in {"market_open", "market_close"}:
        return _next_session_edge(entry, now, calendar, trigger)

    raise ValueError(f"unknown scheduler trigger {trigger!r} for {_job_name(entry)}")


def _next_session_edge(
    entry: dict[str, Any], now: datetime, calendar: TradingCalendar, trigger: str
) -> datetime:
    """The next session open/close, shifted by the entry's offset.

    The offset is applied *to the session*, which is the whole reason this is
    calendar-driven rather than a fixed UTC time: "30 minutes after the close"
    is 20:30Z most days, 17:30Z on a half-day, and nothing at all on a holiday.
    A fixed-time job would run mid-session on every early close.
    """
    offset = timedelta(minutes=int(entry.get("offset_minutes", 0)))
    day = now.astimezone(calendar.tz).date()
    for _ in range(SESSION_SCAN_DAYS):
        session = calendar.session_on(day)
        if session is not None:
            edge = session.open_at if trigger == "market_open" else session.close_at
            due = edge + offset
            if due > now:
                return due
        day += timedelta(days=1)
    raise ValueError(
        f"no trading session within {SESSION_SCAN_DAYS} days of {now.isoformat()} — "
        f"cannot schedule {_job_name(entry)}"
    )


async def run_scheduler(
    *,
    clock: Clock | None = None,
    calendar: TradingCalendar | None = None,
    schedule: list[dict[str, Any]] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Run the schedule until cancelled.

    One loop rather than a task per job: the jobs here are minutes or hours
    apart and none is long-running, so serialising them costs nothing and buys
    the guarantee that the nightly backfill is never racing reconciliation for
    the same connection pool.

    A job that raises is logged and *rescheduled* — a nightly sweep that failed
    tonight should still run tomorrow, and a scheduler that died with it would
    take the last thing still working in this process down too. The exception is
    `NotImplementedError`, which is not a failure but an unbuilt job: it goes
    dormant and is named once, so the log says which responsibilities this
    worker is and is not discharging.
    """
    clock = clock if clock is not None else SystemClock()
    calendar = calendar if calendar is not None else TradingCalendar()
    entries = SCHEDULE if schedule is None else schedule
    do_sleep = sleep if sleep is not None else _sleep_seconds

    now = clock.now()
    jobs = [_Job(entry=entry, due=next_due(entry, now, calendar)) for entry in entries]
    log.info(
        "worker.scheduler.starting",
        jobs=len(jobs),
        next_due={j.name: j.due.isoformat() for j in sorted(jobs, key=lambda j: j.due)},
    )

    while True:
        live = [job for job in jobs if not job.dormant]
        if not live:
            # Nothing left to run. Parking rather than returning: the supervisor
            # reads a responsibility that finished as one that died, and this is
            # an empty schedule rather than a crash.
            log.error(
                "worker.scheduler.nothing_to_run",
                msg="every scheduled job is unimplemented — parking until shutdown",
            )
            await asyncio.Event().wait()
            return  # pragma: no cover - unreachable; the wait above never ends

        now = clock.now()
        job = min(live, key=lambda j: j.due)
        if job.due > now:
            await do_sleep(min((job.due - now).total_seconds(), MAX_SLEEP_SECONDS))
            continue

        await _run_job(job, now, clock, calendar)


async def _run_job(job: _Job, now: datetime, clock: Clock, calendar: TradingCalendar) -> None:
    """Run one due job, then decide when it runs next."""
    if job.entry.get("market_hours_only") and not calendar.is_open(now):
        # Due, but the market shut while it was waiting — an early close, or a
        # holiday the schedule was built before. Defer rather than run.
        job.due = next_due(job.entry, now, calendar)
        log.debug("worker.scheduler.deferred", job=job.name, next_due=job.due.isoformat())
        return

    # One run of one job is a unit of work, so it gets an id and every line it
    # writes — including from `atp_core` several layers down, which knows
    # nothing about schedulers — carries it. A nightly backfill logs from four
    # modules and interleaves with the ingestor; without this, reconstructing
    # which lines belonged to it means reading timestamps and guessing.
    with correlation_id():
        log.info("worker.scheduler.job_starting", job=job.name)
        try:
            await job.entry["job"]()
        except NotImplementedError:
            job.dormant = True
            log.warning(
                "worker.scheduler.job_not_implemented",
                job=job.name,
                msg="not built yet — this worker will not perform it",
            )
            return
        except Exception as exc:
            # Deliberately broad: see the docstring on `run_scheduler`. A
            # scheduled job is unattended, so the alternative to logging is
            # silence.
            log.error("worker.scheduler.job_failed", job=job.name, error=str(exc), exc_info=True)
        else:
            log.info("worker.scheduler.job_done", job=job.name)

    job.due = next_due(job.entry, clock.now(), calendar)


async def _sleep_seconds(seconds: float) -> None:
    """The scheduler's wait, wrapped so the injected one has a plain type."""
    await asyncio.sleep(seconds)
