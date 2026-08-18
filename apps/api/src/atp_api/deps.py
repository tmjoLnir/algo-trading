"""FastAPI dependencies — the composition root.

Every adapter is wired here and injected, so a test binds a fake broker without
touching a router. This is where the hexagonal architecture actually pays off.

Two lifetimes live in this file and the difference matters. A `TradingCalendar`
and a `BrokerPort` are process-wide singletons: they are expensive to build and
hold no per-request state. Connection pools — Postgres, Redis — belong to the
*application*, are opened by `main.lifespan` and are read off `app.state` here.
That is not decoration: a pool built lazily in a dependency has nowhere to be
closed, and a worker process that exits without releasing its Redis connections
leaves them for the server to time out.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atp_core.backtest.costs import alpaca_equities_default
from atp_core.brokers import AlpacaBroker, BrokerPort, SimulatedBroker
from atp_core.clock import Clock, SystemClock, TradingCalendar
from atp_core.config import Settings, get_settings
from atp_core.dashboard.ports import SnapshotStore
from atp_core.domain.enums import RunMode
from atp_core.execution.ports import PortfolioRepository
from atp_core.persistence.dashboard import RedisSnapshotStore
from atp_core.persistence.positions import PostgresPortfolioRepository
from atp_core.risk.killswitch import KillSwitch

#: `Redis` and the SQLAlchemy session types are imported at RUNTIME, not behind
#: `if TYPE_CHECKING`. FastAPI resolves a dependency's annotations when it wires
#: the graph, so a name that exists only for the type checker raises `NameError`
#: on the first request — the same trap `tests/unit/test_api_contract.py` guards
#: the route handlers against, one layer down. The `apps/api/**` TC exemption in
#: pyproject.toml is what stops the linter moving them back.


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


def _from_state(request: Request, name: str, what: str) -> Any:
    """Read a resource `lifespan` was supposed to open.

    Absent means the application was built without its lifespan having run —
    which is normal in a unit test driving the app over ASGI, and a
    misconfiguration anywhere else. Either way the honest answer to a request
    that needs it is 503 rather than an `AttributeError` rendered as a 500: the
    service is not ready, and it is not the caller's fault.
    """
    resource = getattr(request.app.state, name, None)
    if resource is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{what} is not available — the API started without one",
        )
    return resource


async def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """The database session factory, opened by `lifespan`.

    Not a request-scoped `AsyncSession`, which is what a FastAPI skeleton
    usually offers. The repositories in `atp_core.persistence` own their own
    transactional scope — `session_scope` commits on success and rolls back on
    any exception — because they are shared with the worker, which has no
    request to scope anything to. Handing them a session opened elsewhere would
    give one write two owners.
    """
    factory: async_sessionmaker[AsyncSession] = _from_state(
        request, "session_factory", "the database"
    )
    return factory


async def get_redis(request: Request) -> Redis:
    """The shared async Redis client, opened by `lifespan`."""
    client: Redis = _from_state(request, "redis", "Redis")
    return client


async def get_kill_switch(request: Request) -> KillSwitch:
    """The kill switch, over the *synchronous* Redis client.

    Synchronous because `KillSwitchRule.check` is, and the risk chain must not
    be coloured async to reach one key (`persistence.redis_client`). A handler
    calling it therefore blocks the event loop for the length of one Redis
    round trip, which is why the reads on the dashboard path go through
    `asyncio.to_thread`.
    """
    switch: KillSwitch = _from_state(request, "kill_switch", "the kill switch")
    return switch


async def get_snapshot_store(
    redis: Annotated[Redis, Depends(get_redis)],
) -> SnapshotStore:
    """Where the worker publishes the live book (`atp_core.dashboard`)."""
    return RedisSnapshotStore(redis)


async def get_portfolio_repository(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> PortfolioRepository:
    """The book's durable history — read here only for the equity curve.

    The API never *writes* through this. One process owns the live book and
    writes it (`StrategyRunner`), which is what keeps "what do we hold" a
    question with one answer.
    """
    return PostgresPortfolioRepository(session_factory)


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


async def get_current_user() -> str:
    """Resolve the acting user.

    Authentication is deliberately a stub in the skeleton — see docs/SAFETY.md
    'Access control'. Do NOT expose this API on a public interface until it is
    implemented: every endpoint under /risk and /orders can move real money.
    """
    raise NotImplementedError
