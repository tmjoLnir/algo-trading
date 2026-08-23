"""WebSocket endpoint — live push between the dashboard's 5-minute polls.

Two update paths, on purpose:

  polling  (5 min)  → the authoritative, consistent snapshot (requirement #7)
  websocket (live)  → price ticks, fills and halts as they happen

The WebSocket is an enhancement, never the source of truth. A dropped socket
degrades the dashboard to 5-minute freshness rather than showing stale data
indefinitely — which is why the poll exists even though push is "better". Every
decision in this file follows from that: nothing here retries a delivery,
nothing here queues, and a client the server cannot keep up with is dropped
rather than allowed to slow anything down. The poll will fix it.

The API does not connect to the market-data vendor. It subscribes to Redis
pub/sub, which the worker publishes to — the ingestor for quotes and bars, the
strategy runner for fills and signals, and the kill switch for halts
(`atp_core.channels`). One upstream connection, many API replicas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from atp_api.auth import COOKIE_NAME, read_session_token
from atp_core.channels import (
    CHANNEL_BARS,
    CHANNEL_HALTS,
    CHANNEL_ORDERS,
    CHANNEL_QUOTES,
    CHANNEL_SIGNALS,
    DASHBOARD_CHANNELS,
)
from atp_core.clock import SystemClock
from atp_core.config import get_settings
from atp_core.logging import get_logger
from atp_core.ws import backoff_delay

if TYPE_CHECKING:
    from redis.asyncio import Redis

log = get_logger(__name__)

router = APIRouter()

#: Redis channel → the name a client subscribes to. Two vocabularies because
#: they are two contracts: the Redis names are internal and change with the
#: producers, the client names are a published protocol and change with the
#: dashboard. Mapping them here keeps a rename on either side from silently
#: unsubscribing every browser.
CLIENT_CHANNELS: dict[str, str] = {
    CHANNEL_QUOTES: "quotes",
    CHANNEL_BARS: "bars",
    CHANNEL_ORDERS: "fills",
    CHANNEL_SIGNALS: "signals",
    CHANNEL_HALTS: "halts",
}

#: Every name a client may subscribe to. Derived rather than restated so a new
#: producer channel becomes subscribable the moment it is mapped above.
ALL_CLIENT_CHANNELS = frozenset(CLIENT_CHANNELS.values())

#: Channels a client is subscribed to whether it asked or not. A trading halt is
#: not something to opt into: a dashboard that filtered one out would show a
#: green screen while nothing was trading.
ALWAYS_DELIVERED = frozenset({"halts"})

#: Channels whose messages are filtered by the client's symbol list. Market data
#: only — a dashboard watching five symbols should not receive the universe's
#: tick stream. Execution events are deliberately *not* filtered: a fill on a
#: symbol you did not subscribe to is still your money.
SYMBOL_FILTERED = frozenset({"quotes", "bars"})

#: How long one client gets to accept one message before it is dropped. A
#: browser on a bad connection is a slow reader, and a slow reader with no
#: deadline turns into unbounded buffering on the server — which costs every
#: other client. Dropping is cheap here precisely because the poll recovers it.
SEND_TIMEOUT_SECONDS = 2.0

#: How long the bridge waits for one message before going round the loop again.
#:
#: This exists because of how redis-py reads a subscription. `pubsub.listen()`
#: issues a *blocking* read, and a blocking read carries no deadline of its own —
#: so it falls back to the connection's `socket_timeout`, which
#: `persistence.redis_client` sets to 5s for the good reason that a Redis which
#: has stopped answering must fail a caller rather than hang it. On a subscription
#: that combination is wrong: five seconds with nothing published is the normal
#: state of a quiet channel, not a fault, and the blocking read cannot tell the
#: difference. It raises `TimeoutError("Timeout reading from redis:6379")`, the
#: bridge treats it as a dropped connection, and re-subscribes — every five
#: seconds, forever, against a Redis that is perfectly healthy.
#:
#: Passing an explicit timeout takes the other branch of the same redis-py code:
#: a caller-supplied deadline returns `None` on expiry instead of raising, which
#: is the honest answer to "was anything published?" — no. So the idle case stops
#: being an error without touching `socket_timeout`, which still bounds the
#: subscribe and every other command on the shared client.
#:
#: One second is short enough that a dropped connection is still noticed
#: promptly and long enough that a quiet overnight channel costs one wakeup a
#: second, which is nothing next to the fan-out this process exists to do.
IDLE_POLL_SECONDS = 1.0

#: After this much total silence — no message, no pong — prove the connection is
#: still there before trusting the next quiet second.
#:
#: Polling costs the one thing the blocking read gave away for free: an answer
#: to "is this connection alive?". `socket_timeout` expiring used to force a
#: re-subscribe every five seconds, which was the bug, but it also meant a
#: connection that had silently stopped carrying data was rebuilt within ten
#: seconds of dying. A poll that returns "nothing published" cannot tell a quiet
#: market from a socket into a black hole — a peer that vanished without a FIN,
#: a partition, a NAT table that dropped the mapping — and in that state reads
#: return empty forever while TCP keepalive takes hours to notice.
#:
#: redis-py's own `health_check_interval` does not close this: it sends a PING
#: but never requires the PONG, and swallows the reply before a caller can see
#: it. So the check is made here, where the answer can be waited for. Trading
#: does not depend on it — the worker publishes whether or not the API is
#: listening — but a bridge that is quietly delivering nothing looks exactly
#: like a market with nothing happening, and that is the failure this codebase
#: is least able to notice from the outside (CLAUDE.md §5).
LIVENESS_PING_SECONDS = 20.0

#: Silence past this is a dropped connection, pong or no pong. Comfortably more
#: than a local round trip, so a Redis merely under load is not written off.
LIVENESS_TIMEOUT_SECONDS = 30.0


class ConnectionManager:
    """Tracks connected clients and their subscriptions.

    Fan out per subscription, not to everyone: a dashboard watching 5 symbols
    should not receive the whole universe's tick stream.
    """

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._channels: dict[str, set[str]] = {}
        self._subscriptions: dict[str, set[str]] = {}

    @property
    def client_count(self) -> int:
        return len(self._connections)

    async def connect(self, client_id: str, ws: WebSocket) -> None:
        """Accept the socket and register the client with no subscriptions.

        None rather than a default set: a client that has not said what it wants
        gets halts and nothing else, which is the one thing it must have.
        """
        await ws.accept()
        self._connections[client_id] = ws
        self._channels[client_id] = set()
        self._subscriptions[client_id] = set()
        log.info("ws.connected", client_id=client_id, clients=len(self._connections))

    def disconnect(self, client_id: str) -> None:
        """Forget a client. Safe to call for one that was never registered."""
        self._connections.pop(client_id, None)
        self._channels.pop(client_id, None)
        self._subscriptions.pop(client_id, None)

    def subscribe(self, client_id: str, channels: list[str], symbols: list[str]) -> None:
        """Add channels and symbols to what this client receives.

        Additive rather than replacing. A dashboard subscribes as each panel
        mounts, and a second call that silently dropped the first panel's
        symbols would leave a table that stops updating for no visible reason.

        Symbols are upper-cased because `symbol` is always an uppercase ticker
        here (CLAUDE.md §4) and a client sending `aapl` means the same
        instrument, not a different one.
        """
        if client_id not in self._connections:
            return
        self._channels[client_id].update(c for c in channels if c in ALL_CLIENT_CHANNELS)
        self._subscriptions[client_id].update(s.upper() for s in symbols)

    def unsubscribe(self, client_id: str, symbols: list[str]) -> None:
        if client_id not in self._connections:
            return
        self._subscriptions[client_id].difference_update(s.upper() for s in symbols)

    def _wants(self, client_id: str, channel: str, symbol: str | None) -> bool:
        if channel in ALWAYS_DELIVERED:
            return True
        if channel not in self._channels.get(client_id, ()):
            return False
        if channel not in SYMBOL_FILTERED or symbol is None:
            return True
        # An empty symbol set means "everything on this channel". A dashboard
        # holding no positions still wants the ticks for what it is watching,
        # and treating empty as "nothing" would make the first subscribe
        # deliver silence.
        watched = self._subscriptions.get(client_id, set())
        return not watched or symbol in watched

    async def broadcast(self, channel: str, message: dict[str, Any]) -> None:
        """Send to subscribers. Never let one dead client block the loop.

        Sends run concurrently and each carries its own deadline, so a client
        that has stopped reading delays nobody: it is dropped and cleaned up,
        and its dashboard falls back to the 5-minute poll it never stopped
        making.
        """
        symbol = message.get("symbol")
        targets = [
            (client_id, ws)
            for client_id, ws in self._connections.items()
            if self._wants(client_id, channel, symbol if isinstance(symbol, str) else None)
        ]
        if not targets:
            return

        results = await asyncio.gather(
            *(self._send(ws, message) for _, ws in targets), return_exceptions=True
        )
        for (client_id, _), outcome in zip(targets, results, strict=True):
            if isinstance(outcome, BaseException):
                log.info("ws.dropping_client", client_id=client_id, error=str(outcome))
                self.disconnect(client_id)

    @staticmethod
    async def _send(ws: WebSocket, message: dict[str, Any]) -> None:
        await asyncio.wait_for(ws.send_json(message), timeout=SEND_TIMEOUT_SECONDS)


manager = ConnectionManager()


async def redis_bridge(
    redis: Redis, connections: ConnectionManager, *, channels: tuple[str, ...] = DASHBOARD_CHANNELS
) -> None:
    """Forward everything the worker publishes to the browsers holding sockets.

    Runs for the life of the process, started by `main.lifespan`. Reconnects
    with the same jittered ladder the market-data adapters use
    (`atp_core.ws.backoff_delay`) rather than a bare retry loop: every API
    replica reconnects at the same instant when Redis comes back otherwise,
    which is how a recovering server goes down again.

    It never gives up, and that is the difference between this and a feed
    adapter. A market-data stream that cannot reconnect must raise, because
    trading on missing data is worse than stopping. Nothing here is traded on:
    losing the bridge costs the dashboard its live updates and the poll still
    works, so retrying forever is strictly better than a live API with a dead
    socket handler.

    A message that is not JSON, or is not an object, is dropped with a log
    rather than taking the bridge down. The only thing that publishes here is
    this platform, so one is a bug worth seeing — and worth seeing *without* an
    outage attached.

    Reads poll with an explicit deadline rather than blocking, so that a channel
    with nothing on it is not mistaken for a broken connection. That distinction
    is the whole of `IDLE_POLL_SECONDS`, and getting it wrong is not a cosmetic
    bug: every re-subscribe is a window in which the platform is publishing to
    nobody, and pub/sub has no replay to make it up afterwards.

    Silence is therefore not evidence of anything, which is why the connection
    is asked directly once it has gone on long enough
    (`LIVENESS_PING_SECONDS`). The two failures are opposites and both are
    silent from the outside: treating a quiet market as a dead socket drops
    messages nobody knows were sent, and treating a dead socket as a quiet
    market delivers nothing while looking calm.
    """
    attempt = 0
    while True:
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(*channels)
            attempt = 0
            log.info("ws.bridge_subscribed", channels=list(channels))
            # Loop time rather than `Clock.now()`: this measures how long a
            # socket has been silent, not when anything happened. It must be
            # monotonic — a wall clock stepping backwards over an NTP correction
            # would suppress the liveness check exactly when it is due — and it
            # is never read by the domain, so §1.2 does not reach it.
            clock = asyncio.get_running_loop().time
            last_seen = clock()
            awaiting_pong = False
            while pubsub.subscribed:
                raw = await pubsub.get_message(timeout=IDLE_POLL_SECONDS)
                if raw is not None:
                    # Anything at all — a published message or the pong from the
                    # check below — proves the connection is still carrying data.
                    last_seen = clock()
                    awaiting_pong = False
                    if raw.get("type") == "message":
                        _dispatch(raw, connections)
                    continue
                # Nothing published inside the window. That is a quiet market,
                # not a fault — unless it has been quiet for so long that a dead
                # connection would look identical (`LIVENESS_PING_SECONDS`).
                silent_for = clock() - last_seen
                if silent_for >= LIVENESS_TIMEOUT_SECONDS:
                    raise ConnectionError(
                        f"no response from Redis for {silent_for:.0f}s on a live subscription"
                    )
                if silent_for >= LIVENESS_PING_SECONDS and not awaiting_pong:
                    # Sent once per quiet stretch, not once per poll: the answer
                    # resets `last_seen`, and until it arrives another ping adds
                    # nothing but traffic.
                    awaiting_pong = True
                    await pubsub.ping()
            # The loop above ends only when the subscription is gone from under
            # us. Raised rather than looped back to directly, because falling
            # through would re-subscribe with the attempt counter reset and spin
            # at full speed against whatever just dropped it.
            raise ConnectionError("the Redis subscription ended")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt += 1
            delay = backoff_delay(attempt)
            log.warning(
                "ws.bridge_failed", error=str(exc), attempt=attempt, retry_in=round(delay, 2)
            )
            await asyncio.sleep(delay)
        finally:
            # `aclose` is untyped in redis-py 5.x, so mypy refuses the call in a
            # typed context. Narrowed here rather than with a blanket ignore,
            # which would also hide a genuine error at this line.
            close: Any = pubsub.aclose
            with contextlib.suppress(Exception):
                await close()


def _dispatch(raw: dict[str, Any], connections: ConnectionManager) -> asyncio.Task[None] | None:
    """Turn one Redis message into one fan-out, or drop it loudly.

    Returns the fan-out task, or None for a message that was dropped. The
    bridge ignores the return — it is deliberately fire-and-forget — but a test
    needs something to await, and the alternative is sleeping and hoping, which
    is how a suite acquires a flaky test that only fails on a loaded CI runner.
    """
    source = raw.get("channel")
    client_channel = CLIENT_CHANNELS.get(source) if isinstance(source, str) else None
    if client_channel is None:
        log.warning("ws.bridge_unknown_channel", channel=source)
        return None
    try:
        payload = json.loads(raw.get("data") or "")
    except (TypeError, ValueError) as exc:
        log.warning("ws.bridge_undecodable", channel=source, error=str(exc))
        return None
    if not isinstance(payload, dict):
        log.warning("ws.bridge_not_an_object", channel=source, kind=type(payload).__name__)
        return None
    # Fire-and-forget: `listen()` must keep draining while the fan-out runs, or
    # one slow client's deadline would hold up every message behind it.
    task = asyncio.create_task(connections.broadcast(client_channel, payload))
    _BRIDGE_TASKS.add(task)
    task.add_done_callback(_BRIDGE_TASKS.discard)
    return task


#: Strong references to in-flight fan-outs. `asyncio` keeps only a weak one, so
#: a task nobody holds can be garbage-collected mid-send — which shows up as a
#: dashboard that misses updates under load and nothing in the log.
_BRIDGE_TASKS: set[asyncio.Task[None]] = set()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Client protocol:

        → {"type": "subscribe",   "channels": ["quotes"], "symbols": ["AAPL"]}
        → {"type": "unsubscribe", "symbols": ["AAPL"]}
        → {"type": "ping"}

        ← {"type": "quote",     "symbol": "AAPL", "bid": ..., "ask": ..., "ts": ...}
        ← {"type": "fill",      "order_id": ..., "symbol": ..., "qty": ..., "price": ...}
        ← {"type": "signal",    "strategy": ..., "symbol": ..., "action": ..., "reason": ...}
        ← {"type": "halt",      "scope": ..., "reason": ...}
        ← {"type": "pong"}

    `halt` messages reach the client even if it subscribed to nothing — a
    trading halt is not something to opt into.

    **Authenticated, in here rather than by a dependency.** Everything this
    socket carries is the book — positions, fills, signals — so a reader is a
    disclosure whether or not they can send anything. The check is inline
    because a dependency raising `HTTPException` cannot refuse a WebSocket
    politely: it surfaces to the browser as a transport error indistinguishable
    from a dead server, and the client reconnects forever against it. Closing
    with 1008 gives the dashboard something it can act on, which is to stop
    retrying and show the login screen (ADR 0008).

    The cookie arrives on the handshake by itself. That is the whole reason the
    session is a cookie and not a bearer token: a browser cannot set headers on
    a WebSocket, so bearer would have meant the token in a query string, and
    from there into nginx's access log on every reconnect.
    """
    settings = get_settings()
    if read_session_token(ws.cookies.get(COOKIE_NAME, ""), settings, SystemClock().now()) is None:
        # Refused before `accept()`, so nothing is ever delivered to an
        # unauthenticated socket — not even the halt broadcast that every other
        # client gets unconditionally.
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="not authenticated")
        log.info("ws.rejected_unauthenticated")
        return

    client_id = uuid.uuid4().hex
    await manager.connect(client_id, ws)
    try:
        while True:
            await _handle(client_id, ws, await ws.receive_text())
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover - transport-level failure
        log.info("ws.client_error", client_id=client_id, error=str(exc))
    finally:
        manager.disconnect(client_id)
        log.info("ws.disconnected", client_id=client_id, clients=manager.client_count)


