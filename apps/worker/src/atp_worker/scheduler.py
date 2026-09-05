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

from atp_core.alerts.ports import Alert, Severity
from atp_core.analytics.daily import DailyReport, render, summarise
from atp_core.clock import SystemClock, TradingCalendar
from atp_core.config import get_settings
from atp_core.data.backfill import GapBackfillResult, backfill_gaps
from atp_core.data.corporate_actions import Adjustment, detect_adjustment
from atp_core.data.gaps import SUPPORTED_TIMEFRAMES
from atp_core.data.providers.alpaca import AlpacaHistoricalProvider
from atp_core.logging import correlation_id, get_logger
from atp_core.persistence.audit import PostgresAuditLog
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.orders import PostgresOrderRepository
from atp_core.risk.killswitch import HaltReason
from atp_core.risk.rules import DAILY_LOSS_RULE

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from atp_core.alerts.ports import AlertSink
    from atp_core.audit.ports import AuditEntry
    from atp_core.clock import Clock
    from atp_core.domain import Order, Portfolio, Timeframe
    from atp_core.execution.reconciliation import Reconciler
    from atp_core.risk.killswitch import KillSwitch
    from atp_worker.runner import RunnerStats

log = get_logger(__name__)

#: How far back the nightly sweep looks. Long enough to cover a three-day
#: weekend plus the night the feed was down, short enough that the scan stays
#: cheap on a minute series. Anything older is a deliberate operator job —
#: `scripts/backfill_bars.py --verify` over the range in question — not
#: something a nightly cron should rediscover and re-fetch every single night.
NIGHTLY_LOOKBACK_DAYS = 7

#: How many of the day's orders the report reads. A session that submitted more
#: than this has a bigger problem than a truncated report, and an unbounded
#: query in an unattended job is how a slow night becomes a stuck one.
DAILY_REPORT_ORDER_LIMIT = 1000

#: Audit rows scanned back for the day's halts. The table holds a handful of
#: rows a day — logins, halts, config saves — so this is generous rather than
#: tight, and it is bounded for the reason above.
DAILY_REPORT_AUDIT_LIMIT = 500

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


#: Who the rollover's clear is attributed to. A process name and not a person,
#: because nothing authenticated one — the same choice `scripts/halt.py` makes
#: for an unattended `engage`, and for ADR 0008's reason: an actor the caller
#: filled in is not an audit trail.
ROLLOVER_ACTOR = "daily_loss_rollover"


@dataclass(frozen=True, slots=True)
class SessionWatch:
    """What the two escalation jobs below need, supplied by `main.py`.

    Separate from `SessionJobs` because the two answer different questions and
    are available at different times. `SessionJobs` is the live *book* and
    exists only when this worker is trading; this is the *halt state and a way
    to reach a human*, which matter whether or not it is — a worker ingesting
    data for a platform that is halted is exactly the case day 1 produced.

    `stats` is optional and callable for the reason `SessionJobs.open_orders` is
    one: the numbers change under the job, and a snapshot taken at wiring time
    would report the day the worker started rather than the day it is closing.
    """

    kill_switch: KillSwitch
    alerts: AlertSink
    stats: Callable[[], RunnerStats] | None = None


async def remind_about_halts(watch: SessionWatch) -> None:
    """While anything is halted, keep saying so. Every 15 minutes, in session.

    **The reminder is durable because its state is Redis, not this process.**
    That is the whole of the fix. On day 1 a global halt stood for 2h37m and
    produced exactly one alert, at the moment it engaged: the halt's own
    deduplication is the Redis record (`killswitch.engage` returns early when a
    halt is already active), which is correct for *engagement* and meant nothing
    ever repeated it. The other continuous signal, the `atp_halt_active` metric,
    was uncollectable because `METRICS_TOKEN` was unset. Both escalation paths
    failed for the same underlying reason, and an operator went home
    (docs/paper-week/day-1-review.md, F8).

    Reading `active_halts` each time is what makes this survive a restart: five
    workers died that afternoon and any per-process reminder flag would have
    died with them.

    The alert `key` carries the reminder's own count so a transport that
    collapses repeats — which is what `key` is for (`alerts.ports`) — does not
    collapse the reminders into the original halt and silence the thing whose
    entire job is not to be silent.
    """
    halts = watch.kill_switch.active_halts()
    if not halts:
        return

    _HALT_REMINDERS["count"] += 1
    count = _HALT_REMINDERS["count"]
    lines = [
        f"{h.scope.value}{f' [{h.target}]' if h.target else ''} — {h.reason.value}, "
        f"by {h.engaged_by}, since {h.engaged_at.isoformat()}"
        for h in halts
    ]
    log.critical("worker.halt_reminder", halts=len(halts), reminder=count)
    watch.alerts.send(
        Alert(
            severity=Severity.CRITICAL,
            title=f"Still halted — {len(halts)} active",
            body="\n".join([*lines, "Nothing is trading. docs/RUNBOOK.md."]),
            key=f"halt.reminder.{count}",
            context={"active": str(len(halts))},
        )
    )


