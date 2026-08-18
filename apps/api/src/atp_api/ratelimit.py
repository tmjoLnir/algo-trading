"""Rate limiting, over the Redis that is already there.

Scoped to the **unauthenticated** surface, and specifically to signing in. That
is a deliberate limit rather than an unfinished one, so the reasoning is worth
stating.

The threat this platform has is someone guessing the one password. That happens
at `/auth/login`, before any session exists, and nothing else about the API is
reachable until it succeeds. Throttling the authenticated surface as well would
be defending against an operator abusing their own single-user trading platform,
at the cost of a limit that can misfire on the dashboard's own polling — and the
dashboard polls the same endpoints on a timer, from a tab that may be open in
several windows.

**`/risk/halt` must never be rate limited**, whatever is added later. It is the
one endpoint whose whole purpose is to work in the worst moment, and the same
reasoning that lets a read-only session call it (ADR 0009) applies with more
force here: a limiter that refuses a halt has chosen the wrong thing to protect.

**This fails open.** If Redis is unreachable the attempt is allowed and the
failure is logged `CRITICAL`. Failing closed on a login limiter means an
operator locked out of their own platform by an outage — during which they most
need to look at it — and the degraded state is not "no protection" but "bcrypt
alone", which is where this endpoint stood before this module existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import Request
from redis.asyncio import Redis

from atp_core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitVerdict:
    allowed: bool
    #: Seconds until the window rolls over. Sent as `Retry-After` so a client is
    #: told when to come back rather than left to guess, and so an honest client
    #: stops hammering.
    retry_after: int = 0


class RateLimiter(Protocol):
    """Counts attempts against a key and says whether to allow this one."""

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitVerdict: ...


def client_address(request: Request) -> str:
    """The caller's address, as far as it can be known.

    `X-Forwarded-For`'s first hop, because in the deployed arrangement every
    request arrives from nginx and `request.client.host` is the proxy for all of
    them — which would put every caller in one bucket and let one attacker lock
    out everybody, including the operator.

    That header is caller-supplied and trivially spoofed, which matters and is
    survivable here: this stack always sits behind its own nginx
    (infra/docker/web.nginx.conf), which overwrites it. Exposed without that
    proxy, an attacker could rotate the header to sidestep the limit — worth
    knowing before anyone puts this on a public address, which docs/SAFETY.md
    already says not to do.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RedisRateLimiter:
    """A fixed-window counter per key.

    Fixed rather than sliding, and the imprecision is bounded and acceptable: a
    caller who spends their whole allowance at the end of one window and again
    at the start of the next gets through twice the limit across that seam. For
    a login limiter measured in tens of attempts per five minutes, that is the
    difference between 10 and 20 guesses against a bcrypt hash, which is not the
    difference between safe and unsafe. A sliding window costs a sorted set per
    caller and the bookkeeping to trim it.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitVerdict:
        """Count this attempt and say whether it is allowed.

        The counter is incremented before the verdict, so refused attempts count
        too. That is intentional: a caller who keeps trying while limited should
        not shorten their own wait by doing so.
        """
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, window_seconds)
            if count <= limit:
                return RateLimitVerdict(allowed=True)
            ttl = await self._redis.ttl(key)
            return RateLimitVerdict(allowed=False, retry_after=max(ttl, 1))
        except Exception as exc:
            log.critical(
                "ratelimit.unreachable",
                error=str(exc),
                key=key,
                effect="failing open — the attempt is allowed and is not counted",
            )
            return RateLimitVerdict(allowed=True)


class AlwaysAllows:
    """The limiter used when the application has no Redis client at all.

    Distinct from Redis being *unreachable*, which `RedisRateLimiter.check`
    handles by failing open with a `CRITICAL`. This is the case where the app was
    built without its lifespan having run — normal in a unit test driving the app
    over ASGI, a misconfiguration anywhere else.

    Allowing rather than refusing, for the reason the module docstring gives: a
    login limiter that fails closed locks the operator out of their own platform,
    and the degraded state here is bcrypt alone rather than nothing.
    """

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitVerdict:
        return RateLimitVerdict(allowed=True)
