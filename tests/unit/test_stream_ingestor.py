"""The real-time ingestor's fan-out and its reconnect gap backfill.

Pure unit tests. The ingestor only ever talks to ports, so scripted fakes
exercise every decision it makes with no socket, no Redis and no database
(CLAUDE.md §1.7).

The test that matters most is `TestReconnect::test_backfills_before_handling_the
_next_event`: the whole module exists so that a hole in the history is closed
*before* anything downstream reads across it, and that ordering is the one
property no downstream test could detect the loss of.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from atp_core.channels import CHANNEL_BARS, CHANNEL_QUOTES
from atp_core.clock import SimulatedClock
from atp_core.data.ports import FeedReconnected
from atp_core.data.stream import StreamIngestor
from atp_core.domain import Bar, Quote, Timeframe, Trade
from atp_core.errors import DataError, DataGapError
from atp_core.risk.killswitch import HaltReason, HaltRecord, HaltScope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from atp_core.data.ports import StreamEvent

NOW = datetime(2024, 6, 3, 14, 30, 30, tzinfo=UTC)


# ── fakes ───────────────────────────────────────────────────────────────────


def make_bar(symbol: str = "SPY", ts: datetime = NOW, close: str = "100.5") -> Bar:
    """High and low bracket open and close, so a test picking an arbitrary close
    still produces a bar `Bar.__post_init__` accepts."""
    open_ = Decimal("100.25")
    close_ = Decimal(close)
    return Bar(
        symbol=symbol,
        ts=ts,
        timeframe=Timeframe.M1,
        open=open_,
        high=max(open_, close_),
        low=min(open_, close_),
        close=close_,
        volume=Decimal("1000"),
    )


def make_quote(symbol: str = "SPY", ts: datetime = NOW) -> Quote:
    return Quote(
        symbol=symbol,
        ts=ts,
        bid=Decimal("100.10"),
        ask=Decimal("100.12"),
        bid_size=Decimal("3"),
        ask_size=Decimal("5"),
    )


class FakeFeed:
    """Replays a script. Records what was subscribed and when."""

    def __init__(
        self, events: Sequence[StreamEvent] | None = None, raises: Exception | None = None
    ):
        self._events = list(events or ())
        self._raises = raises
        self.subscribed: list[tuple[tuple[str, ...], bool, bool, bool]] = []

    async def subscribe(
        self, symbols: list[str], *, bars: bool = True, quotes: bool = True, trades: bool = False
    ) -> None:
        self.subscribed.append((tuple(symbols), bars, quotes, trades))

    async def unsubscribe(self, symbols: list[str]) -> None:  # pragma: no cover - unused here
        raise AssertionError("the ingestor should not unsubscribe")

    async def stream(self) -> AsyncIterator[StreamEvent]:
        for event in self._events:
            yield event
        if self._raises is not None:
            raise self._raises

    def on_disconnect(self, callback: Any) -> None:  # pragma: no cover - unused here
        pass

    @property
    def is_connected(self) -> bool:
        return True

    @property
    def last_message_at(self) -> datetime | None:
        return None


class FakeQuoteCache:
    def __init__(self) -> None:
        self.quotes: dict[str, Quote] = {}
        self.writes: list[Quote] = []

    async def set_quote(self, quote: Quote) -> None:
        self.quotes[quote.symbol] = quote
        self.writes.append(quote)

    async def get_quote(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: self.quotes[s] for s in symbols if s in self.quotes}


class FakeRepository:
    """Records writes in order — which is what the ordering test reads.

    `stored` is the *newest* bar the table would answer with, per symbol. It
    exists because the reconnect backfill and the staleness watchdog now both
    read storage rather than trusting process-local state: on day 1 three
    crashes reset the feed's gap origin and ~108 bars were lost as a result
    (docs/paper-week/day-1-review.md, F5). A fake that always answered "no bars"
    could not express that failure, and a fake that cannot express a failure
    cannot catch it.
    """

    def __init__(self, stored: dict[str, Bar] | None = None) -> None:
        self.batches: list[list[Bar]] = []
        self.stored = stored or {}
        self.raise_on_read: Exception | None = None

    async def upsert_bars(self, bars: list[Bar]) -> int:
        self.batches.append(list(bars))
        return len(bars)

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        return []

    async def get_last_n_bars(self, symbol: str, timeframe: Timeframe, n: int) -> list[Bar]:
        if self.raise_on_read is not None:
            raise self.raise_on_read
        bar = self.stored.get(symbol)
        return [bar] if bar is not None else []

    async def find_gaps(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        return []

    async def stored_series(self) -> list[tuple[str, Timeframe]]:
        return []


class FakeProvider:
    def __init__(self, *, bars: int = 2, error: Exception | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], Timeframe, datetime, datetime, bool]] = []
        self._bars = bars
        self._error = error

    async def get_bars(
        self,
        symbols: list[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        adjusted: bool = True,
    ) -> dict[str, list[Bar]]:
        self.calls.append((tuple(symbols), timeframe, start, end, adjusted))
        if self._error is not None:
            raise self._error
        return {
            s: [make_bar(s, start + timedelta(minutes=i), close="99.5") for i in range(self._bars)]
            for s in symbols
        }

    async def get_latest_bar(self, symbol: str, timeframe: Timeframe) -> Bar | None:
        return None


class FakePublisher:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self._error = error

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        if self._error is not None:
            raise self._error
        self.published.append((channel, message))


class FakeKillSwitch:
    def __init__(self) -> None:
        self.engaged: list[HaltRecord] = []

    def is_engaged(self, strategy_id: str | None = None, symbol: str | None = None) -> bool:
        return bool(self.engaged)

    def engage(
        self,
        scope: HaltScope,
        reason: HaltReason,
        engaged_by: str,
        detail: str = "",
        target: str | None = None,
    ) -> HaltRecord:
        record = HaltRecord(
            scope=scope,
            reason=reason,
            engaged_at=NOW,
            engaged_by=engaged_by,
            detail=detail,
            target=target,
        )
        self.engaged.append(record)
        return record

    def clear(  # pragma: no cover - unused here
        self, scope: HaltScope, cleared_by: str, target: str | None = None
    ) -> None:
        raise AssertionError("the ingestor must never clear a halt")

    def active_halts(self) -> list[HaltRecord]:
        return list(self.engaged)


def build(
    events: Sequence[StreamEvent] = (),
    *,
    raises: Exception | None = None,
    provider: FakeProvider | None = None,
    publisher: FakePublisher | None = None,
    kill_switch: FakeKillSwitch | None = None,
    repo: FakeRepository | None = None,
    **kwargs: Any,
) -> tuple[StreamIngestor, FakeQuoteCache, FakeRepository, FakeProvider]:
    cache = FakeQuoteCache()
    repo = repo if repo is not None else FakeRepository()
    provider = provider if provider is not None else FakeProvider()
    ingestor = StreamIngestor(
        FakeFeed(events, raises=raises),
        cache,
        repo,
        provider,
        publisher=publisher,
        kill_switch=kill_switch,
        clock=SimulatedClock(NOW),
        **kwargs,
    )
    return ingestor, cache, repo, provider


# ── tests ───────────────────────────────────────────────────────────────────


class TestFanOut:
    async def test_quote_is_cached_and_published_but_not_persisted(self) -> None:
        publisher = FakePublisher()
        ingestor, cache, repo, _ = build([make_quote()], publisher=publisher)

        await ingestor.run(["SPY"])

        assert cache.quotes["SPY"].bid == Decimal("100.10")
        # The volume is enormous and the value is in the latest one only.
        assert repo.batches == []
        assert publisher.published[0][0] == CHANNEL_QUOTES

    async def test_bar_is_persisted_and_published(self) -> None:
        publisher = FakePublisher()
        ingestor, _, repo, _ = build([make_bar()], publisher=publisher)

        await ingestor.run(["SPY"])

        assert [b.ts for b in repo.batches[0]] == [NOW]
        assert publisher.published[0][0] == CHANNEL_BARS

    async def test_prices_are_published_as_strings(self) -> None:
        """A JSON number is a float, and a float price has already lost the
        precision rule §1.1 exists to keep."""
        publisher = FakePublisher()
        ingestor, _, _, _ = build([make_quote(), make_bar()], publisher=publisher)

        await ingestor.run(["SPY"])

        quote_msg = publisher.published[0][1]
        bar_msg = publisher.published[1][1]
        assert quote_msg["bid"] == "100.10"
        assert bar_msg["close"] == "100.5"
        assert all(isinstance(v, str) for v in quote_msg.values())

    async def test_publish_failure_does_not_lose_the_bar(self) -> None:
        publisher = FakePublisher(error=RuntimeError("redis is down"))
        ingestor, cache, repo, _ = build([make_bar(), make_quote()], publisher=publisher)

        await ingestor.run(["SPY"])

        assert repo.batches, "a dead subscriber must not stop the durable write"
        assert cache.quotes["SPY"] is not None

    async def test_runs_without_a_publisher(self) -> None:
        ingestor, cache, repo, _ = build([make_bar(), make_quote()])

        await ingestor.run(["SPY"])

        assert repo.batches and cache.quotes

    async def test_subscribes_to_bars_and_quotes_only(self) -> None:
        ingestor, _, _, _ = build()

        await ingestor.run(["SPY", "AAPL", "SPY"])

        feed = ingestor.feed
        assert isinstance(feed, FakeFeed)
        # Deduplicated, order preserved; trades are volume nothing here reads.
        assert feed.subscribed == [(("SPY", "AAPL"), True, True, False)]

    async def test_trade_events_are_ignored_not_stored(self) -> None:
        trade = Trade(symbol="SPY", ts=NOW, price=Decimal("100"), size=Decimal("10"))
        ingestor, cache, repo, _ = build([trade])

        await ingestor.run(["SPY"])

        assert repo.batches == [] and cache.quotes == {}
        assert ingestor.stats.messages_received == 1

    async def test_stats_track_the_stream(self) -> None:
        ingestor, _, _, _ = build([make_quote(), make_bar()])

        await ingestor.run(["SPY"])

        assert ingestor.stats.messages_received == 2
        assert ingestor.stats.symbols == {"SPY"}
        assert ingestor.stats.last_message_at == NOW


class TestArguments:
    async def test_rejects_no_symbols(self) -> None:
        ingestor, _, _, _ = build()
        with pytest.raises(ValueError, match="no symbols"):
            await ingestor.run([])

    async def test_rejects_lowercase_symbols(self) -> None:
        ingestor, _, _, _ = build()
        with pytest.raises(ValueError, match="uppercase"):
            await ingestor.run(["spy"])


class TestReconnect:
    def reconnect(self, seconds_ago: float = 90.0, attempts: int = 2) -> FeedReconnected:
        return FeedReconnected(
            gap_since=NOW - timedelta(seconds=seconds_ago),
            reconnected_at=NOW,
            attempts=attempts,
        )

    async def test_backfills_before_handling_the_next_event(self) -> None:
        """The point of the whole module.

        The bar that arrives after the reconnect must land in storage *after*
        the bars that closed the gap — otherwise an indicator reading the table
        between the two sees a hole that is about to be filled and computes a
        number nothing downstream can tell is wrong.
        """
        live = make_bar(ts=NOW, close="123.75")
        ingestor, _, repo, provider = build([self.reconnect(), live])

        await ingestor.run(["SPY"])

        assert provider.calls, "a reconnect that fetches nothing has not closed the gap"
        written = [bar for batch in repo.batches for bar in batch]
        assert written[-1].close == Decimal("123.75")
        assert len(written) > 1

    async def test_backfill_window_starts_on_the_bar_grid(self) -> None:
        """A drop at 14:29:00 lost part of that minute's bar and the socket will
        never re-send it, so the refetch has to start at the bar's open."""
        ingestor, _, _, provider = build([self.reconnect(seconds_ago=90)])

        await ingestor.run(["SPY"])

        _, timeframe, start, end, adjusted = provider.calls[0]
        assert start == datetime(2024, 6, 3, 14, 29, tzinfo=UTC)
        # Stops at the last *completed* bar: the one in progress is not missing,
        # the feed will deliver it whole when it closes.
        assert end == datetime(2024, 6, 3, 14, 30, tzinfo=UTC)
        assert timeframe is Timeframe.M1
        # Raw: this is the live path, and it halves the requests.
        assert adjusted is False

    async def test_gap_inside_one_bar_fetches_nothing(self) -> None:
        """A blip is not a data incident. Dropped at 14:30:20 and back at
        14:30:30, the 14:30 bar still arrives over the socket intact."""
        ingestor, _, _, provider = build([self.reconnect(seconds_ago=10)])

        await ingestor.run(["SPY"])

        assert provider.calls == []
        assert ingestor.stats.reconnects == 1

    async def test_long_gap_is_truncated_and_the_rest_left_to_the_sweep(self) -> None:
        ingestor, _, _, provider = build(
            [self.reconnect(seconds_ago=86_400)],
            max_reconnect_backfill=timedelta(hours=2),
        )

        await ingestor.run(["SPY"])

        _, _, start, _, _ = provider.calls[0]
        assert start == datetime(2024, 6, 3, 12, 30, tzinfo=UTC)

    async def test_counts_the_reconnect_and_the_bars_it_recovered(self) -> None:
        ingestor, _, _, _ = build([self.reconnect()], provider=FakeProvider(bars=3))

        await ingestor.run(["SPY"])

        assert ingestor.stats.reconnects == 1
        assert ingestor.stats.gaps_backfilled == 3
        assert ingestor.stats.connected_since == NOW

    async def test_backfills_every_subscribed_symbol(self) -> None:
        ingestor, _, _, provider = build([self.reconnect()])

        await ingestor.run(["SPY", "AAPL"])

        assert provider.calls[0][0] == ("SPY", "AAPL")

    async def test_symbol_with_no_bars_in_the_gap_is_not_an_error(self) -> None:
        """Ordinary on an illiquid name: Alpaca emits no bar for a minute in
        which nothing traded."""
        ingestor, _, _, _ = build(
            [self.reconnect()], provider=FakeProvider(error=DataGapError("nothing there"))
        )

        await ingestor.run(["SPY"])

        assert ingestor.stats.reconnects == 1


