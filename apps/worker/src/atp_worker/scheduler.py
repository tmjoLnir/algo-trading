"""Scheduled jobs.

Times are UTC (rule §1.2) but the schedule is driven by the trading calendar,
not the clock: "30 minutes after the close" is not a fixed UTC time — the US
market closes early on several days a year, and a fixed-time job would run
mid-session on those days.
"""

from __future__ import annotations

from typing import Any


async def reconcile_with_broker() -> None:
    """Every 5 minutes during market hours, and at startup.

    Any mismatch halts trading — see `execution.reconciliation`.
    """
    raise NotImplementedError


async def backfill_missing_bars() -> None:
    """Nightly. Find gaps via `BarRepository.find_gaps` and fetch them.

    Calendar-aware, or every weekend registers as a gap and the alert becomes
    noise within a fortnight.
    """
    raise NotImplementedError


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
