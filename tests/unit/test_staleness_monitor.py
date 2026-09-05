"""The staleness watchdog.

This is the only thing in the platform that catches a feed which is *connected
and frozen*, so the cases worth writing down are the ones where silence is
innocent — overnight, a weekend, a holiday, the first seconds after a restart —
and the one where it is not.

`evaluate` is pure, so most of this needs no loop and no sleeping. `watch` gets
its own small set with an injected sleep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest

from atp_core.alerts.ports import Alert, Severity
from atp_core.clock import TradingCalendar
from atp_core.data.stream import STALENESS_ACTOR, IngestorStats, StalenessMonitor
from atp_core.risk.killswitch import HaltReason, HaltRecord, HaltScope

if TYPE_CHECKING:
    from atp_core.data.stream import StreamIngestor

#: Monday 3 June 2024, 14:30 UTC — 10:30 in New York, an hour into a regular
#: session. Chosen so the session's open (13:30 UTC) is comfortably before it.
MIDSESSION = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)
SESSION_OPEN = datetime(2024, 6, 3, 13, 30, tzinfo=UTC)

#: Same clock time on the Sunday before. The market is shut; silence is correct.
SUNDAY = datetime(2024, 6, 2, 14, 30, tzinfo=UTC)

#: Independence Day 2024 fell on a Thursday. A weekday the market was shut.
HOLIDAY = datetime(2024, 7, 4, 14, 30, tzinfo=UTC)


class FakeKillSwitch:
    def __init__(self) -> None:
        self.engaged: list[HaltRecord] = []

    def is_engaged(self, strategy_id: str | None = None, symbol: str | None = None) -> bool:
        return bool(self.engaged)

    def engage(
        self,
        scope: HaltScope,
        reason: HaltReason,
        engaged_by: str,
        detail: str = "",
        target: str | None = None,
    ) -> HaltRecord:
        record = HaltRecord(
            scope=scope,
            reason=reason,
            engaged_at=MIDSESSION,
            engaged_by=engaged_by,
            detail=detail,
            target=target,
        )
        self.engaged.append(record)
        return record

    def clear(  # pragma: no cover - the watchdog must never call this
        self, scope: HaltScope, cleared_by: str, target: str | None = None
    ) -> None:
        raise AssertionError("a watchdog must never clear a halt")

    def active_halts(self) -> list[HaltRecord]:
        return list(self.engaged)


class StubIngestor:
    """Only `stats` is read, so only `stats` is provided."""

    def __init__(
        self,
        *,
        last_message_at: datetime | None = None,
        connected_since: datetime | None = None,
        storage_watermark: datetime | None = None,
    ) -> None:
        self.stats = IngestorStats(
            connected_since=connected_since,
            last_message_at=last_message_at,
            storage_watermark=storage_watermark,
            symbols={"SPY"},
        )


#: One calendar for the module. Building one costs ~50ms and imports pandas;
#: they are immutable in use, so sharing is free.
CALENDAR = TradingCalendar("NYSE")


def monitor(**kwargs: Any) -> StalenessMonitor:
    kwargs.setdefault("calendar", CALENDAR)
    # `alerts` is required rather than defaulted on the real constructor, so
    # that the production wiring cannot omit it silently again — see the
    # constructor's own note. Defaulted here, where "no sink" is what most of
    # these tests mean.
    kwargs.setdefault("alerts", None)
    return StalenessMonitor(**kwargs)


def ingestor(**kwargs: Any) -> StreamIngestor:
    return cast("StreamIngestor", StubIngestor(**kwargs))


class _ClockExhaustedError(Exception):
    """Raised when the scripted clock runs out — how a test ends the watch loop,
    which is infinite by design."""


class ScriptedClock:
    """Hands out prepared instants, then stops the loop.

    Running out is how a test ends `watch()`: the alternative is cancelling a
    task, which makes every test a two-step dance and hides which iteration the
    assertion is really about. `remaining` is public so a test can extend the
    script mid-run and drive the same monitor through a second outage.
    """

    def __init__(self, instants: list[datetime]) -> None:
        self.remaining = list(instants)

    def now(self) -> datetime:
        if not self.remaining:
            raise _ClockExhaustedError
        return self.remaining.pop(0)


async def _no_sleep(_seconds: float) -> None:
    return None


class TestSilenceIsExpected:
    async def test_overnight(self) -> None:
        verdict = monitor().evaluate(
            ingestor(last_message_at=MIDSESSION),
            datetime(2024, 6, 4, 3, 0, tzinfo=UTC),
        )

        assert verdict.stale is False
        assert verdict.market_open is False
        assert verdict.silent_for_seconds is None

    async def test_sunday(self) -> None:
        verdict = monitor().evaluate(ingestor(last_message_at=SUNDAY - timedelta(days=2)), SUNDAY)

        assert verdict.stale is False and verdict.market_open is False

    async def test_holiday_on_a_weekday(self) -> None:
        """The case a naive "is it a weekday between 09:30 and 16:00" check gets
        wrong, and the reason this takes a calendar."""
        verdict = monitor().evaluate(ingestor(last_message_at=HOLIDAY - timedelta(days=1)), HOLIDAY)

        assert verdict.stale is False and verdict.market_open is False

    async def test_the_instant_of_the_close(self) -> None:
        """Half-open, matching `TradingCalendar.is_open`: at 16:00:00 the market
        is shut and the feed owes us nothing."""
        close = datetime(2024, 6, 3, 20, 0, tzinfo=UTC)

        verdict = monitor().evaluate(ingestor(last_message_at=SESSION_OPEN), close)

        assert verdict.market_open is False


class TestSilenceIsNot:
    async def test_frozen_feed_midsession(self) -> None:
        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(last_message_at=MIDSESSION - timedelta(minutes=5)), MIDSESSION
        )

        assert verdict.stale is True
        assert verdict.silent_for_seconds == 300
        assert "300s" in verdict.reason

    async def test_a_current_feed_is_not_stale(self) -> None:
        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(last_message_at=MIDSESSION - timedelta(seconds=5)), MIDSESSION
        )

        assert verdict.stale is False
        assert verdict.market_open is True

    async def test_the_budget_boundary_is_exclusive(self) -> None:
        at_budget = monitor(max_silence_seconds=60).evaluate(
            ingestor(last_message_at=MIDSESSION - timedelta(seconds=60)), MIDSESSION
        )
        past_budget = monitor(max_silence_seconds=60).evaluate(
            ingestor(last_message_at=MIDSESSION - timedelta(seconds=61)), MIDSESSION
        )

        assert at_budget.stale is False
        assert past_budget.stale is True


class TestBaseline:
    """Silence is measured from the latest instant data is known to have been
    fine. Each clause stops a specific false alarm, and each gets a test —
    including the one that stops a *restart* from counting as such an instant
    (docs/paper-week/day-1-review.md, F7)."""

    async def test_a_worker_started_midsession_is_not_blamed_for_the_open(self) -> None:
        """Started at 14:29, checked at 14:30, no tick yet. Measuring from the
        13:30 open would halt trading one second into the process's life."""
        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(connected_since=MIDSESSION - timedelta(seconds=30)), MIDSESSION
        )

        assert verdict.stale is False
        assert verdict.silent_for_seconds == 30

    async def test_a_feed_that_died_yesterday_is_measured_from_todays_open(self) -> None:
        """Not from yesterday's last tick: the fifteen hours the market was shut
        were not an outage, and reporting them as one buries the real number."""
        just_after_open = SESSION_OPEN + timedelta(seconds=30)

        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(last_message_at=datetime(2024, 5, 31, 20, 0, tzinfo=UTC)), just_after_open
        )

        assert verdict.stale is False
        assert verdict.silent_for_seconds == 30

    async def test_but_a_feed_dead_since_the_open_is_still_caught(self) -> None:
        """The other half of the same rule: measuring from the open means a feed
        that never delivered a tick today is reported, which is exactly when it
        matters most."""
        verdict = monitor(max_silence_seconds=60).evaluate(ingestor(), MIDSESSION)

        assert verdict.stale is True
        assert verdict.silent_for_seconds == 3600

    async def test_a_backfilled_reconnect_is_not_stale(self) -> None:
        """A reconnect whose gap was closed leaves the data current, even though
        `last_message_at` still points before the outage.

        The watermark is what says so. It used to be `connected_since`, which
        also moved on a plain process restart — and a restart is not a statement
        about the data at all (docs/paper-week/day-1-review.md, F7). The
        ingestor advances the watermark only when `_backfill_gap` actually
        succeeded, so a *failed* backfill cannot claim recovery.
        """
        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(
                last_message_at=MIDSESSION - timedelta(minutes=10),
                storage_watermark=MIDSESSION - timedelta(seconds=10),
            ),
            MIDSESSION,
        )

        assert verdict.stale is False
        assert verdict.silent_for_seconds == 10

    async def test_a_restart_alone_does_not_refresh_the_clock(self) -> None:
        """The F7 regression, in one assertion.

        Five workers were started in three minutes on day 1, each measuring
        silence from its own birth, so a worker that died inside
        `max_silence_seconds` never halted. Here the process has just booted
        into a ten-minute-old feed outage and must say so.
        """
        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(
                last_message_at=None,
                storage_watermark=MIDSESSION - timedelta(minutes=10),
                connected_since=MIDSESSION - timedelta(seconds=1),
            ),
            MIDSESSION,
        )

        assert verdict.stale is True
        assert verdict.silent_for_seconds == 600


