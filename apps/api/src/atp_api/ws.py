"""WebSocket endpoint — live push between the dashboard's 5-minute polls.

Two update paths, on purpose:

  polling  (5 min)  → the authoritative, consistent snapshot (requirement #7)
  websocket (live)  → price ticks, fills and halts as they happen

The WebSocket is an enhancement, never the source of truth. A dropped socket
degrades the dashboard to 5-minute freshness rather than showing stale data
indefinitely — which is why the poll exists even though push is "better".

The API does not connect to the market-data vendor. It subscribes to Redis
pub/sub, which the worker's `StreamIngestor` publishes to. One upstream
connection, many API replicas.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, WebSocket

router = APIRouter()


class ConnectionManager:
    """Tracks connected clients and their symbol subscriptions.

    Fan out per subscription, not to everyone: a dashboard watching 5 symbols
    should not receive the whole universe's tick stream.
    """

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._subscriptions: dict[str, set[str]] = {}

    async def connect(self, client_id: str, ws: WebSocket) -> None:
        raise NotImplementedError

    def disconnect(self, client_id: str) -> None:
        raise NotImplementedError

    async def broadcast(self, channel: str, message: dict[str, Any]) -> None:
        """Send to subscribers. Never let one dead client block the loop —
        drop and clean up on send failure."""
        raise NotImplementedError


manager = ConnectionManager()


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
        ← {"type": "position_update", ...}
        ← {"type": "pong"}

    `halt` messages must reach the client even if it subscribed to nothing —
    a trading halt is not something to opt into.
    """
    raise NotImplementedError
