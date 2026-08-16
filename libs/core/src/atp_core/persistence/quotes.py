"""Latest-quote cache — the `QuoteCache` port over Redis.

Read on every risk check, so the shape here is chosen for the read: one key per
symbol holding a small JSON document, fetched with `GET`, or the whole watchlist
with one `MGET`. No hashes, no sorted sets — the only question ever asked is
"what is the newest quote for this symbol", and a string answers it in one round
trip.

**Freshness is a property of the payload, not of the key.** `ts` travels with
the quote and `StaleDataRule` judges against it. The TTL below is garbage
collection for symbols that stopped being watched, deliberately far longer than
any freshness budget: if expiry were the freshness mechanism, a dead feed would
turn into `get_quote() -> None`, and "I have no quote for AAPL" and "my AAPL
quote is four hours old" would become the same answer. They are not. The second
one means something is broken and needs to say so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from atp_core.domain import Quote
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from atp_core.data.ports import QuoteCache

log = get_logger(__name__)

#: One key per symbol. Prefixed so `atp:md:*` names everything market data owns
#: and an operator can see the whole cache with one `SCAN MATCH`.
KEY_PREFIX = "atp:md:quote:"

#: Seven days: long enough to span a three-day weekend plus a holiday without a
#: watched symbol falling out, short enough that a universe that changed months
#: ago is not still resident. Not a freshness mechanism — see the module
#: docstring.
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60


class RedisQuoteCache:
    """`QuoteCache` over Redis.

    Takes a client rather than a URL: the client owns a connection pool, and
    core does not open sockets on its own behalf (CLAUDE.md §1.3).
    """

    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str = KEY_PREFIX,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError(f"ttl_seconds must be at least 1, got {ttl_seconds}")
        self._client = client
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    # ── writes ──────────────────────────────────────────────────────────────

    async def set_quote(self, quote: Quote) -> None:
        """Overwrite the symbol's quote.

        Last write wins, with no read-compare first. Two writers would be a bug
        worth fixing at its source — one process owns the market-data
        connection (`data.stream`) — and a compare-and-set here would buy
        ordering we do not need while costing a round trip on the hottest write
        path in the system.
        """
        await self._client.set(
            self._key(quote.symbol), json.dumps(_encode(quote)), ex=self._ttl_seconds
        )

    # ── reads ───────────────────────────────────────────────────────────────

    async def get_quote(self, symbol: str) -> Quote | None:
        """The latest quote, or None if the cache has never seen this symbol.

        None also covers an unreadable payload. That is the safe direction: the
        rules downstream refuse to trade without a quote, so a corrupt record
        stops trading on that symbol rather than being guessed at — and it is
        logged loudly, because a cache that cannot read its own writes is a
        format change somebody shipped without a migration.
        """
        raw: Any = await self._client.get(self._key(symbol))
        return None if raw is None else self._decode(symbol, raw)

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Every quote the cache holds for `symbols`, in one round trip.

        Symbols with no quote are absent from the result rather than mapped to
        None — the caller asked which quotes exist, and a dict of mostly-None is
        a worse answer to that than a smaller dict.

        `MGET` rather than a loop: a risk check over a 20-symbol watchlist is
        one round trip instead of twenty, and it is a consistent snapshot.
        """
        if not symbols:
            return {}

        ordered = list(dict.fromkeys(symbols))
        values: Any = await self._client.mget([self._key(s) for s in ordered])

        quotes: dict[str, Quote] = {}
        for symbol, raw in zip(ordered, values, strict=True):
            if raw is None:
                continue
            quote = self._decode(symbol, raw)
            if quote is not None:
                quotes[symbol] = quote
        return quotes

    # ── mapping ─────────────────────────────────────────────────────────────

    def _key(self, symbol: str) -> str:
        return f"{self._key_prefix}{symbol}"

    def _decode(self, symbol: str, raw: object) -> Quote | None:
        try:
            if not isinstance(raw, str):
                raise TypeError(f"expected str from Redis, got {type(raw).__name__}")
            payload = json.loads(raw)
            return Quote(
                symbol=payload["symbol"],
                ts=_parse_ts(payload["ts"]),
                bid=Decimal(payload["bid"]),
                ask=Decimal(payload["ask"]),
                bid_size=Decimal(payload["bid_size"]),
                ask_size=Decimal(payload["ask_size"]),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            log.error(
                "data.quotes.unreadable",
                symbol=symbol,
                error=str(exc),
                hint="a stored quote did not parse — format change without a migration?",
            )
            return None


def _encode(quote: Quote) -> dict[str, str]:
    """Quote → the stored document.

    Every number is a string. JSON has exactly one numeric type and it is a
    binary float, so a price serialised as a JSON number comes back with the
    rounding error rule §1.1 exists to keep out — and it comes back that way
    silently, which is the part that matters.

    Deliberately not the same document as `data.stream._quote_message`. That one
    is a client protocol with a `type` discriminator, versioned by whatever the
    dashboard understands; this one is a storage record. They look alike today
    and are free to diverge, which is why neither imports the other.
    """
    return {
        "symbol": quote.symbol,
        "ts": quote.ts.isoformat(),
        "bid": str(quote.bid),
        "ask": str(quote.ask),
        "bid_size": str(quote.bid_size),
        "ask_size": str(quote.ask_size),
    }


def _parse_ts(raw: str) -> datetime:
    """Stored ISO-8601 → tz-aware UTC (rule §1.2).

    `Quote.__post_init__` rejects a naive timestamp, so this converting rather
    than assuming is what keeps a hand-edited key from becoming a naive quote.
    """
    ts = datetime.fromisoformat(raw)
    if ts.tzinfo is None:
        raise ValueError(f"stored quote timestamp is naive: {raw!r}")
    return ts.astimezone(UTC)


if TYPE_CHECKING:
    # mypy enforces that the adapter still satisfies its port.
    def _conforms(adapter: RedisQuoteCache) -> QuoteCache:
        return adapter
