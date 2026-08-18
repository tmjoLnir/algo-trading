"""FastAPI application.

The API is deliberately thin: validate input, call into `atp_core`, serialise
the result. No trading logic lives here. If you find yourself computing a
position size or deciding whether to place an order in a router, it belongs in
core — where it is testable without HTTP and shared with the backtest path.

The API never places an order directly either; it publishes an intent the worker
consumes, so there remains exactly one execution path (rule §1.5).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from atp_api.deps import get_current_session, require_write_scope
from atp_api.routers import (
    analytics,
    audit,
    auth,
    backtests,
    dashboard,
    health,
    marketdata,
    orders,
    positions,
    risk,
    strategies,
)
from atp_api.ws import manager as ws_manager
from atp_api.ws import redis_bridge
from atp_api.ws import router as ws_router
from atp_core.config import get_settings
from atp_core.logging import configure as configure_logging
from atp_core.logging import get_logger
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.redis_client import close_redis, create_redis, create_sync_redis
from atp_core.risk.killswitch import RedisKillSwitch

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    The live-mode banner is not decoration: an operator who cannot tell at a
    glance whether a process is trading real money will eventually assume wrong.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if settings.is_live:
        log.critical(
            "startup.live_trading_enabled",
            broker_url=settings.broker_base_url,
            msg="REAL MONEY IS AT RISK",
        )
    else:
        log.info("startup", run_mode=settings.run_mode, broker_url=settings.broker_base_url)

    # Fail closed, and say so here rather than at the login screen. With no hash
    # configured there is no password that works — not "any password works" —
    # so the API is safe but unusable, and the difference between those two is
    # exactly what an operator needs told at startup rather than discovered.
    if not settings.api_password_hash.get_secret_value():
        log.critical(
            "startup.no_credentials",
            msg="API_PASSWORD_HASH is unset — every login will be refused",
            fix="uv run python scripts/hash_password.py, then put the line in .env",
        )

    # Pools belong to the application, not to a dependency. A client built
    # lazily on first use has nowhere to be closed, and an API that exits
    # holding Redis connections leaves them for the server to time out.
    #
    # None of these connect here: `create_async_engine` and `Redis.from_url`
    # both open lazily, so a database or a Redis that is down delays nothing at
    # startup and surfaces on the request that needs it. That is deliberate —
    # the probe endpoints are what an orchestrator gates on, and an API that
    # refused to boot without Redis could not serve `/healthz` to say so.
    engine = create_engine(settings.database_url)
    redis = create_redis(settings.redis_url)
    # A second client, synchronous, for the kill switch alone — the risk chain
    # that consults it is synchronous and must not be coloured async to reach
    # one key (`persistence.redis_client`). Both point at the same server.
    sync_redis = create_sync_redis(settings.redis_url)

    app.state.session_factory = create_session_factory(engine)
    app.state.redis = redis
    app.state.kill_switch = RedisKillSwitch(sync_redis)

    # Live push. Started here rather than on the first socket so that the
    # subscription exists before any client does — a bridge brought up by the
    # first connection would drop everything published while nobody was
    # watching, which includes the halt that happened thirty seconds ago.
    bridge = asyncio.create_task(redis_bridge(redis, ws_manager), name="ws_bridge")
    app.state.ws_bridge = bridge

    try:
        yield
    finally:
        bridge.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bridge
        await close_redis(redis)
        sync_redis.close()
        await engine.dispose()
        log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="ATP — Algorithmic Trading Platform",
        version="0.1.0",
        description="Automated execution, backtesting, risk management and analytics.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health probes and the WebSocket are deliberately UNVERSIONED. Orchestrators
    # hit /healthz directly — including this repo's own Docker HEALTHCHECK and
    # compose `depends_on: service_healthy` gates — and a probe URL that moves
    # when the API version bumps is a probe that fails for no real reason.
    unversioned = (health.router, ws_router)

    # Everything that is not on this list requires a session (ADR 0008). Stated
    # as an allow-list on purpose: a router added to the loop below and to
    # nothing else comes out authenticated, which is the safe direction for the
    # mistake. `tests/unit/test_api_contract.py` holds the same line from the
    # outside, against the generated schema, so neither this list nor that test
    # can drift alone.
    #
    # `health` — orchestrators hit /healthz and /readyz, including this repo's
    #   own Docker HEALTHCHECK and compose `depends_on` gates.
    # `auth`   — you cannot log in through a door that requires being logged in.
    # `ws`     — authenticates inside its handler instead. A dependency raising
    #   HTTPException cannot close a WebSocket handshake politely; it surfaces
    #   as a transport error rather than a refusal the client can act on.
    open_routers = (health.router, auth.router, ws_router)
    # Both, and in this order, for reading rather than for effect:
    # `require_write_scope` resolves the session itself, so it alone would
    # enforce authentication too. Naming the session dependency beside it keeps
    # the list saying what it does — a list holding only a scope check reads
    # like scope is all it checks. FastAPI caches the shared sub-dependency, so
    # the session is resolved once per request either way.
    session_required = [Depends(get_current_session), Depends(require_write_scope)]

    for router in (
        health.router,
        auth.router,
        audit.router,
        dashboard.router,
        strategies.router,
        backtests.router,
        orders.router,
        positions.router,
        marketdata.router,
        analytics.router,
        risk.router,
        ws_router,
    ):
        app.include_router(
            router,
            prefix="" if router in unversioned else "/api/v1",
            dependencies=[] if router in open_routers else session_required,
        )

    return app


app = create_app()


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    settings = get_settings()
    return {
        "name": "ATP",
        "version": "0.1.0",
        # Surfaced so the dashboard can render an unmissable banner in live mode.
        "run_mode": settings.run_mode,
        "docs": "/docs",
    }
