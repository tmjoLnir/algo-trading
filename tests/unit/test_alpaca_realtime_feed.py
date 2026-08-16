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
    """Advances a fixed step per read, so timestamps in assertions are exact."""

    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        self._now = start
        self._step = step

    def now(self) -> datetime:
        current = self._now
        self._now += self._step
        return current


def make_settings() -> Settings:
    return Settings(
        alpaca_api_key=SecretStr("test-key-id"),
        alpaca_api_secret=SecretStr("test-secret"),
        alpaca_stream_url="wss://stream.data.alpaca.markets/v2/iex",
        alpaca_data_feed="iex",
    )


def build(
    *connections: FakeConnection,
    max_reconnect_attempts: int = 0,
    clock: FakeClock | None = None,
) -> tuple[AlpacaRealtimeFeed, list[float], list[FakeConnection]]:
    """A feed wired to a queue of connections. Returns it, the sleeps it asked
    for, and the connections it was handed."""
    queue = list(connections)
    handed: list[FakeConnection] = []
    slept: list[float] = []

    async def connect(url: str) -> FakeConnection:
        if not queue:
            raise DroppedError("nothing left to connect to")
        connection = queue.pop(0)
        handed.append(connection)
        return connection

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    feed = AlpacaRealtimeFeed(
        make_settings(),
        connect=connect,
        sleep=sleep,
        clock=clock or FakeClock(datetime(2024, 6, 3, 14, 30, tzinfo=UTC)),
        rng=random.Random(0),
        max_reconnect_attempts=max_reconnect_attempts,
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
        await stream.aclose()
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

    async def test_backs_off_exponentially_and_gives_up(self) -> None:
        feed, slept, _ = build(max_reconnect_attempts=3)  # no connections at all
        await feed.subscribe(["SPY"])

        _, error = await collect(feed)

        assert error is not None and "did not come back after 3 attempts" in str(error)
        assert len(slept) == 3
        # Jittered into [d/2, d], so the ladder is bounded rather than exact —
        # an assertion on exact delays would be testing `random`.
        for delay, unjittered in zip(slept, [1.0, 2.0, 4.0], strict=True):
            assert unjittered / 2 <= delay <= unjittered

    async def test_backoff_is_capped(self) -> None:
        feed, slept, _ = build(max_reconnect_attempts=12)
        await feed.subscribe(["SPY"])

        _, error = await collect(feed)

        assert error is not None
        assert max(slept) <= 60.0

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
        feed, slept, _ = build(*flapping, max_reconnect_attempts=2)
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
        await stream.aclose()

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