class TestArguments:
    async def test_rejects_a_naive_now(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            monitor().evaluate(ingestor(), datetime(2024, 6, 3, 14, 30))  # noqa: DTZ001

    async def test_rejects_a_nonsense_budget(self) -> None:
        with pytest.raises(ValueError, match="max_silence_seconds"):
            monitor(max_silence_seconds=0)

    async def test_rejects_a_nonsense_poll_interval(self) -> None:
        with pytest.raises(ValueError, match="poll_interval_seconds"):
            monitor(poll_interval_seconds=0)


class TestWatch:
    """The loop. A scripted clock drives it and runs out to end it."""

    def build(
        self, instants: list[datetime], alerts: RecordingAlerts | None = None
    ) -> tuple[StalenessMonitor, FakeKillSwitch, ScriptedClock]:
        switch = FakeKillSwitch()
        clock = ScriptedClock(instants)
        watchdog = monitor(
            max_silence_seconds=60,
            kill_switch=switch,
            clock=clock,
            sleep=_no_sleep,
            alerts=alerts,
        )
        return watchdog, switch, clock

    async def run(self, watchdog: StalenessMonitor, target: StreamIngestor) -> None:
        """Drive the loop until the scripted clock is exhausted."""
        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)

    async def test_halts_globally_when_the_feed_goes_quiet(self) -> None:
        watchdog, switch, _ = self.build([MIDSESSION])

        await self.run(watchdog, ingestor(last_message_at=MIDSESSION - timedelta(minutes=5)))

        assert len(switch.engaged) == 1
        halt = switch.engaged[0]
        assert halt.scope is HaltScope.GLOBAL
        assert halt.reason is HaltReason.DATA_FEED_LOST
        assert halt.engaged_by == STALENESS_ACTOR
        assert "300s" in halt.detail

    async def test_halts_once_per_outage_not_once_per_poll(self) -> None:
        """At a 5s poll this would otherwise engage twelve times a minute and
        bury the first, most useful log line under the rest."""
        watchdog, switch, _ = self.build([MIDSESSION] * 5)

        await self.run(watchdog, ingestor(last_message_at=MIDSESSION - timedelta(minutes=5)))

        assert len(switch.engaged) == 1

    async def test_quiet_market_never_halts(self) -> None:
        watchdog, switch, _ = self.build([SUNDAY] * 5)

        await self.run(watchdog, ingestor(last_message_at=SUNDAY - timedelta(days=2)))

        assert switch.engaged == []

    async def test_recovery_re_arms_but_never_clears_the_halt(self) -> None:
        """`FakeKillSwitch.clear` raises if called. A watchdog that un-halted
        itself would let a feed flapping every 30s trade through every gap —
        so recovery re-arms the *alert*, and the halt stays for a human."""
        watchdog, switch, clock = self.build([MIDSESSION])
        target = ingestor(last_message_at=MIDSESSION - timedelta(minutes=5))

        await self.run(watchdog, target)
        assert len(switch.engaged) == 1

        # Data resumes: the watchdog sees a current feed and re-arms.
        later = MIDSESSION + timedelta(minutes=5)
        target.stats.last_message_at = later
        clock.remaining.append(later)
        await self.run(watchdog, target)
        assert len(switch.engaged) == 1, "recovery must not engage anything"

        # And then it stops again. The second outage is reported on its own.
        clock.remaining.append(later + timedelta(minutes=10))
        await self.run(watchdog, target)

        assert len(switch.engaged) == 2

    async def test_without_a_kill_switch_it_still_runs(self) -> None:
        """Logged CRITICAL rather than crashing: the Redis kill switch is a
        Phase 3 item, and a watchdog that refused to start until then would
        leave nothing watching at all."""
        watchdog = monitor(
            max_silence_seconds=60,
            clock=ScriptedClock([MIDSESSION]),
            sleep=_no_sleep,
            kill_switch=None,
        )

        await self.run(watchdog, ingestor(last_message_at=MIDSESSION - timedelta(minutes=5)))


