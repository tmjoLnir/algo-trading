"""Queued jobs (arq) — work triggered by the API rather than the clock.

Backtests live here because they are long-running: minutes for a multi-year
minute-bar run. Running one inline would block an API worker for the duration.
"""

from __future__ import annotations

from typing import Any


async def run_backtest_task(ctx: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Execute a queued backtest and store the result.

    Report progress to Redis so the UI can show a bar rather than a spinner.
    On failure, persist the error to `backtest_runs.error` — a run stuck at
    "running" forever is the worst outcome for a user.
    """
    raise NotImplementedError


async def backfill_symbol_task(ctx: dict[str, Any], symbol: str, start: str, end: str) -> int:
    """Fetch and store history for one symbol. Returns bars written."""
    raise NotImplementedError


async def generate_report_task(ctx: dict[str, Any], report_type: str, params: dict[str, Any]) -> str:
    """Render a report (CSV/PDF); return its storage key."""
    raise NotImplementedError
