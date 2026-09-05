"""Alpaca's market-data WebSocket adapter.

No socket is opened: the connection factory is injected, so the handshake, the
reconnect ladder and the parser are all driven from a scripted fake
(CLAUDE.md §1.7). The frames below are shaped like Alpaca's real ones — arrays
of tagged messages, prices as JSON numbers — because how those numbers are
parsed is one of the things under test.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from atp_core import ws
from atp_core.config import Settings
from atp_core.data.ports import FeedReconnected
from atp_core.data.providers.alpaca import AlpacaRealtimeFeed
from atp_core.domain import Bar, Quote, Timeframe, Trade
from atp_core.errors import DataError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from atp_core.data.ports import StreamEvent

CONNECTED = {"T": "success", "msg": "connected"}
AUTHENTICATED = {"T": "success", "msg": "authenticated"}

QUOTE_MSG = {
    "T": "q",
    "S": "SPY",
    "bp": 439.71,
    "bs": 3,
    "ap": 439.73,
    "as": 5,
    "t": "2024-06-03T14:30:00.123456Z",
}
BAR_MSG = {
    "T": "b",
    "S": "SPY",
    "o": 439.70,
    "h": 439.90,
    "l": 439.60,
    "c": 439.85,
    "v": 12_345,
    "n": 87,
    "vw": 439.77,
    "t": "2024-06-03T14:30:00Z",
}
TRADE_MSG = {
    "T": "t",
    "S": "SPY",
    "p": 439.80,
    "s": 100,
    "c": ["@"],
    "t": "2024-06-03T14:30:00.5Z",
}

# ── frames captured verbatim from the live IEX stream, 2026-08-17 14:11 UTC ──
#
# The three above were written from Alpaca's documentation. These were pasted
# unedited off the wire, vendor fields and all — see `TestRealWireFormat`.

#: `bx`/`ax` (quoting exchange), `z` (tape) and `c` (quote condition) are fields
#: the parser ignores. A parser that rejected unknown keys rather than ignoring
#: them would break on every real message.
REAL_QUOTE_MSG = {
    "T": "q",
    "S": "SPY",
    "bx": "V",
    "bp": 775.29,
    "bs": 40,
    "ax": "V",
    "ap": 775.58,
    "as": 240,
    "c": ["R"],
    "z": "B",
    "t": "2026-08-17T14:11:05.168570617Z",
}
REAL_TRADE_MSG = {
    "T": "t",
    "S": "SPY",
    "i": 52983562843699,
    "x": "V",
    "p": 775.545,
    "s": 100,
    "c": [" "],
    "z": "B",
    "t": "2026-08-17T14:11:06.103625135Z",
}
REAL_BAR_MSG = {
    "T": "b",
    "S": "SPY",
    "o": 775.59,
    "h": 775.62,
    "l": 775.425,
    "c": 775.425,
    "v": 3407,
    "t": "2026-08-17T14:11:00Z",
    "n": 43,
    "vw": 775.528336,
}

#: The server confirms channels nobody asked for: `corrections` and
#: `cancelErrors` come back on any subscription.
REAL_SUBSCRIPTION_MSG = {
    "T": "subscription",
    "trades": ["SPY"],
    "quotes": ["SPY"],
    "bars": ["SPY"],
    "corrections": ["SPY"],
    "cancelErrors": ["SPY"],
}


class DroppedError(Exception):
    """Stands in for `websockets.ConnectionClosed`, which the adapter only ever
    sees as "something went wrong on this socket"."""


class FakeConnection:
    """Hands back scripted frames, then whatever ends the connection.

    A script entry is either a list of messages (one frame) or an exception to
    raise from `recv`.
    """

    def __init__(self, script: Sequence[Any]) -> None:
        self._script = list(script)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        if not self._script:
            raise DroppedError("script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def close(self) -> None:
        self.closed = True


class FakeClock:
    """Advances a fixed step per read, so timestamps in assertions are exact.

    `advance` is the other half, used by `build`'s fake sleep: the reconnect
    budget is measured in elapsed time, so a ladder test needs waiting to *be*
    the passage of time. Those tests pass `step=timedelta(0)` as well, which
    makes the elapsed total exactly the sum of the waits rather than the waits
    plus however many times the loop happened to read the clock.
    """

    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        self._now = start
        self._step = step

    def now(self) -> datetime:
        current = self._now
        self._now += self._step
        return current

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def make_settings() -> Settings:
    return Settings(
        alpaca_api_key=SecretStr("test-key-id"),
        alpaca_api_secret=SecretStr("test-secret"),
        alpaca_stream_url="wss://stream.data.alpaca.markets/v2/iex",
        alpaca_data_feed="iex",
    )


def ladder_clock() -> FakeClock:
    """A clock that moves only when the ladder waits.

    The default `FakeClock` steps a second per *read*, which is what makes
    timestamp assertions elsewhere exact — and which would quietly inflate the
    reconnect budget here by however many times the loop consulted it. With a
    zero step, elapsed time is exactly the sum of the waits.
    """
    return FakeClock(datetime(2024, 6, 3, 14, 30, tzinfo=UTC), step=timedelta(0))


def build(
    *connections: FakeConnection,
    reconnect_budget_seconds: float = 0.0,
    clock: FakeClock | None = None,
) -> tuple[AlpacaRealtimeFeed, list[float], list[FakeConnection]]:
    """A feed wired to a queue of connections. Returns it, the sleeps it asked
    for, and the connections it was handed.

    The fake sleep advances the clock, because the reconnect budget is elapsed
    time (docs/paper-week/day-1-review.md, F6): waiting has to move the clock or
    a fifteen-minute budget would never expire in a test and never be tested.
    """
    queue = list(connections)
    handed: list[FakeConnection] = []
    slept: list[float] = []
    the_clock = clock or FakeClock(datetime(2024, 6, 3, 14, 30, tzinfo=UTC))

    async def connect(url: str) -> FakeConnection:
        if not queue:
            raise DroppedError("nothing left to connect to")
        connection = queue.pop(0)
        handed.append(connection)
        return connection

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        the_clock.advance(seconds)

    feed = AlpacaRealtimeFeed(
        make_settings(),
        connect=connect,
        sleep=sleep,
        clock=the_clock,
        rng=random.Random(0),
        reconnect_budget_seconds=reconnect_budget_seconds,
    )
    return feed, slept, handed


async def collect(
    feed: AlpacaRealtimeFeed, limit: int = 50
) -> tuple[list[StreamEvent], DataError | None]:
    """Consume the stream to its end, and hand back whatever ended it.

    A reconnecting feed never finishes cleanly — that is the point of it — so a
    scripted fake ends by running out of connections, and the adapter reports
    that as the `DataError` returned here. Tests about a failure assert on it;
    tests about the happy path ignore it.
    """
    events: list[StreamEvent] = []
    stream = feed.stream()
    try:
        async for event in stream:
            events.append(event)
            if len(events) >= limit:  # pragma: no cover - a failing test's net
                break
    except DataError as exc:
        return events, exc
    finally:
        # `DataFeed.stream` promises only an `AsyncIterator` — deliberately, see
        # its docstring — while every implementation is an async generator that
        # must be closed or it leaks the connection.
        await stream.aclose()  # type: ignore[attr-defined]
    return events, None


class TestHandshake:
    async def test_authenticates_then_subscribes(self) -> None:
        connection = FakeConnection([[CONNECTED], [AUTHENTICATED], [QUOTE_MSG]])
        feed, _, _ = build(connection)
        await feed.subscribe(["SPY"], bars=True, quotes=True, trades=False)

        await collect(feed)

        auth, subscribe = connection.sent
        assert auth["action"] == "auth"
        assert auth["key"] == "test-key-id"
        assert subscribe == {"action": "subscribe", "bars": ["SPY"], "quotes": ["SPY"]}
        # trades=False must not appear at all: an unwanted subscription is
        # bandwidth and a symbol-limit charge for data nothing reads.
        assert "trades" not in subscribe

    async def test_bad_credentials_raise_rather_than_retry(self) -> None:
        """Reconnecting would just present the same rejected key again."""
        refusal = [{"T": "error", "code": 402, "msg": "auth failed"}]
        feed, slept, handed = build(FakeConnection([[CONNECTED], refusal]))
        await feed.subscribe(["SPY"])

        _, error = await collect(feed)

        assert error is not None and "402" in str(error)
        assert slept == [], "a permanent refusal must not back off and try again"
        assert len(handed) == 1

    async def test_connection_limit_is_permanent(self) -> None:
        """One process owns the upstream connection. A second one racing the
        first is worse than a second one that fails loudly."""
        taken = [{"T": "error", "code": 406, "msg": "connection limit exceeded"}]
        feed, _, handed = build(FakeConnection([[CONNECTED], taken]))
        await feed.subscribe(["SPY"])

        _, error = await collect(feed)

        assert error is not None and "406" in str(error)
        assert len(handed) == 1

    async def test_secrets_never_reach_the_error(self) -> None:
        refusal = [{"T": "error", "code": 402, "msg": "auth failed"}]
        feed, _, _ = build(FakeConnection([[CONNECTED], refusal]))
        await feed.subscribe(["SPY"])

        _, error = await collect(feed)

        assert error is not None
        assert "test-secret" not in str(error)

    async def test_handshake_failure_closes_the_socket(self) -> None:
        """A half-open socket left behind still counts against the one-connection
        limit — the retry would then be refused by our own leak."""
        connection = FakeConnection([[CONNECTED], [{"T": "error", "code": 402, "msg": "nope"}]])
        feed, _, _ = build(connection)
        await feed.subscribe(["SPY"])

        _, error = await collect(feed)

        assert error is not None
        assert connection.closed

    async def test_subscription_is_batched_by_frame(self) -> None:
        symbols = [f"SYM{i:04d}" for i in range(600)]
        connection = FakeConnection([[CONNECTED], [AUTHENTICATED]])
        feed, _, _ = build(connection)
        await feed.subscribe(symbols, bars=True, quotes=False, trades=False)

        await collect(feed)

        frames = [m for m in connection.sent if m.get("action") == "subscribe"]
        assert len(frames) == 3
        assert sum(len(f["bars"]) for f in frames) == 600


class TestParsing:
    async def test_quote_bar_and_trade(self) -> None:
        feed, _, _ = build(
            FakeConnection([[CONNECTED], [AUTHENTICATED], [QUOTE_MSG, BAR_MSG, TRADE_MSG]])
        )
        await feed.subscribe(["SPY"], trades=True)

        (quote, bar, trade), _ = await collect(feed)

        assert isinstance(quote, Quote)
        assert quote.bid == Decimal("439.71") and quote.ask_size == Decimal("5")
        assert isinstance(bar, Bar)
        assert bar.timeframe is Timeframe.M1
        assert bar.close == Decimal("439.85") and bar.trade_count == 87
        assert isinstance(trade, Trade)
        assert trade.price == Decimal("439.80") and trade.conditions == ("@",)

    async def test_prices_are_exact_not_floats(self) -> None:
        """`Decimal(0.1)` is 0.1000000000000000055511151231257827. Parsing the
        frame with `parse_float=Decimal` is what keeps rule §1.1 true here."""
        message = {**QUOTE_MSG, "bp": 0.1, "ap": 0.3}
        feed, _, _ = build(FakeConnection([[CONNECTED], [AUTHENTICATED], [message]]))
        await feed.subscribe(["SPY"])

        (quote,), _ = await collect(feed)

        assert isinstance(quote, Quote)
        assert quote.bid == Decimal("0.1")
        assert quote.ask - quote.bid == Decimal("0.2")

    async def test_bar_timestamp_is_utc(self) -> None:
        feed, _, _ = build(FakeConnection([[CONNECTED], [AUTHENTICATED], [BAR_MSG]]))
        await feed.subscribe(["SPY"])

        (bar,), _ = await collect(feed)

        assert isinstance(bar, Bar)
        assert bar.ts == datetime(2024, 6, 3, 14, 30, tzinfo=UTC)

    async def test_malformed_message_is_dropped_not_fatal(self) -> None:
        """One bad print must not take down the connection every other symbol
        is riding on — but the good messages either side of it must survive."""
        impossible = {**BAR_MSG, "S": "AAPL", "l": 500.0}  # low above high
        feed, _, _ = build(
            FakeConnection([[CONNECTED], [AUTHENTICATED], [QUOTE_MSG, impossible, TRADE_MSG]])
        )
        await feed.subscribe(["SPY"], trades=True)

        events, _ = await collect(feed)

        assert [type(e).__name__ for e in events] == ["Quote", "Trade"]

    async def test_unknown_tags_are_ignored(self) -> None:
        feed, _, _ = build(
            FakeConnection(
                [
                    [CONNECTED],
                    [AUTHENTICATED],
                    [{"T": "subscription", "bars": ["SPY"], "quotes": ["SPY"]}],
                    [{"T": "d", "S": "SPY"}],
                    [QUOTE_MSG],
                ]
            )
        )
        await feed.subscribe(["SPY"])

        events, _ = await collect(feed)

        assert [type(e).__name__ for e in events] == ["Quote"]


class TestRealWireFormat:
    """Frames captured verbatim from the live IEX stream, 2026-08-17 14:11 UTC.

    Every other fixture in this file was written from Alpaca's documentation,
    which is how they came to differ from the wire in three ways that all look
    like nothing and are not. A socket was held open during the session, and the
    `REAL_*_MSG` fixtures above are what actually arrived, pasted unedited —
    vendor fields and all — so that the next person to touch the parser is
    checked against the feed rather than against our reading of the docs.

    The adapter already handles all three. These pin that, because each is a
    change someone could make while the rest of this file stayed green — as one
    did: a `strptime`-based parser accepting an optional six-digit fraction
    passes every other test in this suite and rejects every real quote.
    """

    async def collect_real(self) -> list[StreamEvent]:
        feed, _, _ = build(
            FakeConnection(
                [
                    [CONNECTED],
                    [AUTHENTICATED],
                    [REAL_QUOTE_MSG, REAL_TRADE_MSG, REAL_BAR_MSG],
                ]
            )
        )
        await feed.subscribe(["SPY"], trades=True)
        events, _ = await collect(feed)
        return events

    async def test_nanosecond_timestamps_are_truncated_not_rejected(self) -> None:
        """Quotes and trades are stamped to the nanosecond — nine fractional
        digits — while bars carry none at all.

        `datetime` holds microseconds, so the extra three digits have to go
        somewhere. `fromisoformat` truncates, which is the right direction: a
        quote is an observation at an instant, and rounding it *up* would move
        it to a microsecond it was not observed in. The wrong `%f`-based
        `strptime` accepts at most six digits and raises on all of these, and
        every other fixture in this file is six or fewer — so that regression
        would break every live message and pass this file without this test.
        """
        quote, trade, bar = await self.collect_real()

        assert isinstance(quote, Quote)
        assert quote.ts == datetime(2026, 8, 17, 14, 11, 5, 168570, tzinfo=UTC)
        assert quote.ts.microsecond == 168570, "truncated, not rounded to 168571"
        assert isinstance(trade, Trade)
        assert trade.ts == datetime(2026, 8, 17, 14, 11, 6, 103625, tzinfo=UTC)
        # A streamed bar is stamped at its open, to the second, no fraction.
        assert isinstance(bar, Bar)
        assert bar.ts == datetime(2026, 8, 17, 14, 11, tzinfo=UTC)

    async def test_sub_cent_prices_survive_exactly(self) -> None:
        """Real prices are not two-decimal. This bar's low and close are
        775.425 and its VWAP carries six decimals; the trade printed at
        775.545. Rule §1.1 is only kept here by `parse_float=Decimal`, and a
        half-cent is exactly the magnitude a float would start losing."""
        quote, trade, bar = await self.collect_real()

        assert isinstance(bar, Bar)
        assert bar.low == Decimal("775.425")
        assert bar.close == Decimal("775.425")
        assert bar.vwap == Decimal("775.528336")
        assert isinstance(trade, Trade)
        assert trade.price == Decimal("775.545")
        # The half-cent is real: it is not representable as a 2dp price.
        assert bar.high - bar.low == Decimal("0.195")
        assert isinstance(quote, Quote)
        assert quote.ask - quote.bid == Decimal("0.29")

    async def test_vendor_fields_are_ignored_and_sizes_are_decimal(self) -> None:
        """`bx`, `ax`, `z`, `i` and `x` are not read. Sizes arrive as JSON
        integers and must still become `Decimal` — a quantity is rule §1.1's
        other half, not just a price."""
        quote, trade, bar = await self.collect_real()

        assert isinstance(quote, Quote)
        assert quote.bid_size == Decimal("40")
        assert quote.ask_size == Decimal("240")
        assert isinstance(quote.bid_size, Decimal)
        assert isinstance(trade, Trade)
        assert trade.size == Decimal("100")
        # A trade condition can be a single space — real, and not a reason to
        # drop the print.
        assert trade.conditions == (" ",)
        assert isinstance(bar, Bar)
        assert bar.volume == Decimal("3407")
        assert bar.trade_count == 43
        # A streamed bar has no corporate-action history behind it yet.
        assert bar.adj_close is None

    async def test_real_subscription_confirmation_yields_no_events(self) -> None:
        """The server confirms channels we never asked for — `corrections` and
        `cancelErrors` come back on any subscription. That frame is
        acknowledgement, not data, and must not reach the ingestor."""
        feed, _, _ = build(
            FakeConnection(
                [[CONNECTED], [AUTHENTICATED], [REAL_SUBSCRIPTION_MSG], [REAL_QUOTE_MSG]]
            )
        )
        await feed.subscribe(["SPY"])

        events, _ = await collect(feed)

        assert [type(e).__name__ for e in events] == ["Quote"]


class TestReconnect:
    def script(self, *tail: Any) -> list[Any]:
        return [[CONNECTED], [AUTHENTICATED], *tail]

    async def test_resubscribes_and_announces_the_gap(self) -> None:
        first = FakeConnection(self.script([QUOTE_MSG], DroppedError("socket closed")))
        second = FakeConnection(self.script([BAR_MSG]))
        feed, _, _ = build(first, second)
        await feed.subscribe(["SPY"], bars=True, quotes=True)

        events, _ = await collect(feed)

        assert [type(e).__name__ for e in events] == ["Quote", "FeedReconnected", "Bar"]
        # The gap must start at the last message we actually saw — 14:30:01,
        # when the quote arrived — and not at 14:30:00 when the connection
        # opened. Everything between those two instants is known good; only
        # what came after the quote is missing.
        reconnected = events[1]
        assert isinstance(reconnected, FeedReconnected)
        assert reconnected.gap_since == datetime(2024, 6, 3, 14, 30, 1, tzinfo=UTC)
        assert reconnected.reconnected_at == datetime(2024, 6, 3, 14, 30, 2, tzinfo=UTC)
        assert reconnected.attempts == 1
        # The whole point of holding the subscription set: it comes back up
        # subscribed to what it went down with.
        assert [m for m in second.sent if m.get("action") == "subscribe"] == [
            {"action": "subscribe", "bars": ["SPY"], "quotes": ["SPY"]}
        ]

    async def test_no_reconnect_event_on_the_first_connection(self) -> None:
        feed, _, _ = build(FakeConnection(self.script([QUOTE_MSG])))
        await feed.subscribe(["SPY"])

        events, _ = await collect(feed)

        assert not any(isinstance(e, FeedReconnected) for e in events)

    async def test_backs_off_exponentially_and_gives_up_on_elapsed_time(self) -> None:
        """The ladder shape is unchanged; what ends it is now a duration.

        An attempt ceiling made "how long will this keep trying" a question you
        had to answer by integrating the backoff schedule by hand — and on day 1
        the answer was about four minutes against a seven-minute outage
        (docs/paper-week/day-1-review.md, F6).
        """
        feed, slept, _ = build(  # no connections at all
            reconnect_budget_seconds=120.0, clock=ladder_clock()
        )
        await feed.subscribe(["SPY"])

        _, error = await collect(feed)

        assert error is not None and "did not come back within 120s" in str(error)
        assert sum(slept) >= 120.0, "it must not give up before the budget is spent"
        # Jittered into [d/2, d], so the ladder is bounded rather than exact —
        # an assertion on exact delays would be testing `random`.
        for delay, unjittered in zip(slept[:3], [1.0, 2.0, 4.0], strict=True):
            assert unjittered / 2 <= delay <= unjittered

    async def test_backoff_is_capped(self) -> None:
        feed, slept, _ = build(reconnect_budget_seconds=600.0, clock=ladder_clock())
        await feed.subscribe(["SPY"])

        _, error = await collect(feed)

        assert error is not None
        assert max(slept) <= ws.BACKOFF_MAX_SECONDS

    async def test_a_seven_minute_venue_outage_is_survived(self) -> None:
        """The day-1 regression, on the feed side.

        Alpaca was unreachable for roughly seven minutes. This stream gave up
        about four minutes in and took the worker with it — and each restart
        reset the gap marker, which is how a seven-minute outage became ~108
        permanently missing bars (F5) with the staleness clock reset under it
        (F7). Here the venue comes back after seven minutes and the feed is
        still trying.
        """
        failures = [FakeConnection(self.script(DroppedError("closed"))) for _ in range(25)]
        good = FakeConnection(self.script([BAR_MSG]))
        # The shipped default, not a test value: the claim is that what actually
        # runs in production is enough to outlast the outage that broke day 1.
        feed, slept, _ = build(
            *failures,
            good,
            reconnect_budget_seconds=ws.RECONNECT_BUDGET_SECONDS,
            clock=ladder_clock(),
        )
        await feed.subscribe(["SPY"])

        events, _ = await collect(feed)

        assert sum(slept) >= 7 * 60, "the outage must actually have been waited out"
        assert any(isinstance(e, FeedReconnected) for e in events)

    async def test_a_connection_that_delivers_resets_the_ladder(self) -> None:
        first = FakeConnection(self.script([QUOTE_MSG], DroppedError("closed")))
        second = FakeConnection(self.script([QUOTE_MSG], DroppedError("closed")))
        third = FakeConnection(self.script([BAR_MSG]))
        feed, slept, _ = build(first, second, third)
        await feed.subscribe(["SPY"])

        await collect(feed)

        # Two clean reconnects, each on the first attempt: nothing to sleep for.
        assert slept == []

    async def test_a_connection_that_never_delivers_keeps_backing_off(self) -> None:
        """A server that accepts and immediately hangs up — a connection-limit
        fight, a flapping upstream — must not become a hot loop."""
        flapping = [FakeConnection(self.script(DroppedError("closed"))) for _ in range(4)]
        feed, slept, _ = build(*flapping, reconnect_budget_seconds=120.0, clock=ladder_clock())
        await feed.subscribe(["SPY"])

        _, error = await collect(feed)

        assert error is not None
        assert slept, "attempts must not reset on a connection that delivered nothing"

    async def test_transient_server_error_reconnects(self) -> None:
        """'Slow client' is the server hanging up on us in words rather than at
        the socket. Coming back is the right answer."""
        slow = [{"T": "error", "code": 407, "msg": "slow client"}]
        first = FakeConnection(self.script([QUOTE_MSG], slow))
        second = FakeConnection(self.script([BAR_MSG]))
        feed, _, handed = build(first, second)
        await feed.subscribe(["SPY"])

        events, _ = await collect(feed)

        assert len(handed) == 2
        assert isinstance(events[1], FeedReconnected)

    async def test_disconnect_callbacks_fire_and_cannot_break_the_loop(self) -> None:
        seen: list[str] = []
        first = FakeConnection(self.script([QUOTE_MSG], DroppedError("closed")))
        second = FakeConnection(self.script([BAR_MSG]))
        feed, _, _ = build(first, second)
        feed.on_disconnect(lambda exc: seen.append(str(exc)))
        feed.on_disconnect(lambda exc: (_ for _ in ()).throw(RuntimeError("handler is broken")))
        await feed.subscribe(["SPY"])

        events, _ = await collect(feed)

        assert "closed" in seen
        # The second handler raises on every call. If that could escape, the
        # reconnect would never happen and this bar would never arrive.
        assert any(isinstance(e, Bar) for e in events)

    async def test_the_socket_is_closed_when_the_consumer_stops(self) -> None:
        connection = FakeConnection(self.script([QUOTE_MSG, BAR_MSG, TRADE_MSG]))
        feed, _, _ = build(connection)
        await feed.subscribe(["SPY"])

        stream = feed.stream()
        async for _ in stream:
            break
        await stream.aclose()  # type: ignore[attr-defined]  # as in `collect`, above

        assert connection.closed
        assert feed.is_connected is False


class TestSubscriptions:
    async def test_rejects_lowercase_symbols(self) -> None:
        feed, _, _ = build()
        with pytest.raises(ValueError, match="uppercase"):
            await feed.subscribe(["spy"])

    async def test_unsubscribe_drops_the_symbol_from_the_replay(self) -> None:
        connection = FakeConnection([[CONNECTED], [AUTHENTICATED]])
        feed, _, _ = build(connection)
        await feed.subscribe(["SPY", "AAPL"])
        await feed.unsubscribe(["AAPL"])

        await collect(feed)

        (subscribe,) = [m for m in connection.sent if m.get("action") == "subscribe"]
        assert subscribe["bars"] == ["SPY"]
