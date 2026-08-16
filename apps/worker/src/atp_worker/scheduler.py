"""Scheduled jobs.

Times are UTC (rule §1.2) but the schedule is driven by the trading calendar,
not the clock: "30 minutes after the close" is not a fixed UTC time — the US
market closes early on several days a year, and a fixed-time job would run
mid-session on those days.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from atp_core.clock import SystemClock
from atp_core.config import get_settings
from atp_core.data.backfill import GapBackfillResult, backfill_gaps
from atp_core.data.gaps import SUPPORTED_TIMEFRAMES
from atp_core.data.providers.alpaca import AlpacaHistoricalProvider
from atp_core.logging import get_logger
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory

if TYPE_CHECKING:
    from atp_core.domain import Timeframe

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


async def reconcile_with_broker() -> None:
    """Every 5 minutes during market hours, and at startup.

    Any mismatch halts trading — see `execution.reconciliation`.
    """
    raise NotImplementedError


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


async def snapshot_positions() -> None:
    """Every minute during market hours — feeds the dashboard's history charts
    and gives reconciliation something to compare against after a restart."""
    raise NotImplementedError


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


SCHEDULE: list[dict[str, Any]] = [
    {"job": reconcile_with_broker, "trigger": "interval", "minutes": 5, "market_hours_only": True},
    {"job": snapshot_positions, "trigger": "interval", "minutes": 1, "market_hours_only": True},
    {"job": rollover_daily_counters, "trigger": "market_open", "offset_minutes": -5},
    {"job": apply_corporate_actions, "trigger": "market_open", "offset_minutes": -60},
    {"job": generate_daily_report, "trigger": "market_close", "offset_minutes": 30},
    {"job": backfill_missing_bars, "trigger": "cron", "hour": 2, "minute": 0},
]
