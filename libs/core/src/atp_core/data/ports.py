"""Market-data ports — requirement #4.

Historical and real-time are separate interfaces because their failure modes are
different. A historical fetch that fails can be retried; a dropped real-time
stream loses data permanently unless you backfill the gap, and code that treats
the two identically will silently trade on an incomplete picture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from datetime import datetime

    from atp_core.domain import Bar, Quote, Timeframe, Trade


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

    def stream(self) -> AsyncIterator[Bar | Quote | Trade]:
        """Yield events as they arrive.

        Deliberately not `async def`. An `async def` returning `AsyncIterator`
        is a *coroutine* that returns an iterator, so callers would have to
        write `async for e in await feed.stream()` and no async generator could
        ever satisfy it. Declared this way, the natural implementation — an
        `async def` with `yield` in it — conforms, and callers write
        `async for e in feed.stream()`.

        Implementations must reconnect with exponential backoff and, on
        reconnect, backfill the gap via the historical provider before resuming
        (CLAUDE.md §5). Silently resuming leaves a hole in every indicator
        computed across it.
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


class QuoteCache(Protocol):
    """Latest quote per symbol (Redis). Read on every risk check, so it must be
    fast and it must expose age."""

    async def set_quote(self, quote: Quote) -> None: ...

    async def get_quote(self, symbol: str) -> Quote | None: ...

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
