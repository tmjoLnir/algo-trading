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
- Streaming: one connection per key — a second is refused with code 406, and
  that is load-bearing (see `data.stream`). Subscriptions are sent in frames
  of bounded size: the cap is on the message, so a universe of a few thousand
  tickers in one frame is how you find it in production.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from atp_core import ws
from atp_core.clock import SystemClock
from atp_core.data.ports import FeedReconnected
from atp_core.domain import Bar, Quote, Timeframe, Trade
from atp_core.errors import DataError, DataGapError
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

    from atp_core.clock import Clock
    from atp_core.config import Settings
    from atp_core.data.ports import HistoricalDataProvider, RealtimeDataFeed, StreamEvent

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


#: Alpaca's message tags. `d` (daily bar) and `u` (corrected bar) exist too and
#: arrive only for subscriptions this class does not make.
_TAG_TRADE = "t"
_TAG_QUOTE = "q"
_TAG_BAR = "b"

#: The subscription channels this class speaks, spelled exactly as Alpaca's
#: subscribe frame expects them.
_CHANNELS = ("bars", "quotes", "trades")

#: Error codes worth reconnecting for. 407 is "slow client" — the server hung up
#: because we could not keep up, and coming back is the right response. 500 is
#: theirs. Everything else Alpaca documents is a credential, a plan, a syntax or
#: a connection-limit problem, and reconnecting just performs it again.
#:
#: An *unrecognised* code is treated as permanent on purpose. A loop against an
#: error the server keeps returning burns the rate limit and buries the reason
#: in a scrolling log; stopping puts it in front of somebody.
_TRANSIENT_ERROR_CODES = frozenset({407, 500})

#: Symbols per subscribe frame. Self-imposed rather than a published vendor
#: ceiling: Alpaca caps the *message*, not the symbol count, and a universe of a
#: few thousand tickers in one frame is how you find that limit in production.
_MAX_SYMBOLS_PER_FRAME = 250

#: The stream's transport knobs live in `atp_core.ws`, shared with the trade-
#: updates stream so two connections to the same vendor behave the same way
#: under the same outage. Aliased here because this module's own tests and
#: constructor defaults read them by these names.
_HANDSHAKE_TIMEOUT_SECONDS = ws.HANDSHAKE_TIMEOUT_SECONDS
_MAX_HANDSHAKE_FRAMES = ws.MAX_HANDSHAKE_FRAMES
_STREAM_BACKOFF_BASE_SECONDS = ws.BACKOFF_BASE_SECONDS
_STREAM_BACKOFF_MAX_SECONDS = ws.BACKOFF_MAX_SECONDS
_MAX_RECONNECT_ATTEMPTS = ws.MAX_RECONNECT_ATTEMPTS


class _PermanentFeedError(DataError):
    """The feed refused in a way another connection would not fix.

    Internal: callers see `DataError`. It exists so the reconnect loop can tell
    "try again" from "stop and tell somebody" without inspecting messages.
    """


#: The narrow transport seam, from `atp_core.ws`. Aliased rather than renamed
#: throughout: it is the same protocol, and the scripted fakes in
#: `test_alpaca_realtime_feed.py` are written against this name.
_WebSocketConnection = ws.WebSocketConnection


