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

from atp_api.auth import COOKIE_NAME, Session, read_session_token
from atp_api.ratelimit import AlwaysAllows, RateLimiter, RedisRateLimiter
from atp_core.audit.ports import Action, AuditEntry, AuditSink
from atp_core.backtest.costs import alpaca_equities_default
from atp_core.backtest.ports import BacktestQueue, BacktestRunRepository
from atp_core.brokers import AlpacaBroker, BrokerPort, SimulatedBroker
from atp_core.clock import Clock, SystemClock, TradingCalendar
from atp_core.config import Settings, get_settings
from atp_core.dashboard.ports import SnapshotStore
from atp_core.data.ports import BarRepository
from atp_core.domain.enums import RunMode
from atp_core.errors import MissingBrokerCredentialsError
from atp_core.execution.ports import OrderRepository, PortfolioRepository
from atp_core.logging import get_logger
from atp_core.persistence.audit import PostgresAuditLog
from atp_core.persistence.backtests import PostgresBacktestRunRepository
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.dashboard import RedisSnapshotStore
from atp_core.persistence.orders import PostgresOrderRepository
from atp_core.persistence.positions import PostgresPortfolioRepository
from atp_core.persistence.signals import PostgresSignalRepository
from atp_core.persistence.strategies import PostgresStrategyRepository
from atp_core.risk.killswitch import KillSwitch
from atp_core.strategy.ports import SignalRepository, StrategyRepository

#: `Redis` and the SQLAlchemy session types are imported at RUNTIME, not behind
#: `if TYPE_CHECKING`. FastAPI resolves a dependency's annotations when it wires
#: the graph, so a name that exists only for the type checker raises `NameError`
#: on the first request — the same trap `tests/unit/test_api_contract.py` guards
#: the route handlers against, one layer down. The `apps/api/**` TC exemption in
#: pyproject.toml is what stops the linter moving them back.


log = get_logger(__name__)


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


class _DroppedAuditLog:
    """The sink used when there is no database to write to.

    Not a 503, which is what every other missing resource here answers, and the
    difference is the audit log's own rule: a failed audit write must never fail
    the action (atp_core.audit.ports). A dependency that refused the request
    would make the record's absence more disruptive than the record's presence
    is useful — and the actions being audited include halting trading.

    Reading is the opposite case and is *not* routed through here: the endpoint
    that lists entries depends on the database directly and answers 503 when it
    is gone, because an empty page and "the record is unreachable" are different
    sentences and only one of them is safe to believe.
    """

    async def record(self, entry: AuditEntry) -> None:
        log.critical(
            "audit.no_sink",
            action=entry.action,
            actor=entry.actor,
            effect="the action proceeded; this event is missing from the audit trail",
        )

    async def recent(
        self,
        limit: int = 100,
        before_id: int | None = None,
        action: str | None = None,
    ) -> list[tuple[int, AuditEntry]]:
        return []


async def get_audit_sink(request: Request) -> AuditSink:
    """Where to append audit entries. Never refuses the request."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return _DroppedAuditLog()
    return PostgresAuditLog(factory)


async def get_rate_limiter(request: Request) -> RateLimiter:
    """The limiter for the unauthenticated surface. Never refuses the request.

    Tolerant of a missing Redis for the same reason `get_audit_sink` is tolerant
    of a missing database: refusing here would turn an infrastructure gap into a
    locked door on the one endpoint that has to keep working for anyone to
    diagnose it.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return AlwaysAllows()
    return RedisRateLimiter(redis)


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