async def summarise_the_session(watch: SessionWatch) -> None:
    """At the close: say what the day actually did, to a human, once.

    Day 1 ran ten hours, submitted zero orders, spent its last 74 minutes
    halted, and told nobody any of it — the two alerts all day were the halt
    engaging and, hours later, a human clearing it. A summary is the one message
    that is worth sending when *nothing* happened, because nothing happening is
    indistinguishable from working perfectly until somebody says so
    (docs/paper-week/day-1-review.md, F8).

    Deliberately not `generate_daily_report`, which sits half an hour after this
    and is still a stub. That is a document; this is a sentence on a phone, and
    it must not wait on the report being built.
    """
    halts = watch.kill_switch.active_halts()
    stats = watch.stats() if watch.stats is not None else None

    if stats is None:
        headline = "no strategy ran today"
    else:
        headline = (
            f"{stats.orders_submitted} orders submitted, "
            f"{stats.signals_generated} signals, "
            f"{stats.evaluations} evaluations, "
            f"{stats.orders_rejected_by_risk} refused by risk"
        )

    lines = [headline]
    if halts:
        lines.append(f"STILL HALTED at the close — {len(halts)} active:")
        lines += [
            f"  {h.scope.value}{f' [{h.target}]' if h.target else ''} since "
            f"{h.engaged_at.isoformat()} ({h.reason.value})"
            for h in halts
        ]
    else:
        lines.append("Not halted.")

    log.info(
        "worker.session_summary",
        orders_submitted=stats.orders_submitted if stats else None,
        evaluations=stats.evaluations if stats else None,
        halted=bool(halts),
    )
    watch.alerts.send(
        Alert(
            # CRITICAL when the day ended halted, because that is a state
            # somebody has to act on before tomorrow's open; INFO otherwise.
            severity=Severity.CRITICAL if halts else Severity.INFO,
            title="Session closed" + (" — STILL HALTED" if halts else ""),
            body="\n".join(lines),
            # Dated, so one summary a day survives a transport that collapses
            # on `key` and two summaries never do.
            key=f"session.summary.{_session_day(watch)}",
            context={"halted": str(bool(halts))},
        )
    )


#: The reminder counter. Module state, and the one piece of this that is *not*
#: durable — deliberately, because it only has to make consecutive alert keys
#: differ. What must survive a restart is the halt itself, and that is in Redis.
_HALT_REMINDERS: dict[str, int] = {"count": 0}


def _session_day(watch: SessionWatch) -> str:
    """Today, for the summary's alert key. Off the halt record when there is
    one, so the key cannot depend on a clock this module does not own."""
    halts = watch.kill_switch.active_halts()
    if halts:
        return halts[0].engaged_at.date().isoformat()
    return SystemClock().now().date().isoformat()


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