class RecordingAlerts:
    """An `AlertSink` that keeps what it was handed."""

    def __init__(self, *, explode: bool = False) -> None:
        self.sent: list[Alert] = []
        self._explode = explode

    def send(self, alert: Alert) -> None:
        if self._explode:
            raise RuntimeError("the transport is down")
        self.sent.append(alert)


class TestAnnouncingRecovery:
    """F7. `data.staleness.recovered` was declared in the code and never fired:
    data resumed at 18:52:26 on day 1 and nothing observed it. Even once it
    fires, a log line is not an escalation — the halt it engaged reached a
    phone, and the all-clear did not (docs/paper-week/day-1-review.md)."""

    def build(
        self, instants: list[datetime], alerts: RecordingAlerts
    ) -> tuple[StalenessMonitor, FakeKillSwitch]:
        watchdog = monitor(
            max_silence_seconds=60,
            kill_switch=FakeKillSwitch(),
            clock=ScriptedClock(instants),
            sleep=_no_sleep,
            alerts=alerts,
        )
        return watchdog, FakeKillSwitch()

    async def test_recovery_reaches_a_human(self) -> None:
        alerts = RecordingAlerts()
        watchdog, _ = self.build([MIDSESSION, MIDSESSION], alerts)
        target = ingestor(last_message_at=MIDSESSION - timedelta(minutes=5))

        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)
        # The feed comes back between polls.
        target.stats.last_message_at = MIDSESSION
        watchdog._clock = ScriptedClock([MIDSESSION])
        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)

        assert [a.key for a in alerts.sent] == ["staleness.recovered"]
        assert alerts.sent[0].severity is Severity.INFO

    async def test_the_all_clear_says_the_halt_is_still_on(self) -> None:
        """The watchdog deliberately never clears what it engaged, so an
        operator who got the CRITICAL and then a bare 'recovered' would have no
        way to tell 'still broken' from 'fixed itself, waiting for you'. On day
        1 that gap was 2h37m."""
        alerts = RecordingAlerts()
        watchdog, _ = self.build([MIDSESSION], alerts)
        target = ingestor(last_message_at=MIDSESSION - timedelta(minutes=5))
        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)
        target.stats.last_message_at = MIDSESSION
        watchdog._clock = ScriptedClock([MIDSESSION])

        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)

        assert "still engaged" in alerts.sent[0].body

    async def test_a_feed_that_never_broke_announces_nothing(self) -> None:
        alerts = RecordingAlerts()
        watchdog, _ = self.build([MIDSESSION] * 3, alerts)

        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(ingestor(last_message_at=MIDSESSION))

        assert alerts.sent == []

    async def test_a_broken_transport_does_not_stop_the_watchdog(self) -> None:
        """It is still watching, and being wrong about `AlertSink` not raising
        must not take down the thing that is."""
        alerts = RecordingAlerts(explode=True)
        watchdog, _ = self.build([MIDSESSION], alerts)
        target = ingestor(last_message_at=MIDSESSION - timedelta(minutes=5))
        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)
        target.stats.last_message_at = MIDSESSION
        watchdog._clock = ScriptedClock([MIDSESSION])

        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)


