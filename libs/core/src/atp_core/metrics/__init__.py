"""Operational metrics — what this platform is doing, as numbers.

Distinct from `backtest.metrics` and `analytics.performance`, which answer *was
the strategy any good*. This package answers *is the platform working*, which is
a question asked at 09:31 with positions open and nobody able to read a log file
fast enough.

Every metric is declared in `registry` and nowhere else, rendered by
`exposition`, and served by `apps/api` and `apps/worker`. ADR 0013 is why it is
shaped this way, and `docs/OBSERVABILITY.md` is how to use it.
"""

from __future__ import annotations

from atp_core.metrics.exposition import CONTENT_TYPE, render
from atp_core.metrics.registry import (
    alert_failed,
    alert_sent,
    api_request,
    build_info,
    get_registry,
    halt_cleared,
    halt_engaged,
    order_rejected,
    order_submit_seconds,
    order_submitted,
    reset_for_tests,
    risk_checked,
    strategy_evaluated,
    stream_gap_bars,
    stream_last_tick,
    stream_message,
    stream_reconnected,
)

__all__ = [
    "CONTENT_TYPE",
    "alert_failed",
    "alert_sent",
    "api_request",
    "build_info",
    "get_registry",
    "halt_cleared",
    "halt_engaged",
    "order_rejected",
    "order_submit_seconds",
    "order_submitted",
    "render",
    "reset_for_tests",
    "risk_checked",
    "strategy_evaluated",
    "stream_gap_bars",
    "stream_last_tick",
    "stream_message",
    "stream_reconnected",
]
