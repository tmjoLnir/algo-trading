"""The worker's scrape endpoint.

**The worker exports its own metrics rather than pushing them anywhere.** That
is the decision in ADR 0013 and this file is where it lands, so it is worth
restating: the interesting numbers in this platform — the feed's pulse, the
order flow, the halts — are produced by this process, and the alternative design
was for the worker to write them into Redis for the API to serve.

The reason it does not is in `main.py`'s own words: *"a worker that half-runs is
more dangerous than one that is plainly down, because monitoring still sees a
live process while positions go unmanaged"*. Values pushed into Redis stay there
after the process that wrote them dies. A dashboard reading them shows the
feed's last healthy tick rate, the last healthy quote age and no errors — a
photograph of a working platform, served for as long as anybody cares to look at
it. A scrape, in contrast, fails when the process is gone, and a failed scrape
is the one signal that cannot be faked by a corpse.

The cost is a listener in a process that had none. It is a WSGI server on a
thread, which is what `prometheus_client` does everywhere, and the registry it
reads is thread-safe by design.
"""

from __future__ import annotations

import hmac
import threading
from typing import TYPE_CHECKING, Any
from wsgiref.simple_server import WSGIRequestHandler, make_server

from prometheus_client import make_wsgi_app

from atp_core.logging import get_logger
from atp_core.metrics import get_registry

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from atp_core.config import Settings

log = get_logger(__name__)

_UNAUTHORISED = [b"unauthorised\n"]
_NOT_FOUND = [b"not found\n"]


class _QuietHandler(WSGIRequestHandler):
    """A request handler that does not narrate.

    `wsgiref` writes an Apache-style line to stderr per request. A scraper calls
    every fifteen seconds forever, and that is a stream of unstructured text
    through the middle of a structured log — the one place an operator reads
    during an incident.
    """

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def _guard(app: Callable[..., Iterable[bytes]], token: str) -> Callable[..., Iterable[bytes]]:
    """Refuse anything without the scrape token, and anything off `/metrics`.

    The same token as the API's endpoint, because they are one scrape config
    with two targets and giving them separate credentials would mean two things
    to rotate for no gain in isolation.

    An unset token refuses everything. The endpoint carries this worker's order
    flow and watchlist, and the failure mode of the opposite default — open when
    unconfigured — is a listener nobody remembers is there. Unlike the API, there
    is no session to fall back on, so an unconfigured worker simply has no
    reachable metrics, and `run` says so at startup rather than leaving it to be
    discovered.
    """

    def guarded(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        # Anything that is not the metrics path is 404 rather than served.
        # `make_wsgi_app` answers on every path, which would make this listener
        # a mirror of the scrape at any URL somebody happened to probe.
        if environ.get("PATH_INFO", "/") not in ("/metrics", "/"):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return _NOT_FOUND

        scheme, _, presented = environ.get("HTTP_AUTHORIZATION", "").partition(" ")
        if not token or scheme.lower() != "bearer" or not hmac.compare_digest(presented, token):
            start_response(
                "401 Unauthorized",
                [("Content-Type", "text/plain"), ("WWW-Authenticate", "Bearer")],
            )
            return _UNAUTHORISED
        return app(environ, start_response)

    return guarded


class MetricsServer:
    """A scrape listener on a background thread. Start it, close it."""

    def __init__(self, host: str, port: int, token: str) -> None:
        self._server = make_server(
            host,
            port,
            _guard(make_wsgi_app(get_registry()), token),
            handler_class=_QuietHandler,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="metrics_server",
            # A daemon thread so a worker that is exiting is never held open by
            # a scraper mid-request. Losing one scrape during shutdown is
            # nothing; a process that will not die is an incident, and this
            # listener is the least important thing in it.
            daemon=True,
        )

    @property
    def port(self) -> int:
        """The port actually bound, which is not the requested one when it was 0."""
        port: int = self._server.server_address[1]
        return port

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        # `shutdown` blocks until `serve_forever` has returned, so it must not be
        # called from the serving thread. It is called from the worker's exit
        # stack, which is the main one.
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def start_metrics_server(settings: Settings) -> MetricsServer | None:
    """Bring up the listener, or explain why there is not one.

    Returns `None` rather than raising on a bind failure. A worker that refused
    to start because its metrics port was taken would be a platform taken down
    by its own monitoring — the exact inversion of failure modes that
    `alerts/ports.py` refuses for notifications and ADR 0010 refuses for the
    audit trail.
    """
    token = settings.metrics_token.get_secret_value()
    if not token:
        log.warning(
            "worker.metrics_disabled",
            msg="METRICS_TOKEN is unset — the worker exports no metrics endpoint",
            fix="set METRICS_TOKEN in .env, or in the SOPS bundle (docs/DEPLOYMENT.md)",
        )
        return None

    try:
        server = MetricsServer(settings.worker_metrics_addr, settings.worker_metrics_port, token)
        server.start()
    except OSError as exc:
        log.error(
            "worker.metrics_unavailable",
            port=settings.worker_metrics_port,
            error=str(exc),
            msg="the worker is running and is not scrapeable",
        )
        return None

    log.info("worker.metrics_serving", port=server.port, path="/metrics")
    return server
