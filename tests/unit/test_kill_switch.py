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
from structlog.testing import capture_logs

from atp_core.alerts import Alert, Severity
from atp_core.channels import CHANNEL_HALTS
from atp_core.errors import KillSwitchUnavailableError
from atp_core.risk.killswitch import (
    HaltReason,
    HaltScope,
    RedisKillSwitch,
)
from tests.fakes import FakeRedis


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
    """A halt on the screen within a second, not whenever somebody next reloads.

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


class TestTheHaltCarriesItsReason:
    """ADR 0029. A halt's `reason` says what stopped trading; its `impugned`
    says which positions the platform cannot prove. The exit carve-out reads the
    second, and these are the tests that keep the two from collapsing into one.
    """

    def test_engaging_with_evidence_records_it(self) -> None:
        ks, _ = switch()

        record = ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            detail="broker says 300, we say 100",
            unproven_symbols=("SPY", "QQQ"),
        )

        assert record.unproven_symbols == frozenset({"SPY", "QQQ"})
        assert record.book_is_unproven
        assert len(record.impugned) == 1
        assert record.impugned[0].reason is HaltReason.RECONCILIATION_MISMATCH
        assert record.impugned[0].by == "reconciler"

    def test_engaging_without_evidence_impugns_nothing(self) -> None:
        """The case that must stay the majority. A manual halt, a feed halt and
        a rate-limit storm all stop trading without saying anything about any
        position, and an exit out of each of them must still be permitted."""
        ks, _ = switch()

        record = ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        assert record.impugned == ()
        assert record.unproven_symbols == frozenset()
        assert not record.book_is_unproven

    def test_evidence_survives_the_round_trip(self) -> None:
        """The rule reads this back out of Redis, not off the object `engage`
        returned. An impugnment that did not serialise would be a carve-out that
        works in-process and is silently absent in production."""
        ks, _ = switch()
        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.BROKER_UNREACHABLE,
            "router",
            unproven_symbols=("SPY",),
        )

        state = ks.halt_state("any_strategy", "SPY")

        assert state.engaged
        assert state.position_is_unproven("SPY")
        assert not state.position_is_unproven("QQQ")

    def test_symbols_are_normalised(self) -> None:
        ks, _ = switch()

        record = ks.engage(
            HaltScope.GLOBAL, HaltReason.MANUAL, "ops", unproven_symbols=(" spy ", "SPY", "qqq")
        )

        assert record.impugned[0].symbols == ("QQQ", "SPY")

    def test_a_blank_symbol_is_refused(self) -> None:
        """The `_NO_SYMBOL` trap, and the reason `_clean_symbols` exists.

        `Discrepancy` carries the empty string for a cash mismatch, which has no
        symbol. Letting that through would store an impugnment naming `""` — a
        symbol no order can ever match, so nothing could clear it, and an
        operator reading the record would be told a position is unproven without
        being told which. Loud is the only safe answer.
        """
        ks, _ = switch()

        with pytest.raises(ValueError, match="cannot be blank"):
            ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops", unproven_symbols=("SPY", ""))


class TestEscalation:
    """The one-way latch. A halt's reason may rise when evidence arrives and may
    never fall, and nothing else about the record moves at all."""

    def test_evidence_arriving_at_a_bare_halt_raises_its_reason(self) -> None:
        ks, redis = switch()
        first = ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops", detail="eyeballing it")

        second = ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            detail="SPY: broker 300, book 100",
            unproven_symbols=("SPY",),
        )

        assert second.reason is HaltReason.RECONCILIATION_MISMATCH
        assert second.unproven_symbols == frozenset({"SPY"})
        # …and the original is kept rather than overwritten.
        assert second.escalation is not None
        assert second.escalation.from_reason is HaltReason.MANUAL
        assert second.escalation.by == "reconciler"
        # The halt is still the one that started, to the second.
        assert second.engaged_at == first.engaged_at
        assert second.engaged_by == "ops"
        assert second.detail == "eyeballing it"
        assert redis.evals == 1, "the merge must land through the compare-and-set"

    def test_a_bare_halt_cannot_lower_a_standing_one(self) -> None:
        """The half that makes it a latch rather than a last-writer-wins field.

        An operator pressing HALT beside a standing `reconciliation_mismatch`
        must not turn it into a manual halt — that would drop the impugnment
        the exit carve-out reads, and the flatten that was refused a second ago
        would go through against a quantity nobody can vouch for.
        """
        ks, _ = switch()
        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY",),
        )

        after = ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        assert after.reason is HaltReason.RECONCILIATION_MISMATCH
        assert after.unproven_symbols == frozenset({"SPY"})
        assert after.escalation is None

    def test_a_second_reason_without_evidence_changes_nothing(self) -> None:
        """`manual` then `data_feed_lost` — the no-change case today, and it
        stays the no-change case. Neither says anything about a position, so
        there is nothing to escalate to."""
        ks, redis = switch()
        first = ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        after = ks.engage(HaltScope.GLOBAL, HaltReason.DATA_FEED_LOST, "monitor")

        assert after == first
        assert redis.evals == 0, "nothing changed, so nothing should have been written"

    def test_new_symbols_are_appended_without_re_escalating(self) -> None:
        """A second incident adds evidence. It does not restart the halt, and it
        does not overwrite the escalation that recorded the first one."""
        ks, _ = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY",),
        )

        third = ks.engage(
            HaltScope.GLOBAL,
            HaltReason.BROKER_UNREACHABLE,
            "router",
            unproven_symbols=("QQQ",),
        )

        assert third.unproven_symbols == frozenset({"SPY", "QQQ"})
        assert len(third.impugned) == 2
        # The reason and the escalation both still describe the *first* rise.
        assert third.reason is HaltReason.RECONCILIATION_MISMATCH
        assert third.escalation is not None
        assert third.escalation.from_reason is HaltReason.MANUAL

    def test_a_repeated_impugnment_is_not_appended_again(self) -> None:
        """The property that keeps a two-hour incident from becoming 24 alerts.

        `reconcile_positions` runs every five minutes and a mismatch stands
        until a human fixes it, so the *same* finding arrives over and over.
        Appending each one would grow the record without bound and re-alert on
        every pass — the dedup ADR 0012 already applies to the halt itself.
        """
        ks, redis = switch()
        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY",),
        )

        for _ in range(24):
            latest = ks.engage(
                HaltScope.GLOBAL,
                HaltReason.RECONCILIATION_MISMATCH,
                "reconciler",
                unproven_symbols=("SPY",),
            )

        assert len(latest.impugned) == 1
        assert redis.evals == 0, "a repeated finding is not a write"

    def test_a_partially_overlapping_finding_brings_its_new_symbol_in(self) -> None:
        """The boundary the subset test is actually for, and the shape the
        reconciler produces every five minutes.

        Each pass re-reports the **whole** disputed set, so the second one is
        `{SPY, QQQ}` against a standing `{SPY}` — overlapping, not disjoint and
        not a repeat. `not set(...) <= existing` says fresh, and QQQ becomes
        unproven. Written as the obvious wrong thing —
        `not (set(...) & existing)`, "have I seen any of these before" — the
        same call answers "already covered" and QQQ is silently closeable while
        the reconciler goes on reporting it. Every other test in this class
        passes under that mutation: the exact repeat, the strict subset and the
        disjoint case all agree with it. Only this one disagrees.
        """
        ks, _ = switch()
        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY",),
        )

        after = ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY", "QQQ"),
        )

        assert after.unproven_symbols == frozenset({"SPY", "QQQ"})
        assert ks.halt_state(symbol="QQQ").position_is_unproven("QQQ")

    def test_a_subset_of_a_standing_impugnment_is_not_appended(self) -> None:
        """The reconciler's finding shrinks as positions are fixed one at a
        time. `{SPY}` arriving against a standing `{SPY, QQQ}` is not news."""
        ks, _ = switch()
        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY", "QQQ"),
        )

        after = ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY",),
        )

        assert len(after.impugned) == 1
        assert after.unproven_symbols == frozenset({"SPY", "QQQ"})

    def test_escalating_alerts_and_counts(self) -> None:
        """An escalation is a new fact about the incident, so it reaches a human
        — but it is not a new halt, so it does not count as one. The two
        counters are separate for exactly that reason."""
        redis = FakeRedis()
        sink = RecordingSink()
        ks = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY",),
        )

        assert len(sink.sent) == 2
        escalation = sink.sent[1]
        assert escalation.severity is Severity.CRITICAL
        assert "SPY" in escalation.body

    def test_a_second_incident_is_not_swallowed_by_the_first(self) -> None:
        """The dedup key is keyed on the symbols, and this is why.

        A `reconciliation_mismatch` on SPY, then a second one naming QQQ: same
        scope, same target, same reason. A key built from those three would be
        byte-identical, so a deduping sink would swallow the second — which is
        exactly the message `_alert_escalated` exists to get past, about a
        symbol the operator has not yet been told about.
        """
        redis = FakeRedis()
        sink = RecordingSink()
        ks = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY",),
        )

        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("QQQ",),
        )

        first, second = sink.sent[1], sink.sent[2]
        assert first.key != second.key
        # And the second names what is *new*, not the whole set again.
        assert "cannot prove QQQ" in second.title
        assert "QQQ, SPY" in second.body, "the total is still stated, just not as the headline"

    def test_an_append_does_not_log_a_transition_that_did_not_happen(self) -> None:
        """`from_reason` is absent when the reason did not move.

        The append path merges evidence onto a halt whose reason has already
        risen as far as it goes. Reporting `from_reason` there would print
        `reconciliation_mismatch -> reconciliation_mismatch`, which reads as a
        transition and is not one.
        """
        redis = FakeRedis()
        ks = RedisKillSwitch(redis)  # type: ignore[arg-type]
        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY",),
        )

        with capture_logs() as logs:
            ks.engage(
                HaltScope.GLOBAL,
                HaltReason.RECONCILIATION_MISMATCH,
                "reconciler",
                unproven_symbols=("QQQ",),
            )

        line = next(entry for entry in logs if entry["event"] == "risk.killswitch.escalated")
        assert line["from_reason"] is None
        assert line["newly_unproven"] == ["QQQ"]
        assert line["unproven"] == ["QQQ", "SPY"]

    def test_a_rise_logs_where_it_came_from(self) -> None:
        redis = FakeRedis()
        ks = RedisKillSwitch(redis)  # type: ignore[arg-type]
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        with capture_logs() as logs:
            ks.engage(
                HaltScope.GLOBAL,
                HaltReason.RECONCILIATION_MISMATCH,
                "reconciler",
                unproven_symbols=("SPY",),
            )

        line = next(entry for entry in logs if entry["event"] == "risk.killswitch.escalated")
        assert line["from_reason"] == "manual"
        assert line["reason"] == "reconciliation_mismatch"
        assert line["newly_unproven"] == ["SPY"]


class TestContendedWrites:
    """AUDIT.md finding 48. `engage` used to be GET-then-SET, so two processes
    reacting to one incident could lose an update. It cost an audit field then;
    it would now cost the impugnment the exit carve-out reads, so the write is
    atomic and the retry is bounded and loud."""

    def test_a_first_halt_lands_through_set_nx(self) -> None:
        """The uncontended path takes one round trip and no script at all."""
        ks, redis = switch()

        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        assert redis.evals == 0

    def test_a_writer_landing_mid_merge_is_retried_not_lost(self) -> None:
        """The race the compare-and-set exists for.

        Another process engages between our read and our write. The CAS sees a
        value that is not the one we read, refuses, and we round again against
        what is actually there — so *both* impugnments survive. A blind SET
        would have kept ours and dropped theirs.
        """
        ks, redis = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        intruder = RedisKillSwitch(redis)  # type: ignore[arg-type]
        landed = False

        def other_process(_client: FakeRedis) -> None:
            nonlocal landed
            if landed:
                return
            landed = True
            intruder.engage(
                HaltScope.GLOBAL,
                HaltReason.BROKER_UNREACHABLE,
                "router",
                unproven_symbols=("QQQ",),
            )

        redis.before_cas = other_process

        final = ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY",),
        )

        assert final.unproven_symbols == frozenset({"SPY", "QQQ"})
        # Three: the intruder's own merge, our refused CAS, and our retry.
        assert redis.evals == 3, "the first CAS must have been refused"

    def test_a_key_cleared_mid_engage_is_re_engaged_not_dereferenced(self) -> None:
        """A human clears the halt in the window between our `SET NX` and our
        `GET`. The record is gone, so there is nothing to merge with — and the
        answer is to round again and engage cleanly, not to dereference `None`
        out of the platform's stop button.
        """
        ks, redis = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        real_get = redis.get
        cleared = False

        def get_then_clear(key: str) -> str | None:
            nonlocal cleared
            value = real_get(key)
            if not cleared:
                cleared = True
                redis.store.pop(key, None)
            return value

        # The clear lands *before* our read returns, so the CAS finds nothing.
        redis.get = get_then_clear  # type: ignore[method-assign]

        final = ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            unproven_symbols=("SPY",),
        )

        assert final.reason is HaltReason.RECONCILIATION_MISMATCH
        assert final.unproven_symbols == frozenset({"SPY"})
        assert ks.is_engaged() is True

    def test_unresolvable_contention_raises_rather_than_returning(self) -> None:
        """The bounded half. Three rounds and then a loud failure, because a
        caller that believes it recorded evidence and did not is worse than an
        exception: the exit carve-out goes on sizing a flatten against the very
        quantity the reconciler could not prove.

        **The message must not read like a store outage.** Reaching here means
        every round found the key occupied, so a halt *is* standing and the
        store *is* answering — the opposite of the unreachable-Redis case, which
        writes nothing and halts nothing. `POST /risk/halt` tells an operator
        "nothing was written, trading resumes when the store recovers", and
        saying that here would send them to re-halt an already halted platform
        while the unproven symbol stays flattenable.
        """
        ks, redis = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        rival = 0

        def always_lose(client: FakeRedis) -> None:
            nonlocal rival
            rival += 1
            # Someone else always writes first. The CAS can never match.
            client.store[client.scan_iter("*")[0]] = json.dumps(
                {
                    "scope": "global",
                    "reason": "manual",
                    "engaged_at": f"2024-06-03T14:30:{rival:02d}+00:00",
                    "engaged_by": "ops",
                    "detail": "",
                    "target": None,
                }
            )

        redis.before_cas = always_lose

        with pytest.raises(KillSwitchUnavailableError) as raised:
            ks.engage(
                HaltScope.GLOBAL,
                HaltReason.RECONCILIATION_MISMATCH,
                "reconciler",
                unproven_symbols=("SPY",),
            )

        message = str(raised.value)
        assert "a halt is standing" in message
        assert "The store is reachable" in message
        assert "SPY is NOT recorded as unproven" in message, (
            "the symbol whose evidence was lost is the one thing an operator has to act on"
        )
        assert redis.evals == 3, "bounded at _MAX_ENGAGE_ATTEMPTS, not spinning"

    def test_contention_without_evidence_says_what_is_missing_instead(self) -> None:
        """A contended engage that named no symbols lost only its reason, and
        the message says that rather than naming an empty set."""
        ks, redis = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.DATA_FEED_LOST, "monitor")

        rival = 0

        def always_lose(client: FakeRedis) -> None:
            nonlocal rival
            rival += 1
            client.store[client.scan_iter("*")[0]] = json.dumps(
                {
                    "scope": "global",
                    "reason": "data_feed_lost",
                    "engaged_at": f"2024-06-03T14:30:{rival:02d}+00:00",
                    "engaged_by": "monitor",
                    "detail": "",
                    "target": None,
                    "impugned": [
                        {
                            "symbols": ["QQQ"],
                            "reason": "data_feed_lost",
                            "at": "2024-06-03T14:30:00+00:00",
                            "by": "monitor",
                            "detail": "",
                        }
                    ],
                }
            )

        redis.before_cas = always_lose

        with pytest.raises(KillSwitchUnavailableError) as raised:
            ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops", unproven_symbols=("SPY",))

        assert "SPY is NOT recorded as unproven" in str(raised.value)


class TestHaltStateFailsClosed:
    def test_an_unreachable_redis_is_engaged_and_impugns_nothing(self) -> None:
        """docs/SAFETY.md layers 5 and 6, and the one place they must not fail
        together.

        A Redis outage is not evidence about the book — it is evidence that we
        cannot read halt metadata. Treating it as impugnment would refuse every
        exit and every protective stop on every blip, which is layer 5 taken
        down by a layer 6 fault that says nothing about any position. The halt
        itself still stands; only the carve-out is unaffected.
        """
        ks, _ = switch(broken=True)

        state = ks.halt_state("strat", "SPY")

        assert state.engaged is True
        assert state.unreadable is True
        assert state.position_is_unproven("SPY") is False

    def test_an_undecodable_record_is_as_unreadable_as_an_outage(self) -> None:
        """A newer process writing an unknown `HaltReason` during a rolling
        deploy. Letting the `ValueError` out of the risk chain would be worse
        than either failure it sits between."""
        ks, redis = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        key = redis.scan_iter("*")[0]
        redis.store[key] = redis.store[key].replace('"manual"', '"a_reason_from_the_future"')

        state = ks.halt_state()

        assert state.engaged is True
        assert state.unreadable is True

    def test_halts_from_every_scope_are_composed(self) -> None:
        """One order can be covered by three halts at once, and the evidence
        that matters may be on any of them."""
        ks, _ = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        ks.engage(HaltScope.STRATEGY, HaltReason.UNHANDLED_EXCEPTION, "runner", target="sma")
        ks.engage(
            HaltScope.SYMBOL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            target="SPY",
            unproven_symbols=("SPY",),
        )

        state = ks.halt_state("sma", "SPY")

        assert len(state.halts) == 3
        assert state.position_is_unproven("SPY")
        # A different strategy on a different symbol sees only the global halt.
        other = ks.halt_state("mean_reversion", "QQQ")
        assert len(other.halts) == 1
        assert not other.position_is_unproven("QQQ")


class TestOneUnreadableRecordDoesNotErasTheOthers:
    """Up to three halts cover one order and they are independent documents.

    Decoding them as a single generator inside one `try` meant an unknown
    `HaltReason` on *any* of them collapsed the whole answer to
    `unreadable=True` — which by design impugns nothing. So a symbol halt that
    had successfully recorded "we cannot prove SPY" was discarded because a
    global halt beside it was written by a newer deploy, and the flatten against
    SPY's disputed quantity was approved. Found by an adversarial review of this
    branch's own diff and reproduced by execution before it was changed.
    """

    def a_mixed_read(self) -> tuple[RedisKillSwitch, FakeRedis]:
        ks, redis = switch()
        ks.engage(
            HaltScope.SYMBOL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            target="SPY",
            unproven_symbols=("SPY",),
        )
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        key = "atp:halt:global"
        redis.store[key] = redis.store[key].replace('"manual"', '"a_reason_from_a_newer_deploy"')
        return ks, redis

    def test_the_evidence_on_a_readable_halt_survives(self) -> None:
        ks, _ = self.a_mixed_read()

        state = ks.halt_state("strat", "SPY")

        assert state.position_is_unproven("SPY"), (
            "the symbol halt decoded fine — discarding it reopens the exit carve-out"
        )
        assert len(state.halts) == 1

    def test_it_still_fails_closed_on_the_one_it_could_not_read(self) -> None:
        """The strictness is not traded away for the evidence. Both hold."""
        ks, _ = self.a_mixed_read()

        state = ks.halt_state("strat", "SPY")

        assert state.unreadable is True
        assert state.engaged is True

    def test_an_undecodable_record_alone_is_still_unreadable_and_impugns_nothing(self) -> None:
        """The case ADR 0029 reasons about — nothing was read, so there is no
        evidence to keep and a store fault is not evidence about a position."""
        ks, redis = switch()
        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        key = "atp:halt:global"
        redis.store[key] = redis.store[key].replace('"manual"', '"from_the_future"')

        state = ks.halt_state("strat", "SPY")

        assert state.engaged is True
        assert state.unreadable is True
        assert state.position_is_unproven("SPY") is False


class TestAFirstHaltCanArriveWithEvidence:
    """And it is the common case, not the exotic one.

    The scheduled reconcile finds a quantity it cannot prove on a platform that
    was trading happily a second ago. Nothing was halted, so `SET NX` wins and
    this is a *creation*. Routing only the escalation path through the
    symbol-naming alert left exactly that path silent about which positions the
    platform would no longer close.
    """

    def engaged_with_evidence(self) -> RecordingSink:
        redis = FakeRedis()
        sink = RecordingSink()
        ks = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]
        ks.engage(
            HaltScope.GLOBAL,
            HaltReason.RECONCILIATION_MISMATCH,
            "reconciler",
            detail="SPY: broker 300, book 100",
            unproven_symbols=("SPY",),
        )
        return sink

    def test_the_operator_is_told_which_symbol_will_not_close(self) -> None:
        sink = self.engaged_with_evidence()

        assert len(sink.sent) == 2, "the halt, and what it means for exits"
        bodies = " ".join(a.body for a in sink.sent)
        assert "SPY" in bodies
        assert any("cannot prove SPY" in a.title for a in sink.sent)
        assert any("broker's own UI" in a.body for a in sink.sent)

    def test_the_two_alerts_do_not_share_a_key(self) -> None:
        """A deduping sink would otherwise deliver one of them (ADR 0012)."""
        sink = self.engaged_with_evidence()

        assert sink.sent[0].key != sink.sent[1].key

    def test_a_halt_with_no_evidence_still_sends_exactly_one(self) -> None:
        """The majority case is unchanged — a manual halt says nothing about any
        position and must not manufacture a second page."""
        redis = FakeRedis()
        sink = RecordingSink()
        ks = RedisKillSwitch(redis, alerts=sink)  # type: ignore[arg-type]

        ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        assert len(sink.sent) == 1


class TestContentionSaysWhichFailureItWas:
    """Two opposite states reach the same raise, and an operator acts
    differently on each. Asserting either unconditionally is a lie half the
    time — and the dangerous half is telling someone trading is stopped when
    nothing is recorded."""

    def test_a_key_cleared_on_the_final_round_does_not_claim_a_halt(self) -> None:
        """Every round's `SET NX` loses, and every `GET` then finds the key
        gone. Nothing was ever written by us and nothing may be in force."""
        ks, redis = switch()
        real_set = redis.set

        def always_taken(key: str, value: str, nx: bool = False) -> bool | None:
            if nx:
                return None  # somebody else always holds it at SET time…
            return real_set(key, value)

        def always_gone(key: str) -> str | None:
            return None  # …and it is gone by the time we read it

        redis.set = always_taken  # type: ignore[method-assign]
        redis.get = always_gone  # type: ignore[method-assign]

        with pytest.raises(KillSwitchUnavailableError) as raised:
            ks.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops", unproven_symbols=("SPY",))

        message = str(raised.value)
        assert "NO halt may be in force" in message
        assert "trading is stopped" in message, "the operator must go and check"
        assert "a halt is standing" not in message
        assert "SPY is NOT recorded as unproven" in message
