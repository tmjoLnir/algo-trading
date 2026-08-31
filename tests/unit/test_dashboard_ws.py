"""The dashboard WebSocket: who gets what, and what happens when a client stops
reading.

The socket is an enhancement and the dashboard's own aggregate read is the
source of truth, so nothing here is about delivery guarantees — it is about the
two ways a fan-out can be wrong in a trading UI:

- **a halt that does not arrive**, because the client did not think to subscribe
  to one. `ws.py` promises it reaches every client regardless;
- **one slow client holding up everyone else**, which is how a single browser on
  a bad connection stops the whole dashboard fleet updating.

And one way the bridge feeding them can be wrong, which no test covered until it
happened in production: **a quiet channel read as a broken one**, which drops the
subscription on a cadence and takes the messages published in each gap with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from atp_api.ws import ConnectionManager, _dispatch, _string_list, redis_bridge
from atp_core.channels import CHANNEL_HALTS, CHANNEL_ORDERS, CHANNEL_QUOTES


class FakeSocket:
    """Just enough `WebSocket`. Can be told to hang or to fail."""

    def __init__(self, *, hang: bool = False, error: Exception | None = None) -> None:
        self.accepted = False
        self.sent: list[dict[str, Any]] = []
        self.hang = hang
        self.error = error

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, message: dict[str, Any]) -> None:
        if self.error is not None:
            raise self.error
        if self.hang:
            await asyncio.Event().wait()  # never returns
        self.sent.append(message)

    def types(self) -> list[str]:
        return [m.get("type", "") for m in self.sent]


async def connected(
    manager: ConnectionManager, client_id: str, **subscription: list[str]
) -> FakeSocket:
    socket = FakeSocket()
    await manager.connect(client_id, socket)  # type: ignore[arg-type]
    if subscription:
        manager.subscribe(
            client_id, subscription.get("channels", []), subscription.get("symbols", [])
        )
    return socket


@pytest.fixture
def manager() -> ConnectionManager:
    return ConnectionManager()


class TestHaltsReachEveryone:
    async def test_a_client_that_subscribed_to_nothing_still_gets_a_halt(
        self, manager: ConnectionManager
    ) -> None:
        """A trading halt is not something to opt into. A dashboard that
        filtered one out would show a green screen while nothing was trading."""
        socket = await connected(manager, "a")

        await manager.broadcast("halts", {"type": "halt", "scope": "global"})

        assert socket.types() == ["halt"]

    async def test_a_symbol_scoped_halt_reaches_a_client_watching_other_symbols(
        self, manager: ConnectionManager
    ) -> None:
        """The halt carries a symbol, and symbol filtering must not apply to it:
        an operator watching AAPL still needs to know MSFT was halted, because
        the halt is a fact about the platform rather than about their watchlist.
        """
        socket = await connected(manager, "a", channels=["quotes"], symbols=["AAPL"])

        await manager.broadcast("halts", {"type": "halt", "scope": "symbol", "symbol": "MSFT"})

        assert socket.types() == ["halt"]


class TestSubscriptionFiltering:
    async def test_quotes_go_only_to_subscribers_of_that_symbol(
        self, manager: ConnectionManager
    ) -> None:
        """A dashboard watching five symbols should not receive the universe's
        tick stream."""
        watcher = await connected(manager, "a", channels=["quotes"], symbols=["AAPL"])
        other = await connected(manager, "b", channels=["quotes"], symbols=["MSFT"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL", "bid": "1"})

        assert watcher.types() == ["quote"]
        assert other.sent == []

    async def test_a_client_on_no_channels_gets_no_quotes(self, manager: ConnectionManager) -> None:
        socket = await connected(manager, "a")

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})

        assert socket.sent == []

    async def test_subscribing_to_a_channel_with_no_symbols_means_all_of_them(
        self, manager: ConnectionManager
    ) -> None:
        """Empty is "everything on this channel", not "nothing". Treating it as
        nothing would make the first subscribe deliver silence."""
        socket = await connected(manager, "a", channels=["quotes"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})

        assert socket.types() == ["quote"]

    async def test_a_fill_is_not_symbol_filtered(self, manager: ConnectionManager) -> None:
        """A fill on a symbol you did not subscribe to is still your money."""
        socket = await connected(manager, "a", channels=["fills"], symbols=["AAPL"])

        await manager.broadcast("fills", {"type": "fill", "symbol": "TSLA", "qty": "10"})

        assert socket.types() == ["fill"]

    async def test_symbols_are_upper_cased(self, manager: ConnectionManager) -> None:
        """`aapl` means the same instrument, not a different one."""
        socket = await connected(manager, "a", channels=["quotes"], symbols=["aapl"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})

        assert socket.types() == ["quote"]

    async def test_subscribing_again_adds_rather_than_replaces(
        self, manager: ConnectionManager
    ) -> None:
        """A dashboard subscribes as each panel mounts. A second call that
        dropped the first panel's symbols would leave a table that stops
        updating for no visible reason."""
        socket = await connected(manager, "a", channels=["quotes"], symbols=["AAPL"])
        manager.subscribe("a", ["quotes"], ["MSFT"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})
        await manager.broadcast("quotes", {"type": "quote", "symbol": "MSFT"})

        assert socket.types() == ["quote", "quote"]

    async def test_unsubscribing_stops_that_symbol_only(self, manager: ConnectionManager) -> None:
        socket = await connected(manager, "a", channels=["quotes"], symbols=["AAPL", "MSFT"])
        manager.unsubscribe("a", ["AAPL"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})
        await manager.broadcast("quotes", {"type": "quote", "symbol": "MSFT"})

        assert [m["symbol"] for m in socket.sent] == ["MSFT"]

    async def test_an_unknown_channel_name_is_not_subscribable(
        self, manager: ConnectionManager
    ) -> None:
        """A typo in a client build must not silently create a channel nobody
        publishes to — it would look exactly like a broken producer."""
        socket = await connected(manager, "a", channels=["qoutes"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})

        assert socket.sent == []


class TestOneDeadClientCostsNobodyElse:
    async def test_a_failing_client_is_dropped_and_the_rest_still_receive(
        self, manager: ConnectionManager
    ) -> None:
        broken = FakeSocket(error=RuntimeError("socket is gone"))
        await manager.connect("broken", broken)  # type: ignore[arg-type]
        manager.subscribe("broken", ["quotes"], [])
        healthy = await connected(manager, "healthy", channels=["quotes"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})

        assert healthy.types() == ["quote"]
        assert manager.client_count == 1

    async def test_a_client_that_stops_reading_is_dropped_on_the_deadline(
        self, manager: ConnectionManager
    ) -> None:
        """Unbounded buffering for one slow reader costs every other client. The
        next read recovers whatever it misses, so dropping is cheap."""
        import atp_api.ws as ws_module

        stalled = FakeSocket(hang=True)
        await manager.connect("stalled", stalled)  # type: ignore[arg-type]
        manager.subscribe("stalled", ["quotes"], [])
        healthy = await connected(manager, "healthy", channels=["quotes"])

        original = ws_module.SEND_TIMEOUT_SECONDS
        ws_module.SEND_TIMEOUT_SECONDS = 0.01
        try:
            await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})
        finally:
            ws_module.SEND_TIMEOUT_SECONDS = original

        assert healthy.types() == ["quote"]
        assert manager.client_count == 1

    async def test_disconnecting_an_unknown_client_is_not_an_error(
        self, manager: ConnectionManager
    ) -> None:
        """The endpoint's `finally` runs even when `connect` never did."""
        manager.disconnect("never-connected")

    async def test_subscribing_a_disconnected_client_is_ignored(
        self, manager: ConnectionManager
    ) -> None:
        """A frame that arrives after the socket closed must not resurrect it as
        an entry nothing will ever clean up."""
        manager.subscribe("ghost", ["quotes"], ["AAPL"])

        assert manager.client_count == 0