async def _handle(client_id: str, ws: WebSocket, raw: str) -> None:
    """One client frame.

    An unparseable or unknown frame is answered with an error and the socket
    stays open. Closing on a bad message would let one buggy client version
    disconnect itself in a loop, and the reconnect ladder in the browser turns
    that into a reconnect storm against an API that is working perfectly.
    """
    try:
        message = json.loads(raw)
    except ValueError:
        await ws.send_json({"type": "error", "detail": "expected a JSON object"})
        return
    if not isinstance(message, dict):
        await ws.send_json({"type": "error", "detail": "expected a JSON object"})
        return

    kind = message.get("type")
    if kind == "ping":
        await ws.send_json({"type": "pong"})
        return
    if kind == "subscribe":
        manager.subscribe(
            client_id,
            _string_list(message.get("channels")),
            _string_list(message.get("symbols")),
        )
        await ws.send_json({"type": "subscribed"})
        return
    if kind == "unsubscribe":
        manager.unsubscribe(client_id, _string_list(message.get("symbols")))
        await ws.send_json({"type": "unsubscribed"})
        return
    await ws.send_json({"type": "error", "detail": f"unknown message type {kind!r}"})


def _string_list(value: object) -> list[str]:
    """Whatever the client sent, as a list of strings.

    Non-strings are dropped rather than coerced. `str(None)` is `"None"`, which
    would enter a symbol set and quietly match nothing forever.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
