"""API contract guards.

These are cheap, and each one pins a failure that has already happened once.

Generating the OpenAPI schema is the useful part: it forces FastAPI to resolve
every route handler's annotations at runtime. Importing the app does NOT — a
handler whose `datetime` sits behind `if TYPE_CHECKING` imports perfectly well
and then fails on the first request. Building the schema catches it here rather
than in production.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from atp_api.auth import Scope, Session
from atp_api.deps import READ_ONLY_MAY_CALL, SAFE_METHODS, get_current_session
from atp_api.main import create_app
from atp_api.ws import websocket_endpoint
from atp_core.config import get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from fastapi import FastAPI


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return create_app().openapi()


def test_openapi_schema_generates(spec: dict[str, Any]) -> None:
    """Forces runtime resolution of every handler annotation.

    If this fails with a Pydantic 'not fully defined' error, an import a
    handler's signature depends on has been moved into `if TYPE_CHECKING`.
    FastAPI needs those at runtime; see the `apps/api/**` TC per-file-ignore
    in pyproject.toml.
    """
    assert spec["paths"], "no routes registered"


@pytest.mark.parametrize("probe", ["/healthz", "/readyz"])
def test_probes_are_unversioned(spec: dict[str, Any], probe: str) -> None:
    """Orchestrators hit these directly.

    `infra/docker/api.Dockerfile` HEALTHCHECKs `/healthz`, and compose gates
    `depends_on: service_healthy` on it. Versioning the probe silently breaks
    both — the container never reports healthy and dependents never start.
    """
    assert probe in spec["paths"], (
        f"{probe} must stay unversioned — the Docker HEALTHCHECK targets it directly"
    )


def test_business_routes_are_versioned(spec: dict[str, Any]) -> None:
    """Everything that is not a probe lives under /api/v1.

    `/metrics` joins the probes for the same reason they are here: a scraper's
    target list is configuration on another machine, and a URL that moves when
    the API version bumps stops collecting silently — the first anyone hears of
    it is a gap in a graph nobody was looking at.
    """
    unversioned = {"/healthz", "/readyz", "/metrics", "/"}
    stray = [p for p in spec["paths"] if p not in unversioned and not p.startswith("/api/v1/")]
    assert not stray, f"unversioned business routes: {stray}"


def test_money_fields_serialise_as_strings(spec: dict[str, Any]) -> None:
    """Decimal must not cross the wire as a JSON number.

    JSON numbers are IEEE 754 doubles in every browser. A P&L value that round
    trips through one is no longer exact, which defeats the point of using
    Decimal server-side (CLAUDE.md §1.1).
    """
    schemas = spec.get("components", {}).get("schemas", {})
    position = schemas.get("PositionView")
    if position is None:  # pragma: no cover - router not implemented yet
        pytest.skip("PositionView not in the schema yet")

    for field in ("qty", "avg_entry_price", "unrealized_pnl", "market_value"):
        prop = position["properties"][field]
        assert prop.get("type") != "number", (
            f"PositionView.{field} serialises as a JSON number; it must be a string"
        )


#: Routes that must stay reachable without a session, and why each one does.
#: This is the same allow-list `main.create_app` applies, written again from the
#: outside so the two cannot drift together — a router quietly added to the open
#: set there fails here.
OPEN_WITHOUT_A_SESSION = {
    "/",  # the banner, which carries run mode and nothing else
    "/healthz",  # the Docker HEALTHCHECK and compose's depends_on gate
    "/readyz",
    "/api/v1/auth/login",  # you cannot log in through a door that needs a login
    "/api/v1/auth/logout",  # logging out of an expired session must still work
    # The run mode, for the login screen. A warning about whether real money is
    # at stake, which is the one thing worth knowing before you sign in.
    "/api/v1/auth/context",
}


async def test_every_other_route_requires_a_session() -> None:
    """No route may be reachable unauthenticated except the five above.

    Held from outside, against the generated schema, because the enforcement in
    `create_app` is an allow-list and an allow-list is only as good as noticing
    when something joins it. A new router added to that loop and to nothing else
    comes out protected; a new router added to the *open* tuple fails here.

    This is the assertion `docs/SAFETY.md` "Access control" now rests on, so it
    is deliberately exhaustive rather than a sample.
    """
    app = create_app()
    spec = app.openapi()
    checked = 0

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path, operations in spec["paths"].items():
            if path in OPEN_WITHOUT_A_SESSION:
                continue
            # Path parameters need *a* value; which one is irrelevant, because
            # the session check runs before the handler ever sees it.
            concrete = re.sub(r"\{[^}]+\}", "placeholder", path)
            for method in operations:
                response = await client.request(method.upper(), concrete, json={})
                assert response.status_code == 401, (
                    f"{method.upper()} {path} answered {response.status_code} "
                    f"without a session — it must be 401, or be listed in "
                    f"OPEN_WITHOUT_A_SESSION with a reason"
                )
                checked += 1

    assert checked > 0, "no routes were checked — the schema is empty"


class RefusableSocket:
    """Just enough `WebSocket` to see whether the handshake was refused.

    Modelled on `test_dashboard_ws.FakeSocket` rather than driven through
    `TestClient`, which is what its neighbours do and which keeps
    `fastapi.testclient` — and its deprecation warning — out of the suite.
    """

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.accepted = False
        self.closed_with: tuple[int, str] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


async def test_the_websocket_refuses_an_unauthenticated_handshake() -> None:
    """The socket carries the whole book, so a reader is a disclosure in itself.

    Checked separately from the routes above because this is enforced inside the
    handler rather than by a dependency — the two cannot fail together, and a
    dependency cannot refuse a WebSocket politely anyway.
    """
    socket = RefusableSocket(cookies={})

    await websocket_endpoint(socket)  # type: ignore[arg-type]

    assert socket.closed_with is not None, "an unauthenticated socket was not closed"
    # 1008 is "policy violation": a refusal the dashboard can act on by showing
    # the login screen, rather than a transport error indistinguishable from a
    # dead server, which it would reconnect against forever.
    assert socket.closed_with[0] == 1008
    # Refused *before* accept, so nothing is ever delivered — not even the halt
    # broadcast that every connected client otherwise receives unconditionally.
    assert not socket.accepted, "the socket was accepted before being refused"


async def test_a_valid_session_cookie_gets_past_the_handshake() -> None:
    """The other half: the refusal must be about the cookie, not about everything.

    Without this, a handler that closed every socket unconditionally would pass
    the test above and look like working authentication.
    """
    from atp_api.auth import COOKIE_NAME, create_session_token
    from atp_core.clock import SystemClock
    from atp_core.config import get_settings

    settings = get_settings()
    token = create_session_token("operator", settings, SystemClock().now())
    socket = RefusableSocket(cookies={COOKIE_NAME: token})

    with contextlib.suppress(Exception):
        # Runs past the check and on into the receive loop, which this fake
        # cannot serve; the assertion is only about how far it got.
        await websocket_endpoint(socket)  # type: ignore[arg-type]

    assert socket.accepted, "a valid session was refused at the handshake"
    assert socket.closed_with is None or socket.closed_with[0] != 1008


def _mutating_routes(spec: dict[str, Any]) -> list[tuple[str, str]]:
    """Every (method, path) that is not a safe read."""
    return [
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method.upper() not in SAFE_METHODS
    ]


async def _walk(
    app: FastAPI,
    spec: dict[str, Any],
    methods_and_paths: list[tuple[str, str]],
) -> AsyncIterator[tuple[str, str, int]]:
    """Call each route and yield what it answered.

    `raise_app_exceptions=False` because most handlers here are still
    `NotImplementedError` stubs: a permitted call reaches one and raises, and
    the point of these tests is the gate in front of it, not the hole behind.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        for method, path in methods_and_paths:
            concrete = re.sub(r"\{[^}]+\}", "placeholder", path)
            response = await client.request(method, concrete, json={})
            yield method, path, response.status_code


