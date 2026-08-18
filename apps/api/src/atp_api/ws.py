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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from atp_core.channels import (
    CHANNEL_BARS,
    CHANNEL_HALTS,
    CHANNEL_ORDERS,
    CHANNEL_QUOTES,
    CHANNEL_SIGNALS,
    DASHBOARD_CHANNELS,
)
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
    """
    attempt = 0
    while True:
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(*channels)
            attempt = 0
            log.info("ws.bridge_subscribed", channels=list(channels))
            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                _dispatch(raw, connections)
            # `listen()` on a live subscription blocks forever, so reaching here
            # means the subscription ended under us. Raised rather than looped
            # back to directly, because falling through would re-subscribe with
            # the attempt counter reset and spin at full speed against whatever
            # just dropped it.
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

    Deliberately unauthenticated, like the rest of the API: `deps
    .get_current_user` is still a stub and docs/SAFETY.md says not to expose
    this on a public interface until it is not. What this socket carries is
    read-only — no message a client can send places an order or moves money —
    so the exposure is disclosure rather than control, which is why it is not a
    separate blocker from the one Phase 6 already records.
    """
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
