"""The `SnapshotStore` port over Redis.

One key per run mode holding one JSON document. The document is small — an
account block, a handful of positions, a capped signal feed — and the only
question ever asked of it is "what is the current picture", which a `GET`
answers in one round trip. No hashes, no per-field keys: a partially-updated
book is precisely what the single aggregate snapshot exists to make impossible,
and anything other than one atomic value would reintroduce it.

**Freshness is a property of the payload, not of the key**, exactly as it is for
`RedisQuoteCache` and for the same reason. The TTL below is garbage collection
for a run mode nobody is running any more. If expiry were the freshness
mechanism then a dead worker would read back as "nothing has ever been
published", and "no book exists" and "the book is four hours old" would become
the same answer — where the second one means something is broken and needs to
say so. `LiveSnapshot.as_of` carries the age; the dashboard displays it and
warns on it (docs/DASHBOARD.md).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from atp_core.dashboard.snapshot import decode_snapshot, encode_snapshot
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from atp_core.dashboard.ports import SnapshotStore
    from atp_core.dashboard.snapshot import LiveSnapshot
    from atp_core.domain import RunMode

log = get_logger(__name__)

#: Prefixed so `atp:dashboard:*` names everything the read path owns and an
#: operator can see it with one `SCAN MATCH`.
KEY_PREFIX = "atp:dashboard:live:"

#: Seven days. Long enough that a weekend plus a holiday does not expire the
#: last book a paper run produced, short enough that a run mode nobody uses
#: does not sit in Redis indefinitely. Not a freshness mechanism — see above.
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60


class RedisSnapshotStore:
    """`SnapshotStore` over Redis.

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

    def _key(self, run_mode: RunMode) -> str:
        return f"{self._key_prefix}{run_mode.value}"

    async def put(self, snapshot: LiveSnapshot) -> None:
        await self._client.set(
            self._key(snapshot.run_mode),
            json.dumps(encode_snapshot(snapshot)),
            ex=self._ttl_seconds,
        )

    async def get(self, run_mode: RunMode) -> LiveSnapshot | None:
        """The stored snapshot, or None if nothing has been published.

        An unreadable payload **raises** rather than reading as None. That is
        the opposite of `RedisQuoteCache`, which returns None on a corrupt
        record, and the difference is what the caller does next: a missing
        quote stops trading on that symbol, which is safe, whereas a missing
        book renders a dashboard reporting no positions. Here the safe
        direction is to fail the request and let the client keep the last good
        data on screen, clearly stale (docs/DASHBOARD.md).
        """
        raw: Any = await self._client.get(self._key(run_mode))
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise TypeError(f"expected str from Redis, got {type(raw).__name__}")
        try:
            return decode_snapshot(json.loads(raw))
        except Exception as exc:
            log.error(
                "dashboard.snapshot.unreadable",
                run_mode=run_mode.value,
                error=str(exc),
                hint="a stored snapshot did not parse — format change without a version bump?",
            )
            raise


if TYPE_CHECKING:
    # mypy enforces that the adapter still satisfies its port.
    def _conforms(adapter: RedisSnapshotStore) -> SnapshotStore:
        return adapter
