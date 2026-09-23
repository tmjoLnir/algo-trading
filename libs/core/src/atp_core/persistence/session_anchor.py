"""The `SessionAnchorStore` port over Redis — each session's starting equity.

One key per run mode per exchange-local session date, holding the anchor as a
decimal string (rule §1.1). The same client and the same TTL reasoning as
`RedisWorkerStatusStore`: the TTL is garbage collection for sessions nobody
will ask about again, not a freshness mechanism, because a key is only ever
read for the session it names.

**Unlike that store, an unreadable value raises.** A status blob that does not
parse costs a settings screen its decoration. An anchor that does not parse is
a question about how much the platform may still lose today, and answering it
with "not anchored yet" would re-anchor to whatever the book is worth now —
the second allowance this store exists to prevent.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from atp_core.errors import ATPError

if TYPE_CHECKING:
    from datetime import date

    from redis.asyncio import Redis

    from atp_core.domain import RunMode

#: Under `atp:risk:`, so everything the risk layer keeps can be listed with one
#: `SCAN MATCH`.
KEY_PREFIX = "atp:risk:session_anchor:"

#: Seven days, matching the other worker-owned keys. A session's anchor is read
#: only during that session, so this is housekeeping rather than meaning.
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60


class SessionAnchorUnreadableError(ATPError):
    """A stored anchor exists and is not a number."""


class RedisSessionAnchorStore:
    """`SessionAnchorStore` over Redis.

    Takes a client rather than a URL: core does not open sockets on its own
    behalf (CLAUDE.md §1.3).
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

    def _key(self, run_mode: RunMode, day: date) -> str:
        return f"{self._key_prefix}{run_mode.value}:{day.isoformat()}"

    async def get(self, run_mode: RunMode, day: date) -> Decimal | None:
        raw: Any = await self._client.get(self._key(run_mode, day))
        if raw is None:
            return None
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            equity = Decimal(text)
        except InvalidOperation as exc:
            raise SessionAnchorUnreadableError(
                f"the {run_mode.value} anchor for {day.isoformat()} is not a number"
            ) from exc
        if not equity.is_finite():
            raise SessionAnchorUnreadableError(
                f"the {run_mode.value} anchor for {day.isoformat()} is not finite"
            )
        return equity

    async def put(self, run_mode: RunMode, day: date, equity: Decimal) -> None:
        await self._client.set(self._key(run_mode, day), str(equity), ex=self._ttl_seconds)
