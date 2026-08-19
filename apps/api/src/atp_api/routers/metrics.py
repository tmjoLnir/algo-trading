"""The scrape endpoint.

Unversioned, for the same reason `/healthz` is: a scrape URL that moves when the
API version bumps is a scrape config that silently stops collecting, and the
first anyone hears of it is a dashboard with a gap in it.

Guarded, unlike `/healthz`. The body carries the watchlist, the order and
rejection counts, the request mix and the times of day this platform is busy.
None of that is a balance or a P&L — `atp_core.metrics` has no access to either
— but it is a description of somebody's trading operation, and docs/SAFETY.md's
posture towards those is not "it is only metadata".

Two ways in, because there are two callers with nothing in common. A scraper
holds a bearer token and cannot hold a cookie; an operator holds a cookie and
should not have to go and find a token to look at a number. Both end in the
same 401 so that neither tells a prober which half they got right.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from prometheus_client import CollectorRegistry, Gauge

from atp_api.auth import COOKIE_NAME, read_session_token
from atp_api.deps import get_clock
from atp_core import metrics
from atp_core.clock import Clock
from atp_core.config import Settings, get_settings
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from atp_core.risk.killswitch import KillSwitch

log = get_logger(__name__)

router = APIRouter(tags=["metrics"])


def _authorised(request: Request, settings: Settings, clock: Clock) -> bool:
    """A valid scrape token, or a valid session. Either is enough."""
    configured = settings.metrics_token.get_secret_value()
    presented = request.headers.get("Authorization", "")
    scheme, _, token = presented.partition(" ")
    if (
        configured
        and scheme.lower() == "bearer"
        # Constant time. The comparison is against a static secret over an
        # endpoint with no rate limit in front of it, which is the exact shape
        # a timing oracle needs — unlike the login path, where bcrypt dominates
        # and a limiter counts the attempts.
        and hmac.compare_digest(token, configured)
    ):
        return True

    return (
        read_session_token(request.cookies.get(COOKIE_NAME, ""), settings, clock.now()) is not None
    )


def _halt_state(kill_switch: KillSwitch | None) -> CollectorRegistry:
    """The kill switch, read at scrape time, as its own registry.

    Not a counter maintained by `engage` and `clear`. Halt state lives in Redis
    and several processes write it; a copy incremented by whichever process
    happened to make the call would disagree with the platform the moment
    another one did, and a metrics system that disagrees with the platform is
    worse than no metrics system. So this is an authoritative read, and it
    happens here because core may not open a socket (CLAUDE.md §1.3).

    A failed read is reported rather than raised. Redis being down is exactly
    when somebody is looking at this page, and taking the whole scrape with it
    would remove every other number at the same moment. `atp_halt_state_readable`
    is the metric to alert on: the kill switch fails *closed*, so an unreadable
    state means every order is being refused.
    """
    registry = CollectorRegistry()
    readable = Gauge(
        "atp_halt_state_readable",
        "1 if the kill-switch state could be read. 0 means orders are being refused.",
        registry=registry,
    )
    active = Gauge(
        "atp_halt_active",
        "1 per halt currently in effect, labelled by what it stops.",
        ["scope", "reason", "target"],
        registry=registry,
    )
    if kill_switch is None:
        readable.set(0)
        return registry

    try:
        halts = kill_switch.active_halts()
    except Exception as exc:
        readable.set(0)
        log.error("metrics.halt_state_unreadable", error=str(exc))
        return registry

    readable.set(1)
    for halt in halts:
        # `target` is a strategy id or a symbol, both drawn from configuration
        # rather than from a request, and there is at most a handful of halts
        # at once. An empty string for a global halt rather than the label being
        # absent: a series that changes its label set when scope changes cannot
        # be summed across the change.
        active.labels(
            scope=halt.scope.value, reason=halt.reason.value, target=halt.target or ""
        ).set(1)
    return registry


@router.get("/metrics", response_class=PlainTextResponse)
async def scrape(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> Response:
    """Prometheus text exposition for this process, plus the halt state.

    **In the OpenAPI schema on purpose**, despite nothing in the dashboard ever
    calling it. `tests/unit/test_api_contract.py` walks the generated schema and
    asserts every route refuses an unauthenticated caller, and a route hidden
    from that schema is a route the sweep cannot see. Given the choice between
    an unused row in the generated types and an endpoint outside the one check
    that is exhaustive, the row is cheaper — and this endpoint authenticates
    itself rather than through the dependency, which is exactly the arrangement
    worth having checked from outside.
    """
    if not _authorised(request, settings, clock):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            # Names the scheme without hinting whether a token is configured.
            headers={"WWW-Authenticate": "Bearer"},
        )

    kill_switch: KillSwitch | None = getattr(request.app.state, "kill_switch", None)
    body = metrics.render(_halt_state(kill_switch))
    return Response(content=body, media_type=metrics.CONTENT_TYPE)
