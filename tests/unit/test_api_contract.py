"""API contract guards.

These are cheap, and each one pins a failure that has already happened once.

Generating the OpenAPI schema is the useful part: it forces FastAPI to resolve
every route handler's annotations at runtime. Importing the app does NOT — a
handler whose `datetime` sits behind `if TYPE_CHECKING` imports perfectly well
and then fails on the first request. Building the schema catches it here rather
than in production.
"""

from __future__ import annotations

import pytest

from atp_api.main import create_app


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
