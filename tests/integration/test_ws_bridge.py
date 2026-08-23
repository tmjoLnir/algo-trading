"""The dashboard's Redis bridge against a real server.

This cannot be a unit test. What broke here was not our logic but the contract
between two libraries: redis-py decides a subscription read's deadline from the
connection's `socket_timeout` when the caller does not supply one, and reports
that deadline expiring as a dropped connection. A fake that agreed with us about
which reads raise would be proving only that we agree with ourselves — and we
were wrong about exactly that.

So the assertion here is about real redis-py talking to real Redis: a
subscription with nothing published on it for longer than `socket_timeout` stays
up, and the message that eventually arrives is delivered rather than dropped
into a re-subscribe.

The client is built here rather than with `create_redis` because the timeout is
the variable under test: production's 5s would make every case below a five
second wait. The path exercised is identical — the same kwargs, one number
smaller (`IDLE_GAP_SECONDS`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import TYPE_CHECKING, Any

import pytest
from redis.asyncio import Redis

import atp_api.ws
from atp_api.ws import ConnectionManager, redis_bridge
from atp_core.channels import CHANNEL_QUOTES
from atp_core.persistence.redis_client import HEALTH_CHECK_SECONDS, SOCKET_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

#: The real quotes channel, because the mapping from it to the client-facing
#: name is part of what the bridge does — a test channel would be dropped by
#: `_dispatch` as unknown, which is not the code path anyone cares about here.
#:
#: Sharing the channel with a stack that happens to be running is handled by the
#: tickers rather than by the channel name: `quotes` is symbol-filtered, so a
#: client watching only these three receives nothing else. They are deliberately
#: not instruments.
TEST_CHANNEL = CHANNEL_QUOTES
TEST_SYMBOLS = ("ZZTESTA", "ZZTESTB", "ZZTESTC")

#: The subscription's read deadline for these tests, standing in for
#: `SOCKET_TIMEOUT_SECONDS`. Small enough that "quiet for longer than the
#: timeout" costs a second rather than five.
SOCKET_TIMEOUT = 1.0

#: How long to leave the channel silent. Comfortably past `SOCKET_TIMEOUT`, so
#: a blocking read would certainly have raised by the time the test publishes.
IDLE_GAP_SECONDS = 2.5

#: How long to wait for one message to cross Redis and reach a client. Generous
#: for a local round trip, and it is a ceiling rather than a sleep — the test
#: waits on the delivery, so a fast machine does not wait at all.
DELIVERY_TIMEOUT_SECONDS = 5.0


class RecordingSocket:
    """Just enough `WebSocket` to collect what the bridge fans out."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.received = asyncio.Event()

    async def accept(self) -> None: ...

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        self.received.set()


@pytest.fixture
async def client() -> AsyncIterator[Redis]:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is unset — start the stack with `make up`")

    redis: Redis = Redis.from_url(
        url,
        decode_responses=True,
        health_check_interval=HEALTH_CHECK_SECONDS,
        socket_keepalive=True,
        socket_timeout=SOCKET_TIMEOUT,
        socket_connect_timeout=SOCKET_TIMEOUT,
    )
    try:
        await redis.ping()
    except Exception as exc:  # pragma: no cover - environment, not logic
        await redis.aclose()
        pytest.skip(f"Redis at {url} is not reachable: {exc}")
    yield redis
    await redis.aclose()


@pytest.fixture
async def publisher() -> AsyncIterator[Redis]:
    """A second connection, because a publish must actually cross the server to
    prove anything about the subscriber."""
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is unset — start the stack with `make up`")
    redis: Redis = Redis.from_url(url, decode_responses=True)
    yield redis
    await redis.aclose()


class RunningBridge:
    """A bridge under test, plus the two things worth asserting about it: what
    reached a client, and how many subscriptions it took to get there."""

    def __init__(self, socket: RecordingSocket) -> None:
        self.socket = socket
        #: How many times the bridge has called `redis.pubsub()`. One means it
        #: subscribed at startup and has held that subscription ever since,
        #: which is the invariant a quiet channel used to break.
        self.subscriptions = 0

    @property
    def symbols(self) -> list[str]:
        return [message["symbol"] for message in self.socket.sent]


@pytest.fixture
async def bridge(client: Redis, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RunningBridge]:
    """A running bridge with one client attached, torn down after the test."""
    manager = ConnectionManager()
    socket = RecordingSocket()
    running = RunningBridge(socket)
    await manager.connect("a", socket)  # type: ignore[arg-type]
    manager.subscribe("a", ["quotes"], list(TEST_SYMBOLS))

    # Counted at the seam the bug moved through. Asserting on re-subscribes
    # rather than on a lost message is what makes these tests deterministic:
    # whether any *particular* publish fell into a gap depends on where it
    # landed in the tear-down cycle, but the tear-downs themselves either happen
    # or they do not.
    opened = client.pubsub

    def counting_pubsub(**kwargs: Any) -> Any:
        running.subscriptions += 1
        return opened(**kwargs)

    monkeypatch.setattr(client, "pubsub", counting_pubsub)

    task = asyncio.create_task(redis_bridge(client, manager, channels=(TEST_CHANNEL,)))
    # The subscription must exist before anything is published: pub/sub has no
    # replay, so a race here would look exactly like the bug under test.
    await asyncio.sleep(0.25)
    yield running
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def publish(redis: Redis, publisher: Redis, socket: RecordingSocket, **payload: Any) -> None:
    """Publish one message and wait for the fan-out, rather than sleeping."""
    socket.received.clear()
    await publisher.publish(TEST_CHANNEL, json.dumps({"type": "quote", **payload}))
    async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
        await socket.received.wait()


