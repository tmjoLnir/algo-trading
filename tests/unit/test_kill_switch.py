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

from atp_core.alerts import Alert, Severity
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

    def test_clearing_returns_the_halt_it_removed(self) -> None:
        """The record, not an acknowledgement.

        `/risk/resume` reports what was cleared from this, and the fields that
        matter are the *original* halt's: an operator who has just resumed wants
        to see the reason they overrode, and if it names the risk layer rather
        than themselves they have just cancelled a decision a machine made.
        """
        ks, _ = switch()
        engaged = ks.engage(HaltScope.GLOBAL, HaltReason.DAILY_LOSS_LIMIT, "risk", detail="-3.2%")

        cleared = ks.clear(HaltScope.GLOBAL, cleared_by="alice")

        assert cleared == engaged
        assert cleared is not None
        assert cleared.engaged_by == "risk", "the clearer is not the engager"
        assert cleared.reason is HaltReason.DAILY_LOSS_LIMIT

    def test_clearing_nothing_returns_none(self) -> None:
        """The half that carries the weight.

        "Resumed trading" and "there was nothing to resume" are both successes
        and read completely differently on a screen, and this is the only thing
        that separates them — `clear` refuses to treat the second as an error,
        so a caller cannot learn it from an exception.
        """
        ks, _ = switch()

        assert ks.clear(HaltScope.GLOBAL, cleared_by="alice") is None

    def test_clearing_a_target_that_is_not_halted_returns_none(self) -> None:
        """Keyed on the pair, so a near miss is a miss.

        Clearing SPY while QQQ is the halted one resumes nothing, and has to say
        so: an operator told "resumed" here would walk away from a symbol that
        is still stopped.
        """
        ks, _ = switch()
        ks.engage(HaltScope.SYMBOL, HaltReason.DATA_FEED_LOST, "worker", target="QQQ")

        assert ks.clear(HaltScope.SYMBOL, cleared_by="alice", target="SPY") is None
        assert ks.is_engaged("any", "QQQ"), "the halt that was not named must stand"


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
        ks._client = broken

        record = ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        assert record.reason is HaltReason.MANUAL
        assert ks.is_engaged() is True


class RecordingSink:
    """An `AlertSink` that keeps what it was given."""

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)


class TestAlerting:
    """Reaching a human who is not looking at a screen (docs/SAFETY.md).

    The kill switch is where this belongs because every automated halt already
    arrives here — a lost feed, a reconciliation mismatch, a supervised task
    dying. Hooking the three separately would mean a fourth halt reason added
    later silently alerts nobody.
    """

    def test_a_new_halt_alerts_critical(self) -> None:
        redis = FakeRedis()
        sink = RecordingSink()
        ks = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]

        ks.engage(HaltScope.GLOBAL, HaltReason.DATA_FEED_LOST, "staleness-monitor", "no ticks")

        assert len(sink.sent) == 1
        alert = sink.sent[0]
        assert alert.severity is Severity.CRITICAL
        assert "data_feed_lost" in alert.title
        assert "staleness-monitor" in alert.body

    def test_re_engaging_an_active_halt_alerts_once(self) -> None:
        """The property the whole placement exists for. `StalenessMonitor` polls
        every five seconds and re-engages while the outage lasts; alerting on
        each would be twelve notifications a minute, which is the same as none.

        There is no dedup flag anywhere — `engage` returns early when a halt is
        already recorded, so the Redis state *is* the deduplication and cannot
        drift out of step with it.
        """
        redis = FakeRedis()
        sink = RecordingSink()
        ks = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]

        for _ in range(12):
            ks.engage(HaltScope.GLOBAL, HaltReason.DATA_FEED_LOST, "staleness-monitor")

        assert len(sink.sent) == 1

    def test_a_second_reason_alerts_again(self) -> None:
        """Different scopes are different halts. Collapsing them would hide the
        second thing that broke behind the first."""
        redis = FakeRedis()
        sink = RecordingSink()
        ks = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]

        ks.engage(HaltScope.GLOBAL, HaltReason.DATA_FEED_LOST, "staleness-monitor")
        ks.engage(HaltScope.SYMBOL, HaltReason.RECONCILIATION_MISMATCH, "reconciler", target="SPY")

        assert len(sink.sent) == 2
        assert sink.sent[1].severity is Severity.CRITICAL
        assert "SPY" in sink.sent[1].body

    def test_clearing_alerts_info(self) -> None:
        """A halt with no matching all-clear is how somebody spends an afternoon
        believing the platform is stopped when it is trading."""
        redis = FakeRedis()
        sink = RecordingSink()
        ks = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]

        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "jo")
        ks.clear(HaltScope.GLOBAL, cleared_by="jo")

        assert [a.severity for a in sink.sent] == [Severity.CRITICAL, Severity.INFO]
        assert "jo" in sink.sent[1].body

    def test_clearing_nothing_alerts_nothing(self) -> None:
        """Clearing an unengaged scope is a no-op, and a notification saying
        trading resumed when it never stopped is worse than silence."""
        redis = FakeRedis()
        sink = RecordingSink()
        ks = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]

        ks.clear(HaltScope.GLOBAL, cleared_by="jo")

        assert sink.sent == []

    def test_an_alert_carries_no_numbers_from_the_book(self) -> None:
        """`alerts.ports` states the rule: a notification renders on a lock
        screen and travels through a third party, so it says what happened and
        never what the account is worth. The detail is the caller's, so this
        pins the fields this class composes."""
        redis = FakeRedis()
        sink = RecordingSink()
        ks = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]

        ks.engage(HaltScope.GLOBAL, HaltReason.DAILY_LOSS_LIMIT, "risk-engine")

        alert = sink.sent[0]
        assert "Check the dashboard" in alert.body
        assert set(alert.context) == {"scope", "reason", "engaged_by"}

    def test_a_failing_sink_does_not_fail_the_halt(self) -> None:
        """The same rule as the announcement above, and ADR 0010's for the audit
        trail. A platform that refused to stop trading because a push service
        was down would have its failure modes exactly inverted.

        `AlertSink` says implementations must not raise; this is what happens
        when one does anyway, because "must not" is not "cannot".
        """
        redis = FakeRedis()

        class Exploding:
            def send(self, alert: Alert) -> None:
                raise RuntimeError("push service is down")

        ks = RedisKillSwitch(redis, alerts=Exploding())  # type: ignore[arg-type]

        record = ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        assert record.reason is HaltReason.MANUAL
        assert ks.is_engaged() is True

    def test_no_sink_still_halts(self) -> None:
        """Alerting is opt-in and the kill switch predates it. An unalertable
        halt is still a halt."""
        redis = FakeRedis()
        ks = RedisKillSwitch(redis)  # type: ignore[arg-type]

        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        assert ks.is_engaged() is True
