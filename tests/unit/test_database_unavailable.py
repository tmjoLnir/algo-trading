"""An unreachable database is a 503, and a bad query is still a 500.

The outage this file is about: every panel on the dashboard answering
`500 Internal Server Error` — strategies, positions, orders, backtests, the
equity curve, all three analytics endpoints — while `/dashboard/live` and
`/risk/status` answered 200 because they read Redis. Nothing about that shape
says "one fault"; it says the API is broken in eight places. The actual fault
was one line in `docker compose logs db`:

    FATAL: password authentication failed for user "atp"

The reason it reached the browser as a 500 is worth stating, because it is the
thing these tests hold: asyncpg raises `InvalidPasswordError` while *opening* a
connection, and SQLAlchemy does not wrap a failure at that stage. It is not a
`DBAPIError`, not a `SQLAlchemyError`, not an `OSError` — so nothing in the
stack recognised it and Starlette rendered it as a bug in this repository.

The driver's real exception classes are used throughout rather than stand-ins.
A fake that raises what we *believe* asyncpg raises would have passed against
the code that shipped the outage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg.exceptions as pg
import httpx
import pytest
import sqlalchemy.exc as sa_exc
from sqlalchemy import text

from atp_api.auth import Scope, Session
from atp_api.deps import get_current_session, get_session_factory
from atp_api.errors import DATABASE_UNREACHABLE_DETAIL
from atp_api.main import create_app
from atp_core import metrics
from atp_core.config import Settings, get_settings
from atp_core.errors import DatabaseUnavailableError
from atp_core.metrics import get_registry
from atp_core.persistence.db import is_unavailable, read_scope, session_scope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

#: Stands in for the password in the DSN. A driver's connection error is free to
#: quote the string it failed to connect with, so this is carried through the
#: whole stack and looked for in every response body — if it ever appears in
#: one, the assertion that fails says which endpoint leaked it (CLAUDE.md §1.6).
SECRET_IN_THE_DSN = "sup3r-s3cret-db-password"

#: The exception the outage actually produced, with the DSN in its message the
#: way a connection error is entitled to have.
WRONG_PASSWORD = pg.InvalidPasswordError(
    f'password authentication failed for user "atp" '
    f"(postgresql+asyncpg://atp:{SECRET_IN_THE_DSN}@db:5432/atp)"
)

#: Every route in the logs that answered 500 during the outage. The whole point
#: is that one fault produced one status across all of them, so they are asserted
#: together rather than one test each.
DATABASE_BACKED = [
    "/api/v1/analytics/trades",
    "/api/v1/analytics/performance",
    "/api/v1/analytics/attribution",
    "/api/v1/strategies",
    "/api/v1/positions",
    "/api/v1/orders",
    "/api/v1/backtests",
    "/api/v1/risk/rejections",
    "/api/v1/dashboard/equity-curve",
    "/api/v1/audit",
]


class DeadSession:
    """The half of `AsyncSession` a repository touches, on a dead connection.

    Split by whether the call reaches Postgres. `add` stages an object in memory
    and `rollback` on a connection that was never established is a no-op — a
    fake that raised from those would be testing a database that fails
    differently from the real one.
    """

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.rollbacks = 0

    async def __aenter__(self) -> DeadSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    async def scalars(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    async def flush(self, *args: Any, **kwargs: Any) -> None:
        raise self._error

    async def commit(self) -> None:
        raise self._error

    def add(self, instance: object) -> None:
        return None

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        return None


class DeadSessionFactory:
    """`async_sessionmaker` reduced to the one thing a repository calls it for."""

    def __init__(self, error: BaseException = WRONG_PASSWORD) -> None:
        self.session = DeadSession(error)

    def __call__(self) -> DeadSession:
        return self.session


def pinned_settings() -> Settings:
    return Settings(ATP_RUN_MODE="backtest", _env_file=None)


@pytest.fixture
def app() -> FastAPI:
    """The real application with a database that refuses every login.

    Only `get_session_factory` is overridden, and that is the point rather than
    a shortcut: every repository dependency in `deps.py` is built from it, so
    the real `PostgresOrderRepository`, `PostgresBarRepository` and the rest run
    against it and translate through core's own `session_scope`. Overriding the
    repositories instead would have tested the fakes.

    `app.state` is populated too, because `ASGITransport` runs no lifespan and
    an empty state is a *different* fault — `_from_state` answers "the API
    started without one", which is a 503 that would have passed the assertions
    below without the database being consulted at all. The factory is put there
    as well as behind the dependency because `/readyz` reads the state directly,
    and the queue because `GET /backtests` resolves it before its handler runs.
    """
    application = create_app()
    application.state.session_factory = DeadSessionFactory()
    #: Never called: `list_backtests` reads the run list first and does not
    #: reach the queue. It is here only so the dependency resolves, the way
    #: `lifespan` would have made it.
    application.state.backtest_queue = object()
    application.dependency_overrides[get_settings] = pinned_settings
    application.dependency_overrides[get_session_factory] = lambda: DeadSessionFactory()
    application.dependency_overrides[get_current_session] = lambda: Session(
        "test-operator", Scope.FULL
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # `raise_app_exceptions=False` so an unhandled exception arrives as the 500
    # a browser would see, rather than being re-raised into the test. Without it
    # the "a bug is still a 500" case below cannot be asserted at all.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as http:
        yield http


class TestWhatTheDriverRaisesIsClassified:
    """`is_unavailable` — the one place the judgement is made."""

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(WRONG_PASSWORD, id="28P01 wrong password — the outage"),
            pytest.param(pg.InvalidCatalogNameError("no such db"), id="3D000 no such database"),
            pytest.param(pg.CannotConnectNowError("starting up"), id="57P03 still starting"),
            pytest.param(pg.TooManyConnectionsError("full"), id="53300 out of connections"),
            pytest.param(pg.ConnectionDoesNotExistError("gone"), id="08003 connection dropped"),
            pytest.param(pg.AdminShutdownError("bye"), id="57P01 admin shutdown"),
            pytest.param(ConnectionRefusedError("[Errno 111]"), id="refused — nothing listening"),
            pytest.param(OSError("[Errno -2] Name or service not known"), id="DNS failure"),
            pytest.param(TimeoutError("connect timed out"), id="connect timed out"),
        ],
    )
    def test_the_database_being_out_of_reach(self, error: BaseException) -> None:
        assert is_unavailable(error)

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(pg.UniqueViolationError("dup"), id="23505 — a duplicate strategy name"),
            pytest.param(pg.PostgresSyntaxError("nope"), id="42601 — a bug in a query here"),
            pytest.param(pg.UndefinedColumnError("nope"), id="42703 — a bug in a query here"),
            pytest.param(pg.QueryCanceledError("timed out"), id="57014 — a cancelled query"),
            pytest.param(ValueError("not a database problem at all"), id="not a database error"),
        ],
    )
    def test_a_statement_that_failed_is_not_an_outage(self, error: BaseException) -> None:
        """The half that matters more, because getting it wrong is silent.

        A bug reclassified as an outage stops being a bug report: it answers
        503, the dashboard renders "the database is unreachable", and somebody
        goes and restarts a database that was never the problem. `57014` is why
        SQLSTATE class `57` is enumerated code by code rather than taken whole —
        a query cancelled by `statement_timeout` shares its class with a
        database shutting down and means nothing like it.
        """
        assert not is_unavailable(error)

    def test_a_connection_sqlalchemy_invalidated(self) -> None:
        """SQLAlchemy's own verdict on its own connection, believed as given.

        This is the mid-request case: the pool had a working connection, the
        database went away underneath it, and SQLAlchemy has already decided the
        connection cannot be reused. It arrives wrapped, unlike the connect-time
        failure above, which is why both shapes have to be recognised.
        """
        wrapped = sa_exc.OperationalError("SELECT 1", {}, pg.ConnectionDoesNotExistError("gone"))
        wrapped.connection_invalidated = True

        assert is_unavailable(wrapped)

    def test_an_integrity_error_is_left_alone(self) -> None:
        """`PostgresStrategyRepository.create` reads this one as a 409.

        It catches `IntegrityError` to say "a strategy of that name already
        exists". Classifying it as unavailability would take a duplicate name —
        the author's own mistake, and fixable in the form in front of them — and
        report it as an outage.
        """
        duplicate = sa_exc.IntegrityError("INSERT", {}, pg.UniqueViolationError("dup"))

        assert not is_unavailable(duplicate)
        assert isinstance(duplicate, sa_exc.DBAPIError)  # it did reach the wrapper


class TestTheScopesTranslate:
    """`session_scope` and `read_scope` — where the judgement is applied."""

    async def test_session_scope_raises_the_typed_error(self) -> None:
        factory = DeadSessionFactory()

        with pytest.raises(DatabaseUnavailableError) as caught:
            async with session_scope(factory) as session:  # type: ignore[arg-type]
                await session.execute(text("SELECT 1"))

        assert caught.value.__cause__ is WRONG_PASSWORD
        assert caught.value.cause_type == "InvalidPasswordError"

    async def test_read_scope_raises_the_typed_error(self) -> None:
        """The readers that bypass `session_scope` to skip its commit.

        `PostgresBarRepository` is the one that matters: the analytics endpoints
        reach it for MAE/MFE, so a bar reader left outside this would have gone
        on answering 500 while every endpoint around it answered 503.
        """
        factory = DeadSessionFactory()

        with pytest.raises(DatabaseUnavailableError):
            async with read_scope(factory) as session:  # type: ignore[arg-type]
                await session.execute(text("SELECT 1"))

    async def test_the_message_never_carries_the_dsn(self) -> None:
        """The exception is built from the cause's *type*, not its text."""
        factory = DeadSessionFactory()

        with pytest.raises(DatabaseUnavailableError) as caught:
            async with session_scope(factory) as session:  # type: ignore[arg-type]
                await session.execute(text("SELECT 1"))

        assert SECRET_IN_THE_DSN not in str(caught.value)

    async def test_a_failed_statement_comes_out_unchanged(self) -> None:
        """Anything the caller was going to catch, it can still catch."""
        duplicate = sa_exc.IntegrityError("INSERT", {}, pg.UniqueViolationError("dup"))
        factory = DeadSessionFactory(duplicate)

        with pytest.raises(sa_exc.IntegrityError):
            async with session_scope(factory) as session:  # type: ignore[arg-type]
                await session.flush()

    async def test_the_transaction_is_still_rolled_back(self) -> None:
        """Translating the error must not skip the cleanup it was wrapped in."""
        factory = DeadSessionFactory()

        with pytest.raises(DatabaseUnavailableError):
            async with session_scope(factory) as session:  # type: ignore[arg-type]
                await session.execute(text("SELECT 1"))

        assert factory.session.rollbacks == 1

    async def test_nesting_does_not_wrap_twice(self) -> None:
        """A repository that calls another must not bury the cause deeper."""
        factory = DeadSessionFactory()

        with pytest.raises(DatabaseUnavailableError) as caught:
            async with read_scope(factory) as outer:  # type: ignore[arg-type]
                async with session_scope(factory):  # type: ignore[arg-type]
                    await outer.execute(text("SELECT 1"))

        assert caught.value.__cause__ is WRONG_PASSWORD