class TestHalting:
    async def test_feed_giving_up_halts_trading_and_propagates(self) -> None:
        switch = FakeKillSwitch()
        ingestor, _, _, _ = build(
            [make_bar()], raises=DataError("stream did not come back"), kill_switch=switch
        )

        with pytest.raises(DataError):
            await ingestor.run(["SPY"])

        assert [r.reason for r in switch.engaged] == [HaltReason.DATA_FEED_LOST]
        assert switch.engaged[0].scope is HaltScope.GLOBAL

    async def test_feed_loss_without_a_kill_switch_still_propagates(self) -> None:
        ingestor, _, _, _ = build(raises=DataError("stream did not come back"))

        with pytest.raises(DataError):
            await ingestor.run(["SPY"])

    async def test_failed_gap_backfill_halts_but_keeps_ingesting(self) -> None:
        """The stream is healthy; the history is not.

        Trading through a known hole is the failure this module exists to
        prevent — but taking the quote cache and the dashboard down with it
        would buy nothing.
        """
        switch = FakeKillSwitch()
        ingestor, cache, _, _ = build(
            [
                FeedReconnected(
                    gap_since=NOW - timedelta(minutes=5), reconnected_at=NOW, attempts=1
                ),
                make_quote(),
            ],
            provider=FakeProvider(error=DataError("Alpaca /v2/stocks/bars failed")),
            kill_switch=switch,
        )

        await ingestor.run(["SPY"])

        assert [r.reason for r in switch.engaged] == [HaltReason.DATA_FEED_LOST]
        assert cache.quotes["SPY"] is not None, "ingestion must continue while halted"

    async def test_halt_detail_names_the_window_that_is_missing(self) -> None:
        switch = FakeKillSwitch()
        ingestor, _, _, _ = build(
            [FeedReconnected(gap_since=NOW - timedelta(minutes=5), reconnected_at=NOW, attempts=1)],
            provider=FakeProvider(error=DataError("boom")),
            kill_switch=switch,
        )

        await ingestor.run(["SPY"])

        assert "2024-06-03T14:25:00+00:00" in switch.engaged[0].detail
        assert switch.engaged[0].engaged_by == "stream_ingestor"


