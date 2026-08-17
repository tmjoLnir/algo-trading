"""FastAPI dependencies — the composition root.

Every adapter is wired here and injected, so a test binds a fake broker without
touching a router. This is where the hexagonal architecture actually pays off.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from atp_core.backtest.costs import alpaca_equities_default
from atp_core.brokers import AlpacaBroker, BrokerPort, SimulatedBroker
from atp_core.clock import Clock, SystemClock, TradingCalendar
from atp_core.config import Settings, get_settings
from atp_core.domain.enums import RunMode


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


#: One adapter for the process, keyed by nothing: `AlpacaBroker` owns an
#: `httpx.AsyncClient`, and a per-request adapter would open a fresh connection
#: pool for every call — which on a 200 req/min rate limit is the wrong thing
#: to be spending. Not `lru_cache` on the settings object, which is a mutable
#: pydantic model and therefore unhashable; `get_settings` is already the
#: process-wide singleton, so there is only ever one value to key on.
_broker_instance: BrokerPort | None = None


def _build_broker(settings: Settings) -> BrokerPort:
    if settings.run_mode is RunMode.BACKTEST:
        # A backtest driven through the API has no clock of its own here; the
        # engine owns time and binds its own broker. This exists so the
        # dependency resolves rather than raising in a mode that does not use
        # it, and `alpaca_equities_default()` keeps its fills priced the same
        # way the backtest CLI prices them.
        return SimulatedBroker(clock=SystemClock(), cost_model=alpaca_equities_default())
    return AlpacaBroker(settings)


async def get_broker(settings: Annotated[Settings, Depends(get_settings)]) -> BrokerPort:
    """Bind the broker adapter for the current run mode.

    backtest → SimulatedBroker, paper → Alpaca paper, live → Alpaca live.
    The only place in the API that knows run mode exists.

    Paper and live are the *same* adapter — `Settings.broker_base_url` picks
    the endpoint and `Settings` refuses live without the second lock (rule
    §1.8). There is deliberately no `if paper:` here or anywhere in core.

    Built once per process and then reused, so it does **not** notice a later
    change to settings. A test wanting a different broker overrides this
    dependency — `app.dependency_overrides[get_broker] = lambda: FakeBroker()`
    — rather than editing settings and expecting a rebuild. That is the
    affordance this module exists for, and it is why the fake never needs to
    reach a router.
    """
    global _broker_instance
    if _broker_instance is None:
        _broker_instance = _build_broker(settings)
    return _broker_instance


async def get_kill_switch() -> object:
    raise NotImplementedError


async def get_current_user() -> str:
    """Resolve the acting user.

    Authentication is deliberately a stub in the skeleton — see docs/SAFETY.md
    'Access control'. Do NOT expose this API on a public interface until it is
    implemented: every endpoint under /risk and /orders can move real money.
    """
    raise NotImplementedError
