"""Pub/sub fan-out — the `EventPublisher` port over Redis.

The third leg of the real-time pipeline (docs/DATA.md): the worker owns the one
upstream market-data connection and publishes here, and every API replica
subscribes. That is what lets the dashboard have many readers behind one
vendor connection.

Redis pub/sub is fire-and-forget with no persistence and no delivery guarantee.
That is the correct trade for this traffic and worth being explicit about: a
subscriber that is down misses ticks and catches up on the dashboard's next
read, which is the authoritative path (`atp_api.ws`). Nothing that must not be
lost may travel over this — a fill, a halt or a position change belongs in the
database first and on a channel second.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from atp_core.logging import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from atp_core.data.ports import EventPublisher

log = get_logger(__name__)


class RedisEventPublisher:
    """`EventPublisher` over Redis pub/sub.

    Raises rather than swallowing. The port documents publishing as best-effort
    and callers as expected to absorb its failures — `data.stream` does exactly
    that — but the swallowing belongs at the call site, where there is enough
    context to know that a dropped tick is survivable. An adapter that returned
    quietly on failure would take that decision away from everyone.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        """Send `message` to `channel`.

        Redis reports how many subscribers received it; that count is
        deliberately dropped rather than returned or logged. Zero is the normal
        state of a deployment with no dashboard open, so it distinguishes
        nothing useful on the hot path, and returning it would put a value in a
        signature the port declares as `None`.
        """
        _reject_floats(channel, message)
        await self._client.publish(channel, json.dumps(message))


def _reject_floats(channel: str, message: dict[str, Any], path: str = "") -> None:
    """Refuse to publish a binary float (rule §1.1).

    The guard exists because this is the last place a price can be checked
    before it leaves the process. Producers render `Decimal` as a string; a
    float arriving here means somebody built a message the other way, and JSON
    would encode it without complaint — the corruption would first become
    visible on a dashboard, as a price ending in a run of 9s, long after the
    code that caused it shipped.

    A `Decimal` is not rejected: `json.dumps` cannot encode one and raises on
    its own, which is a clear enough error without duplicating it here.
    """
    for key, value in message.items():
        where = f"{path}{key}"
        if isinstance(value, bool):
            continue  # bool is an int subclass and is a perfectly good flag
        if isinstance(value, float):
            raise ValueError(
                f"refusing to publish a float on {channel}: {where}={value!r}. "
                "Render prices and quantities as strings (rule §1.1)."
            )
        if isinstance(value, dict):
            _reject_floats(channel, value, f"{where}.")


if TYPE_CHECKING:
    # mypy enforces that the adapter still satisfies its port.
    def _conforms(adapter: RedisEventPublisher) -> EventPublisher:
        return adapter
