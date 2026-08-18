"""The kill switch.

The one control on docs/SAFETY.md's list whose failure mode is written down:
layer 6 fails "Redis unreachable — **fail closed**". So the test that matters
most here is not that engaging works, it is that a broken Redis stops trading
rather than waving it through.

The asymmetry between engaging and clearing is the other thing under test.
Stopping should be reflexive and take no argument beyond a reason; restarting
should require a named human and leave a record. A switch that is easy to
clear is a switch someone clears at 3am to make an alert go away.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from atp_core.channels import CHANNEL_HALTS
from atp_core.risk.killswitch import (
    HaltReason,
    HaltScope,
    RedisKillSwitch,
)


class FakeRedis:
    """Just enough Redis, in a dict. `broken` makes every call raise."""

    def __init__(self, broken: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []
        self.broken = broken

    def _guard(self) -> None:
        if self.broken:
            raise ConnectionError("redis is down")

    def get(self, key: str) -> str | None:
        self._guard()
        return self.store.get(key)

    def mget(self, keys: list[str]) -> list[str | None]:
        self._guard()
        return [self.store.get(k) for k in keys]

    def set(self, key: str, value: str) -> None:
        self._guard()
        self.store[key] = value

    def delete(self, key: str) -> int:
        self._guard()
        return 1 if self.store.pop(key, None) is not None else 0

    def scan_iter(self, match: str) -> list[str]:
        self._guard()
        prefix = match.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]

    def publish(self, channel: str, message: str) -> int:
        self._guard()
        self.published.append((channel, message))
        return 0


def switch(**kwargs: Any) -> tuple[RedisKillSwitch, FakeRedis]:
    client = FakeRedis(**kwargs)
    return RedisKillSwitch(client), client  # type: ignore[arg-type]


class TestFailClosed:
    def test_an_unreachable_redis_halts_trading(self) -> None:
        """docs/SAFETY.md layer 6. A false halt costs missed opportunity; a
        false clear trades the account through whatever broke Redis."""
        ks, _ = switch(broken=True)
        assert ks.is_engaged() is True
        assert ks.is_engaged("strat", "SPY") is True

    def test_a_reachable_redis_with_no_halt_allows_trading(self) -> None:
        """The other half — failing closed must not mean always closed."""
        ks, _ = switch()
        assert ks.is_engaged() is False

    def test_engaging_does_not_swallow_a_redis_failure(self) -> None:
        """Engaging must never fail quietly. `is_engaged` is already refusing
        everything on the same outage, so the loud path is the safe one."""
        ks, _ = switch(broken=True)
        with pytest.raises(ConnectionError):
            ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")


class TestScopes:
    def test_a_global_halt_covers_everything(self) -> None:
        ks, _ = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        assert ks.is_engaged()
        assert ks.is_engaged("any_strategy", "ANY")

    def test_a_strategy_halt_covers_only_that_strategy(self) -> None:
        ks, _ = switch()
        ks.engage(HaltScope.STRATEGY, HaltReason.MANUAL, "ops", target="sma_crossover")
        assert ks.is_engaged("sma_crossover", "SPY")
        assert not ks.is_engaged("mean_reversion", "SPY")
        assert not ks.is_engaged()

    def test_a_symbol_halt_covers_only_that_symbol(self) -> None:
        ks, _ = switch()
        ks.engage(HaltScope.SYMBOL, HaltReason.DATA_FEED_LOST, "worker", target="SPY")
        assert ks.is_engaged("any", "SPY")
        assert not ks.is_engaged("any", "QQQ")

    def test_a_scoped_halt_needs_a_target(self) -> None:
        ks, _ = switch()
        with pytest.raises(ValueError, match="needs a target"):
            ks.engage(HaltScope.STRATEGY, HaltReason.MANUAL, "ops")


class TestEngageAndClear:
    def test_the_record_carries_who_and_why(self) -> None:
        ks, _ = switch()
        before = datetime.now(UTC)
        record = ks.engage(
            HaltScope.GLOBAL, HaltReason.RECONCILIATION_MISMATCH, "reconciler", detail="3 orphans"
        )
        assert record.reason is HaltReason.RECONCILIATION_MISMATCH
        assert record.engaged_by == "reconciler"
        assert record.detail == "3 orphans"
        assert record.engaged_at >= before

    def test_re_engaging_keeps_the_original_record(self) -> None:
        """Idempotent, and specifically: the second engagement does not reset
        the timestamp. A halt that keeps re-stamping itself erases the only
        evidence of when trading actually stopped."""
        ks, _ = switch()
        first = ks.engage(HaltScope.GLOBAL, HaltReason.DAILY_LOSS_LIMIT, "risk", detail="down 3%")
        second = ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "someone_else", detail="different")

        assert second == first
        assert second.engaged_by == "risk"
        assert second.reason is HaltReason.DAILY_LOSS_LIMIT

    def test_clearing_resumes_trading(self) -> None:
        ks, _ = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        ks.clear(HaltScope.GLOBAL, cleared_by="alice")
        assert not ks.is_engaged()

    def test_clearing_requires_a_named_human(self) -> None:
        """The asymmetry docs/SAFETY.md asks for: engaging needs no
        confirmation, clearing needs a name."""
        ks, _ = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        for anonymous in ("", "   "):
            with pytest.raises(ValueError, match="named human"):
                ks.clear(HaltScope.GLOBAL, cleared_by=anonymous)
        assert ks.is_engaged(), "a refused clear must leave the halt in place"

    def test_clearing_one_scope_leaves_the_others(self) -> None:
        ks, _ = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        ks.engage(HaltScope.SYMBOL, HaltReason.DATA_FEED_LOST, "worker", target="SPY")
        ks.clear(HaltScope.GLOBAL, cleared_by="alice")

        assert not ks.is_engaged("any", "QQQ")
        assert ks.is_engaged("any", "SPY"), "the symbol halt was not the one cleared"

    def test_clearing_something_that_was_not_halted_is_not_an_error(self) -> None:
        """An operator clearing defensively should not get an exception for
        being early."""
        ks, _ = switch()
        ks.clear(HaltScope.GLOBAL, cleared_by="alice")


class TestActiveHalts:
    def test_lists_every_scope(self) -> None:
        ks, _ = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        ks.engage(HaltScope.STRATEGY, HaltReason.UNHANDLED_EXCEPTION, "worker", target="sma")
        ks.engage(HaltScope.SYMBOL, HaltReason.DATA_FEED_LOST, "worker", target="SPY")

        halts = ks.active_halts()
        assert len(halts) == 3
        assert {h.scope for h in halts} == set(HaltScope)
        assert {h.target for h in halts} == {None, "sma", "SPY"}

    def test_empty_when_nothing_is_halted(self) -> None:
        ks, _ = switch()
        assert ks.active_halts() == []

    def test_a_broken_redis_raises_rather_than_reporting_all_clear(self) -> None:
        """A display read, not a trading gate. "Nothing is halted" is exactly
        the wrong thing to show a human when the truth is unknown."""
        ks, _ = switch(broken=True)
        with pytest.raises(ConnectionError):
            ks.active_halts()


class TestRoundTrip:
    def test_a_record_survives_serialisation(self) -> None:
        """It crosses a process boundary as JSON — that is the entire reason
        the state is in Redis rather than in memory."""
        ks, _ = switch()
        original = ks.engage(
            HaltScope.SYMBOL,
            HaltReason.BROKER_UNREACHABLE,
            "worker-2",
            detail="timeout after 3 retries",
            target="AAPL",
        )
        assert ks.active_halts() == [original]


class TestAnnouncements:
    """A halt on the screen within a second, not within five minutes.

    The state is in Redis before any of this runs and every risk check reads
    that state, so what is under test here is the *notification*: `atp_api.ws`
    fans it out to every open dashboard regardless of what the client
    subscribed to, because a trading halt is not something to opt into.
    """

    def test_engaging_announces_the_record(self) -> None:
        ks, redis = switch()

        ks.engage(HaltScope.GLOBAL, HaltReason.DAILY_LOSS_LIMIT, "risk_engine", detail="-3.2%")

        channel, raw = redis.published[-1]
        message = json.loads(raw)
        assert channel == CHANNEL_HALTS
        assert message["type"] == "halt"
        assert message["transition"] == "engaged"
        assert message["reason"] == "daily_loss_limit"
        assert message["engaged_by"] == "risk_engine"

    def test_clearing_announces_it_too(self) -> None:
        """The banner has to come *down* as well. An operator who cleared a halt
        and watched the screen stay red would clear it again."""
        ks, redis = switch()
        ks.engage(HaltScope.SYMBOL, HaltReason.MANUAL, "ops", target="AAPL")

        ks.clear(HaltScope.SYMBOL, cleared_by="alice", target="AAPL")

        message = json.loads(redis.published[-1][1])
        assert message["transition"] == "cleared"
        assert message["target"] == "AAPL"
        assert message["actor"] == "alice"

    def test_clearing_a_halt_that_was_not_engaged_announces_nothing(self) -> None:
        """There is no transition to report, and a phantom "cleared" would take
        a banner down that another scope's halt is still holding up."""
        ks, redis = switch()

        ks.clear(HaltScope.GLOBAL, cleared_by="alice")

        assert redis.published == []

    def test_re_engaging_does_not_announce_twice(self) -> None:
        """`engage` is idempotent and keeps the original record. A second
        announcement would carry the same halt with the same timestamp and tell
        every dashboard something happened that did not."""
        ks, redis = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        assert len(redis.published) == 1

    def test_a_failed_announcement_does_not_fail_the_halt(self) -> None:
        """Engaging must never fail quietly, and an exception raised on the way
        out of the announcement would break that promise in the one direction
        that matters — by making an unpublishable halt look like one that did
        not happen.
        """
        ks, redis = switch()

        class Unpublishable(type(redis)):  # type: ignore[misc]
            def publish(self, channel: str, message: str) -> int:
                raise ConnectionError("pub/sub is down")

        broken = Unpublishable()
        ks._client = broken  # type: ignore[attr-defined]

        record = ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        assert record.reason is HaltReason.MANUAL
        assert ks.is_engaged() is True