class TestTheStorageWatermark:
    """F5 and F7. Every field on `IngestorStats` dies with the process, and on
    day 1 the process died three times in 158 seconds. Storage is the one
    witness a restart cannot reset (docs/paper-week/day-1-review.md)."""

    async def test_it_is_read_at_startup(self) -> None:
        stored = make_bar(ts=NOW - timedelta(minutes=8))
        ingestor, _, _, _ = build(repo=FakeRepository({"SPY": stored}))

        await ingestor.run(["SPY"])

        # The bar's *close*, not its open: a bar is stamped at its open, so
        # reporting the open would make every healthy restart look one bar
        # stale to a watchdog whose budget is a minute.
        assert ingestor.stats.storage_watermark == stored.ts + timedelta(minutes=1)

    async def test_it_takes_the_newest_across_the_watchlist(self) -> None:
        """The maximum, and the alternative is the trap: a symbol that simply
        did not print in a minute produces no bar at all on IEX, so a minimum
        would treat the least liquid name as a permanent outage."""
        repo = FakeRepository(
            {
                "SPY": make_bar("SPY", ts=NOW - timedelta(minutes=30)),
                "QQQ": make_bar("QQQ", ts=NOW - timedelta(minutes=2)),
            }
        )
        ingestor, _, _, _ = build(repo=repo)

        await ingestor.run(["SPY", "QQQ"])

        assert ingestor.stats.storage_watermark == NOW - timedelta(minutes=1)

    async def test_an_empty_table_leaves_it_unset(self) -> None:
        ingestor, _, _, _ = build()

        await ingestor.run(["SPY"])

        assert ingestor.stats.storage_watermark is None

    async def test_an_unreadable_store_does_not_stop_ingestion(self) -> None:
        """This runs on the startup path of the process that owns the
        market-data connection. Refusing to ingest because a watermark could not
        be read would trade a degraded signal for no data at all."""
        repo = FakeRepository()
        repo.raise_on_read = ConnectionError("the table is gone")
        ingestor, _, _, _ = build(repo=repo)

        await ingestor.run(["SPY"])

        assert ingestor.stats.storage_watermark is None


