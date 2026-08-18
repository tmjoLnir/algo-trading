"""Real-time ingestion pipeline — requirement #4.

    Alpaca WS ──▶ StreamIngestor ──┬──▶ QuoteCache (Redis)   risk checks read this
                                   ├──▶ BarRepository        durable history
                                   └──▶ Redis pub/sub ──▶ API WebSocket ──▶ dashboard

One process owns the upstream connection. Everything else reads Redis. Alpaca
allows a limited number of concurrent connections per key, and more importantly
a single writer means one place where gap detection and reconnect logic live —
duplicated across consumers, they would drift.

This module is the fan-out half and is pure: it is handed ports and decides what
to do with each event. Reconnecting the socket belongs to the feed adapter,
which is the only thing that knows what a socket is; closing the *data* gap a
reconnect leaves behind belongs here, because the historical provider and the
bar store are here. The two meet at `FeedReconnected`, which arrives in the
event stream so that the backfill provably completes before the first event of
the new connection is handled (CLAUDE.md §5, docs/DATA.md 'Real-time pipeline').
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from atp_core.clock import SystemClock
from atp_core.data.backfill import backfill_bars
from atp_core.data.ports import FeedReconnected
from atp_core.domain import Bar, Quote, Timeframe, Trade
from atp_core.errors import DataError
from atp_core.logging import get_logger
from atp_core.risk.killswitch import HaltReason, HaltScope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from atp_core.clock import Clock, TradingCalendar
    from atp_core.data.ports import (
        BarRepository,
        EventPublisher,
        HistoricalDataProvider,
        QuoteCache,
        RealtimeDataFeed,
        StreamEvent,
    )
    from atp_core.risk.killswitch import KillSwitch

log = get_logger(__name__)

CHANNEL_QUOTES = "atp:md:quotes"
CHANNEL_BARS = "atp:md:bars"
CHANNEL_ORDERS = "atp:exec:orders"
CHANNEL_SIGNALS = "atp:exec:signals"

#: Who a halt engaged from here is attributed to. A halt is cleared by a named
#: human, so the record has to say plainly what stopped trading.
HALT_ACTOR = "stream_ingestor"

#: Ditto for the watchdog. Distinct from `HALT_ACTOR` on purpose: "the feed gave
#: up" and "the feed went quiet without saying so" are different incidents and
#: the halt record should not make an operator guess which one happened.
STALENESS_ACTOR = "staleness_monitor"

#: The most history one reconnect will chase before handing the rest to the
#: nightly sweep. A minute-long blip is the case this is built for; a
#: `last_message_at` from three days ago means the process has been down, and
#: turning that into a three-day minute backfill would block the live stream
#: behind thousands of requests at exactly the moment it came back. Whatever is
#: dropped is named in the log and picked up by
#: `atp_worker.scheduler.backfill_missing_bars`, which exists for that job.
MAX_RECONNECT_BACKFILL = timedelta(hours=6)


@dataclass(slots=True)
class IngestorStats:
    connected_since: datetime | None = None
    messages_received: int = 0
    reconnects: int = 0
    last_message_at: datetime | None = None
    gaps_backfilled: int = 0
    symbols: set[str] = field(default_factory=set)
    #: symbol → when that symbol last printed a quote. Distinct from
    #: `last_message_at`, which is the *feed's* pulse: on a watchlist where one
    #: name is halted and the rest are busy, the feed looks healthy while that
    #: one symbol has not traded for an hour. `StaleDataRule` has to refuse an
    #: order priced off the halted one, so it needs the per-symbol answer.
    last_tick_at: dict[str, datetime] = field(default_factory=dict)


class StreamIngestor:
    """Owns the market-data connection and fans data out."""

    def __init__(
        self,
        feed: RealtimeDataFeed,
        quote_cache: QuoteCache,
        bar_repo: BarRepository,
        provider: HistoricalDataProvider,
        *,
        publisher: EventPublisher | None = None,
        kill_switch: KillSwitch | None = None,
        clock: Clock | None = None,
        bar_timeframe: Timeframe = Timeframe.M1,
        max_reconnect_backfill: timedelta = MAX_RECONNECT_BACKFILL,
    ) -> None:
        """Bind the ports this needs.

        `provider` is not optional and is the reason this class exists in the
        shape it does: a reconnect that cannot re-fetch what it missed is the
        silent failure the whole module is written to avoid.

        `publisher` and `kill_switch` are optional because their adapters are
        later roadmap items — the Redis quote cache and pub/sub, and the Redis
        kill switch in Phase 3. Absent, this still does its durable work (cache
        the quote, store the bar, close the gap); it just has nobody to tell.
        A feed loss with no kill switch bound is logged CRITICAL rather than
        passed over, because "trading was not halted" is the part an operator
        needs to know.

        Takes `redis_url` no longer: core does not open sockets (CLAUDE.md
        §1.3), and a URL here would mean it had to.
        """
        self.feed = feed
        self.quote_cache = quote_cache
        self.bar_repo = bar_repo
        self.provider = provider
        self.publisher = publisher
        self.kill_switch = kill_switch
        self.stats = IngestorStats()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._bar_timeframe = bar_timeframe
        self._max_reconnect_backfill = max_reconnect_backfill
        self._symbols: tuple[str, ...] = ()

    async def run(self, symbols: Sequence[str]) -> None:
        """Connect, subscribe, and pump until cancelled.

        Reconnect policy lives in the feed: exponential backoff, 1s → 60s,
        jittered, subscriptions restored on the way back up. What is enforced
        here is the other half — after each reconnect, backfill from the last
        message we actually saw before handling anything from the new
        connection. Indicators computed across an unfilled gap are wrong in a
        way nothing downstream can detect.

        If the feed gives up — attempts exhausted, or an error retrying cannot
        fix — the kill switch is engaged and the exception propagates. Not
        having data is a reason to stop trading, not to keep going on the last
        price we happened to see.

        Cancellation is an ordinary shutdown and does not halt trading: the
        supervisor in `atp_worker.main` is stopping the whole process, and
        leaving a halt behind that a human has to clear would make every clean
        restart a manual operation.
        """
        if not symbols:
            raise ValueError("no symbols to ingest")
        for symbol in symbols:
            if symbol != symbol.upper():
                raise ValueError(f"symbol must be uppercase, got {symbol!r}")

        self._symbols = tuple(dict.fromkeys(symbols))
        self.stats.symbols = set(self._symbols)
        self.stats.connected_since = self._clock.now()

        await self.feed.subscribe(list(self._symbols), bars=True, quotes=True, trades=False)
        log.info(
            "data.stream.started",
            symbols=len(self._symbols),
            timeframe=self._bar_timeframe.value,
        )

        stream = self.feed.stream()
        try:
            async for event in stream:
                await self._dispatch(event)
        except DataError as exc:
            # The feed has stopped trying. Everything downstream is now working
            # from a price that is only getting older.
            log.error("data.stream.feed_lost", error=str(exc))
            self._halt(HaltReason.DATA_FEED_LOST, f"market-data feed lost: {exc}")
            raise
        finally:
            # Leaving the loop — cancelled, halted, or done — does not finalise
            # an async generator; Python leaves that to the garbage collector.
            # Here that would strand an open WebSocket, and Alpaca allows one
            # per key: the next connection would be refused by the corpse of
            # this one. Close it while we still know it exists.
            if isinstance(stream, AsyncGenerator):
                await stream.aclose()

        log.warning("data.stream.ended", msg="the feed's stream finished without an error")

    def last_tick_at(self, symbol: str) -> datetime | None:
        """When `symbol` last printed, for `risk.rules.StaleDataRule`.

        Sync and cheap on purpose: the risk chain runs inside order submission
        and cannot await, and this is consulted before every single order.
        """
        return self.stats.last_tick_at.get(symbol)

    async def _dispatch(self, event: StreamEvent) -> None:
        """Route one event. Every branch is awaited before the next arrives."""
        if isinstance(event, FeedReconnected):
            await self._on_reconnect(event)
            return

        self.stats.messages_received += 1
        # Arrival time, not `event.ts`. A minute bar is stamped at its *open*,
        # so reading freshness off the payload would report a healthy feed as a
        # minute stale — and the staleness watchdog is the one reader that must
        # not be told a lie in that direction.
        self.stats.last_message_at = self._clock.now()

        if isinstance(event, Quote):
            await self._handle_quote(event)
        elif isinstance(event, Bar):
            await self._handle_bar(event)
        elif isinstance(event, Trade):
            # `run` subscribes bars and quotes only, so a trade here means a
            # caller subscribed the feed itself. Counted, not dropped silently.
            log.debug("data.stream.trade_ignored", symbol=event.symbol)

    async def _handle_quote(self, quote: Quote) -> None:
        """Cache and publish. Do not persist every quote — the volume is
        enormous and the value is almost entirely in the latest one."""
        # The quote's own timestamp, not the clock: what the rule needs to know
        # is how old the *price* is, and a vendor replaying a backlog would
        # otherwise stamp stale prices as fresh on arrival.
        self.stats.last_tick_at[quote.symbol] = quote.ts
        await self.quote_cache.set_quote(quote)
        await self._publish(CHANNEL_QUOTES, _quote_message(quote))

    async def _handle_bar(self, bar: Bar) -> None:
        """Persist and publish. Bars are the durable record."""
        await self.bar_repo.upsert_bars([bar])
        await self._publish(CHANNEL_BARS, _bar_message(bar))

    async def _on_reconnect(self, event: FeedReconnected) -> None:
        """Close the data gap the outage left, before anything else is handled."""
        self.stats.reconnects += 1
        self.stats.connected_since = event.reconnected_at
        gap_seconds = (event.reconnected_at - event.gap_since).total_seconds()
        log.warning(
            "data.stream.reconnected",
            attempts=event.attempts,
            gap_seconds=round(gap_seconds, 3),
            gap_since=event.gap_since.isoformat(),
            symbols=len(self._symbols),
        )
        self.stats.gaps_backfilled += await self._backfill_gap(event.gap_since)

    async def _backfill_gap(self, since: datetime) -> int:
        """Fetch and store bars missed while disconnected.

        Raw prices, not adjusted. That halves the requests — the provider makes
        a second pass over the window to fill `adj_close` — and this is the live
        path, where raw is what orders and reconciliation are compared against
        anyway (CLAUDE.md §5). The nightly sweep re-fetches the same range
        adjusted, so nothing is permanently raw-only.

        Both ends are floored to the bar grid. The start, because a drop at
        10:30:45 lost part of the 10:30 bar and the socket will never re-send
        it, so the refetch has to begin at that bar's open. The end, because the
        bar in progress right now is not missing — we are subscribed again
        before it closes, and the feed will deliver it complete. Fetching it
        from REST would trade a whole bar for a partial one.

        A gap that opens and closes inside one bar therefore costs nothing at
        all, which is the common case: a blip is not a data incident.

        A failure to close the gap halts trading rather than raising. The
        stream itself is healthy — quotes and bars keep flowing to the cache and
        the table — but there is now a known hole in the history every indicator
        reads, and trading through it is the failure this module exists to
        prevent. Stopping the ingestor too would take the dashboard and the
        quote cache down with it for no gain.
        """
        # The last bar that has actually finished: everything up to here is
        # fetchable and everything after it is still being built.
        end = _floor_to_grid(self._clock.now(), self._bar_timeframe)
        start = _floor_to_grid(since, self._bar_timeframe)
        if start >= end:
            log.debug(
                "data.stream.backfill_skipped",
                reason="the outage did not span a completed bar",
            )
            return 0

        earliest = end - self._max_reconnect_backfill
        if start < earliest:
            log.warning(
                "data.stream.backfill_truncated",
                requested_from=start.isoformat(),
                fetching_from=earliest.isoformat(),
                limit_hours=self._max_reconnect_backfill / timedelta(hours=1),
                hint="the rest is left to the nightly gap sweep",
            )
            start = _floor_to_grid(earliest, self._bar_timeframe)

        try:
            result = await backfill_bars(
                self.provider,
                self.bar_repo,
                symbols=self._symbols,
                timeframe=self._bar_timeframe,
                start=start,
                end=end,
                adjusted=False,
            )
        except DataError as exc:
            log.error(
                "data.stream.backfill_failed",
                start=start.isoformat(),
                end=end.isoformat(),
                error=str(exc),
            )
            self._halt(
                HaltReason.DATA_FEED_LOST,
                f"could not backfill {start.isoformat()} to {end.isoformat()} after a "
                f"feed reconnect — the bar history has a hole in it",
            )
            return 0

        for window in result.empty_windows:
            # Ordinary for a symbol that simply did not trade in the gap; a
            # symptom worth seeing for a liquid one. Not something this can
            # tell apart, so it says what it saw rather than guessing.
            log.info(
                "data.stream.backfill_empty",
                symbol=window.symbol,
                start=window.start.isoformat(),
                end=window.end.isoformat(),
            )

        log.info(
            "data.stream.backfill_done",
            start=start.isoformat(),
            end=end.isoformat(),
            bars=result.bars_written,
            requests=result.requests,
            symbols_without_data=len(result.empty_windows),
        )
        return result.bars_written

    async def _publish(self, channel: str, message: dict[str, Any]) -> None:
        """Best-effort fan-out. A dead subscriber is not a reason to stop.

        The dashboard's authority is its 5-minute poll, not this (`atp_api.ws`),
        so a dropped tick costs freshness and nothing else. Losing a bar we had
        already stored because Redis blinked would cost data.
        """
        if self.publisher is None:
            return
        try:
            await self.publisher.publish(channel, message)
        except Exception as exc:  # deliberate breadth — see the docstring
            log.warning("data.stream.publish_failed", channel=channel, error=str(exc))

    def _halt(self, reason: HaltReason, detail: str) -> None:
        """Stop trading platform-wide, or say loudly that we could not."""
        if self.kill_switch is None:
            log.critical(
                "data.stream.halt_unavailable",
                reason=reason.value,
                detail=detail,
                msg="no kill switch bound — TRADING IS NOT HALTED",
            )
            return
        self.kill_switch.engage(HaltScope.GLOBAL, reason, engaged_by=HALT_ACTOR, detail=detail)
        log.critical("data.stream.halted", reason=reason.value, detail=detail)


@dataclass(frozen=True, slots=True)
class StalenessVerdict:
    """One reading of the watchdog. Pure data, so the decision is testable
    without a clock, a calendar or a running ingestor."""

    stale: bool
    #: How long the feed has been silent *within the current session*, or None
    #: when the market is shut and silence carries no information.
    silent_for_seconds: float | None
    market_open: bool
    reason: str


class StalenessMonitor:
    """Watchdog: alert and halt when data stops arriving during market hours.

    Must be calendar-aware. Silence at 02:00 on a Sunday is correct; the same
    silence at 14:30 on a Tuesday means something is broken.

    This is the only thing that catches a feed which is *connected and frozen*.
    A dropped socket is the feed adapter's problem and it reconnects; a socket
    that stays open and stops delivering looks perfectly healthy from every
    other vantage point in the system, and a quiet market looks identical to it
    from the inside. Hence a clock and a calendar rather than a connection
    check.
    """

    def __init__(
        self,
        max_silence_seconds: int = 60,
        *,
        kill_switch: KillSwitch | None = None,
        calendar: TradingCalendar | None = None,
        clock: Clock | None = None,
        poll_interval_seconds: float = 5.0,
        exchange: str = "NYSE",
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if max_silence_seconds < 1:
            raise ValueError(f"max_silence_seconds must be at least 1, got {max_silence_seconds}")
        if poll_interval_seconds <= 0:
            raise ValueError(f"poll_interval_seconds must be positive, got {poll_interval_seconds}")
        self.max_silence_seconds = max_silence_seconds
        self.kill_switch = kill_switch
        self.poll_interval_seconds = poll_interval_seconds
        self._calendar = calendar
        self._exchange = exchange
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else _sleep_seconds
        )
        #: Whether the current outage has already been reported. Without it a
        #: 5-second poll would engage the same halt twelve times a minute and
        #: bury the first, most useful log line under the rest.
        self._alerted = False

    def evaluate(self, ingestor: StreamIngestor, now: datetime) -> StalenessVerdict:
        """Is the feed too quiet, right now?

        Silence is measured from the latest of three instants, and all three
        matter:

        - the last message actually received — the obvious one;
        - when the current connection came up (`connected_since`), so a worker
          started at 11:00 is not immediately accused of having missed the
          09:30 open it was never running for;
        - the session open, so a feed that died at yesterday's close does not
          register as silent for eighteen hours the moment the bell rings —
          the fifteen hours the market was shut were not an outage.

        Take the earliest of them instead and the watchdog fires on every
        restart and every morning. Take only `last_message_at` and it cannot
        speak at all before the first tick of the day, which is exactly when a
        broken feed most needs reporting.
        """
        if now.tzinfo is None:
            raise ValueError(f"now must be timezone-aware (rule §1.2), got naive {now!r}")
        now = now.astimezone(UTC)

        calendar = self._get_calendar()
        # One lookup rather than `is_open()` followed by `session_on()`: the
        # session object is needed either way, and asking twice leaves a window
        # where the two answers could be reasoned about separately.
        session = calendar.session_on(now.astimezone(calendar.tz).date())
        if session is None or not (session.open_at <= now < session.close_at):
            return StalenessVerdict(
                stale=False,
                silent_for_seconds=None,
                market_open=False,
                reason="market is shut — silence is expected",
            )

        baseline = max(
            [session.open_at]
            + [
                ts
                for ts in (ingestor.stats.last_message_at, ingestor.stats.connected_since)
                if ts is not None
            ]
        )
        silent_for = (now - baseline).total_seconds()
        stale = silent_for > self.max_silence_seconds
        return StalenessVerdict(
            stale=stale,
            silent_for_seconds=silent_for,
            market_open=True,
            reason=(
                f"no market data for {silent_for:.0f}s during the session"
                if stale
                else "feed is current"
            ),
        )

    async def watch(self, ingestor: StreamIngestor) -> None:
        """Poll until cancelled, halting the first time the feed goes quiet.

        Halts once per outage and never clears: engaging is reflexive and
        clearing is deliberate (`risk.killswitch`). When data resumes this
        re-arms so the *next* outage is reported too, and says so — but the halt
        it engaged stays engaged until a human clears it. A watchdog that
        un-halted itself would let a feed flapping every thirty seconds trade
        through every one of the gaps.
        """
        log.info(
            "data.staleness.watching",
            max_silence_seconds=self.max_silence_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
            exchange=self._exchange,
        )
        while True:
            await self._sleep(self.poll_interval_seconds)
            verdict = self.evaluate(ingestor, self._clock.now())

            if verdict.stale and not self._alerted:
                self._alerted = True
                log.critical(
                    "data.staleness.detected",
                    silent_for_seconds=round(verdict.silent_for_seconds or 0.0, 1),
                    max_silence_seconds=self.max_silence_seconds,
                    symbols=sorted(ingestor.stats.symbols),
                )
                self._halt(verdict)
            elif not verdict.stale and self._alerted:
                self._alerted = False
                log.warning(
                    "data.staleness.recovered",
                    msg="market data is flowing again — the halt it engaged is still engaged",
                )

    def _halt(self, verdict: StalenessVerdict) -> None:
        if self.kill_switch is None:
            log.critical(
                "data.staleness.halt_unavailable",
                detail=verdict.reason,
                msg="no kill switch bound — TRADING IS NOT HALTED",
            )
            return
        self.kill_switch.engage(
            HaltScope.GLOBAL,
            HaltReason.DATA_FEED_LOST,
            engaged_by=STALENESS_ACTOR,
            detail=verdict.reason,
        )
        log.critical("data.staleness.halted", detail=verdict.reason)

    def _get_calendar(self) -> TradingCalendar:
        """Built on first use — constructing one imports pandas, and a process
        that never asks about sessions should not pay for it at startup."""
        if self._calendar is None:
            from atp_core.clock import TradingCalendar

            self._calendar = TradingCalendar(self._exchange)
        return self._calendar


async def _sleep_seconds(seconds: float) -> None:
    """The watchdog's poll sleep, wrapped so the injected one has a plain type."""
    await asyncio.sleep(seconds)


