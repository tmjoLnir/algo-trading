"""Turning a failed dependency into an honest status code.

FastAPI's default for an exception nobody handled is `500 Internal Server
Error`, and for a bug that is exactly right. For an unreachable database it is a
false confession: the API is fine, Postgres is not, and 500 says the opposite to
the one person trying to work out which of the two to go and restart.

That is not hypothetical. An operator watched every panel on the dashboard
answer 500 — strategies, positions, orders, backtests, the equity curve, all
three analytics endpoints — while `/dashboard/live` and `/risk/status` answered
200 because they read Redis. The platform looked half-broken in a way no single
fault explains, and the actual fault was one line in `docker compose logs db`:
`password authentication failed for user "atp"`.

Registered once in `create_app` rather than caught per router, for the reason
`deps.require_write_scope` gives about scope checks: a rule applied handler by
handler is a rule someone adds a handler without. The classification itself is
not made here — `atp_core.persistence.db.is_unavailable` makes it, at the point
where a session is opened, so the worker gets the same verdict without going
through HTTP to ask for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fastapi import status
from fastapi.responses import JSONResponse

from atp_core.errors import DatabaseUnavailableError
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

log = get_logger(__name__)

#: What the caller is told. Deliberately the same word `/readyz` uses for the
#: same condition, so an operator who reads both does not have to wonder whether
#: `unreachable` and something else are two faults or one.
#:
#: It carries no exception text, and that is a rule rather than terseness: a
#: driver's connection error is free to quote the DSN it failed to connect with,
#: and the DSN carries the password (CLAUDE.md §1.6). The reason goes to the log
#: below, where an operator with a shell can read it and a browser cannot —
#: which is the same split `routers.health._probe` makes.
DATABASE_UNREACHABLE_DETAIL: Final = "the database is unreachable"

#: Seconds. Not a prediction — nothing here knows when Postgres comes back — but
#: the header is how a 503 says "transient, come back" rather than "gone", and a
#: client that honours it stops hammering a database that is already struggling.
RETRY_AFTER_SECONDS: Final = 5


async def database_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer 503 for a database the API could not reach.

    `error` and the exception type go to the log rather than the response, and
    `ERROR` rather than `warning`: unlike a readiness probe failing — which is a
    routine thing to see during a restart — this fired *inside a request*, so
    somebody was refused an answer.
    """
    cause_type = getattr(exc, "cause_type", type(exc).__name__)
    log.error(
        "api.database_unavailable",
        path=request.url.path,
        method=request.method,
        cause=cause_type,
        error=str(exc),
        fix='docs/RUNBOOK.md, "password authentication failed"',
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": DATABASE_UNREACHABLE_DETAIL},
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Wire the handlers above onto the application.

    Starlette resolves a handler by walking the exception's `__mro__`, so this
    one registration also covers any future `DatabaseUnavailableError` subclass.
    It lives in the router's own exception middleware — *inside* the middleware
    stack — which is what lets `ObservabilityMiddleware` see and count a real
    503 rather than an exception on its way out.
    """
    app.add_exception_handler(DatabaseUnavailableError, database_unavailable_handler)