class TestARestartCannotShrinkAGap:
    """F5, stated as the incident. The feed measures `gap_since` from *this
    process's* stream start, so three crashes inside an eight-minute outage each
    reset the origin — and the fourth worker asked for a one-minute window
    against an eight-minute hole. It succeeded by its own definition, fired none
    of `backfill_failed`, `backfill_skipped` or `backfill_truncated`, and ~108
    bars are permanently absent."""

    async def test_storage_widens_a_gap_the_feed_understates(self) -> None:
        stored = make_bar(ts=NOW - timedelta(minutes=8))
        reconnect = FeedReconnected(
            # What a restarted process believes: a 23-second blip.
            gap_since=NOW - timedelta(seconds=23),
            reconnected_at=NOW,
            attempts=3,
        )
        provider = FakeProvider()
        ingestor, _, _, _ = build(
            [reconnect], provider=provider, repo=FakeRepository({"SPY": stored})
        )

        await ingestor.run(["SPY"])

        (_, _, start, _, _) = provider.calls[0]
        assert start <= stored.ts + timedelta(minutes=1), (
            "the refetch must start where storage stops, not where this process booted"
        )

    async def test_a_gap_the_feed_states_correctly_is_left_alone(self) -> None:
        """Storage is a second opinion, not a replacement. When the feed's
        origin is the earlier one — the ordinary case — it wins."""
        stored = make_bar(ts=NOW - timedelta(minutes=1))
        reconnect = FeedReconnected(
            gap_since=NOW - timedelta(minutes=20),
            reconnected_at=NOW,
            attempts=1,
        )
        provider = FakeProvider()
        ingestor, _, _, _ = build(
            [reconnect], provider=provider, repo=FakeRepository({"SPY": stored})
        )

        await ingestor.run(["SPY"])

        (_, _, start, _, _) = provider.calls[0]
        assert start == NOW.replace(second=0, microsecond=0) - timedelta(minutes=20)

    async def test_a_stale_watermark_does_not_widen_a_short_blip(self) -> None:
        """The regression the first draft of this fix would have shipped.

        The watermark is read once at startup, so six hours into a healthy
        session it is six hours old — and comparing a thirty-second blip against
        it alone would turn every reconnect into a six-hour refetch. What this
        process has actually *seen* is the better witness whenever it exists;
        the day-1 case is exactly the one where it does not, because worker #4
        had received nothing at all by the time it reconnected.
        """
        stored = make_bar(ts=NOW - timedelta(hours=6))
        events: list[StreamEvent] = [
            make_bar(ts=NOW - timedelta(seconds=30)),
            FeedReconnected(gap_since=NOW - timedelta(seconds=20), reconnected_at=NOW, attempts=1),
        ]
        provider = FakeProvider()
        ingestor, _, _, _ = build(events, provider=provider, repo=FakeRepository({"SPY": stored}))

        await ingestor.run(["SPY"])

        assert provider.calls == [], (
            "a blip inside one bar must still cost nothing, however old the watermark is"
        )

    async def test_a_successful_reconnect_marks_the_data_current_again(self) -> None:
        """Otherwise the watchdog would read the pre-outage `last_message_at`
        and halt a feed that has just come back and been backfilled."""
        reconnect = FeedReconnected(
            gap_since=NOW - timedelta(minutes=5), reconnected_at=NOW, attempts=2
        )
        ingestor, _, _, _ = build([reconnect])

        await ingestor.run(["SPY"])

        assert ingestor.stats.storage_watermark == NOW

    async def test_a_failed_backfill_does_not_claim_the_data_is_current(self) -> None:
        """A halt is engaged and the hole is still there. Advancing the
        watermark would be the false 'recovered' this whole change is about."""
        switch = FakeKillSwitch()
        reconnect = FeedReconnected(
            gap_since=NOW - timedelta(minutes=5), reconnected_at=NOW, attempts=2
        )
        ingestor, _, _, _ = build(
            [reconnect],
            provider=FakeProvider(error=DataError("the provider is down")),
            kill_switch=switch,
        )

        await ingestor.run(["SPY"])

        assert ingestor.stats.storage_watermark is None
        assert switch.engaged, "a gap that could not be closed must halt trading"
