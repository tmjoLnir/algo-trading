"""Rendering the registry as Prometheus text.

A thin wrapper, and worth having anyway for two reasons. It is the one place
that decides what a scrape of this platform contains — the API adds a
scrape-time collector of its own, and a caller assembling that by hand would
have to know the exposition format's concatenation rule. And it keeps
`prometheus_client`'s exposition API behind our own name, so the day this grows
an OpenMetrics variant or a second format, the callers do not change.

The format itself is a promise to every dashboard and alerting rule ever written
against it, so it is version 0.0.4 text and stays that way until somebody
decides otherwise on purpose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from atp_core.metrics.registry import get_registry

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry

#: What to put in the `Content-Type` header. Prometheus is content-type driven
#: and will parse a body served as `text/plain` without the version parameter,
#: but other scrapers are stricter and the header is free to get right.
CONTENT_TYPE: Final = CONTENT_TYPE_LATEST


def render(*extra: CollectorRegistry) -> bytes:
    """The scrape body: this process's metrics, plus any `extra` registries.

    Concatenation is legal in the text format — a scrape is a sequence of
    families and the parser does not care which registry produced each one — but
    only while no metric *name* appears in two of them. A `HELP` line repeated
    for the same name is a parse error at the scraper, which surfaces as the
    whole target going down rather than as one bad metric. So `extra` is for
    collectors that exist nowhere else: the API's authoritative read of the
    kill-switch state, which cannot live in the core registry because it is
    I/O and has to happen at scrape time to be true.
    """
    body = generate_latest(get_registry())
    for registry in extra:
        body += generate_latest(registry)
    return body
