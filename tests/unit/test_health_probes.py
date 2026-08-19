"""`/healthz` and `/readyz`, over ASGI.

These two endpoints exist to be read when something else is broken, so what
they are tested for is mostly what they must *not* do: `/healthz` must not
depend on anything, `/readyz` must not raise, and neither may hand an
unauthenticated caller the contents of an exception.

The case that motivated this file: a dashboard served by nginx showing
`Failed to load dashboard: Error: 502: Bad Gateway`. A 502 is nginx reporting
that it could not complete a request to the API, and it looks the same whether
the API is down, still starting, or up and unable to reach Redis. `/readyz`
answered `500` to every one of those — it raised `NotImplementedError` — so the
one endpoint that could have told them apart instead read as a third fault.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from atp_api.main import create_app
from atp_api.routers import health

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

HEALTHZ = "/healthz"
READYZ = "/readyz"

#: Stands in for the database password in the connection string. If this string
#: ever reaches an HTTP response, the assertion that looks for it fails and says
#: so — CLAUDE.md §1.6.
SECRET_IN_THE_DSN = "sup3r-s3cret-db-password"


class FakeSession:
    """The half of `AsyncSession` a `SELECT 1` touches."""

    def __init__(self, fail_with: Exception | None = None, hang: bool = False) -> None:
        self._fail_with = fail_with
        self._hang = hang
        self.executed: list[str] = []

    async def execute(self, statement: Any) -> object:
        if self._hang:
            await asyncio.sleep(3600)
        if self._fail_with is not None:
            raise self._fail_with
        self.executed.append(str(statement))
        return object()

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSessionFactory:
    """`async_sessionmaker` reduced to the one thing the probe calls it for."""

    def __init__(self, fail_with: Exception | None = None, hang: bool = False) -> None:
        self._fail_with = fail_with
        self._hang = hang
        self.session = FakeSession(fail_with, hang)

    def __call__(self) -> FakeSession:
        return self.session


class FakeRedis:
    def __init__(self, fail_with: Exception | None = None, hang: bool = False) -> None:
        self._fail_with = fail_with
        self._hang = hang
        self.pings = 0

    async def ping(self) -> bool:
        if self._hang:
            await asyncio.sleep(3600)
        if self._fail_with is not None:
            raise self._fail_with
        self.pings += 1
        return True


def an_app(session_factory: object = None, redis: object = None) -> FastAPI:
    """The real application, with whatever `lifespan` would have opened.

    `ASGITransport` runs no lifespan, so `app.state` starts empty — which is
    itself one of the states under test, and is why the probe reads the state
    defensively rather than through the dependencies that raise on a missing
    one.
    """
    app = create_app()
    if session_factory is not None:
        app.state.session_factory = session_factory
    if redis is not None:
        app.state.redis = redis
    return app


@pytest.fixture
async def client_for() -> AsyncIterator[Any]:
    clients: list[httpx.AsyncClient] = []

    async def make(app: FastAPI) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


class TestLiveness:
    async def test_healthz_answers_without_any_dependency(self, client_for: Any) -> None:
        """The liveness probe must not touch the database.

        This is what the Docker HEALTHCHECK runs and what compose's
        `depends_on: service_healthy` gates on. If it consulted Postgres, a slow
        database would mark the container unhealthy and the orchestrator would
        kill an API that was working — and `web-prod` would never start.
        """
        client = await client_for(an_app())  # no state at all: nothing is open
        response = await client.get(HEALTHZ)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_healthz_is_still_ok_while_readyz_is_not(self, client_for: Any) -> None:
        """The two must be able to disagree — that disagreement is the diagnosis.

        `/healthz` 200 with `/readyz` 503 is "the API is fine, something behind
        it is not", which is a different thing to go and fix from either a 502
        (the API is not running) or a 200/200 (look at the browser).
        """
        app = an_app(FakeSessionFactory(fail_with=ConnectionRefusedError("no db")), FakeRedis())
        client = await client_for(app)

        assert (await client.get(HEALTHZ)).status_code == 200
        assert (await client.get(READYZ)).status_code == 503


class TestReadiness:
    async def test_ready_when_both_dependencies_answer(self, client_for: Any) -> None:
        factory, redis = FakeSessionFactory(), FakeRedis()
        client = await client_for(an_app(factory, redis))

        response = await client.get(READYZ)

        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "checks": {"database": "ok", "redis": "ok"},
        }
        # Actually asked, rather than assumed from a status code.
        assert factory.session.executed == ["SELECT 1"]
        assert redis.pings == 1

    async def test_a_dead_redis_is_503_and_names_redis(self, client_for: Any) -> None:
        """The failure the dashboard's own 503 is about, seen from the probe.

        Redis holds the kill-switch state, so an API that cannot read it cannot
        say whether trading is halted — which `/dashboard/live` refuses to guess
        at. Readiness has to agree with that refusal.
        """
        app = an_app(FakeSessionFactory(), FakeRedis(fail_with=ConnectionError("redis is down")))
        client = await client_for(app)

        response = await client.get(READYZ)

        assert response.status_code == 503
        assert response.json() == {
            "status": "not ready",
            "checks": {"database": "ok", "redis": "unreachable"},
        }

    async def test_a_dead_database_is_503_and_names_the_database(self, client_for: Any) -> None:
        app = an_app(
            FakeSessionFactory(fail_with=ConnectionRefusedError("[Errno 111]")), FakeRedis()
        )
        client = await client_for(app)

        response = await client.get(READYZ)

        assert response.status_code == 503
        assert response.json()["checks"] == {"database": "unreachable", "redis": "ok"}

    async def test_nothing_opened_reports_absent_rather_than_raising(self, client_for: Any) -> None:
        """An API built without its lifespan is not ready, and does not 500.

        `absent` rather than `unreachable` because they are different faults: a
        dependency that was never opened is a misconfigured process, not a
        dependency that is down, and an operator chasing a 502 needs to be sent
        to different places by each.
        """
        client = await client_for(an_app())

        response = await client.get(READYZ)

        assert response.status_code == 503
        assert response.json() == {
            "status": "not ready",
            "checks": {"database": "absent", "redis": "absent"},
        }

    async def test_a_hanging_dependency_is_bounded_rather_than_hanging_the_probe(
        self, client_for: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe that waits as long as its dependency is a probe that hangs.

        Which reads, through nginx, as a 504 that names nothing — the exact
        ambiguity this endpoint exists to remove. Bounded here so the answer is
        a 503 that says which dependency stopped answering.
        """
        monkeypatch.setattr(health, "PROBE_TIMEOUT_SECONDS", 0.05)
        client = await client_for(an_app(FakeSessionFactory(), FakeRedis(hang=True)))

        response = await asyncio.wait_for(client.get(READYZ), timeout=5)

        assert response.status_code == 503
        assert response.json()["checks"] == {"database": "ok", "redis": "unreachable"}

    async def test_the_body_never_carries_the_exception_text(self, client_for: Any) -> None:
        """`/readyz` is open without a session (ADR 0008).

        So the response says *which* dependency is down and never *why* — the
        why goes to the log, where an operator with a shell can read it and a
        passer-by cannot. A driver's connection error quotes the DSN it tried,
        and that DSN carries the database password (CLAUDE.md §1.6).
        """
        leaky = ConnectionRefusedError(
            f"could not connect to postgresql://atp:{SECRET_IN_THE_DSN}@db:5432/atp"
        )
        client = await client_for(an_app(FakeSessionFactory(fail_with=leaky), FakeRedis()))

        response = await client.get(READYZ)

        assert response.status_code == 503
        assert SECRET_IN_THE_DSN not in response.text
        assert "postgresql://" not in response.text

    async def test_both_dependencies_are_checked_even_when_the_first_fails(
        self, client_for: Any
    ) -> None:
        """One failure must not hide another.

        Checked concurrently and reported together: an operator who fixes the
        database only to discover Redis was also down has been told half the
        truth, and has spent a restart finding out.
        """
        app = an_app(
            FakeSessionFactory(fail_with=ConnectionRefusedError("no db")),
            FakeRedis(fail_with=ConnectionError("no redis")),
        )
        client = await client_for(app)

        assert (await client.get(READYZ)).json()["checks"] == {
            "database": "unreachable",
            "redis": "unreachable",
        }
