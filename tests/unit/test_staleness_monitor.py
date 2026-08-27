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
        self, *, last_message_at: datetime | None = None, connected_since: datetime | None = None
    ) -> None:
        self.stats = IngestorStats(
            connected_since=connected_since,
            last_message_at=last_message_at,
            symbols={"SPY"},
        )


#: One calendar for the module. Building one costs ~50ms and imports pandas;
#: they are immutable in use, so sharing is free.
CALENDAR = TradingCalendar("NYSE")


def monitor(**kwargs: Any) -> StalenessMonitor:
    kwargs.setdefault("calendar", CALENDAR)
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
    """Silence is measured from the latest of three instants. Each clause stops
    a specific false alarm, and each gets a test."""

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

    async def test_the_latest_of_the_three_wins(self) -> None:
        """A reconnect advances `connected_since` past a stale `last_message_at`,
        because the gap it left has already been backfilled."""
        verdict = monitor(max_silence_seconds=60).evaluate(
            ingestor(
                last_message_at=MIDSESSION - timedelta(minutes=10),
                connected_since=MIDSESSION - timedelta(seconds=10),
            ),
            MIDSESSION,
        )

        assert verdict.stale is False
        assert verdict.silent_for_seconds == 10


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
        self, instants: list[datetime]
    ) -> tuple[StalenessMonitor, FakeKillSwitch, ScriptedClock]:
        switch = FakeKillSwitch()
        clock = ScriptedClock(instants)
        watchdog = monitor(max_silence_seconds=60, kill_switch=switch, clock=clock, sleep=_no_sleep)
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