class TestTheRedisBridge:
    """`_dispatch` — turning one Redis message into one fan-out."""

    def message(self, channel: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": "message", "channel": channel, "data": json.dumps(payload)}

    async def dispatched(self, raw: dict[str, Any], manager: ConnectionManager) -> None:
        """Dispatch and wait for the fan-out it started, if it started one.

        Awaited rather than slept on: a `sleep(0)` yields once, which is not
        enough for a task that itself awaits a gather, and a longer sleep is a
        flaky test waiting for a loaded CI runner.
        """
        task = _dispatch(raw, manager)
        if task is not None:
            await task

    async def test_it_maps_a_redis_channel_to_a_client_channel(
        self, manager: ConnectionManager
    ) -> None:
        """Two vocabularies: the Redis names are internal, the client names are
        a published protocol. A rename on either side must not silently
        unsubscribe every browser."""
        socket = await connected(manager, "a", channels=["fills"])

        await self.dispatched(
            self.message(CHANNEL_ORDERS, {"type": "fill", "symbol": "AAPL"}), manager
        )

        assert socket.types() == ["fill"]

    async def test_a_halt_from_redis_reaches_an_unsubscribed_client(
        self, manager: ConnectionManager
    ) -> None:
        socket = await connected(manager, "a")

        await self.dispatched(
            self.message(CHANNEL_HALTS, {"type": "halt", "scope": "global"}), manager
        )

        assert socket.types() == ["halt"]

    async def test_undecodable_data_is_dropped_not_raised(self, manager: ConnectionManager) -> None:
        """One malformed message must not take the bridge down and with it every
        client's live updates."""
        socket = await connected(manager, "a", channels=["quotes"])

        await self.dispatched(
            {"type": "message", "channel": CHANNEL_QUOTES, "data": "not json"}, manager
        )

        assert socket.sent == []

    async def test_a_json_scalar_is_dropped(self, manager: ConnectionManager) -> None:
        socket = await connected(manager, "a", channels=["quotes"])

        await self.dispatched({"type": "message", "channel": CHANNEL_QUOTES, "data": "42"}, manager)

        assert socket.sent == []

    async def test_an_unmapped_channel_is_dropped(self, manager: ConnectionManager) -> None:
        socket = await connected(manager, "a", channels=["quotes"])

        await self.dispatched(self.message("atp:something:else", {"type": "quote"}), manager)

        assert socket.sent == []


class TestClientFrameParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (["AAPL", "MSFT"], ["AAPL", "MSFT"]),
            (["AAPL", None, 7], ["AAPL"]),
            ("AAPL", []),
            (None, []),
        ],
    )
    def test_non_strings_are_dropped_rather_than_coerced(
        self, value: object, expected: list[str]
    ) -> None:
        """`str(None)` is `"None"`, which would enter a symbol set and quietly
        match nothing forever."""
        assert _string_list(value) == expected