async def generate_daily_report(watch: SessionWatch) -> DailyReport:
    """After the close: what the session did, from what the record can support.

    Half an hour after the close, and deliberately *after*
    `summarise_the_session`, which fires at the bell. The two are not the same
    message and neither replaces the other: that one is a sentence on a phone
    telling an operator whether to act tonight; this is the day written down,
    section by section, including the sections nothing can answer.

    **What is not measurable is reported as absent, never as zero.** The API's
    own stub said three of the five promised sections "are not gathered anywhere
    one query can reach". Two of the three have moved since — refused orders are
    rows, and so are halts a person engaged — and feed incidents have not moved
    at all. `analytics.daily` carries that distinction; this job's contribution
    is to fetch honestly, which means passing `audit=None` when the audit table
    could not be read rather than an empty list that reads as "nothing
    happened".

    There is still **no storage and no artifact**, which is why this returns the
    report and logs it rather than writing a file. `queue.generate_report_task`
    is blocked on an object store this platform does not have; a rendered PDF
    nothing can fetch would be a key pointing at nowhere. A structured log line
    and `GET /analytics/reports/daily`, which computes the same report on
    demand from the same records, need neither.
    """
    settings = get_settings()
    clock = SystemClock()
    now = clock.now()
    session_start = now - timedelta(days=1)

    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        orders = await PostgresOrderRepository(factory).recent_orders(
            settings.run_mode, since=session_start, limit=DAILY_REPORT_ORDER_LIMIT
        )
        # None, not [], when the read fails: the report renders "not recorded"
        # for an audit table it could not reach and "0 halts" for one it could,
        # and those are different days.
        try:
            rows = await PostgresAuditLog(factory).recent(limit=DAILY_REPORT_AUDIT_LIMIT)
            audit: list[AuditEntry] | None = [
                entry for _, entry in rows if entry.at >= session_start
            ]
        except Exception as exc:
            log.warning("worker.daily_report.audit_unavailable", error=str(exc))
            audit = None
    finally:
        await engine.dispose()

    report = summarise(now.date(), orders, audit=audit)

    log.info(
        "worker.daily_report",
        day=report.day.isoformat(),
        headline=report.headline(),
        orders_submitted=report.orders_submitted,
        orders_filled=report.orders_filled,
        orders_refused=report.orders_refused,
        refusals_by_rule=report.refusals_by_rule,
        not_measured=[s.name for s in report.absent],
        report=render(report),
    )
    watch.alerts.send(
        Alert(
            severity=Severity.INFO,
            title=f"Daily report — {report.day.isoformat()}",
            body=render(report),
            key=f"daily.report.{report.day.isoformat()}",
            context={"orders": str(report.orders_submitted)},
        )
    )
    return report


async def apply_corporate_actions(watch: SessionWatch) -> list[Adjustment]:
    """Pre-open. Refresh adjusted history, and say what moved overnight.

    A split or a dividend restates every historical *adjusted* close for the
    symbol at the vendor. Nothing in this platform is told: the nightly sweep
    fills **gaps**, so it never re-fetches a bar we already hold, and a bar
    stored last week keeps last week's `adj_close` for ever. Backtests read the
    adjusted series (CLAUDE.md §5), so the drift lands where nobody looks until
    a strategy is being evaluated against prices that stopped being true.

    So this re-fetches the recent window **adjusted** and upserts it. That is
    the whole correction, and it needs no new machinery:
    `PostgresBarRepository.upsert_bars` already merges `adj_close` such that a
    present incoming value wins, and its docstring already says why — "a
    corporate action makes every historical `adj_close` for that symbol stale
    and the newer figure is the correct one".

    **It does not touch a position, and that is a decision rather than a gap.**
    A 4:1 split makes a held 100 read as 400 at the venue, which
    `Reconciler.reconcile` sees as a quantity mismatch and halts on. Applying
    the factor here would prevent that halt — and would mean this job silently
    rewriting share counts and cost bases from a number it *inferred from
    prices*. `adopt_broker_state` is "deliberately NOT automatic" for the same
    reason, in its own words: silently adopting hides whatever caused the drift,
    and a split shares its shape with a duplicate-submission bug. The halt is
    the right outcome. What was missing is that it arrived unexplained.

    Running an hour before the open is what changes: a split-shaped move is
    alerted *now*, naming the symbol and the factor, so the mismatch an hour
    later reads as the thing you were already told about and the operator
    adopts with evidence rather than investigating a phantom.

    Returns what it detected, so a caller — and a test — can see the work rather
    than infer it from a log line.
    """
    settings = get_settings()
    now = SystemClock().now()
    start = now - timedelta(days=NIGHTLY_LOOKBACK_DAYS)

    engine = create_engine(settings.database_url)
    provider = AlpacaHistoricalProvider(
        settings, min_request_interval_seconds=60.0 / NIGHTLY_REQUESTS_PER_MINUTE
    )
    repository = PostgresBarRepository(create_session_factory(engine))
    found: list[Adjustment] = []

    try:
        by_timeframe: dict[Timeframe, list[str]] = {}
        for symbol, timeframe in await repository.stored_series():
            if timeframe in SUPPORTED_TIMEFRAMES:
                by_timeframe.setdefault(timeframe, []).append(symbol)

        for timeframe, symbols in by_timeframe.items():
            fresh = await provider.get_bars(symbols, timeframe, start, now, adjusted=True)
            for symbol in symbols:
                incoming = fresh.get(symbol, [])
                if not incoming:
                    continue
                stored = await repository.get_bars(symbol, timeframe, start, now)
                adjustment = detect_adjustment(symbol, stored, incoming)
                # Written back whether or not one factor explains it. The fresh
                # figures are the vendor's current truth either way, and a
                # series this cannot *name* is still a series that should not
                # keep last week's numbers.
                await repository.upsert_bars(incoming)
                if adjustment is not None:
                    found.append(adjustment)
                    _report_adjustment(watch, adjustment, timeframe)
    finally:
        await provider.aclose()
        await engine.dispose()

    log.info(
        "worker.corporate_actions.done",
        series=sum(len(s) for s in by_timeframe.values()),
        adjustments=len(found),
        split_like=sum(1 for a in found if a.is_split_like),
    )
    return found