async def test_a_read_only_session_cannot_reach_a_mutating_route() -> None:
    """Every write is refused to a read-only session, bar the named exception.

    Exhaustive over the generated schema, like the session check above and for
    the same reason: `require_write_scope` decides from the method and the path,
    so a route added later is refused by default — and this is what notices if
    someone adds one to the exception list instead.
    """
    app = create_app()
    app.dependency_overrides[get_current_session] = lambda: Session(user="reader", scope=Scope.READ)
    spec = app.openapi()

    checked = 0
    async for method, path, status in _walk(app, spec, _mutating_routes(spec)):
        if path in OPEN_WITHOUT_A_SESSION:
            continue  # login and logout answer for themselves
        if path in READ_ONLY_MAY_CALL:
            assert status != 403, (
                f"{method} {path} is listed as callable by a read-only session "
                f"and was refused anyway"
            )
        else:
            assert status == 403, (
                f"{method} {path} answered {status} to a read-only session — it must "
                f"be 403, or be listed in deps.READ_ONLY_MAY_CALL with a reason"
            )
        checked += 1

    assert checked > 0, "no mutating routes were checked"


async def test_the_kill_switch_is_the_exception_and_still_works_read_only() -> None:
    """Halting is permitted to a read-only session, deliberately.

    Pinned on its own rather than left implicit in the sweep above, because it
    is the one place the rule bends and the reason is a domain rule rather than
    a convenience: docs/RISK.md — engaging needs no confirmation, hesitation is
    the expensive part. The person watching the book from a phone is exactly who
    most needs to be able to stop it, and least needs to place an order.
    """
    assert frozenset({"/api/v1/risk/halt"}) == READ_ONLY_MAY_CALL, (
        "the read-only exception list changed — every entry needs a domain reason"
    )

    app = create_app()
    app.dependency_overrides[get_current_session] = lambda: Session(user="reader", scope=Scope.READ)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        halt = await client.post("/api/v1/risk/halt", json={"scope": "global"})
        resume = await client.post("/api/v1/risk/resume", json={"scope": "global", "password": "x"})

    assert halt.status_code != 403, "a read-only session must still be able to halt"
    # The asymmetry docs/RISK.md asks for: stopping is reflexive, restarting is
    # a decision — and a decision needs authority this session does not have.
    assert resume.status_code == 403, "a read-only session must not clear a halt"


