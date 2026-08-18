"""WebSocket plumbing shared by the adapters that hold a socket open.

Two adapters do: `data.providers.alpaca.AlpacaRealtimeFeed` for market data and
`brokers.alpaca.AlpacaBroker` for trade updates. What they share is the boring,
exactly-identical part — how a socket is opened, how it is closed when it is
already going away, and how long to wait before trying again. What they do
*not* share is everything interesting: different hosts, different auth frames,
different message vocabularies, and completely different answers to "what was
lost while we were gone".

So this module is deliberately thin. It is the second application of ADR 0006's
reasoning rather than a new decision: one reconnect ladder with two callers,
because two hand-written ladders drift into two different retry cadences and
nothing tells you which one you are looking at in an incident.

Nothing here knows about Alpaca, orders or bars, and nothing here decides *when*
to reconnect — that stays with the adapter, which is the only thing that knows
what a dropped connection cost it.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Protocol, cast

from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = get_logger(__name__)

#: Defaults for the ladder below. Shared so that two streams reconnecting
#: against the same vendor behave the same way under the same outage.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
MAX_RECONNECT_ATTEMPTS = 8

#: How long an auth/subscribe exchange may take before the connection is
#: written off and retried. Alpaca answers in milliseconds; ten seconds of
#: silence is a half-open socket, which otherwise hangs the caller for as long
#: as the OS keep-alive takes to notice.
HANDSHAKE_TIMEOUT_SECONDS = 10.0

#: Frames a handshake will read before giving up. Bounded so a server that
#: chats without ever completing the handshake cannot spin.
MAX_HANDSHAKE_FRAMES = 20


class WebSocketConnection(Protocol):
    """The slice of a `websockets` client these adapters actually use.

    Narrow on purpose: it is what lets the tests drive a whole reconnect and
    handshake state machine off a scripted fake, with no network anywhere
    (CLAUDE.md §1.7).
    """

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


#: What an adapter injects to open a socket. Named so the adapters' constructors
#: can annotate the seam without restating it.
type Connector = Callable[[str], Awaitable[WebSocketConnection]]


async def connect_websocket(url: str) -> WebSocketConnection:
    """Open the real socket.

    `websockets` is imported here rather than at module scope so that importing
    an adapter for its REST half — which is what a backfill, a backtest or an
    order submission does — does not pull the WebSocket stack in.
    """
    from websockets.asyncio.client import connect

    # Cast rather than a type: ignore — the client's `send`/`recv`/`close` do
    # satisfy the protocol above, and an ignore that stops being needed is an
    # error in itself here (`warn_unused_ignores`).
    return cast("WebSocketConnection", await connect(url))


async def close_quietly(connection: WebSocketConnection) -> None:
    """Close, ignoring the failure. The socket is already going away."""
    try:
        await connection.close()
    except Exception as exc:
        log.debug("ws.close_failed", error=str(exc))


async def sleep_seconds(seconds: float) -> None:
    """The default backoff sleep, wrapped so the injected one has a plain type."""
    await asyncio.sleep(seconds)


def backoff_delay(
    attempt: int,
    *,
    base_seconds: float = BACKOFF_BASE_SECONDS,
    max_seconds: float = BACKOFF_MAX_SECONDS,
    rng: random.Random | None = None,
) -> float:
    """Exponential, capped, jittered. `attempt` is 1-based.

    Jitter is not decoration: every consumer of a vendor that just came back
    reconnects at the same instant otherwise, and the thundering herd is why it
    goes down again. The result is uniform in `[delay/2, delay]` — it only ever
    waits *less* than the ceiling, never more.

    The base is `2.0` rather than `2` because `int ** int` is typed `Any`: a
    negative exponent makes it a float, so typeshed cannot promise an int and
    hands back `Any`, which would then leak out of this function's declared
    `float` under `--strict`.
    """
    generator = rng if rng is not None else random
    delay: float = min(base_seconds * (2.0 ** (attempt - 1)), max_seconds)
    return delay * (0.5 + generator.random() / 2)
