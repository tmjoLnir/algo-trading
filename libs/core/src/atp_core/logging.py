"""Structured logging.

Trading logs are read during incidents, under time pressure, and increasingly by
queries rather than eyes. Structured events beat prose:

    log.info("order.submitted", order_id=..., symbol="AAPL", qty=100)

not `log.info(f"Submitted {qty} {symbol}")`. Never interpolate into the message
(CLAUDE.md §4), and never log a credential or a whole `Settings` object.

Event naming: `<domain>.<past-tense-verb>` — `order.filled`, `risk.rejected`,
`feed.disconnected`, `killswitch.engaged`.

**Correlation ids are this platform's tracing.** `correlation_id` binds an id
into a context variable, and `merge_contextvars` below puts it on every event
emitted underneath — including from code several layers down that knows nothing
about it. One API request, one strategy iteration or one scheduled job is a
unit of work, and after the fact the only question worth asking of a log is
*which lines belong to the same one*. See `docs/OBSERVABILITY.md` and ADR 0013
for why this rather than spans.
"""

from __future__ import annotations

import contextlib
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Keys scrubbed from every event before it is emitted.
REDACT_KEYS = frozenset({"api_key", "api_secret", "secret", "password", "token", "authorization"})

#: The context-variable key an id is bound under, and therefore the field name
#: it appears as in every log line. Named once because queries are written
#: against it: renaming this silently breaks every saved search anyone has.
CORRELATION_ID_KEY: Final = "correlation_id"

#: What an id is allowed to look like when it arrives from outside. Deliberately
#: narrow — see `sanitise_correlation_id`.
_SAFE_ID: Final = re.compile(r"\A[A-Za-z0-9._:-]{1,64}\Z")


def new_correlation_id() -> str:
    """A fresh id. Short, because it is read aloud and typed into greps."""
    return uuid.uuid4().hex[:16]


def sanitise_correlation_id(candidate: str | None) -> str:
    """Return `candidate` if it is safe to log, otherwise a fresh id.

    An inbound id is attacker-controlled and goes verbatim into every log line
    the request produces. Under the JSON renderer that is merely ugly; under the
    console renderer a value containing a newline **writes its own log lines**,
    which is how a log becomes evidence of something that never happened. The
    charset here excludes whitespace and control characters entirely, so no
    accepted value can end a line.

    Length is capped for a duller reason: an id is a correlation key, and a
    kilobyte of it on every line of a busy request is a disk-space attack that
    costs the sender nothing.

    Refused input is replaced rather than rejected. A malformed header is not
    worth failing a request over, and the alternative — logging without an id —
    loses exactly the request somebody may later want to trace.
    """
    if candidate and _SAFE_ID.match(candidate):
        return candidate
    return new_correlation_id()


@contextlib.contextmanager
def correlation_id(value: str | None = None) -> Iterator[str]:
    """Bind an id for the duration of the block, then restore what was there.

    Restoring rather than clearing is what makes this safe to nest: a scheduled
    job that binds its own id inside a request-scoped one must not leave the
    outer scope unlabelled on its way out.

    Context variables follow asyncio tasks, so a coroutine spawned inside the
    block inherits the id and one spawned outside it does not — which is the
    behaviour a unit of work wants, and the reason this is a context manager
    rather than a field threaded through every signature.
    """
    bound = value if value is not None else new_correlation_id()
    tokens = structlog.contextvars.bind_contextvars(**{CORRELATION_ID_KEY: bound})
    try:
        yield bound
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def current_correlation_id() -> str | None:
    """Whatever id is bound right now, if any.

    For the handful of places that must put the id somewhere other than a log
    line — an HTTP response header, so a human who saw a slow request can find
    it again.
    """
    value = structlog.contextvars.get_contextvars().get(CORRELATION_ID_KEY)
    return value if isinstance(value, str) else None


def _redact(logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Last line of defence against a credential reaching a log sink."""
    for key in list(event_dict):
        if key.lower() in REDACT_KEYS:
            event_dict[key] = "***"
    return event_dict


def configure(level: str = "INFO", fmt: str = "console") -> None:
    """Set up structlog. Call once at process start. Use `json` in production
    so logs are queryable."""
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.dev.ConsoleRenderer() if fmt == "console" else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
