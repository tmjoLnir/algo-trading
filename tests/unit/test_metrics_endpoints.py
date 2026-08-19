"""The two scrape endpoints, and the middleware that feeds one of them.

There are two exporters because the platform is two processes, and ADR 0013's
argument is that this is not an accident to be smoothed over: a scrape that
fails is how a dead worker announces itself, and a design where the API served
the worker's numbers out of Redis would have shown a dead worker's last healthy
values indefinitely. So each is tested as its own listener with its own refusal.

The refusals get more attention here than the successes. `/metrics` carries the
watchlist, the order flow and the request mix, and the mistake worth catching is
not "it does not serve" — somebody notices that within a day — but "it serves to
anybody", which nobody notices at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from atp_api.auth import COOKIE_NAME, Scope, create_session_token
from atp_api.deps import get_clock
from atp_api.main import create_app
from atp_api.middleware import UNMATCHED, route_template
from atp_core import metrics
from atp_core.clock import SimulatedClock
from atp_core.config import Settings, get_settings
from atp_core.metrics import get_registry
from atp_core.risk.killswitch import HaltReason, HaltRecord, HaltScope
from tests.fakes import FakeKillSwitch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

NOW = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)
TOKEN = "a-long-random-scrape-token"


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    metrics.reset_for_tests()


def pinned_settings(token: str = TOKEN) -> Settings:
    """Settings that ignore the shell and any local `.env`.

    `METRICS_TOKEN` is exactly the kind of value a developer has exported, and a
    test that read the ambient one would assert against whatever they last set.
    """
    return Settings(ATP_RUN_MODE="backtest", METRICS_TOKEN=token, _env_file=None)


class RaisingKillSwitch(FakeKillSwitch):
    """A kill switch whose Redis is gone. `active_halts` raises, as the real one
    does rather than reporting an empty list it cannot vouch for."""

    def active_halts(self) -> list[Any]:
        raise ConnectionError("redis is down")


@pytest.fixture
def kill_switch() -> FakeKillSwitch:
    return FakeKillSwitch()


@pytest.fixture
def app(kill_switch: FakeKillSwitch) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = pinned_settings
    application.dependency_overrides[get_clock] = lambda: SimulatedClock(NOW)
    # Set directly rather than through a dependency: the endpoint reads
    # `app.state` because a scrape must still answer when the lifespan that
    # would have populated it never ran — which is the state a crashed startup
    # leaves, and the one somebody is scraping to find out about.
    application.state.kill_switch = kill_switch
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


def bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def session_header(scope: Scope = Scope.FULL) -> dict[str, str]:
    """A signed session as a `Cookie` header.

    A header rather than httpx's `cookies=` argument, which it deprecates per
    request — and `filterwarnings = ["error::DeprecationWarning"]` in
    pyproject.toml makes a deprecation a failure, deliberately.
    """
    token = create_session_token("operator", pinned_settings(), NOW, scope)
    return {"Cookie": f"{COOKIE_NAME}={token}"}


class TestWhoMayScrapeTheApi:
    async def test_no_credential_is_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")

        assert response.status_code == 401
        assert "atp_build_info" not in response.text

    async def test_a_wrong_token_is_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics", headers=bearer("not-the-token"))

        assert response.status_code == 401

    async def test_a_token_that_is_a_prefix_of_the_real_one_is_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        """`compare_digest` on unequal lengths, which is the case a naive
        `startswith` or a truncated comparison would wave through."""
        response = await client.get("/metrics", headers=bearer(TOKEN[:-1]))

        assert response.status_code == 401

    async def test_the_scheme_must_be_bearer(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics", headers={"Authorization": f"Basic {TOKEN}"})

        assert response.status_code == 401

    async def test_the_right_token_is_served(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics", headers=bearer())

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "atp_halts_engaged_total" in response.text

    async def test_a_signed_in_operator_is_served_without_a_token(
        self, client: httpx.AsyncClient
    ) -> None:
        """The second caller. A human should not have to go and find a scrape
        token to look at a number from the browser they are already signed in
        to."""
        response = await client.get("/metrics", headers=session_header())

        assert response.status_code == 200
        assert "atp_halts_engaged_total" in response.text

    async def test_a_read_only_session_may_scrape(self, client: httpx.AsyncClient) -> None:
        """Reading a number is a read. ADR 0009's asymmetry is about acts that
        change something, and this changes nothing."""
        response = await client.get("/metrics", headers=session_header(Scope.READ))

        assert response.status_code == 200

    async def test_with_no_token_configured_a_session_still_works(self, app: FastAPI) -> None:
        """An unset token is "no scraper can collect", not "observability off"."""
        app.dependency_overrides[get_settings] = lambda: pinned_settings(token="")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            with_session = await http.get("/metrics", headers=session_header())
            with_empty_bearer = await http.get("/metrics", headers=bearer(""))

        assert with_session.status_code == 200
        # The trap: an unset token compared against an absent header is two
        # empty strings, and `compare_digest("", "")` is True. Nothing may get
        # in on that.
        assert with_empty_bearer.status_code == 401


class TestWhatTheApiScrapeContains:
    async def test_the_halt_state_is_read_at_scrape_time(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """Not a counter kept by `engage`. Several processes write this state,
        so the only correct answer comes from reading it now."""
        kill_switch.halts = [
            HaltRecord(
                scope=HaltScope.SYMBOL,
                reason=HaltReason.DATA_FEED_LOST,
                engaged_at=NOW,
                engaged_by="monitor",
                target="SPY",
            )
        ]

        body = (await client.get("/metrics", headers=bearer())).text

        assert "atp_halt_state_readable 1.0" in body
        assert 'atp_halt_active{reason="data_feed_lost",scope="symbol",target="SPY"} 1.0' in body

    async def test_nothing_halted_is_reported_as_readable_and_empty(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.get("/metrics", headers=bearer())).text

        assert "atp_halt_state_readable 1.0" in body
        assert "atp_halt_active{" not in body

    async def test_an_unreadable_kill_switch_does_not_take_the_scrape_with_it(
        self, app: FastAPI
    ) -> None:
        """Redis being down is when somebody is looking at this page. Failing
        the whole scrape would remove every other number at that moment — and
        the kill switch fails closed, so `readable 0` means orders are being
        refused, which is the thing to alert on."""
        app.state.kill_switch = RaisingKillSwitch()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get("/metrics", headers=bearer())

        assert response.status_code == 200
        assert "atp_halt_state_readable 0.0" in response.text
        assert "atp_build_info" in response.text, "the rest of the scrape was lost"

    async def test_a_missing_kill_switch_is_unreadable_rather_than_healthy(
        self, app: FastAPI
    ) -> None:
        """The state a crashed lifespan leaves. Reporting `readable 1` with no
        halts would say "trading is fine" about a process that never started."""
        del app.state.kill_switch

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get("/metrics", headers=bearer())

        assert response.status_code == 200
        assert "atp_halt_state_readable 0.0" in response.text


class TestTheObservabilityMiddleware:
    async def test_a_request_is_counted_under_its_route_template(
        self, client: httpx.AsyncClient
    ) -> None:
        await client.get("/metrics", headers=bearer())

        assert (
            get_registry().get_sample_value(
                "atp_api_requests_total", {"method": "GET", "route": "/metrics", "status": "200"}
            )
            == 1
        )

    async def test_a_refused_request_is_counted_too(self, client: httpx.AsyncClient) -> None:
        """The requests worth tracing are disproportionately the ones that never
        reached a handler."""
        await client.get("/metrics")

        assert (
            get_registry().get_sample_value(
                "atp_api_requests_total", {"method": "GET", "route": "/metrics", "status": "401"}
            )
            == 1
        )

    async def test_a_versioned_route_keeps_its_version_in_the_label(
        self, client: httpx.AsyncClient
    ) -> None:
        """The regression this exists for, found by running it rather than
        reading it.

        FastAPI mounts each included router as a sub-router, so the route that
        lands in the scope is the one inside `positions.router` and its
        `path_format` is `/positions/{symbol}` — the `/api/v1` prefix
        `include_router` added is nowhere in the scope. Labelling by
        `path_format` alone dropped the version from every business route in the
        platform, and the metric still looked entirely plausible.
        """
        await client.get("/api/v1/positions/AAPL")

        assert (
            get_registry().get_sample_value(
                "atp_api_requests_total",
                {"method": "GET", "route": "/api/v1/positions/{symbol}", "status": "401"},
            )
            == 1
        )

    async def test_a_path_parameter_collapses_into_one_series(
        self, client: httpx.AsyncClient
    ) -> None:
        """The whole reason for using the template. One series per symbol ever
        requested is a watchlist-sized fan-out at best and unbounded at worst."""
        for symbol in ("AAPL", "MSFT", "SPY", "QQQ"):
            await client.get(f"/api/v1/positions/{symbol}")

        assert (
            get_registry().get_sample_value(
                "atp_api_requests_total",
                {"method": "GET", "route": "/api/v1/positions/{symbol}", "status": "401"},
            )
            == 4
        )
        assert "AAPL" not in metrics.render().decode()

    async def test_an_unmatched_path_is_not_labelled_with_the_path(
        self, client: httpx.AsyncClient
    ) -> None:
        """An unrouted path is attacker-chosen. Labelling by it is one time
        series per URL anybody cares to invent, which is how monitoring is taken
        down by a loop in somebody's scanner."""
        for i in range(5):
            await client.get(f"/no-such-route-{i}")

        assert (
            get_registry().get_sample_value(
                "atp_api_requests_total",
                {"method": "GET", "route": UNMATCHED, "status": "404"},
            )
            == 5
        )
        rendered = metrics.render().decode()
        assert "no-such-route" not in rendered

    async def test_the_duration_is_observed(self, client: httpx.AsyncClient) -> None:
        await client.get("/metrics", headers=bearer())

        assert (
            get_registry().get_sample_value(
                "atp_api_request_seconds_count", {"method": "GET", "route": "/metrics"}
            )
            == 1
        )

    async def test_a_correlation_id_is_generated_and_echoed(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/metrics", headers=bearer())

        assert response.headers["X-Request-ID"]

    async def test_a_safe_inbound_id_is_kept(self, client: httpx.AsyncClient) -> None:
        """So that a request traced through nginx keeps one id end to end."""
        response = await client.get(
            "/metrics", headers={**bearer(), "X-Request-ID": "edge-abc-123"}
        )

        assert response.headers["X-Request-ID"] == "edge-abc-123"

    async def test_a_hostile_inbound_id_is_replaced(self, client: httpx.AsyncClient) -> None:
        """A newline here would write its own log lines under the console
        renderer — a caller able to forge log entries about a platform that
        moves money."""
        hostile = "abc\n2026-01-01 [critical] risk.killswitch.engaged"

        response = await client.get("/metrics", headers={**bearer(), "X-Request-ID": hostile})

        echoed = response.headers["X-Request-ID"]
        assert echoed != hostile
        assert "\n" not in echoed

    def test_route_template_reports_unmatched_when_nothing_routed(self) -> None:
        """Directly, because the scope has no route before routing runs and the
        fallback is what stops a path becoming a label."""

        class NoRoute:
            def __init__(self) -> None:
                self.scope: dict[str, Any] = {}

        assert route_template(NoRoute()) == UNMATCHED  # type: ignore[arg-type]


class TestTheWorkerExporter:
    """A second listener, deliberately, so that a dead worker is a failed scrape
    rather than a set of gauges frozen at their last healthy values."""

    def test_no_token_means_no_listener(self) -> None:
        from atp_worker.metrics_server import start_metrics_server

        assert start_metrics_server(pinned_settings(token="")) is None

    def test_it_serves_metrics_to_the_token(self) -> None:
        server, base = _worker_server()
        try:
            metrics.halt_engaged(HaltScope.GLOBAL, HaltReason.MANUAL)
            response = httpx.get(f"{base}/metrics", headers=bearer(), timeout=5)

            assert response.status_code == 200
            assert "atp_halts_engaged_total" in response.text
        finally:
            server.close()

    def test_it_refuses_a_scrape_with_no_token(self) -> None:
        server, base = _worker_server()
        try:
            response = httpx.get(f"{base}/metrics", timeout=5)

            assert response.status_code == 401
            assert "atp_halts_engaged_total" not in response.text
        finally:
            server.close()

    def test_it_refuses_a_wrong_token(self) -> None:
        server, base = _worker_server()
        try:
            assert (
                httpx.get(f"{base}/metrics", headers=bearer("wrong"), timeout=5).status_code == 401
            )
        finally:
            server.close()

    def test_it_serves_nothing_off_the_metrics_path(self) -> None:
        """`make_wsgi_app` answers on every path on its own, which would make
        this listener a mirror of the scrape at any URL somebody probed."""
        server, base = _worker_server()
        try:
            response = httpx.get(f"{base}/anything-else", headers=bearer(), timeout=5)

            assert response.status_code == 404
            assert "atp_halts_engaged_total" not in response.text
        finally:
            server.close()

    def test_closing_it_stops_the_listener(self) -> None:
        """A worker that will not exit is worse than one with no metrics."""
        server, base = _worker_server()
        server.close()

        with pytest.raises(httpx.HTTPError):
            httpx.get(f"{base}/metrics", headers=bearer(), timeout=2)


def _worker_server() -> tuple[Any, str]:
    """A worker exporter on an ephemeral port.

    Port 0 rather than a fixed one: the suite runs in parallel on CI and a
    hard-coded port is a test that fails for a reason that has nothing to do
    with what it is checking.
    """
    from atp_worker.metrics_server import MetricsServer

    server = MetricsServer("127.0.0.1", 0, TOKEN)
    server.start()
    return server, f"http://127.0.0.1:{server.port}"