def _floor_to_grid(ts: datetime, timeframe: Timeframe) -> datetime:
    """Snap `ts` back to the start of the bar it falls in.

    On the UTC epoch grid, which is where intraday bars sit. For `1d` it lands
    on 00:00 UTC rather than the daily bar's 00:00 New York anchor — earlier
    than the bar either way, so the re-fetch window still contains it
    (docs/DATA.md 'The daily anchor').
    """
    step = timeframe.seconds
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % step, tz=UTC)


def _quote_message(quote: Quote) -> dict[str, Any]:
    """The wire shape `atp_api.ws` forwards to the dashboard.

    Prices as strings, never numbers: JSON has one numeric type and it is a
    float, so serialising a `Decimal` as a number hands the browser a price that
    has already lost precision (rule §1.1).
    """
    return {
        "type": "quote",
        "symbol": quote.symbol,
        "ts": quote.ts.isoformat(),
        "bid": str(quote.bid),
        "ask": str(quote.ask),
        "bid_size": str(quote.bid_size),
        "ask_size": str(quote.ask_size),
    }


def _bar_message(bar: Bar) -> dict[str, Any]:
    return {
        "type": "bar",
        "symbol": bar.symbol,
        "timeframe": bar.timeframe.value,
        "ts": bar.ts.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
    }