class TestRecoveryIsAboutTheData:
    """The audit's §3.4a. Wiring the sink to a phone (§3.4) is only safe once
    the all-clear is a claim about *data arriving*, because `not stale` is not
    that claim: the market being shut makes every verdict non-stale, and so
    does the baseline being floored at the session open. A feed that died at
    14:00 and never came back announced "market data is flowing again" at the
    closing bell, and reset the outage as though it were over.

    These drive `watch` end to end, so reverting the branch in `watch` — or the
    `data_is_current` field it reads — turns them red.
    """

    #: 20:00 UTC, 16:00 in New York: the bell, and the first poll at which
    #: `evaluate` reports the market shut.
    THE_CLOSE = datetime(2024, 6, 3, 20, 0, tzinfo=UTC)
    #: The next session's open, Tuesday 4 June.
    NEXT_OPEN = datetime(2024, 6, 4, 13, 30, tzinfo=UTC)

    def build(
        self, instants: list[datetime], alerts: RecordingAlerts
    ) -> tuple[StalenessMonitor, FakeKillSwitch]:
        switch = FakeKillSwitch()
        watchdog = monitor(
            max_silence_seconds=60,
            kill_switch=switch,
            clock=ScriptedClock(instants),
            sleep=_no_sleep,
            alerts=alerts,
        )
        return watchdog, switch

    async def run(self, watchdog: StalenessMonitor, target: StreamIngestor) -> None:
        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)

    async def test_the_closing_bell_is_not_a_recovery(self) -> None:
        """The headline case. Two polls, one dead feed: the outage at 14:30 and
        the bell at 20:00. Nothing recovered in between."""
        alerts = RecordingAlerts()
        watchdog, switch = self.build([MIDSESSION, self.THE_CLOSE], alerts)
        dead = ingestor(last_message_at=MIDSESSION - timedelta(minutes=5))

        await self.run(watchdog, dead)

        assert len(switch.engaged) == 1, "the outage is still one halt"
        assert alerts.sent == [], "a feed that never came back has nothing to announce"

    async def test_the_next_open_is_not_a_recovery_either(self) -> None:
        """Where gating on `market_open` alone would have put the lie.

        The baseline is floored at the session open, so a feed dead since
        yesterday reads as non-stale for the first `max_silence_seconds` of
        every morning — deliberately, so the overnight is not billed to it. An
        all-clear there would reach the operator at 09:30, which is worse than
        at the bell, and be followed by a CRITICAL two minutes later.
        """
        alerts = RecordingAlerts()
        watchdog, switch = self.build(
            [
                MIDSESSION,
                self.THE_CLOSE,
                self.NEXT_OPEN + timedelta(seconds=5),
                self.NEXT_OPEN + timedelta(minutes=2),
            ],
            alerts,
        )
        dead = ingestor(last_message_at=MIDSESSION - timedelta(minutes=5))

        await self.run(watchdog, dead)

        assert alerts.sent == [], "nothing arrived overnight, so nothing recovered"
        assert len(switch.engaged) == 2, (
            "the close re-arms, so the second session reports its own outage"
        )

    async def test_a_feed_that_really_comes_back_still_says_so(self) -> None:
        """The other direction, so the gate cannot be satisfied by silence."""
        alerts = RecordingAlerts()
        watchdog, _ = self.build([MIDSESSION, MIDSESSION + timedelta(minutes=5)], alerts)
        target = ingestor(last_message_at=MIDSESSION - timedelta(minutes=5))

        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)
        target.stats.last_message_at = MIDSESSION + timedelta(minutes=5)
        watchdog._clock = ScriptedClock([MIDSESSION + timedelta(minutes=5)])
        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)

        assert [a.key for a in alerts.sent] == ["staleness.recovered"]

    async def test_data_from_before_the_open_is_not_current(self) -> None:
        """`evaluate` in isolation: yesterday's last bar is what makes the
        morning non-stale, and it is not a witness that the feed is alive."""
        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(last_message_at=SESSION_OPEN - timedelta(hours=3)),
            SESSION_OPEN + timedelta(seconds=5),
        )

        assert verdict.stale is False
        assert verdict.market_open is True
        assert verdict.data_is_current is False
        assert "no data yet this session" in verdict.reason

    async def test_a_worker_that_has_only_just_booted_is_not_current_either(self) -> None:
        """`connected_since` is the process's birthday, which F7 demoted for
        exactly this reason. It keeps the watchdog quiet; it must not be able
        to announce a recovery."""
        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(connected_since=MIDSESSION - timedelta(seconds=10)),
            MIDSESSION,
        )

        assert verdict.stale is False
        assert verdict.data_is_current is False