class FakePubSub:
    """A `redis.asyncio.PubSub` narrowed to the one behaviour this bug turned on.

    redis-py picks the read deadline in `Connection.read_response`:

        read_timeout = timeout if timeout is not None else self.socket_timeout

    and then splits on which one it got. A deadline the *caller* asked for
    returns `None` when it expires — nothing was published, ask again. No
    deadline falls through to the connection's `socket_timeout` and *raises*
    `TimeoutError`, because at that layer a read that does not complete is a
    connection that has stopped answering.

    `listen()` takes the second path, which is what made a quiet channel
    indistinguishable from a dead Redis. Both paths are modelled here, so this
    fake fails the code that shipped and passes the code that replaced it.
    """

    #: Nothing was published during the read. Not an error — a quiet market.
    IDLE = object()

    def __init__(self, script: list[Any], *, answers_ping: bool = True) -> None:
        self._script = list(script)
        self.subscribed = False
        self.channels: tuple[str, ...] = ()
        #: The `timeout=` of every read, in order. `None` is a blocking read.
        self.reads: list[float | None] = []
        self.closed = 0
        #: How many liveness checks the bridge has sent.
        self.pings = 0
        #: Whether a ping comes back. False is the connection that has stopped
        #: carrying data while staying open — reads keep returning "nothing
        #: published", which is exactly what a quiet market returns.
        self.answers_ping = answers_ping
        #: Set once the script runs out, so a test can wait for the bridge to
        #: have done everything it was going to rather than sleeping and hoping.
        self.exhausted = asyncio.Event()

    async def subscribe(self, *channels: str) -> None:
        self.subscribed = True
        self.channels = channels

    async def get_message(self, *, timeout: float | None = 0.0) -> dict[str, Any] | None:
        self.reads.append(timeout)
        if not self._script:
            self.exhausted.set()
            await asyncio.Event().wait()  # a channel that stays quiet forever
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if step is self.IDLE:
            if timeout is None:
                # The blocking path: no deadline of its own, so `socket_timeout`
                # supplies one and its expiry is reported as a broken connection.
                raise RedisTimeoutError("Timeout reading from redis:6379")
            return None
        if step == "ended":
            self.subscribed = False
            return None
        assert isinstance(step, dict)
        return step

    async def ping(self) -> None:
        """redis-py answers this on the subscription itself, and `handle_message`
        hands the reply back as a `pong` — which is what makes it observable
        here, and what the built-in health check swallows."""
        self.pings += 1
        if self.answers_ping:
            self._script.insert(0, {"type": "pong", "channel": None, "data": ""})

    async def listen(self) -> Any:
        """redis-py's `while self.subscribed: ... parse_response(block=True)`."""
        while self.subscribed:
            message = await self.get_message(timeout=None)
            if message is not None:
                yield message

    async def aclose(self) -> None:
        self.closed += 1