def _report_adjustment(watch: SessionWatch, adjustment: Adjustment, timeframe: Timeframe) -> None:
    """Say what moved, at the volume the size of the move deserves.

    A dividend adjustment is a fact worth recording and not worth a phone call
    at 08:30; a split-shaped one changes what every position is worth and is
    going to halt the platform within the hour. Alerting on both would train an
    operator to ignore the one that matters.
    """
    detail = {
        "symbol": adjustment.symbol,
        "timeframe": timeframe.value,
        "factor": str(adjustment.factor),
        "bars_compared": adjustment.bars_compared,
        "bars_agreeing": adjustment.bars_agreeing,
        "consistent": adjustment.is_consistent,
    }

    if not adjustment.is_consistent:
        # Not a smaller version of a detected split: the history moved in a way
        # one corporate action cannot account for, so naming a factor would be
        # inventing a story the bars do not support.
        log.critical("worker.corporate_actions.inconsistent", **detail)
        watch.alerts.send(
            Alert(
                severity=Severity.CRITICAL,
                title=f"{adjustment.symbol}: adjusted history changed inconsistently",
                body=(
                    f"{adjustment.bars_agreeing} of {adjustment.bars_compared} bars agree on a "
                    f"factor of {adjustment.factor}. One corporate action would move all of "
                    f"them. The refreshed prices are stored; the cause is not a split this "
                    f"can name. Check the vendor's series before the open."
                ),
                key=f"corporate_action.inconsistent.{adjustment.symbol}",
                context={"symbol": adjustment.symbol},
            )
        )
        return

    if not adjustment.is_split_like:
        log.info("worker.corporate_actions.adjusted", **detail)
        return

    log.critical("worker.corporate_actions.split_like", **detail)
    watch.alerts.send(
        Alert(
            severity=Severity.CRITICAL,
            title=f"{adjustment.symbol}: corporate action, factor {adjustment.factor}",
            body=(
                f"Adjusted history moved by {adjustment.factor} across "
                f"{adjustment.bars_compared} bars. A held quantity will read "
                f"{adjustment.implied_position_factor}x larger at the venue, so "
                f"reconciliation will halt on the mismatch. That halt is expected: "
                f"check the broker's position against this factor, then adopt "
                f"(docs/RUNBOOK.md, 'Reconciliation mismatch')."
            ),
            key=f"corporate_action.{adjustment.symbol}.{adjustment.factor}",
            context={"symbol": adjustment.symbol, "factor": str(adjustment.factor)},
        )
    )


