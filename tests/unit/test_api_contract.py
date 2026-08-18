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

import httpx
import pytest

from atp_api.main import create_app
from atp_api.ws import websocket_endpoint


@pytest.fixture(scope="module")
def spec() -> dict:
    return create_app().openapi()


def test_openapi_schema_generates(spec: dict) -> None:
    """Forces runtime resolution of every handler annotation.

    If this fails with a Pydantic 'not fully defined' error, an import a
    handler's signature depends on has been moved into `if TYPE_CHECKING`.
    FastAPI needs those at runtime; see the `apps/api/**` TC per-file-ignore
    in pyproject.toml.
    """
    assert spec["paths"], "no routes registered"


@pytest.mark.parametrize("probe", ["/healthz", "/readyz"])
def test_probes_are_unversioned(spec: dict, probe: str) -> None:
    """Orchestrators hit these directly.

    `infra/docker/api.Dockerfile` HEALTHCHECKs `/healthz`, and compose gates
    `depends_on: service_healthy` on it. Versioning the probe silently breaks
    both — the container never reports healthy and dependents never start.
    """
    assert probe in spec["paths"], (
        f"{probe} must stay unversioned — the Docker HEALTHCHECK targets it directly"
    )


def test_business_routes_are_versioned(spec: dict) -> None:
    """Everything that is not a probe lives under /api/v1."""
    unversioned = {"/healthz", "/readyz", "/"}
    stray = [p for p in spec["paths"] if p not in unversioned and not p.startswith("/api/v1/")]
    assert not stray, f"unversioned business routes: {stray}"


def test_money_fields_serialise_as_strings(spec: dict) -> None:
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