async def get_order_repository(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> OrderRepository:
    """Every order that reached a venue — read here for trade reconstruction.

    Read-only from this process, for the same reason as the book above: the
    runner is the only thing that writes an order, which is what keeps "what did
    we submit" a question with one answer.
    """
    return PostgresOrderRepository(session_factory)


async def get_strategy_repository(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> StrategyRepository:
    """The `strategies` table — read here to say which strategies exist.

    Read-only from this process, like the book and the orders above. The runner
    writes a row at every session open (`ensure`), and it is the only thing that
    does: a strategy row edited from here while a worker held a different view
    of it would be two answers to "what is this strategy", which is the problem
    ADR 0007 solves for the book.

    Takes the clock because the adapter stamps rows with it (rule §1.2). Nothing
    this process calls writes, so it goes unused on every read — but a
    constructor that quietly read the wall clock instead would be wrong the
    moment anything here did write.
    """
    return PostgresStrategyRepository(session_factory, clock)


async def get_signal_repository(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> SignalRepository:
    """The `signals` table — every decision a strategy made and what became of it.

    Read-only from this process, like every other repository here. The runner is
    the only writer, at every evaluation.

    This is the table that answers "why is nothing happening": a strategy whose
    every idea the risk chain refused is, from the orders table alone,
    indistinguishable from a strategy that had no ideas, and those two call for
    opposite responses. `/risk/rejections` is the reader.
    """
    return PostgresSignalRepository(session_factory)


async def get_backtest_repository(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> BacktestRunRepository:
    """The `backtest_runs` table.

    The one repository in this file the API both reads and **writes**, and the
    exception is narrow enough to state exactly: it writes `create`, and nothing
    else. A request for a backtest is a fact this process is the authority on —
    somebody asked, at this instant — and every transition after it is written by
    the process that knows it, which is the queue worker.

    That is not the two-writer problem ADR 0007 refuses. Those are disjoint
    columns at disjoint times, not two processes computing the same number. The
    alternative would be this handler waiting for a worker to acknowledge a job,
    which is a synchronous call into a queue whose whole purpose is that nothing
    waits on it.
    """
    return PostgresBacktestRunRepository(session_factory)


async def get_backtest_queue(request: Request) -> BacktestQueue:
    """Where a queued backtest goes, and where its progress is read from.

    Read off `app.state`, not built here, and this is the rule at the top of this
    module rather than a preference: the adapter owns an arq connection pool, and
    a pool built in a dependency has nowhere to be closed. Per-request it would be
    worse than untidy — every `POST /backtests` would open a pool and abandon it,
    leaving the connections for the server to time out.

    `lifespan` constructs it and closes it. Constructing does **not** connect:
    `ArqBacktestQueue` opens its pool on the first `enqueue`, so a Redis that is
    down still lets the API boot and answer `/healthz` — the property the whole
    lifespan is written around — and every request that only reads costs no arq
    connection at all.
    """
    queue: BacktestQueue = _from_state(request, "backtest_queue", "the job queue")
    return queue


async def get_bar_repository(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> BarRepository:
    """Stored bars.

    Two readers now: the analytics layer measures MAE/MFE over a trade's holding
    period, and `POST /backtests` checks that the history a run needs exists
    before queueing it — because the alternative is a job that fails four minutes
    in, from a different process.
    """
    return PostgresBarRepository(session_factory)


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
        try:
            _broker_instance = _build_broker(settings)
        except MissingBrokerCredentialsError as exc:
            # 503 and not a 500: the API is fine and this deployment has no
            # venue configured, which is a fact about configuration rather than
            # a bug. Every other endpoint keeps working — the book, the halts,
            # the audit trail and the login screen do not need a broker — so
            # refusing only the requests that actually reach for one is the
            # same shape as `_from_state` above.
            #
            # `str(exc)` is safe to hand back: the message names the variable
            # and never its value (`AlpacaBroker.__init__`).
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
    return _broker_instance


async def get_current_session(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> Session:
    """Resolve the session from the cookie, or refuse the request.

    One operator, one cookie, one signature check — the whole design is ADR 0008.

    This is the *only* place a request becomes a named actor. Handlers that
    record who did something take `CurrentUser` below rather than accepting an
    `actor` field, because an actor the caller fills in is not an audit trail;
    it is a form with a name box on it.

    Every rejection is the same 401 with the same body. Distinguishing "no
    cookie" from "expired" from "bad signature" tells whoever is probing which
    half of the problem they have solved, and the client's response is identical
    in all three cases: log in again.
    """
    session = read_session_token(request.cookies.get(COOKIE_NAME, ""), settings, clock.now())
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
        )
    return session


#: The verified session — who, and what they may do.
CurrentSession = Annotated[Session, Depends(get_current_session)]


async def get_current_user(session: CurrentSession) -> str:
    """The acting user's name, for handlers that record who did something."""
    return session.user


#: The acting user, for handlers that record who did something.
CurrentUser = Annotated[str, Depends(get_current_user)]


#: HTTP methods that cannot change anything, and so need no scope.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The one mutating route a read-only session may still call.
#:
#: Halting is not an exception grudgingly made; it is the rule the domain asks
#: for. docs/DASHBOARD.md keeps the kill switch always visible and never behind
#: a menu, docs/RISK.md says engaging it needs no confirmation because
#: "hesitation is the expensive part", and ws.py delivers halts to clients that
#: subscribed to nothing because a trading halt is not something to opt into. A
#: read-only session held by someone watching the book from a phone is exactly
#: the case where the ability to stop trading matters most and the ability to
#: place an order matters least.
#:
#: Clearing a halt is deliberately NOT here. The asymmetry is docs/RISK.md's:
#: stopping is reflexive, restarting is a decision — and a decision needs the
#: authority that a read-only session is defined by not having.
READ_ONLY_MAY_CALL = frozenset({"/api/v1/risk/halt"})


async def require_write_scope(
    request: Request,
    session: CurrentSession,
    audit: Annotated[AuditSink, Depends(get_audit_sink)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> None:
    """Refuse a mutating request from a read-only session.

    Decided from the request's method and path in one place rather than route by
    route, for the same reason the session requirement is: a rule applied per
    handler is a rule someone adds a handler without. A new POST is refused to
    read-only sessions by default, and admitting one means saying so above.

    403, not 401. The caller *is* authenticated and the credential is fine —
    re-presenting it would change nothing, and answering 401 would send the
    dashboard to the login screen to solve a problem logging in cannot solve.
    """
    if request.method in SAFE_METHODS:
        return
    if request.url.path in READ_ONLY_MAY_CALL:
        return
    if session.may_act:
        return

    # Recorded, not just refused. A read-only session attempting a write is
    # either the operator forgetting which session they are in — harmless and
    # worth being able to confirm — or a cookie somewhere it should not be,
    # which is the case the record exists for. Both look identical at the moment
    # of refusal, and only the audit trail tells them apart afterwards.
    await audit.record(
        AuditEntry(
            at=clock.now(),
            actor=session.user,
            action=Action.FORBIDDEN,
            target=request.url.path,
            detail={"method": request.method, "scope": session.scope.value},
        )
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="this session is read-only",
    )
