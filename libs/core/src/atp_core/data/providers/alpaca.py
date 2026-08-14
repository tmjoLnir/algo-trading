"""Alpaca market-data provider — historical bars and the real-time stream.

Practical notes:

- Historical bars: GET /v2/stocks/bars, paginated via `next_page_token`. Follow
  every page — silently taking the first is a data gap that looks like data.
- `adjustment=all` gives split- and dividend-adjusted prices for backtests;
  `raw` for anything compared against live prices (CLAUDE.md §5).
- Free tier is the IEX feed: roughly 2-3% of consolidated volume. Good enough to
  build against; expect fills and volumes to differ from SIP in production.
- The free tier also withholds the most recent 15 minutes of SIP data. Do not
  build a strategy whose edge lives inside that window and then discover this.
- Streaming: one connection per key. Subscribe in batches; the server caps
  symbols per message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from datetime import datetime

    from atp_core.config import Settings
    from atp_core.domain import Bar, Quote, Timeframe, Trade


class AlpacaHistoricalProvider:
    """`HistoricalDataProvider` over Alpaca's market-data REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def get_bars(
        self,
        symbols: list[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        adjusted: bool = True,
    ) -> dict[str, list[Bar]]:
        raise NotImplementedError("GET /v2/stocks/bars — follow next_page_token to exhaustion")

    async def get_latest_bar(self, symbol: str, timeframe: Timeframe) -> Bar | None:
        raise NotImplementedError


class AlpacaRealtimeFeed:
    """`RealtimeDataFeed` over Alpaca's market-data WebSocket."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connected = False
        self._last_message_at: datetime | None = None

    async def subscribe(
        self, symbols: list[str], *, bars: bool = True, quotes: bool = True, trades: bool = False
    ) -> None:
        raise NotImplementedError

    async def unsubscribe(self, symbols: list[str]) -> None:
        raise NotImplementedError

    async def stream(self) -> AsyncIterator[Bar | Quote | Trade]:
        raise NotImplementedError
        yield  # pragma: no cover

    def on_disconnect(self, callback: Callable[[Exception], None]) -> None:
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self) -> datetime | None:
        return self._last_message_at
