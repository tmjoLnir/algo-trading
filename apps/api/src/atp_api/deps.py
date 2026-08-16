"""FastAPI dependencies — the composition root.

Every adapter is wired here and injected, so a test binds a fake broker without
touching a router. This is where the hexagonal architecture actually pays off.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from atp_core.clock import Clock, SystemClock, TradingCalendar
from atp_core.config import Settings, get_settings


async def get_db() -> AsyncIterator[object]:
    """Request-scoped database session."""
    raise NotImplementedError


async def get_clock() -> Clock:
    """Wall-clock time, behind the port.

    Every read of "now" goes through this rather than `datetime.now()`, so a
    test can pin it and nothing in a handler has to reach for the system clock
    itself (CLAUDE.md §1.2).
    """
    return SystemClock()


@lru_cache(maxsize=1)
def _trading_calendar() -> TradingCalendar:
    """One calendar for the process.

    Cached because it caches: sessions are materialised a year at a time and
    kept on the instance, so a per-request calendar would rebuild the same year
    on every request. Built lazily rather than at import — constructing one
    pulls in pandas, and an API that never asks about sessions should not pay
    for it at startup.
    """
    return TradingCalendar()


async def get_calendar() -> TradingCalendar:
    return _trading_calendar()


async def get_redis() -> object:
    raise NotImplementedError


async def get_broker(settings: Annotated[Settings, Depends(get_settings)]) -> object:
    """Bind the broker adapter for the current run mode.

    backtest → SimulatedBroker, paper → Alpaca paper, live → Alpaca live.
    The only place in the API that knows run mode exists.
    """
    raise NotImplementedError


async def get_kill_switch() -> object:
    raise NotImplementedError


async def get_current_user() -> str:
    """Resolve the acting user.

    Authentication is deliberately a stub in the skeleton — see docs/SAFETY.md
    'Access control'. Do NOT expose this API on a public interface until it is
    implemented: every endpoint under /risk and /orders can move real money.
    """
    raise NotImplementedError
