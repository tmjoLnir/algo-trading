"""FastAPI application.

The API is deliberately thin: validate input, call into `atp_core`, serialise
the result. No trading logic lives here. If you find yourself computing a
position size or deciding whether to place an order in a router, it belongs in
core — where it is testable without HTTP and shared with the backtest path.

The API never places an order directly either; it publishes an intent the worker
consumes, so there remains exactly one execution path (rule §1.5).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from atp_api.routers import (
    analytics,
    backtests,
    dashboard,
    health,
    marketdata,
    orders,
    positions,
    risk,
    strategies,
)
from atp_api.ws import router as ws_router
from atp_core.config import get_settings
from atp_core.logging import configure as configure_logging
from atp_core.logging import get_logger

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

    # TODO: init DB engine, Redis pool, kill switch; attach to app.state
    yield
    # TODO: dispose engine, close Redis
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

    for router in (
        health.router,
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
        app.include_router(router, prefix="" if router in unversioned else "/api/v1")

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
