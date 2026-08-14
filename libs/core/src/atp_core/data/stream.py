"""Real-time ingestion pipeline — requirement #4.

    Alpaca WS ──▶ StreamIngestor ──┬──▶ QuoteCache (Redis)   risk checks read this
                                   ├──▶ BarRepository        durable history
                                   └──▶ Redis pub/sub ──▶ API WebSocket ──▶ dashboard

One process owns the upstream connection. Everything else reads Redis. Alpaca
allows a limited number of concurrent connections per key, and more importantly
a single writer means one place where gap detection and reconnect logic live —
duplicated across consumers, they would drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.data.ports import BarRepository, QuoteCache, RealtimeDataFeed
    from atp_core.domain import Bar, Quote

CHANNEL_QUOTES = "atp:md:quotes"
CHANNEL_BARS = "atp:md:bars"
CHANNEL_ORDERS = "atp:exec:orders"
CHANNEL_SIGNALS = "atp:exec:signals"


@dataclass(slots=True)
class IngestorStats:
    connected_since: datetime | None = None
    messages_received: int = 0
    reconnects: int = 0
    last_message_at: datetime | None = None
    gaps_backfilled: int = 0
    symbols: set[str] = field(default_factory=set)


class StreamIngestor:
    """Owns the market-data connection and fans data out."""

    def __init__(
        self,
        feed: RealtimeDataFeed,
        quote_cache: QuoteCache,
        bar_repo: BarRepository,
        redis_url: str,
    ) -> None:
        self.feed = feed
        self.quote_cache = quote_cache
        self.bar_repo = bar_repo
        self.redis_url = redis_url
        self.stats = IngestorStats()

    async def run(self, symbols: list[str]) -> None:
        """Connect, subscribe, and pump until cancelled.

        Reconnect policy: exponential backoff, 1s → 60s, jittered. After each
        reconnect, backfill from `last_message_at` to now via the historical
        provider BEFORE resuming the stream — indicators computed across an
        unfilled gap are wrong in a way nothing downstream can detect.

        If reconnection fails past the threshold, engage the kill switch. Not
        having data is a reason to stop trading, not to keep going on the last
        price we happened to see.
        """
        raise NotImplementedError

    async def _handle_quote(self, quote: Quote) -> None:
        """Cache and publish. Do not persist every quote — the volume is
        enormous and the value is almost entirely in the latest one."""
        raise NotImplementedError

    async def _handle_bar(self, bar: Bar) -> None:
        """Persist and publish. Bars are the durable record."""
        raise NotImplementedError

    async def _backfill_gap(self, since: datetime) -> int:
        """Fetch and store bars missed while disconnected."""
        raise NotImplementedError


class StalenessMonitor:
    """Watchdog: alert and halt when data stops arriving during market hours.

    Must be calendar-aware. Silence at 02:00 on a Sunday is correct; the same
    silence at 14:30 on a Tuesday means something is broken.
    """

    def __init__(self, max_silence_seconds: int = 60) -> None:
        self.max_silence_seconds = max_silence_seconds

    async def watch(self, ingestor: StreamIngestor) -> None:
        raise NotImplementedError
