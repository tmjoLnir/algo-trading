"""Structured logging.

Trading logs are read during incidents, under time pressure, and increasingly by
queries rather than eyes. Structured events beat prose:

    log.info("order.submitted", order_id=..., symbol="AAPL", qty=100)

not `log.info(f"Submitted {qty} {symbol}")`. Never interpolate into the message
(CLAUDE.md §4), and never log a credential or a whole `Settings` object.

Event naming: `<domain>.<past-tense-verb>` — `order.filled`, `risk.rejected`,
`feed.disconnected`, `killswitch.engaged`.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

#: Keys scrubbed from every event before it is emitted.
REDACT_KEYS = frozenset({"api_key", "api_secret", "secret", "password", "token", "authorization"})


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
