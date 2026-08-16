"""The Redis adapters against a real server.

These cannot be unit tests. Everything asserted here is behaviour of Redis
rather than of Python: whether `SET ... EX` really expires, whether `MGET`
really returns a hole for a missing key in the right position, whether a
message published on one connection actually reaches a subscriber on another.
A fake that agreed with us about all of that would be proving only that we
agree with ourselves.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from atp_core.domain import Quote
from atp_core.persistence.events import RedisEventPublisher
from atp_core.persistence.quotes import RedisQuoteCache
from atp_core.persistence.redis_client import close_redis, create_redis

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from redis.asyncio import Redis

pytestmark = pytest.mark.integration

TS = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)

#: Namespaced away from anything a running stack owns, so these tests can flush
#: their own keys without touching a developer's live cache.
TEST_PREFIX = "atp:test:quote:"
TEST_CHANNEL = "atp:test:quotes"


def make_quote(symbol: str = "SPY", bid: str = "0.1", ask: str = "0.3") -> Quote:
    return Quote(
        symbol=symbol,
        ts=TS,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal("3"),
        ask_size=Decimal("5"),
    )


@pytest.fixture
async def client() -> AsyncIterator[Redis]:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is unset — start the stack with `make up`")

    redis = create_redis(url)
    try:
        await redis.ping()
    except Exception as exc:  # pragma: no cover - environment, not logic
        await close_redis(redis)
        pytest.skip(f"Redis at {url} is not reachable: {exc}")

    # Before rather than after: a failed test leaves its keys behind to be
    # inspected instead of tidying away the evidence.
    await _delete_test_keys(redis)
    yield redis
    await close_redis(redis)


async def _delete_test_keys(redis: Redis) -> None:
    keys = [key async for key in redis.scan_iter(match=f"{TEST_PREFIX}*")]
    if keys:
        await redis.delete(*keys)


@pytest.fixture
def cache(client: Redis) -> RedisQuoteCache:
    return RedisQuoteCache(client, key_prefix=TEST_PREFIX)


class TestQuoteCache:
    async def test_round_trips_a_decimal_exactly(self, cache: RedisQuoteCache) -> None:
        """0.1 and 0.3 are the pair that exposes binary float error: their
        difference is 0.19999999999999998 as floats and 0.2 as Decimals."""
        await cache.set_quote(make_quote(bid="0.1", ask="0.3"))

        recovered = await cache.get_quote("SPY")

        assert recovered is not None
        assert recovered.bid == Decimal("0.1")
        assert recovered.spread == Decimal("0.2")

    async def test_last_write_wins(self, cache: RedisQuoteCache) -> None:
        await cache.set_quote(make_quote(bid="1.00", ask="1.02"))
        await cache.set_quote(make_quote(bid="2.00", ask="2.02"))

        recovered = await cache.get_quote("SPY")

        assert recovered is not None and recovered.bid == Decimal("2.00")

    async def test_ttl_is_actually_set(self, client: Redis) -> None:
        """The TTL is garbage collection, not freshness — but a TTL that never
        made it to the server would leave the cache growing forever."""
        store = RedisQuoteCache(client, key_prefix=TEST_PREFIX, ttl_seconds=120)

        await store.set_quote(make_quote())

        ttl = await client.ttl(f"{TEST_PREFIX}SPY")
        assert 0 < ttl <= 120

    async def test_expiry_removes_the_key(self, client: Redis) -> None:
        store = RedisQuoteCache(client, key_prefix=TEST_PREFIX, ttl_seconds=1)

        await store.set_quote(make_quote())
        await asyncio.sleep(1.2)

        assert await store.get_quote("SPY") is None

    async def test_mget_holes_line_up_with_the_symbols(self, cache: RedisQuoteCache) -> None:
        """The failure this guards against is an off-by-one that pairs AAPL's
        quote with MSFT's symbol — which would be silent, and catastrophic."""
        await cache.set_quote(make_quote("AAPL", bid="1.00", ask="1.02"))
        await cache.set_quote(make_quote("MSFT", bid="3.00", ask="3.02"))

        quotes = await cache.get_quotes(["SPY", "AAPL", "NOPE", "MSFT"])

        assert sorted(quotes) == ["AAPL", "MSFT"]
        assert quotes["AAPL"].bid == Decimal("1.00")
        assert quotes["MSFT"].bid == Decimal("3.00")

    async def test_unknown_symbols_only(self, cache: RedisQuoteCache) -> None:
        assert await cache.get_quotes(["NOPE", "NOTHING"]) == {}


class TestEventPublisher:
    async def test_a_subscriber_receives_the_message(self, client: Redis) -> None:
        """The whole point of the publisher: one writer, many readers, across
        connections. A fake client cannot show this."""
        publisher = RedisEventPublisher(client)
        subscriber = client.pubsub()
        await subscriber.subscribe(TEST_CHANNEL)
        try:
            # Drain the subscribe confirmation before publishing.
            await _next_message(subscriber, kind="subscribe")

            await publisher.publish(TEST_CHANNEL, {"type": "quote", "bid": "439.71"})

            message = await _next_message(subscriber, kind="message")
            assert json.loads(message["data"]) == {"type": "quote", "bid": "439.71"}
        finally:
            await subscriber.aclose()

    async def test_publishing_to_nobody_is_not_an_error(self, client: Redis) -> None:
        """The normal state of a deployment with no dashboard open. The ingest
        loop must not care."""
        await RedisEventPublisher(client).publish(f"{TEST_CHANNEL}:unwatched", {"a": "1"})


async def _next_message(subscriber: object, *, kind: str, timeout: float = 5.0) -> dict[str, str]:
    """Wait for the next pub/sub frame of `kind`, or fail the test.

    Bounded: a missed message would otherwise hang the suite rather than fail
    it, and a hung CI job is much harder to diagnose than a red one.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        message = await subscriber.get_message(timeout=0.5)  # type: ignore[attr-defined]
        if message is not None and message.get("type") == kind:
            return dict(message)
    raise AssertionError(f"no {kind!r} frame arrived within {timeout}s")
