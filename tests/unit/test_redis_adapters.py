"""The Redis quote cache and the pub/sub publisher.

No Redis here: a fake client stands in, so what is under test is the mapping —
key layout, the stored document, how a Decimal survives the round trip, what
happens to a record that will not parse. The behaviour that belongs to Redis
itself (TTL actually expiring, MGET actually being atomic) is asserted in
`tests/integration/test_redis_adapters.py` against a real server, because a fake
that agreed with us about those would be proving nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from atp_core.domain import Quote
from atp_core.persistence.events import RedisEventPublisher
from atp_core.persistence.quotes import KEY_PREFIX, RedisQuoteCache

TS = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


def make_quote(symbol: str = "SPY", bid: str = "439.71", ask: str = "439.73") -> Quote:
    return Quote(
        symbol=symbol,
        ts=TS,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal("3"),
        ask_size=Decimal("5"),
    )


class FakeRedis:
    """Records what was asked of it. Deliberately literal — it does not expire
    keys or validate types, so a test that needs either says so explicitly."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets: list[tuple[str, str, int | None]] = []
        self.published: list[tuple[str, str]] = []

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.sets.append((key, value, ex))

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store.get(k) for k in keys]

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1


def cache(**kwargs: Any) -> tuple[RedisQuoteCache, FakeRedis]:
    client = FakeRedis()
    return RedisQuoteCache(client, **kwargs), client  # type: ignore[arg-type]


class TestQuoteCacheWrites:
    async def test_key_is_namespaced_per_symbol(self) -> None:
        store, client = cache()

        await store.set_quote(make_quote("AAPL"))

        assert list(client.store) == [f"{KEY_PREFIX}AAPL"]

    async def test_prices_are_stored_as_strings(self) -> None:
        """JSON has one numeric type and it is a binary float. A price stored as
        a JSON number comes back subtly wrong, and comes back silently."""
        store, client = cache()

        await store.set_quote(make_quote(bid="0.1", ask="0.3"))

        payload = json.loads(client.store[f"{KEY_PREFIX}SPY"])
        assert payload["bid"] == "0.1"
        assert all(isinstance(v, str) for v in payload.values())

    async def test_write_carries_the_ttl(self) -> None:
        store, client = cache(ttl_seconds=120)

        await store.set_quote(make_quote())

        assert client.sets[0][2] == 120

    async def test_rejects_a_nonsense_ttl(self) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            cache(ttl_seconds=0)


class TestQuoteCacheReads:
    async def test_round_trips_exactly(self) -> None:
        store, _ = cache()
        original = make_quote(bid="0.1", ask="0.3")

        await store.set_quote(original)
        recovered = await store.get_quote("SPY")

        assert recovered == original
        assert recovered is not None
        # The whole point of the string encoding: this is 0.2 exactly, not
        # 0.19999999999999998.
        assert recovered.spread == Decimal("0.2")

    async def test_missing_symbol_is_none(self) -> None:
        store, _ = cache()

        assert await store.get_quote("NOPE") is None

    async def test_timestamp_comes_back_utc(self) -> None:
        store, _ = cache()

        await store.set_quote(make_quote())
        recovered = await store.get_quote("SPY")

        assert recovered is not None
        assert recovered.ts == TS
        assert recovered.ts.tzinfo is not None

    async def test_corrupt_record_reads_as_absent(self) -> None:
        """Safe direction: the rules downstream refuse to trade without a quote,
        so an unreadable one stops trading on that symbol rather than being
        guessed at."""
        store, client = cache()
        client.store[f"{KEY_PREFIX}SPY"] = "{not json"

        assert await store.get_quote("SPY") is None

    async def test_record_missing_a_field_reads_as_absent(self) -> None:
        store, client = cache()
        client.store[f"{KEY_PREFIX}SPY"] = json.dumps({"symbol": "SPY", "ts": TS.isoformat()})

        assert await store.get_quote("SPY") is None

    async def test_naive_stored_timestamp_is_refused(self) -> None:
        """A hand-edited key must not become a naive quote (rule §1.2)."""
        store, client = cache()
        client.store[f"{KEY_PREFIX}SPY"] = json.dumps(
            {
                "symbol": "SPY",
                "ts": "2024-06-03T14:30:00",
                "bid": "1",
                "ask": "2",
                "bid_size": "0",
                "ask_size": "0",
            }
        )

        assert await store.get_quote("SPY") is None


class TestQuoteCacheBulkReads:
    async def test_one_round_trip_for_the_whole_watchlist(self) -> None:
        store, _ = cache()
        for symbol in ("SPY", "AAPL", "MSFT"):
            await store.set_quote(make_quote(symbol))

        quotes = await store.get_quotes(["SPY", "AAPL", "MSFT"])

        assert sorted(quotes) == ["AAPL", "MSFT", "SPY"]

    async def test_absent_symbols_are_omitted_not_none(self) -> None:
        store, _ = cache()
        await store.set_quote(make_quote("SPY"))

        quotes = await store.get_quotes(["SPY", "NOPE"])

        assert list(quotes) == ["SPY"]

    async def test_duplicate_symbols_are_asked_for_once(self) -> None:
        store, _ = cache()
        await store.set_quote(make_quote("SPY"))

        quotes = await store.get_quotes(["SPY", "SPY"])

        assert list(quotes) == ["SPY"]

    async def test_empty_request_makes_no_call(self) -> None:
        store, _ = cache()

        assert await store.get_quotes([]) == {}

    async def test_one_corrupt_record_does_not_lose_the_others(self) -> None:
        store, client = cache()
        await store.set_quote(make_quote("SPY"))
        client.store[f"{KEY_PREFIX}AAPL"] = "{not json"

        quotes = await store.get_quotes(["SPY", "AAPL"])

        assert list(quotes) == ["SPY"]


class TestEventPublisher:
    async def test_publishes_json_on_the_channel(self) -> None:
        client = FakeRedis()
        publisher = RedisEventPublisher(client)  # type: ignore[arg-type]

        await publisher.publish("atp:md:quotes", {"type": "quote", "bid": "439.71"})

        channel, raw = client.published[0]
        assert channel == "atp:md:quotes"
        assert json.loads(raw) == {"type": "quote", "bid": "439.71"}

    async def test_refuses_to_publish_a_float(self) -> None:
        """The last place a price can be checked before it leaves the process.
        JSON would encode it without complaint and the corruption would first
        show up on a dashboard, as a price ending in a run of 9s."""
        client = FakeRedis()
        publisher = RedisEventPublisher(client)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="float"):
            await publisher.publish("atp:md:quotes", {"symbol": "SPY", "bid": 439.71})

        assert client.published == []

    async def test_refuses_a_float_nested_in_a_sub_document(self) -> None:
        client = FakeRedis()
        publisher = RedisEventPublisher(client)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match=r"quote\.bid"):
            await publisher.publish("atp:md:quotes", {"quote": {"bid": 1.5}})

    async def test_booleans_are_fine(self) -> None:
        """`bool` is an `int` subclass, not a price. A flag must not trip the
        float guard."""
        client = FakeRedis()
        publisher = RedisEventPublisher(client)  # type: ignore[arg-type]

        await publisher.publish("atp:exec:orders", {"halted": True})

        assert json.loads(client.published[0][1]) == {"halted": True}