class AlpacaRealtimeFeed:
    """`RealtimeDataFeed` over Alpaca's market-data WebSocket.

    Owns the transport and nothing else: it connects, authenticates, restores
    its subscriptions after a drop, and reports the outage as a
    `FeedReconnected` event so the ingestor can close the data gap before
    handling anything from the new connection (see `data.ports`).

    One connection per key. Alpaca refuses a second one with code 406, which
    this treats as permanent rather than retrying — the "one process owns the
    upstream connection" invariant in `data.stream` is only worth anything if a
    second process fails loudly instead of racing the first.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        connect: Callable[[str], Awaitable[_WebSocketConnection]] | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
        backoff_base_seconds: float = _STREAM_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = _STREAM_BACKOFF_MAX_SECONDS,
        max_reconnect_attempts: int = _MAX_RECONNECT_ATTEMPTS,
        handshake_timeout_seconds: float = _HANDSHAKE_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings
        self._connect: Callable[[str], Awaitable[_WebSocketConnection]] = (
            connect if connect is not None else _connect_websocket
        )
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else _sleep_seconds
        )
        #: Jitter, not cryptography — `random` is the right tool and a seeded one
        #: is what makes the backoff schedule assertable in a test.
        self._rng = rng if rng is not None else random.Random()
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._max_reconnect_attempts = max_reconnect_attempts
        self._handshake_timeout_seconds = handshake_timeout_seconds

        self._connected = False
        self._last_message_at: datetime | None = None
        #: Desired state, not confirmed state. This is what gets replayed on
        #: reconnect, which is the only reason a dropped socket comes back
        #: subscribed to the same symbols it went down with.
        self._subscriptions: dict[str, set[str]] = {channel: set() for channel in _CHANNELS}
        self._connection: _WebSocketConnection | None = None
        self._disconnect_callbacks: list[Callable[[Exception], None]] = []

    # ── RealtimeDataFeed ────────────────────────────────────────────────────

    async def subscribe(
        self, symbols: list[str], *, bars: bool = True, quotes: bool = True, trades: bool = False
    ) -> None:
        """Add symbols, and tell the server now if we are already connected.

        Safe to call before `stream()`: the subscription set is what the stream
        replays once it has a socket.
        """
        for symbol in symbols:
            if symbol != symbol.upper():
                raise ValueError(f"symbol must be uppercase, got {symbol!r}")

        wanted = {"bars": bars, "quotes": quotes, "trades": trades}
        added: dict[str, set[str]] = {}
        for channel, requested in wanted.items():
            if not requested:
                continue
            new = set(symbols) - self._subscriptions[channel]
            self._subscriptions[channel] |= set(symbols)
            if new:
                added[channel] = new

        if added and self._connection is not None:
            await self._send_subscription_frames(self._connection, "subscribe", added)

    async def unsubscribe(self, symbols: list[str]) -> None:
        """Drop symbols from every channel."""
        removed: dict[str, set[str]] = {}
        for channel, subscribed in self._subscriptions.items():
            gone = subscribed & set(symbols)
            if gone:
                removed[channel] = gone
                subscribed -= gone

        if removed and self._connection is not None:
            await self._send_subscription_frames(self._connection, "unsubscribe", removed)

    async def stream(self) -> AsyncIterator[StreamEvent]:
        """Events as they arrive, across reconnects.

        Structured so that no `yield` sits inside a `try`. A consumer that
        raises while this generator is suspended would otherwise have its
        exception delivered *at the yield*, caught by the reconnect handler, and
        turned into a reconnect — a downstream bug silently becoming a
        connection retry is a very hard thing to find later.
        """
        try:
            async for event in self._stream():
                yield event
        finally:
            await self._close_connection()

    def on_disconnect(self, callback: Callable[[Exception], None]) -> None:
        """Register a handler. A feed loss should engage the kill switch:
        no data means no basis for a trading decision."""
        self._disconnect_callbacks.append(callback)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self) -> datetime | None:
        return self._last_message_at

    # ── reconnect loop ──────────────────────────────────────────────────────

    async def _stream(self) -> AsyncIterator[StreamEvent]:
        attempts = 0
        #: The last instant the data is known good. Seeded with "now" rather
        #: than left empty: before the first message there is nothing to be
        #: missing, and a first connection that takes four attempts to come up
        #: has genuinely lost whatever traded while it was struggling.
        gap_since = self._clock.now()
        reconnecting = False

        while True:
            try:
                connection = await self._open()
            except _PermanentFeedError:
                raise
            except Exception as exc:  # every transport failure retries alike
                attempts += 1
                self._note_disconnect(exc)
                if attempts > self._max_reconnect_attempts:
                    raise DataError(
                        f"Alpaca stream did not come back after {self._max_reconnect_attempts} "
                        f"attempts: {exc}"
                    ) from exc
                log.warning("data.alpaca.stream_reconnecting", attempt=attempts, error=str(exc))
                await self._sleep(self._backoff_delay(attempts))
                reconnecting = True
                continue

            if reconnecting:
                reconnecting = False
                yield FeedReconnected(
                    gap_since=gap_since,
                    reconnected_at=self._clock.now(),
                    attempts=attempts + 1,
                )

            #: Reset only once the connection has proved itself by delivering
            #: something. Resetting on connect alone would turn a server that
            #: accepts and immediately drops us — a connection-limit fight, a
            #: flapping upstream — into a hot loop that never backs off.
            delivered = False

            while True:
                frame = await self._receive(connection)
                if frame is None:
                    break
                if not delivered:
                    delivered = True
                    attempts = 0
                for event in frame:
                    yield event
                gap_since = self._last_message_at or gap_since

            reconnecting = True

    async def _receive(self, connection: _WebSocketConnection) -> list[StreamEvent] | None:
        """One frame's worth of events, or None once the connection has gone.

        Parsing happens here rather than at the call site so that the two kinds
        of server error land in the right place. A permanent one — bad
        credentials, a plan that does not cover this feed, somebody else already
        holding the key's one connection — propagates out of the reconnect loop,
        because trying again would only perform it again. A transient one
        ("slow client", an internal error) is the server hanging up on us in
        words instead of at the socket, and is handled exactly like a drop.
        """
        try:
            raw = await connection.recv()
        except Exception as exc:  # a closed socket arrives in many shapes
            await self._drop(connection, exc)
            return None

        self._last_message_at = self._clock.now()
        try:
            return self._parse_frame(raw)
        except _PermanentFeedError:
            raise
        except DataError as exc:
            await self._drop(connection, exc)
            return None

    async def _drop(self, connection: _WebSocketConnection, exc: Exception) -> None:
        """Tear the connection down and tell whoever registered to be told."""
        self._connected = False
        self._connection = None
        self._note_disconnect(exc)
        log.warning("data.alpaca.stream_disconnected", error=str(exc))
        await _close_quietly(connection)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential, capped, jittered — `atp_core.ws.backoff_delay`.

        Shared with the trade-updates stream rather than written twice, so two
        connections to the same vendor cannot end up with two different retry
        cadences that nobody can tell apart in an incident.
        """
        return ws.backoff_delay(
            attempt,
            base_seconds=self._backoff_base_seconds,
            max_seconds=self._backoff_max_seconds,
            rng=self._rng,
        )

    def _note_disconnect(self, exc: Exception) -> None:
        self._connected = False
        for callback in self._disconnect_callbacks:
            try:
                callback(exc)
            except Exception as callback_error:
                # A broken handler must not take the reconnect loop with it —
                # this is the path that leads to the kill switch.
                log.error("data.alpaca.disconnect_callback_failed", error=str(callback_error))

    # ── connection lifecycle ────────────────────────────────────────────────

    async def _open(self) -> _WebSocketConnection:
        """Connect, authenticate and restore subscriptions, or clean up trying."""
        connection = await self._connect(self._settings.alpaca_stream_url)
        try:
            async with asyncio.timeout(self._handshake_timeout_seconds):
                await self._authenticate(connection)
                await self._send_subscription_frames(
                    connection, "subscribe", {k: v for k, v in self._subscriptions.items() if v}
                )
        except BaseException:
            # Includes the timeout and a cancellation. A half-authenticated
            # socket left open still counts against the one-connection limit,
            # so the retry would be refused with 406 by our own leak.
            await _close_quietly(connection)
            raise

        self._connection = connection
        self._connected = True
        log.info(
            "data.alpaca.stream_connected",
            feed=self._settings.alpaca_data_feed,
            symbols=len(_union(self._subscriptions.values())),
        )
        return connection

    async def _authenticate(self, connection: _WebSocketConnection) -> None:
        """Send credentials and wait for the server to accept them.

        The key never reaches a log line or an exception message here (rule
        §1.6) — the auth frame is built inline and the errors below quote only
        the server's own code and message.
        """
        await connection.send(
            json.dumps(
                {
                    "action": "auth",
                    "key": self._settings.alpaca_api_key.get_secret_value(),
                    "secret": self._settings.alpaca_api_secret.get_secret_value(),
                }
            )
        )

        for _ in range(_MAX_HANDSHAKE_FRAMES):
            for message in _iter_messages(await connection.recv()):
                tag = message.get("T")
                if tag == "error":
                    raise self._error_for(message)
                if tag == "success" and message.get("msg") == "authenticated":
                    return
                # `{"T":"success","msg":"connected"}` is the server's greeting
                # and arrives before the auth reply.
                log.debug("data.alpaca.stream_handshake", tag=tag, msg=message.get("msg"))

        raise _PermanentFeedError(
            f"Alpaca stream sent {_MAX_HANDSHAKE_FRAMES} frames without authenticating"
        )

    async def _send_subscription_frames(
        self,
        connection: _WebSocketConnection,
        action: str,
        channels: dict[str, set[str]],
    ) -> None:
        """Send `subscribe`/`unsubscribe` in frames of bounded size."""
        ordered = sorted(_union(channels.values()))
        for offset in range(0, len(ordered), _MAX_SYMBOLS_PER_FRAME):
            chunk = set(ordered[offset : offset + _MAX_SYMBOLS_PER_FRAME])
            frame: dict[str, Any] = {"action": action}
            for channel, wanted in channels.items():
                overlap = sorted(wanted & chunk)
                if overlap:
                    frame[channel] = overlap
            if len(frame) > 1:
                await connection.send(json.dumps(frame))

    async def _close_connection(self) -> None:
        connection, self._connection = self._connection, None
        self._connected = False
        if connection is not None:
            await _close_quietly(connection)

    # ── parsing ─────────────────────────────────────────────────────────────

    def _parse_frame(self, raw: str | bytes) -> list[StreamEvent]:
        """One WebSocket frame → domain events.

        Alpaca batches: a frame is a JSON *array* of messages. Parsed with
        `parse_float=Decimal` for the same reason the REST client is — a price
        that arrives as a float has already lost the precision rule §1.1 exists
        to keep.
        """
        events: list[StreamEvent] = []
        for message in _iter_messages(raw):
            tag = message.get("T")
            if tag == "error":
                raise self._error_for(message)
            if tag == "subscription":
                log.info(
                    "data.alpaca.stream_subscribed",
                    bars=len(message.get("bars") or []),
                    quotes=len(message.get("quotes") or []),
                    trades=len(message.get("trades") or []),
                )
                continue
            if tag not in {_TAG_QUOTE, _TAG_BAR, _TAG_TRADE}:
                log.debug("data.alpaca.stream_ignored", tag=tag)
                continue
            event = self._to_event(tag, message)
            if event is not None:
                events.append(event)
        return events

    def _to_event(self, tag: str, message: dict[str, Any]) -> StreamEvent | None:
        """Build one domain object, or None if the message will not make one.

        A malformed or self-contradictory print is dropped with a warning rather
        than raised. `Bar.__post_init__` rejects `low > high`, and one bad tick
        is not a reason to tear down the connection every other symbol is
        riding on — but it is absolutely a reason to be able to find it later.
        """
        try:
            symbol = message["S"]
            ts = _parse_ts(message["t"])
            if tag == _TAG_QUOTE:
                return Quote(
                    symbol=symbol,
                    ts=ts,
                    bid=_as_decimal(message["bp"]),
                    ask=_as_decimal(message["ap"]),
                    bid_size=_as_decimal(message.get("bs", 0)),
                    ask_size=_as_decimal(message.get("as", 0)),
                )
            if tag == _TAG_TRADE:
                return Trade(
                    symbol=symbol,
                    ts=ts,
                    price=_as_decimal(message["p"]),
                    size=_as_decimal(message["s"]),
                    conditions=tuple(message.get("c") or ()),
                )
            return Bar(
                symbol=symbol,
                #: Alpaca stamps a streamed bar at its open, which is what
                #: `Bar.ts` means — so no adjustment here. `adj_close` is left
                #: empty: adjustment is a corporate-action fact that does not
                #: exist yet for a bar that closed a second ago.
                ts=ts,
                timeframe=Timeframe.M1,
                open=_as_decimal(message["o"]),
                high=_as_decimal(message["h"]),
                low=_as_decimal(message["l"]),
                close=_as_decimal(message["c"]),
                volume=_as_decimal(message["v"]),
                vwap=_as_decimal(message["vw"]) if message.get("vw") is not None else None,
                trade_count=int(message["n"]) if message.get("n") is not None else None,
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            log.warning(
                "data.alpaca.stream_message_dropped",
                tag=tag,
                symbol=message.get("S"),
                error=str(exc),
            )
            return None

    def _error_for(self, message: dict[str, Any]) -> DataError:
        """Turn an `{"T":"error"}` message into the right kind of failure."""
        code = message.get("code")
        detail = f"Alpaca stream error {code}: {message.get('msg')}"
        if isinstance(code, int) and code in _TRANSIENT_ERROR_CODES:
            return DataError(detail)
        return _PermanentFeedError(
            f"{detail}. Retrying would repeat it — check the credentials, the data "
            f"plan, and that no other process holds this key's one connection."
        )


def _iter_messages(raw: str | bytes) -> list[dict[str, Any]]:
    """A frame's messages. Alpaca sends an array; tolerate a bare object."""
    payload = json.loads(raw, parse_float=Decimal)
    if isinstance(payload, dict):
        return [payload]
    return [m for m in payload if isinstance(m, dict)]


#: Transport glue, from `atp_core.ws`. Aliased rather than inlined at the call
#: sites so the injection seams in this module keep their names.
_close_quietly = ws.close_quietly
_connect_websocket = ws.connect_websocket
_sleep_seconds = ws.sleep_seconds


def _union(groups: Iterable[set[str]]) -> set[str]:
    symbols: set[str] = set()
    for group in groups:
        symbols |= group
    return symbols


if TYPE_CHECKING:
    # Checked by mypy, costs nothing at runtime. The dependency rule only holds
    # if adapters actually satisfy their port, and a signature that drifts from
    # `ports.py` would otherwise be found by whoever swaps the implementation.
    def _conforms_historical(adapter: AlpacaHistoricalProvider) -> HistoricalDataProvider:
        return adapter

    def _conforms_realtime(adapter: AlpacaRealtimeFeed) -> RealtimeDataFeed:
        return adapter