class FakeRedis:
    """Hands out a fresh `FakePubSub` per `pubsub()`, as the real client does —
    which is what lets a test count re-subscribes."""

    def __init__(self, *scripts: list[Any], answers_ping: bool = True) -> None:
        self._scripts = list(scripts)
        self.pubsubs: list[FakePubSub] = []
        self.answers_ping = answers_ping

    def pubsub(self, *, ignore_subscribe_messages: bool = False) -> FakePubSub:
        script = self._scripts.pop(0) if self._scripts else []
        pubsub = FakePubSub(script, answers_ping=self.answers_ping)
        self.pubsubs.append(pubsub)
        return pubsub


async def run_bridge(redis: FakeRedis, manager: ConnectionManager, *, pubsubs: int = 1) -> None:
    """Run the bridge until it has opened `pubsubs` subscriptions and the last
    one has read its whole script, then cancel it.

    Bounded by an event rather than a sleep: the number of reads a fix changes
    is exactly what is under test, so a test that waited a fixed time would
    measure the runner's load instead.
    """
    task = asyncio.create_task(redis_bridge(redis, manager, channels=(CHANNEL_QUOTES,)))  # type: ignore[arg-type]
    try:
        async with asyncio.timeout(5):
            while len(redis.pubsubs) < pubsubs:
                await asyncio.sleep(0)
            await redis.pubsubs[pubsubs - 1].exhausted.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ladder is tested in `test_ws_backoff.py`; here it is just latency."""
    monkeypatch.setattr("atp_api.ws.backoff_delay", lambda attempt: 0.0)


class TestTheBridgeSurvivesAQuietChannel:
    """The bug behind `ws.bridge_failed error='Timeout reading from redis:6379'`.

    A subscription with nothing published on it is the normal overnight and
    weekend state of this platform, and it used to be read as a dropped
    connection: `listen()` blocks, the blocking read inherits the client's 5s
    `socket_timeout`, and its expiry arrives as `TimeoutError`. The bridge did
    what it should do with a dropped connection — backed off and re-subscribed —
    every five seconds, for as long as the market was closed.

    The cost is not the log line. Every re-subscribe is a window with no
    subscriber attached, and Redis pub/sub has no replay: whatever the worker
    published into that window reached no browser at all, halts included.
    """

    async def test_an_idle_read_is_not_a_dropped_connection(
        self, manager: ConnectionManager
    ) -> None:
        socket = await connected(manager, "a", channels=["quotes"])
        quote = {
            "type": "message",
            "channel": CHANNEL_QUOTES,
            "data": json.dumps({"type": "quote"}),
        }
        redis = FakeRedis([FakePubSub.IDLE, FakePubSub.IDLE, FakePubSub.IDLE, quote])

        await run_bridge(redis, manager)
        await asyncio.sleep(0)  # let the fan-out task the last read started run

        assert len(redis.pubsubs) == 1, "a quiet channel must not cause a re-subscribe"
        assert socket.types() == ["quote"], "the message after the quiet stretch must arrive"

    async def test_it_never_issues_a_read_without_a_deadline(
        self, manager: ConnectionManager
    ) -> None:
        """The regression itself, stated directly.

        A read with no deadline of its own is the one that falls back to
        `socket_timeout` and turns silence into `TimeoutError` — so the bridge
        must always name its own, whatever else changes here.
        """
        redis = FakeRedis([FakePubSub.IDLE, FakePubSub.IDLE])

        await run_bridge(redis, manager)

        reads = redis.pubsubs[0].reads
        assert reads, "the bridge read nothing at all"
        assert all(timeout is not None for timeout in reads), f"a blocking read: {reads}"

    async def test_a_message_still_arrives_after_a_long_quiet_stretch(
        self, manager: ConnectionManager
    ) -> None:
        """The halt at 3am, on a subscription that has been silent for hours."""
        socket = await connected(manager, "a")
        halt = {
            "type": "message",
            "channel": CHANNEL_HALTS,
            "data": json.dumps({"type": "halt", "scope": "global"}),
        }
        redis = FakeRedis([*([FakePubSub.IDLE] * 50), halt])

        await run_bridge(redis, manager)
        await asyncio.sleep(0)

        assert len(redis.pubsubs) == 1
        assert socket.types() == ["halt"]


class TestTheBridgeStillReconnectsWhenItShould:
    """Not treating silence as a failure must not stop it noticing a real one."""

    async def test_a_dropped_connection_re_subscribes(
        self, manager: ConnectionManager, instant_backoff: None
    ) -> None:
        socket = await connected(manager, "a", channels=["quotes"])
        quote = {
            "type": "message",
            "channel": CHANNEL_QUOTES,
            "data": json.dumps({"type": "quote"}),
        }
        redis = FakeRedis([RedisConnectionError("connection lost")], [quote])

        await run_bridge(redis, manager, pubsubs=2)
        await asyncio.sleep(0)

        assert len(redis.pubsubs) == 2, "a genuine failure must still reconnect"
        assert redis.pubsubs[0].closed == 1, "the dead pubsub must be released"
        assert socket.types() == ["quote"], "and delivery resumes on the new one"

    async def test_a_socket_timeout_from_a_real_command_still_reconnects(
        self, manager: ConnectionManager, instant_backoff: None
    ) -> None:
        """`socket_timeout` is untouched by this fix and still means what it
        says: a Redis that has stopped answering a command is a dropped
        connection. Only an *idle subscription* stopped being one."""
        redis = FakeRedis([RedisTimeoutError("Timeout reading from redis:6379")], [])

        await run_bridge(redis, manager, pubsubs=2)

        assert len(redis.pubsubs) == 2

    async def test_the_subscription_ending_under_us_reconnects(
        self, manager: ConnectionManager, instant_backoff: None
    ) -> None:
        """`get_message` returning None because the subscription is gone reads
        identically to an idle window at the call site. Only `subscribed` tells
        them apart, which is why the loop is written on it."""
        redis = FakeRedis(["ended"], [])

        await run_bridge(redis, manager, pubsubs=2)

        assert len(redis.pubsubs) == 2, "an ended subscription must not be polled forever"

    async def test_a_failure_does_not_take_the_bridge_down(
        self, manager: ConnectionManager, instant_backoff: None
    ) -> None:
        """It never gives up — losing the bridge costs the dashboard its live
        updates while its reads still work, so retrying forever beats a live API
        with a dead socket handler."""
        redis = FakeRedis(
            [RedisConnectionError("one")],
            [RedisConnectionError("two")],
            [RedisConnectionError("three")],
            [],
        )

        await run_bridge(redis, manager, pubsubs=4)

        assert len(redis.pubsubs) == 4


class TestSilenceIsCheckedRatherThanAssumed:
    """The other half of not tearing down a quiet subscription.

    Polling buys back the messages a five-second re-subscribe cycle was losing,
    and gives away the only thing that cycle did usefully: it proved the
    connection every five seconds. A read that returns "nothing published"
    cannot tell a quiet market from a socket into a black hole — a peer that
    went away without a FIN, a partition, a NAT mapping that expired — and in
    that state the bridge would sit there delivering nothing, looking exactly
    like a market where nothing is happening.

    redis-py's `health_check_interval` is not this check: it sends a PING and
    never waits for the PONG, and swallows the reply before a caller can see it.
    """

    @pytest.fixture
    def check_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ping on the first quiet read, and never give up on time.

        The thresholds are moved rather than the clock: `redis_bridge` measures
        silence on the event loop's monotonic clock, and a test that waited 20
        real seconds to watch one ping would be 20 seconds of nothing.
        """
        monkeypatch.setattr("atp_api.ws.LIVENESS_PING_SECONDS", 0.0)
        monkeypatch.setattr("atp_api.ws.LIVENESS_TIMEOUT_SECONDS", 1e6)

    async def test_a_long_silence_is_proved_rather_than_trusted(
        self, manager: ConnectionManager, check_immediately: None
    ) -> None:
        redis = FakeRedis([FakePubSub.IDLE, FakePubSub.IDLE])

        await run_bridge(redis, manager)

        assert redis.pubsubs[0].pings >= 1, "a quiet connection was never checked"
        assert len(redis.pubsubs) == 1, "checking it must not mean rebuilding it"

    async def test_a_pong_is_proof_and_the_check_is_not_repeated_until_it_lapses(
        self, manager: ConnectionManager, check_immediately: None
    ) -> None:
        """One ping per quiet stretch, not one per poll: the answer resets the
        silence, and until it arrives another ping is only traffic."""
        redis = FakeRedis([FakePubSub.IDLE] * 6)

        await run_bridge(redis, manager)

        pubsub = redis.pubsubs[0]
        # Six reads, each answered by a pong that resets the timer, so a ping
        # can only follow a read that came back empty — never two in a row.
        assert 0 < pubsub.pings <= 6
        assert len(redis.pubsubs) == 1

    async def test_a_connection_that_stops_answering_is_dropped_and_rebuilt(
        self, manager: ConnectionManager, instant_backoff: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The black hole. Reads keep saying "nothing published" and the ping is
        never answered, so the only honest reading left is that the connection
        is gone."""
        monkeypatch.setattr("atp_api.ws.LIVENESS_PING_SECONDS", 0.0)
        monkeypatch.setattr("atp_api.ws.LIVENESS_TIMEOUT_SECONDS", 0.0)
        redis = FakeRedis([FakePubSub.IDLE], [], answers_ping=False)

        await run_bridge(redis, manager, pubsubs=2)

        assert len(redis.pubsubs) == 2, "an unanswerable connection must be rebuilt"
        assert redis.pubsubs[0].closed == 1

    async def test_a_busy_channel_is_never_pinged(self, manager: ConnectionManager) -> None:
        """Traffic is its own proof. A ping on a channel that is delivering
        would be asking a question already answered."""
        socket = await connected(manager, "a", channels=["quotes"])
        quote = {
            "type": "message",
            "channel": CHANNEL_QUOTES,
            "data": json.dumps({"type": "quote"}),
        }
        redis = FakeRedis([quote, quote, quote])

        await run_bridge(redis, manager)
        await asyncio.sleep(0)

        assert redis.pubsubs[0].pings == 0
        assert socket.types() == ["quote", "quote", "quote"]
