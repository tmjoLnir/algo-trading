"""Rate limiting on the sign-in endpoint.

The endpoint's only attack is guessing, and bcrypt alone is a brake rather than
a lock (ADR 0010). What matters here is mostly the awkward cases: that a correct
password does not get a free pass once the limit is reached, that one caller
cannot lock out another, and that an outage in the limiter does not lock out
everybody.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from atp_api.auth import hash_password
from atp_api.deps import get_rate_limiter
from atp_api.main import create_app
from atp_api.ratelimit import RedisRateLimiter, client_address
from atp_core.config import Settings, get_settings

PASSWORD = "a-perfectly-ordinary-password"


class FakeRedis:
    """Enough of Redis to count. Can be told to fail."""

    def __init__(self, *, broken: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.broken = broken

    async def incr(self, key: str) -> int:
        if self.broken:
            raise ConnectionError("redis is down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expiries[key] = seconds

    async def ttl(self, key: str) -> int:
        return self.expiries.get(key, -1)


def settings_for(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "api_user": "operator",
        "api_secret_key": SecretStr("k" * 64),
        "api_password_hash": SecretStr(hash_password(PASSWORD)),
        "api_login_attempts": 3,
        "api_login_window_seconds": 60,
        "_env_file": None,
    }
    return Settings(**(base | overrides))


class TestTheCounter:
    async def test_attempts_up_to_the_limit_are_allowed(self) -> None:
        limiter = RedisRateLimiter(FakeRedis())  # type: ignore[arg-type]

        verdicts = [await limiter.check("k", limit=3, window_seconds=60) for _ in range(3)]

        assert all(v.allowed for v in verdicts)

    async def test_the_next_one_is_not(self) -> None:
        limiter = RedisRateLimiter(FakeRedis())  # type: ignore[arg-type]
        for _ in range(3):
            await limiter.check("k", limit=3, window_seconds=60)

        verdict = await limiter.check("k", limit=3, window_seconds=60)

        assert not verdict.allowed
        assert verdict.retry_after > 0

    async def test_refused_attempts_still_count(self) -> None:
        """Trying harder must not shorten the wait.

        A limiter that stopped counting once it started refusing would let a
        caller hold the door open indefinitely at no cost, and would make the
        `Retry-After` it sends a lie.
        """
        redis = FakeRedis()
        limiter = RedisRateLimiter(redis)  # type: ignore[arg-type]

        for _ in range(6):
            await limiter.check("k", limit=3, window_seconds=60)

        assert redis.counts["k"] == 6

    async def test_the_window_is_set_once_not_extended_on_every_attempt(self) -> None:
        """Otherwise a caller who keeps trying never sees the window expire."""
        redis = FakeRedis()
        limiter = RedisRateLimiter(redis)  # type: ignore[arg-type]

        for _ in range(5):
            await limiter.check("k", limit=3, window_seconds=60)

        assert redis.expiries == {"k": 60}

    async def test_separate_keys_do_not_share_a_budget(self) -> None:
        limiter = RedisRateLimiter(FakeRedis())  # type: ignore[arg-type]
        for _ in range(4):
            await limiter.check("one", limit=3, window_seconds=60)

        assert (await limiter.check("two", limit=3, window_seconds=60)).allowed

    async def test_an_unreachable_redis_allows_rather_than_refuses(self) -> None:
        """Fails open, deliberately.

        Failing closed on a *login* limiter locks the operator out of their own
        platform during the outage they most need to look at it. The degraded
        state is bcrypt alone — where this endpoint stood before the limiter
        existed — rather than nothing.
        """
        limiter = RedisRateLimiter(FakeRedis(broken=True))  # type: ignore[arg-type]

        assert (await limiter.check("k", limit=1, window_seconds=60)).allowed


class TestWhoIsCounted:
    @staticmethod
    def request_from(headers: dict[str, str], client: tuple[str, int] | None = ("10.0.0.1", 1)):
        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": client,
        }
        return httpx.Request("POST", "http://test/"), scope

    def test_the_forwarded_address_is_preferred(self) -> None:
        """Behind nginx every request comes from the proxy.

        Without this, every caller shares one bucket — and one attacker locks
        out the operator along with themselves.
        """
        from starlette.requests import Request

        _, scope = self.request_from({"x-forwarded-for": "203.0.113.7, 10.0.0.9"})
        assert client_address(Request(scope)) == "203.0.113.7"

    def test_the_socket_address_is_the_fallback(self) -> None:
        from starlette.requests import Request

        _, scope = self.request_from({})
        assert client_address(Request(scope)) == "10.0.0.1"

    def test_an_unknown_caller_still_gets_a_key(self) -> None:
        """A missing client must not crash the endpoint that refuses it."""
        from starlette.requests import Request

        _, scope = self.request_from({}, client=None)
        assert client_address(Request(scope)) == "unknown"


class TestTheLoginEndpoint:
    @staticmethod
    def client(redis: FakeRedis, settings: Settings) -> httpx.AsyncClient:
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_rate_limiter] = lambda: RedisRateLimiter(redis)  # type: ignore[arg-type]
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        )

    async def test_a_run_of_wrong_passwords_ends_in_429_with_retry_after(self) -> None:
        settings = settings_for()
        async with self.client(FakeRedis(), settings) as client:
            statuses = []
            for _ in range(4):
                response = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "operator", "password": "wrong"},
                    headers={"X-Forwarded-For": "203.0.113.7"},
                )
                statuses.append(response.status_code)

        assert statuses == [401, 401, 401, 429]
        assert response.headers["Retry-After"] == "60"

    async def test_the_correct_password_is_refused_too_once_limited(self) -> None:
        """The limit counts attempts, not failures.

        Otherwise the last guess in a run — the one that happens to be right —
        is the one the limiter waves through, which is precisely the guess it
        exists to prevent.
        """
        settings = settings_for()
        async with self.client(FakeRedis(), settings) as client:
            for _ in range(3):
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "operator", "password": "wrong"},
                    headers={"X-Forwarded-For": "203.0.113.7"},
                )
            response = await client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": PASSWORD},
                headers={"X-Forwarded-For": "203.0.113.7"},
            )

        assert response.status_code == 429

    async def test_one_caller_cannot_lock_out_another(self) -> None:
        """Counted per address, not per username.

        Counting per username would let anyone who knows the operator's name
        lock them out of their own trading platform by failing to log in as
        them — turning a brute-force defence into a denial of service.
        """
        settings = settings_for()
        async with self.client(FakeRedis(), settings) as client:
            for _ in range(5):
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "operator", "password": "wrong"},
                    headers={"X-Forwarded-For": "203.0.113.7"},
                )
            response = await client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": PASSWORD},
                headers={"X-Forwarded-For": "198.51.100.4"},
            )

        assert response.status_code == 200

    @pytest.mark.parametrize("attempts", [1, 5])
    async def test_the_limit_is_the_configured_one(self, attempts: int) -> None:
        settings = settings_for(api_login_attempts=attempts)
        async with self.client(FakeRedis(), settings) as client:
            for _ in range(attempts):
                allowed = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "operator", "password": "wrong"},
                    headers={"X-Forwarded-For": "203.0.113.7"},
                )
            refused = await client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": "wrong"},
                headers={"X-Forwarded-For": "203.0.113.7"},
            )

        assert allowed.status_code == 401
        assert refused.status_code == 429
