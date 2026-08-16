"""Market-data ports — requirement #4.

Historical and real-time are separate interfaces because their failure modes are
different. A historical fetch that fails can be retried; a dropped real-time
stream loses data permanently unless you backfill the gap, and code that treats
the two identically will silently trade on an incomplete picture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from datetime import datetime

    from atp_core.domain import Bar, Quote, Timeframe, Trade

    #: Everything `RealtimeDataFeed.stream()` can yield. `FeedReconnected` is in
    #: here on purpose — see its docstring. Declared inside the type-checking
    #: block because the names it unions are only imported there.
    type StreamEvent = Bar | Quote | Trade | FeedReconnected


@dataclass(frozen=True, slots=True)
class FeedReconnected:
    """The stream dropped and came back. Everything in between was lost.

    Carried *in* the event stream rather than handed to a callback, so that a
    consumer's gap backfill provably happens before it sees the first event of
    the new connection: one `async for` body runs to completion before the next
    event is delivered, which an out-of-band notification cannot promise. That
    ordering is the whole requirement (CLAUDE.md §5) — indicators computed
    across an unfilled hole are wrong in a way nothing downstream can detect.
    """

    #: The last instant the data is known good — the last message received
    #: before the drop, or the connection's open time if it never delivered one.
    #: The window to backfill is `[gap_since, reconnected_at)`.
    gap_since: datetime
    reconnected_at: datetime
    #: Connection attempts it took to get back; 1 means it returned first try.
    #: A climbing number across successive reconnects is a feed that is flapping
    #: rather than one that dropped once.
    attempts: int


class HistoricalDataProvider(Protocol):
    """Bulk history, for backtests and warmup."""

    async def get_bars(
        self,
        symbols: list[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        adjusted: bool = True,
    ) -> dict[str, list[Bar]]:
        """Bars per symbol, chronological, no duplicates.

        `adjusted=True` applies split/dividend adjustment — correct for
        backtesting. Trade on raw prices (CLAUDE.md §5).

        Raises `DataGapError` if a requested window is not fully covered.
        Returning a short series silently is the failure mode to avoid: the
        backtest then runs over a hole and reports a return that never existed.
        """
        ...

    async def get_latest_bar(self, symbol: str, timeframe: Timeframe) -> Bar | None: ...


class RealtimeDataFeed(Protocol):
    """Streaming market data for live and paper execution."""

    async def subscribe(
        self, symbols: list[str], *, bars: bool = True, quotes: bool = True, trades: bool = False
    ) -> None: ...

    async def unsubscribe(self, symbols: list[str]) -> None: ...

    def stream(self) -> AsyncIterator[StreamEvent]:
        """Yield events as they arrive.

        Deliberately not `async def`. An `async def` returning `AsyncIterator`
        is a *coroutine* that returns an iterator, so callers would have to
        write `async for e in await feed.stream()` and no async generator could
        ever satisfy it. Declared this way, the natural implementation — an
        `async def` with `yield` in it — conforms, and callers write
        `async for e in feed.stream()`.

        Implementations reconnect with exponential backoff, restore their
        subscriptions, and announce the outage as a `FeedReconnected` event
        before the first event of the new connection. They do *not* backfill it
        themselves: closing a data gap needs the historical provider and the bar
        store, neither of which a feed has, and a feed that quietly resumed
        would leave a hole in every indicator computed across it (CLAUDE.md §5).
        The consumer that owns those — `data.stream.StreamIngestor` — fills the
        gap when it sees the event, which is why the event is in the stream
        rather than on a callback.

        Reconnection is not unbounded. Once attempts are exhausted, or the
        server refuses in a way retrying cannot fix (bad credentials, a
        subscription the plan does not cover, another process already holding
        the one allowed connection), this raises `DataError` rather than
        looping. Somebody has to be told.
        """
        ...

    def on_disconnect(self, callback: Callable[[Exception], None]) -> None:
        """Register a handler. A feed loss should engage the kill switch:
        no data means no basis for a trading decision."""
        ...

    @property
    def is_connected(self) -> bool: ...

    @property
    def last_message_at(self) -> datetime | None:
        """Staleness watchdog. A quiet feed and a dead feed look identical from
        the inside — this is how `StaleDataRule` tells them apart."""
        ...


class BarRepository(Protocol):
    """Persistent bar storage (TimescaleDB hypertable)."""

    async def upsert_bars(self, bars: list[Bar]) -> int:
        """Idempotent by (symbol, timeframe, ts) — backfills overlap constantly
        and must not create duplicates."""
        ...

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]: ...

    async def get_last_n_bars(self, symbol: str, timeframe: Timeframe, n: int) -> list[Bar]: ...

    async def find_gaps(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Missing windows, excluding legitimate market closures.

        Must consult the trading calendar — every weekend and holiday looks like
        a gap otherwise, and an alert that fires every Saturday gets ignored by
        the second week.
        """
        ...

    async def stored_series(self) -> list[tuple[str, Timeframe]]:
        """Every `(symbol, timeframe)` this store holds bars for.

        What the nightly gap check sweeps. Deliberately the stored series rather
        than the strategies' universes: this job keeps the dataset we already
        have complete, and a symbol no bar exists for yet needs an initial
        backfill, not a gap fill.
        """
        ...


class QuoteCache(Protocol):
    """Latest quote per symbol (Redis). Read on every risk check, so it must be
    fast and it must expose age."""

    async def set_quote(self, quote: Quote) -> None: ...

    async def get_quote(self, symbol: str) -> Quote | None: ...

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...


class EventPublisher(Protocol):
    """Fan-out to the processes that are not this one (Redis pub/sub).

    A port rather than a Redis client so the ingestor stays pure and testable
    (CLAUDE.md §1.3): the thing that decides *what* to publish has no business
    holding a socket.

    Publishing is best-effort by contract, and callers are expected to swallow
    its failures. The dashboard degrades to its 5-minute poll when a tick is
    dropped (`apps/api/src/atp_api/ws.py`); market data must not stop being
    stored because a subscriber is down.
    """

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        """Send `message` to everything subscribed to `channel`.

        `message` must be JSON-serialisable with prices already rendered as
        strings — a float here would reintroduce exactly the rounding error
        rule §1.1 exists to prevent, one hop before the dashboard.
        """
        ...
