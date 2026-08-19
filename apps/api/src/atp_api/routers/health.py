"""Liveness and readiness probes.

Two questions that look alike and are not. *Liveness* asks whether this process
should be killed and restarted; *readiness* asks whether it can serve a request
right now. Conflating them is how a slow database turns into a restart loop —
the orchestrator kills a process that was fine, and the dependency it was
waiting for never gets the chance to come back.

They are also the two endpoints somebody reaches for when the dashboard is
broken, which is the other reason the split matters. A dashboard served by nginx
answers `502 Bad Gateway` for exactly one reason — nginx could not complete a
request to the API — and it looks identical whether the API is down, still
starting, or up and unable to reach Redis. `/healthz` and `/readyz` are proxied
onto the dashboard's own origin (`infra/docker/web.nginx.conf`) so that those
three can be told apart from a browser, without a shell on the host:

| `/healthz` | `/readyz` | What it means |
|---|---|---|
| 502 | 502 | the API is not running — nginx has nothing to talk to |
| 200 | 503 | the API is up; a dependency it needs is not |
| 200 | 200 | the API and its dependencies are fine — look at the browser |
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = get_logger(__name__)

router = APIRouter(tags=["health"])

#: How long any one dependency gets to answer. A probe that inherits a
#: dependency's own timeout is a probe that can hang for as long as the
#: dependency does, and an unanswered readiness check is read as a hang in the
#: API rather than in the thing behind it. Below nginx's 60s default so the
#: answer arrives as a 503 that names the dependency rather than as a 504 that
#: names nothing.
PROBE_TIMEOUT_SECONDS: Final = 3.0

#: The three states a dependency can be in, as the response spells them.
#: `ABSENT` is not a failure of the dependency — it is this process having been
#: built without one, which is normal under a unit test driving the app over
#: ASGI (no lifespan runs) and a misconfiguration anywhere else.
OK: Final = "ok"
UNREACHABLE: Final = "unreachable"
ABSENT: Final = "absent"


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: is the process up? Must not touch the DB — a slow database
    should not cause the orchestrator to kill a healthy API."""
    return {"status": "ok"}


async def _probe(name: str, check: Callable[[], Awaitable[object]]) -> str:
    """Run one dependency check, bounded, and never raise.

    Every failure is the same answer to the caller — `UNREACHABLE` — because
    this endpoint is open without a session (ADR 0008) and an exception message
    from a connection attempt is not something to hand an unauthenticated
    caller. What went wrong goes to the log, where an operator with a shell can
    read it and a passer-by cannot.
    """
    try:
        await asyncio.wait_for(check(), timeout=PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        log.warning("readyz.dependency_timeout", dependency=name, seconds=PROBE_TIMEOUT_SECONDS)
        return UNREACHABLE
    except Exception as exc:
        # Broad on purpose. This is a probe: a dependency's driver may raise
        # anything at all, and a readiness check that propagates it answers 500
        # — "the API is broken" — for what is actually "the API is fine and
        # Postgres is not". That distinction is the entire point of the
        # endpoint, and it is the one the 500 this handler used to raise
        # destroyed.
        log.warning(
            "readyz.dependency_unreachable",
            dependency=name,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return UNREACHABLE
    return OK


async def _database_state(request: Request) -> str:
    """`SELECT 1` through the pool `lifespan` opened.

    Through the pool rather than a fresh connection: what readiness has to
    answer is whether *this process* can reach the database with what it holds,
    and a probe that opened its own connection would report a healthy database
    behind an exhausted pool.
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return ABSENT

    async def check() -> object:
        async with factory() as session:
            return await session.execute(text("SELECT 1"))

    return await _probe("database", check)


async def _redis_state(request: Request) -> str:
    """`PING` on the shared async client."""
    client = getattr(request.app.state, "redis", None)
    if client is None:
        return ABSENT
    return await _probe("redis", client.ping)


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, object]:
    """Readiness: can this API actually serve a request?

    Fail this to be removed from a load balancer without being restarted.

    **The broker is deliberately not checked**, though an earlier draft of this
    docstring said it would be. Three reasons, and they compound: a probe is
    polled on a schedule and Alpaca rate-limits per key, so a readiness check
    that called it spends the request budget the trading path needs; in
    `backtest` mode there is no broker to reach and "not ready" would be wrong
    rather than informative; and the broker being down is not a reason to take
    this API out of service — the dashboard still has to render the book, the
    halts and the run-mode banner, which is most of what it is for and none of
    what a broker answers. Broker reachability is an alert, not a probe
    (docs/OBSERVABILITY.md).

    Both dependencies are checked concurrently, so a probe costs the slower of
    the two rather than their sum.
    """
    database, redis_state = await asyncio.gather(_database_state(request), _redis_state(request))
    checks = {"database": database, "redis": redis_state}

    ready = all(state == OK for state in checks.values())
    if not ready:
        # 503 with a body, rather than a bare status. The body is what turns
        # "the dashboard is broken" into "Redis is down", and it is readable
        # through the nginx proxy from the same browser that showed the 502.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if ready else "not ready", "checks": checks}