async def rollover_daily_counters(watch: SessionWatch) -> None:
    """At the session open: release the halt yesterday's loss limit engaged.

    **Two of the three things this stub promised were already being done, and
    saying so is most of the fix.** The daily-loss anchor is reset by
    `StrategyRunner.warmup`, which the run loop re-runs at every open and which
    calls `RiskEngine.anchor_session` there (`runner.py`); and the rate-limit
    "counter" is a trailing sixty-second deque that prunes itself on every read,
    so there has never been anything to roll over. Filling this in as written
    would have re-anchored a second time from a job — and `anchor_session`'s own
    docstring names that as the mistake, because re-anchoring mid-session grants
    the day a second allowance against a drawn-down number.

    What was genuinely missing is the third clause, and it could not be built
    because its counterpart did not exist either: **nothing ever engaged a
    daily-loss halt.** `StrategyRunner._escalate` now does, so there is finally
    something here to clear (docs/paper-week/day-1-review.md, F10).

    **This is the only automated clear in the platform, and the narrowness is
    what makes it defensible.** docs/RISK.md is explicit that clearing is
    deliberate where engaging is reflexive, and that asymmetry is not being
    softened: this releases a halt only when *every* one of these holds —

    1. the reason is exactly `DAILY_LOSS_LIMIT`, so a manual halt, a feed halt
       or a reconciliation halt standing beside it is untouched;
    2. it was engaged by the risk chain itself and not by a human who happened
       to pick that reason from `scripts/halt.py --reason`;
    3. it was engaged *before today's session*, so a limit breached this morning
       is never cleared by this morning's rollover.

    A halt about *today's* loss is meaningless tomorrow, and the alternative is
    worse than it looks: a platform that needs a human every morning after a bad
    day teaches that human to clear halts without reading them, which is the
    habit the whole asymmetry exists to prevent.

    `cleared_by` names this job rather than a person, because nothing
    authenticated one — the same honesty `scripts/halt.py` applies to an
    unattended `engage` (ADR 0008).
    """
    now = SystemClock().now()
    for record in watch.kill_switch.active_halts():
        if record.reason is not HaltReason.DAILY_LOSS_LIMIT:
            continue
        if record.engaged_by != DAILY_LOSS_RULE:
            continue
        if record.engaged_at.date() >= now.date():
            log.info(
                "worker.rollover.halt_kept",
                engaged_at=record.engaged_at.isoformat(),
                msg="the loss limit was breached today — this is not yesterday's halt",
            )
            continue

        watch.kill_switch.clear(record.scope, cleared_by=ROLLOVER_ACTOR, target=record.target)
        log.warning(
            "worker.rollover.halt_released",
            scope=record.scope.value,
            target=record.target,
            engaged_at=record.engaged_at.isoformat(),
            msg="a new session resets the daily loss limit, so its halt is released",
        )
        watch.alerts.send(
            Alert(
                severity=Severity.INFO,
                title="Daily loss halt released",
                body=(
                    f"The halt engaged at {record.engaged_at.isoformat()} was the daily "
                    f"loss limit, and a new session resets it. Trading may resume. "
                    f"Nothing else that is halted has been touched."
                ),
                key=f"halt.rollover.{record.engaged_at.date().isoformat()}",
                context={"scope": record.scope.value},
            )
        )


#: What runs whether or not this worker is trading. Two of the three are still
#: stubs and the driver marks each dormant after one attempt.
#:
#: **`rollover_daily_counters` moved out of this list rather than being filled
#: in here.** It needs the kill switch, so it is bound like the other two
#: escalation jobs in `build_schedule` — and most of what it was specified to do
#: turned out to be done already, which its own docstring now records
#: (docs/paper-week/day-1-review.md, F10).
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
    {"job": backfill_missing_bars, "trigger": "cron", "hour": 2, "minute": 0},
]


def build_schedule(
    session: SessionJobs | None = None, watch: SessionWatch | None = None
) -> list[dict[str, Any]]:
    """The schedule this worker will actually run.

    `SCHEDULE` is the part that needs nothing from the trading loop. The jobs
    that need the live book are added only when there is one: a worker with no
    strategy configured holds no portfolio and has no broker to compare it
    against, and an entry that failed every five minutes for want of either
    would be indistinguishable in the log from a venue that had gone away.

    `watch` is added on a *different* condition and that is the point: the halt
    reminder and the session summary need a kill switch and a way to reach a
    human, not a book. A worker ingesting data for a halted platform still owes
    somebody both messages, and on day 1 it was exactly that worker which sent
    neither (docs/paper-week/day-1-review.md, F8).
    """
    watching: list[dict[str, Any]] = []
    if watch is not None:
        watching = [
            {
                # Fifteen minutes: often enough that nobody goes home believing
                # the platform is trading, rare enough not to be the thing an
                # operator mutes. Day 1's halt would have produced ten of these.
                "job": partial(remind_about_halts, watch),
                "trigger": "interval",
                "minutes": 15,
                "market_hours_only": True,
            },
            {
                # At the close itself rather than with the report half an hour
                # later: this is the message that says whether to act tonight.
                "job": partial(summarise_the_session, watch),
                "trigger": "market_close",
                "offset_minutes": 0,
            },
            {
                # Before the open, so a halt released here is released while
                # nothing is trading rather than mid-session.
                "job": partial(rollover_daily_counters, watch),
                "trigger": "market_open",
                "offset_minutes": -5,
            },
            {
                # An hour out, so a split-shaped move is alerted with time to
                # act before the reconciler halts on the mismatch it causes.
                "job": partial(apply_corporate_actions, watch),
                "trigger": "market_open",
                "offset_minutes": -60,
            },
            {
                # Half an hour after the close, and after `summarise_the_session`
                # at the bell: that one says whether to act tonight, this one is
                # the day written down.
                "job": partial(generate_daily_report, watch),
                "trigger": "market_close",
                "offset_minutes": 30,
            },
        ]

    if session is None:
        return [*watching, *SCHEDULE]
    return [
        {
            "job": partial(reconcile_with_broker, session),
            "trigger": "interval",
            "minutes": 5,
            "market_hours_only": True,
        },
        *watching,
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
