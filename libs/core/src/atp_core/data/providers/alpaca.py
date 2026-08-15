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

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from atp_core.domain import Bar, Timeframe
from atp_core.errors import DataError, DataGapError
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from atp_core.config import Settings
    from atp_core.data.ports import HistoricalDataProvider, RealtimeDataFeed
    from atp_core.domain import Quote, Trade

log = get_logger(__name__)

#: Our vocabulary → Alpaca's. Alpaca accepts `<n><Unit>`; these are the exact
#: spellings it recognises, and an unrecognised one is a 400, not a fallback.
_TIMEFRAME_PARAM: dict[Timeframe, str] = {
    Timeframe.M1: "1Min",
    Timeframe.M5: "5Min",
    Timeframe.M15: "15Min",
    Timeframe.M30: "30Min",
    Timeframe.H1: "1Hour",
    Timeframe.H4: "4Hour",
    Timeframe.D1: "1Day",
}

#: Alpaca's per-page ceiling. Asking for the maximum minimises both round trips
#: and rate-limit consumption on a multi-year backfill.
_MAX_PAGE_LIMIT = 10_000

#: Pages are followed to exhaustion, so a bad token or a server that keeps
#: handing back the same cursor must not loop forever. A 5-year minute backfill
#: for one symbol is roughly 50 pages; this is far above any legitimate need.
_MAX_PAGES = 10_000

_MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 1.0
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _as_decimal(value: object) -> Decimal:
    """Money and quantities are `Decimal`, never `float` (CLAUDE.md §1.1).

    Responses are parsed with `parse_float=Decimal`, so numbers arrive already
    exact. This exists for the rest: ints, and the strings Alpaca occasionally
    uses. Never `Decimal(float)` — that inherits the binary rounding error the
    rule exists to avoid.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; nothing sane sends one
        raise TypeError(f"expected a number, got {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"cannot convert {type(value).__name__} {value!r} to Decimal exactly")


def _parse_ts(raw: str) -> datetime:
    """RFC-3339 → tz-aware UTC (CLAUDE.md §1.2)."""
    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise DataError(f"Alpaca returned a naive timestamp: {raw!r}")
    return ts.astimezone(UTC)


def _require_utc(ts: datetime, field: str) -> None:
    if ts.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware (rule §1.2), got naive {ts!r}")


class AlpacaHistoricalProvider:
    """`HistoricalDataProvider` over Alpaca's market-data REST API."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        backoff_base_seconds: float = _BACKOFF_BASE_SECONDS,
        min_request_interval_seconds: float = 0.0,
    ) -> None:
        self._settings = settings
        self._client = client
        #: Only close what we opened — an injected client belongs to its owner.
        self._owns_client = client is None
        self._backoff_base_seconds = backoff_base_seconds
        #: Proactive pacing, off by default. Retrying a 429 is correct but
        #: expensive: the request is spent, and `Retry-After` is typically
        #: longer than the gap that would have avoided it. A backfill sets this
        #: from the vendor's published ceiling and mostly never sees a 429.
        self._min_request_interval = min_request_interval_seconds
        self._next_request_at = 0.0

    # ── plumbing ────────────────────────────────────────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        """Credentials go in headers, never the query string: URLs end up in
        access logs, exception messages and traces (CLAUDE.md §1.6)."""
        return {
            "APCA-API-KEY-ID": self._settings.alpaca_api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": self._settings.alpaca_api_secret.get_secret_value(),
            "Accept": "application/json",
        }

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> AlpacaHistoricalProvider:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _await_rate_limit(self) -> None:
        """Space requests out by at least `min_request_interval_seconds`.

        Serial by construction — this class issues one request at a time — so a
        single next-allowed timestamp is enough and there is no bucket to share.
        """
        if self._min_request_interval <= 0:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if now < self._next_request_at:
            await asyncio.sleep(self._next_request_at - now)
        self._next_request_at = loop.time() + self._min_request_interval

    async def _sleep_before_retry(self, attempt: int, response: httpx.Response | None) -> None:
        """Honour `Retry-After` when the server sets it, else exponential backoff.

        The free tier allows 200 requests a minute and a backfill will find that
        ceiling, so 429 is an expected part of normal operation here rather than
        an error worth surfacing.
        """
        delay = self._backoff_base_seconds * (2**attempt)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                # A malformed header is not worth failing the request over —
                # fall back to the computed backoff.
                with contextlib.suppress(ValueError):
                    delay = max(delay, float(retry_after))
        await asyncio.sleep(delay)

    async def _request_page(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """One GET, with bounded retries on the transient failures.

        A 4xx other than 429 is our bug — a bad symbol, an unparseable
        timeframe — and retrying it just multiplies the same mistake.
        """
        url = f"{self._settings.alpaca_data_url.rstrip('/')}{path}"
        client = self._get_client()
        last_error: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            response: httpx.Response | None = None
            try:
                await self._await_rate_limit()
                response = await client.get(url, params=params, headers=self._auth_headers())
            except httpx.HTTPError as exc:  # timeouts, connection resets, DNS
                last_error = exc
                log.warning("data.alpaca.request_failed", attempt=attempt + 1, error=str(exc))
            else:
                if response.status_code == httpx.codes.OK:
                    # parse_float=Decimal is the whole reason this is not
                    # response.json(): stdlib json would hand back floats and
                    # every price would arrive pre-corrupted (CLAUDE.md §1.1).
                    parsed: dict[str, Any] = json.loads(response.text, parse_float=Decimal)
                    return parsed
                if response.status_code not in _RETRY_STATUSES:
                    # Body, not headers — the request headers hold the API key.
                    raise DataError(
                        f"Alpaca {path} returned {response.status_code}: {response.text[:400]}"
                    )
                last_error = DataError(f"Alpaca {path} returned {response.status_code}")
                log.warning(
                    "data.alpaca.retrying",
                    attempt=attempt + 1,
                    status=response.status_code,
                )

            if attempt < _MAX_ATTEMPTS - 1:
                await self._sleep_before_retry(attempt, response)

        raise DataError(f"Alpaca {path} failed after {_MAX_ATTEMPTS} attempts") from last_error

    async def _fetch_all_pages(
        self, path: str, params: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        """Follow `next_page_token` to exhaustion, merging bars per symbol.

        This loop is the point of the whole class. Stopping at the first page
        returns a plausible-looking series that is missing its tail, and nothing
        downstream can tell that from a symbol that genuinely stopped trading.
        """
        merged: dict[str, list[dict[str, Any]]] = {}
        page_token: str | None = None
        pages = 0

        while True:
            page_params = dict(params)
            if page_token:
                page_params["page_token"] = page_token

            payload = await self._request_page(path, page_params)
            pages += 1

            # `bars` is null, not {}, on a page with no data for the window.
            for symbol, raw_bars in (payload.get("bars") or {}).items():
                merged.setdefault(symbol, []).extend(raw_bars or [])

            page_token = payload.get("next_page_token")
            if not page_token:
                break
            if pages >= _MAX_PAGES:
                raise DataError(
                    f"Alpaca {path} still paginating after {pages} pages — refusing to "
                    "loop further; the cursor is probably not advancing"
                )

        log.info("data.alpaca.pages_fetched", path=path, pages=pages, symbols=len(merged))
        return merged

    # ── HistoricalDataProvider ──────────────────────────────────────────────

    async def get_bars(
        self,
        symbols: list[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        adjusted: bool = True,
    ) -> dict[str, list[Bar]]:
        """Bars per symbol, chronological, no duplicates.

        `adjusted=True` costs two passes over the window rather than one, and
        that is deliberate. Alpaca applies one adjustment mode per request, but
        a `Bar` carries both a raw `close` and an `adj_close` because the
        platform needs both: backtests run on adjusted prices, orders and
        reconciliation on raw ones, and a table holding only one of them cannot
        answer both questions (docs/DATA.md, CLAUDE.md §5). So the raw pass
        supplies OHLCV and the adjusted pass fills `adj_close`.

        Raises `DataGapError` if a requested symbol comes back with no bars at
        all. Interior gaps are deliberately not checked here: telling a real
        hole apart from a weekend needs the trading calendar, which lives in
        `BarRepository.find_gaps`.
        """
        if not symbols:
            return {}
        _require_utc(start, "start")
        _require_utc(end, "end")
        if start >= end:
            raise ValueError(f"start must be before end, got start={start} end={end}")
        for symbol in symbols:
            if symbol != symbol.upper():
                raise ValueError(f"symbol must be uppercase, got {symbol!r}")
        if timeframe not in _TIMEFRAME_PARAM:
            raise ValueError(f"unsupported timeframe {timeframe!r}")

        base_params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": _TIMEFRAME_PARAM[timeframe],
            "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "limit": _MAX_PAGE_LIMIT,
            "feed": self._settings.alpaca_data_feed,
            "sort": "asc",
        }

        raw_pages = await self._fetch_all_pages(
            "/v2/stocks/bars", {**base_params, "adjustment": "raw"}
        )

        adj_close_by_key: dict[tuple[str, datetime], Decimal] = {}
        if adjusted:
            adjusted_pages = await self._fetch_all_pages(
                "/v2/stocks/bars", {**base_params, "adjustment": "all"}
            )
            for symbol, raw_bars in adjusted_pages.items():
                for raw in raw_bars:
                    adj_close_by_key[(symbol, _parse_ts(raw["t"]))] = _as_decimal(raw["c"])

        result: dict[str, list[Bar]] = {}
        for symbol in symbols:
            raw_bars = raw_pages.get(symbol) or []
            if not raw_bars:
                raise DataGapError(
                    f"Alpaca returned no {timeframe} bars for {symbol} between "
                    f"{start.isoformat()} and {end.isoformat()}. Either the window "
                    "contains no trading sessions, the symbol was not listed yet, or "
                    "the feed is missing data — do not backtest over it."
                )
            result[symbol] = self._to_bars(symbol, timeframe, raw_bars, adj_close_by_key)

        log.info(
            "data.alpaca.bars_fetched",
            symbols=len(result),
            bars=sum(len(v) for v in result.values()),
            timeframe=str(timeframe),
            adjusted=adjusted,
        )
        return result

    def _to_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        raw_bars: list[dict[str, Any]],
        adj_close_by_key: dict[tuple[str, datetime], Decimal],
    ) -> list[Bar]:
        """Build domain bars, deduplicated on timestamp and sorted.

        Alpaca returns pages in order, but overlapping pages have been seen at
        page boundaries. `Bar.__post_init__` enforces the OHLC invariant, so a
        malformed print fails here rather than in a backtest six months later.
        """
        by_ts: dict[datetime, Bar] = {}
        for raw in raw_bars:
            ts = _parse_ts(raw["t"])
            by_ts[ts] = Bar(
                symbol=symbol,
                ts=ts,
                timeframe=timeframe,
                open=_as_decimal(raw["o"]),
                high=_as_decimal(raw["h"]),
                low=_as_decimal(raw["l"]),
                close=_as_decimal(raw["c"]),
                volume=_as_decimal(raw["v"]),
                adj_close=adj_close_by_key.get((symbol, ts)),
                vwap=_as_decimal(raw["vw"]) if raw.get("vw") is not None else None,
                trade_count=int(raw["n"]) if raw.get("n") is not None else None,
            )
        return [by_ts[ts] for ts in sorted(by_ts)]

    async def get_latest_bar(self, symbol: str, timeframe: Timeframe) -> Bar | None:
        """The most recent completed bar, or None if the symbol has none.

        Raw prices, unadjusted: the only caller for a *latest* bar is something
        comparing against live prices, and those are raw (CLAUDE.md §5).

        Unlike `get_bars` this returns None rather than raising — "no bar yet"
        is an ordinary answer before the first session of the day, not a gap.
        """
        if symbol != symbol.upper():
            raise ValueError(f"symbol must be uppercase, got {symbol!r}")
        if timeframe not in _TIMEFRAME_PARAM:
            raise ValueError(f"unsupported timeframe {timeframe!r}")

        # Long enough to clear a three-day weekend plus a holiday, so the lookback
        # itself never becomes the reason there is no bar.
        lookback = max(timedelta(seconds=timeframe.seconds * 5), timedelta(days=6))
        now = datetime.now(UTC)

        payload = await self._request_page(
            "/v2/stocks/bars",
            {
                "symbols": symbol,
                "timeframe": _TIMEFRAME_PARAM[timeframe],
                "start": (now - lookback).isoformat().replace("+00:00", "Z"),
                "end": now.isoformat().replace("+00:00", "Z"),
                # Newest first with a single row: no pagination, one round trip.
                "sort": "desc",
                "limit": 1,
                "adjustment": "raw",
                "feed": self._settings.alpaca_data_feed,
            },
        )

        raw_bars = (payload.get("bars") or {}).get(symbol) or []
        if not raw_bars:
            return None
        return self._to_bars(symbol, timeframe, raw_bars[:1], {})[0]


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


if TYPE_CHECKING:
    # Checked by mypy, costs nothing at runtime. The dependency rule only holds
    # if adapters actually satisfy their port, and a signature that drifts from
    # `ports.py` would otherwise be found by whoever swaps the implementation.
    def _conforms_historical(adapter: AlpacaHistoricalProvider) -> HistoricalDataProvider:
        return adapter

    def _conforms_realtime(adapter: AlpacaRealtimeFeed) -> RealtimeDataFeed:
        return adapter
