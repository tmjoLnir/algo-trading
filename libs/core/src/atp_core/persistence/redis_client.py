"""Redis connection management — the counterpart to `db.py`.

Named `redis_client` rather than `redis` on purpose: a module called `redis.py`
inside a package that also imports the third-party `redis` resolves correctly
under Python 3's absolute imports, and confuses every human and half the tooling
that reads it afterwards.

One client per process, shared. `redis.asyncio.Redis` is a connection *pool*
behind a client object, so passing one around is the intended usage — building a
second one per component would open a second pool for no gain.
"""

from __future__ import annotations

from typing import Any

from redis import Redis as SyncRedis
from redis.asyncio import Redis

#: How often an idle connection is pinged. The worker holds Redis connections
#: idle across quiet market periods — overnight, weekends — and the same class
#: of failure `pool_pre_ping` guards against in `db.py` applies here: a
#: connection that died silently surfaces at the moment a quote needs writing.
HEALTH_CHECK_SECONDS = 30

#: Bounded so a Redis that has stopped answering fails the caller rather than
#: hanging the ingest loop. Losing a quote write is recoverable; wedging the
#: process that owns the market-data connection is not.
SOCKET_TIMEOUT_SECONDS = 5.0


def create_redis(redis_url: str, **kwargs: Any) -> Redis:
    """Build the shared async client.

    `decode_responses=True` so reads come back as `str`. Both adapters here
    store JSON text, and the alternative is decoding bytes at every call site —
    where sooner or later one of them forgets and compares a `bytes` to a `str`.

    The result is bound to a typed local before being returned. `from_url` is a
    classmethod whose return annotation redis-py has not always carried, and an
    unannotated return is `Any` — which `--strict` then rejects on the way out
    of a function declared `-> Redis`. The same `no-any-return` that took CI
    down on #18, one library away.
    """
    client: Redis = Redis.from_url(
        redis_url,
        decode_responses=True,
        health_check_interval=HEALTH_CHECK_SECONDS,
        socket_keepalive=True,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        **kwargs,
    )
    return client


async def close_redis(client: Redis) -> None:
    """Release the pool. Safe to call twice."""
    await client.aclose()


def create_sync_redis(redis_url: str, **kwargs: Any) -> SyncRedis:
    """Build a *synchronous* client, for the kill switch and nothing else.

    `RedisKillSwitch` is synchronous because `KillSwitchRule.check` is — the
    risk chain is a synchronous decision on the path of every order, and making
    it async to reach one key would colour the whole chain. So a process that
    both ingests (async) and can halt (sync) genuinely needs two clients
    against the same server; this is not an oversight to be tidied away into
    one.

    Same timeouts as the async client, and for a sharper reason. The kill switch
    fails *closed*: an unreachable Redis reports engaged and trading stops. An
    unbounded socket timeout would turn "Redis is slow" into a hung risk check
    rather than a halt, which is the one failure mode fail-closed exists to rule
    out.
    """
    client: SyncRedis = SyncRedis.from_url(
        redis_url,
        decode_responses=True,
        health_check_interval=HEALTH_CHECK_SECONDS,
        socket_keepalive=True,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        **kwargs,
    )
    return client