async def test_a_full_session_is_refused_nowhere_on_scope() -> None:
    """The converse. A gate that refused everything would pass the test above."""
    app = create_app()
    app.dependency_overrides[get_current_session] = lambda: Session(
        user="operator", scope=Scope.FULL
    )
    spec = app.openapi()

    async for method, path, status in _walk(app, spec, _mutating_routes(spec)):
        assert status != 403, (
            f"{method} {path} refused a FULL session with 403 — scope is not the "
            f"reason anything should be refused here"
        )


class TestTheApiBootsWithoutABroker:
    """The API must be constructible in a run mode it has no credentials for.

    This is the regression, and it presented as a networking problem. `Settings`
    refused to validate when `ATP_RUN_MODE` was `paper` or `live` with an empty
    `ALPACA_API_KEY`; `atp_api.main` calls `Settings()` at import to build
    `app`, so the module could not be imported, uvicorn exited 1, and compose's
    `restart: unless-stopped` restarted it forever. Every request was refused at
    the socket, so the dashboard sat on "Cannot reach the API" — and `/healthz`
    and `/readyz`, the two probes whose whole job is to separate "the API is
    down" from "something behind it is down", were down with it.

    `.env.example` ships `ATP_RUN_MODE=backtest`, the one mode exempt from that
    check, which is why CI never saw this and why it reached an operator: the
    documented next step is to move to `paper`.

    Nothing here asserts that trading is possible without a key — it is not, and
    `test_config_guards.py` holds that line at the adapter.
    """

    @staticmethod
    def _paper_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
        """Paper mode, no key — and no ambient key to accidentally supply one.

        Clearing the environment is load-bearing exactly as it is in
        `test_config_guards.py`: this repo's CI exports real Alpaca keys for the
        live-feed checks, and they would satisfy the old guard and pass this
        test without exercising it.
        """
        for name in ("ALPACA_API_KEY", "ALPACA_API_SECRET"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ATP_RUN_MODE", "paper")
        get_settings.cache_clear()

    @pytest.fixture(autouse=True)
    def _restore_settings_cache(self) -> Iterator[None]:
        """The settings singleton is process-wide; leaving a paper-mode one
        cached would hand it to every test that ran after this class."""
        yield
        get_settings.cache_clear()

    def test_it_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._paper_without_credentials(monkeypatch)

        app = create_app()

        assert app.openapi()["info"]["title"].startswith("ATP")

    @pytest.mark.anyio
    async def test_the_login_gate_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The symptom, stated as the assertion that would have caught it.

        A 401 is what puts the sign-in form on screen. Anything else — including
        the connection refused this used to produce — is the dashboard saying it
        cannot reach the API.
        """
        self._paper_without_credentials(monkeypatch)
        app = create_app()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            gate = await client.get("/api/v1/auth/me")
            health = await client.get("/healthz")
            context = await client.get("/api/v1/auth/context")

        assert gate.status_code == 401
        assert health.status_code == 200
        # The run-mode banner the login screen renders before anyone signs in.
        assert context.json() == {"run_mode": "paper"}