class TestTheAllClearDoesNotOverclaim:
    """The all-clear now reaches a phone, so its second sentence has to be true
    as well as its first."""

    async def test_it_does_not_claim_a_halt_nobody_holds(self) -> None:
        """`_halt` with no kill switch bound logs "TRADING IS NOT HALTED". An
        all-clear that then said "the halt it engaged is still engaged" would
        be inventing one."""
        alerts = RecordingAlerts()
        watchdog = monitor(
            max_silence_seconds=60,
            kill_switch=None,
            clock=ScriptedClock([MIDSESSION]),
            sleep=_no_sleep,
            alerts=alerts,
        )
        target = ingestor(last_message_at=MIDSESSION - timedelta(minutes=5))
        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)
        target.stats.last_message_at = MIDSESSION
        watchdog._clock = ScriptedClock([MIDSESSION])

        with pytest.raises(_ClockExhaustedError):
            await watchdog.watch(target)

        assert alerts.sent[0].context["halted"] == "False"
        assert "nothing is halted" in alerts.sent[0].body


class TestRecoveryRestsOnAFrameNotAReconnect:
    """The all-clear reaches a phone now, so the witness behind it has to be one
    a reconnect cannot forge.

    `storage_watermark` is a legitimate witness about *staleness* — it survives a
    restart, which is why the baseline reads it — but it is written by the
    reconnect path, and a backfill that recovered nothing used to write it. That
    made `data_is_current` true for a feed that had delivered nothing for
    minutes, and the operator's phone got "market data is flowing again"
    (the day-1 fix audit, §4.4b and its consequence for §3.4).
    """

    async def test_a_watermark_alone_is_not_recovery(self) -> None:
        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(storage_watermark=MIDSESSION),
            MIDSESSION,
        )

        assert verdict.stale is False, "the watermark still answers the staleness question"
        assert verdict.data_is_current is False, (
            "a reconnect is evidence about the socket, not about the tape"
        )

    async def test_a_frame_is(self) -> None:
        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(last_message_at=MIDSESSION),
            MIDSESSION,
        )

        assert verdict.data_is_current is True
