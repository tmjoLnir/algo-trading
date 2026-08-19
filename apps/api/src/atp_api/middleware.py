"""Cross-cutting request concerns: a correlation id, and the request metrics.

Both live in one middleware because both need the same two moments — before the
handler and after it — and because the id has to be bound *before* anything the
request does can log, including the metric's own failure path.

Ordering with the rest of the stack matters and is set in `main.create_app`:
this must be the outermost middleware, so that a request refused by
authentication or by CORS is still counted and still carries an id. The
requests worth tracing are disproportionately the ones that did not reach a
handler.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from starlette.middleware.base import BaseHTTPMiddleware

from atp_core import metrics
from atp_core.logging import correlation_id, sanitise_correlation_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

#: Read from the request if present, and always written to the response. The
#: de-facto standard spelling; nginx, most load balancers and every HTTP client
#: library already know it, which is the whole value of not inventing one.
REQUEST_ID_HEADER: Final = "X-Request-ID"

#: What a request that matched no route is labelled as. Never the path it asked
#: for: an unmatched path is attacker-chosen, so labelling by it hands anyone
#: with a URL bar the ability to create unbounded time series — which is how
#: monitoring is taken down by accident and, occasionally, on purpose.
UNMATCHED: Final = "<unmatched>"


def route_template(request: Request) -> str:
    """The route's full pattern — `/api/v1/positions/{symbol}` — not its path.

    Starlette puts the matched route in the scope during routing, so this is
    only meaningful *after* the handler has run. Before that, and for anything
    unrouted, there is no template and `UNMATCHED` is the honest answer.

    **The matched route's `path_format` is not the whole answer**, which is the
    kind of thing only running it reveals. FastAPI mounts each included router
    as a sub-router rather than flattening its routes into the app, so the route
    that ends up in the scope is the one *inside* `positions.router` and its
    `path_format` is `/positions/{symbol}` — the `/api/v1` prefix
    `include_router` added is consumed on the way in and is nowhere in the
    scope. Labelling by that alone drops the version from every business route
    and quietly collides `/api/v1/x` with a future `/api/v2/x`.

    So the prefix is recovered from the two things that *are* known: the
    concrete path, and how many trailing segments the matched template accounts
    for. Everything before those is the prefix, whatever it was and however many
    routers deep it came from. This holds while no path parameter spans more
    than one segment — there is no `:path` converter in this API, and a route
    that added one would land back on `path_format` alone rather than on
    something wrong.
    """
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None)
    if not isinstance(path_format, str):
        return UNMATCHED

    path = request.scope.get("path")
    if not isinstance(path, str):
        return path_format

    tail = path_format.split("/")[1:]
    head = path.split("/")[: -len(tail)] if tail else []
    if len(head) + len(tail) != len(path.split("/")):
        # The template does not fit inside the path it matched, which means a
        # multi-segment parameter. Better a label missing its prefix than one
        # assembled from a guess.
        return path_format
    return "/".join([*head, *tail])


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id, time the request, count the outcome."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER)
        # Sanitised rather than trusted: this value is about to appear on every
        # log line the request writes, and a caller-supplied newline under the
        # console renderer writes its own log lines. See
        # `atp_core.logging.sanitise_correlation_id`.
        with correlation_id(sanitise_correlation_id(inbound)) as bound:
            started = time.perf_counter()
            # No try/except. An exception here has already been turned into a
            # 500 by Starlette's own error middleware, which sits above this
            # one; catching it to count it would mean either swallowing it or
            # re-raising into a second handler. What that costs is the count of
            # requests that died *inside* Starlette itself, which is a bug
            # report rather than a metric.
            response = await call_next(request)
            elapsed = time.perf_counter() - started

            metrics.api_request(
                request.method,
                route_template(request),
                response.status_code,
                elapsed,
            )
            # Echoed so that a human looking at a slow or failed request in a
            # browser's network tab can find its log lines without guessing.
            response.headers[REQUEST_ID_HEADER] = bound
            return response