class TestTheAPIAnswers503:
    """The outage, end to end, through the real routers and repositories."""

    @pytest.mark.parametrize("route", DATABASE_BACKED)
    async def test_every_database_backed_route(self, client: httpx.AsyncClient, route: str) -> None:
        """503, not 500 — the API is fine and Postgres is not.

        All ten in one parametrised case because the shape is the diagnosis: one
        fault, one status, everywhere. A route that drifts back to 500 while its
        neighbours answer 503 recreates the picture that made this outage take
        an hour to name.
        """
        response = await client.get(route)

        assert response.status_code == 503, f"{route} answered {response.status_code}"
        assert response.json() == {"detail": DATABASE_UNREACHABLE_DETAIL}

    @pytest.mark.parametrize("route", DATABASE_BACKED)
    async def test_no_route_leaks_the_dsn(self, client: httpx.AsyncClient, route: str) -> None:
        """CLAUDE.md §1.6, asserted from outside rather than trusted.

        The driver's message is in the response's reach at every one of these —
        it is the exception the handler is answering — so this is the assertion
        that the reason went to the log instead.
        """
        response = await client.get(route)

        assert SECRET_IN_THE_DSN not in response.text
        assert "password authentication failed" not in response.text

    async def test_the_503_says_it_is_worth_retrying(self, client: httpx.AsyncClient) -> None:
        """`Retry-After` is what separates "not now" from "not here"."""
        response = await client.get("/api/v1/analytics/trades")

        assert response.headers["Retry-After"] == "5"

    async def test_healthz_still_answers_200(self, client: httpx.AsyncClient) -> None:
        """The pair that names the fault.

        `/healthz` 200 beside a business route's 503 is "the API is up, a
        dependency it needs is not" — the sentence the operator needed and the
        one a wall of 500s cannot say. Liveness must not consult the database,
        so a dead one cannot make the orchestrator restart a healthy API.
        """
        assert (await client.get("/healthz")).status_code == 200

    async def test_readyz_agrees_and_names_the_database(self, client: httpx.AsyncClient) -> None:
        """The two answers have to be about the same fault."""
        response = await client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["checks"]["database"] == "unreachable"

    async def test_the_503_is_counted_as_one(self, client: httpx.AsyncClient) -> None:
        """The outage has to be visible in the metrics as an outage.

        Not a detail of where the handler is registered — it is the reason it is
        registered there. Starlette runs an exception handler *inside* the
        middleware stack, so `ObservabilityMiddleware` sees a real 503 response
        and records it. A translation done in middleware instead would leave
        these requests counted as 500s, and the one series an operator would
        query during a database outage would blame the API.
        """
        metrics.reset_for_tests()

        await client.get("/api/v1/analytics/trades")

        assert (
            get_registry().get_sample_value(
                "atp_api_requests_total",
                {"method": "GET", "route": "/api/v1/analytics/trades", "status": "503"},
            )
            == 1
        )

    async def test_a_bug_in_a_query_is_still_a_500(self, app: FastAPI) -> None:
        """The guardrail on the whole change.

        Widening this to "any database exception is a 503" would be a much
        shorter diff and would quietly delete the platform's ability to report
        its own bugs — every one of them arriving as somebody else's outage.
        """
        app.dependency_overrides[get_session_factory] = lambda: DeadSessionFactory(
            sa_exc.ProgrammingError("SELECT nope", {}, pg.UndefinedColumnError("no column"))
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as http:
            response = await http.get("/api/v1/strategies")

        assert response.status_code == 500