class TestAQuietChannelIsNotABrokenOne:
    def test_the_production_timeout_is_short_enough_to_matter(self) -> None:
        """The premise. If `socket_timeout` were ever removed or raised beyond
        any plausible quiet period this would stop being a live hazard, and the
        tests below would be asserting about nothing."""
        assert SOCKET_TIMEOUT_SECONDS <= 60, (
            "a quiet market lasts hours; any bounded read deadline will expire "
            "on an idle subscription and must not be read as a failure"
        )

    async def test_an_idle_subscription_is_never_torn_down(self, bridge: RunningBridge) -> None:
        """The bug itself, stated without reference to any message.

        Silence longer than `socket_timeout` used to arrive as
        `TimeoutError("Timeout reading from redis:6379")`, and the bridge did
        what it should do with a dropped connection: backed off and
        re-subscribed. Every gap between two of those subscriptions is a window
        with no subscriber attached, and Redis pub/sub has no replay."""
        await asyncio.sleep(IDLE_GAP_SECONDS)

        assert bridge.subscriptions == 1, (
            f"the bridge re-subscribed {bridge.subscriptions} times while nothing "
            f"was published — each one is a window in which the platform is "
            f"publishing to nobody"
        )

    async def test_a_message_after_a_long_silence_is_delivered(
        self, client: Redis, publisher: Redis, bridge: RunningBridge
    ) -> None:
        """The overnight case, end to end: a halt or a fill arriving on a
        channel that has been silent since the close."""
        await asyncio.sleep(IDLE_GAP_SECONDS)
        await publish(client, publisher, bridge.socket, symbol=TEST_SYMBOLS[0])

        assert bridge.symbols == [TEST_SYMBOLS[0]]
        assert bridge.subscriptions == 1

    async def test_the_subscription_survives_several_idle_windows(
        self, client: Redis, publisher: Redis, bridge: RunningBridge
    ) -> None:
        """Not just the first one: the failure was a steady state, so a bridge
        that recovered once but churned afterwards would still lose messages all
        night."""
        for symbol in TEST_SYMBOLS:
            await asyncio.sleep(IDLE_GAP_SECONDS)
            await publish(client, publisher, bridge.socket, symbol=symbol)

        assert bridge.symbols == list(TEST_SYMBOLS)
        assert bridge.subscriptions == 1

    async def test_delivery_still_works_with_no_idle_gap_at_all(
        self, client: Redis, publisher: Redis, bridge: RunningBridge
    ) -> None:
        """The busy market: back-to-back messages, none of which ever let the
        read reach its deadline. This path always worked, and polling must not
        have cost it anything."""
        for symbol in TEST_SYMBOLS:
            await publish(client, publisher, bridge.socket, symbol=symbol)

        assert bridge.symbols == list(TEST_SYMBOLS)
        assert bridge.subscriptions == 1


class TestALiveConnectionCanProveItself:
    """The check that pays for polling.

    A poll returning "nothing published" is the same answer whether the market
    is quiet or the socket has stopped carrying data, so past a point the bridge
    asks the connection directly. That only works if redis-py hands the reply
    back — its own health-check PING is swallowed before a caller can see it,
    and a check whose answer is invisible is a check that always fails.

    Which is why this is an integration test. Whether a `PING` issued on a
    subscription comes back as an observable `pong` is a fact about redis-py and
    Redis together, and a fake agreeing that it does would be proving nothing.
    """

    @pytest.fixture
    def quick_liveness(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Production waits 20s before asking and 30s before giving up. Both are
        scaled down here so the round trip happens several times over inside a
        test, rather than once inside half a minute of waiting.

        The gap between them stays wide on purpose. A ping every 0.5s of silence
        is what the test is here to exercise; giving up at 3s rather than just
        after leaves a loaded runner room to stall without inventing a dead
        connection, and is still well inside the idle window below — so a pong
        that genuinely stopped arriving fails this test with time to spare."""
        monkeypatch.setattr(atp_api.ws, "LIVENESS_PING_SECONDS", 0.5)
        monkeypatch.setattr(atp_api.ws, "LIVENESS_TIMEOUT_SECONDS", 3.0)

    async def test_an_idle_subscription_outlives_the_liveness_timeout(
        self, quick_liveness: None, client: Redis, publisher: Redis, bridge: RunningBridge
    ) -> None:
        """Silence many times longer than the give-up threshold, on a healthy
        Redis. Every one of those windows is survived by a real PING answered
        with a real PONG — if the answer were invisible the bridge would have
        declared the connection dead within `LIVENESS_TIMEOUT_SECONDS`."""
        await asyncio.sleep(IDLE_GAP_SECONDS * 2)

        assert bridge.subscriptions == 1, (
            "a healthy but quiet connection was written off — the pong that "
            "proves it alive is not reaching the bridge"
        )

        # And it is still a working subscription afterwards, not merely an
        # unclosed one: liveness that cost delivery would be no better.
        await publish(client, publisher, bridge.socket, symbol=TEST_SYMBOLS[0])
        assert bridge.symbols == [TEST_SYMBOLS[0]]
