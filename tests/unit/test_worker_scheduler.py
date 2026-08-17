"""The scheduler driver: when each job is next due, and what a due job does.

Driven against a **real** `TradingCalendar`, not a stub. The thing this module
gets wrong if it gets anything wrong is the calendar arithmetic — an early close,
a holiday, a DST boundary — and a fake calendar would agree with whatever we
believed while writing it. The session times asserted below were read off the
NYSE rules:

    2024-06-03 Mon   13:30Z → 20:00Z   ordinary, summer (EDT)
    2024-07-03 Wed   13:30Z → 17:00Z   early close, the day before July 4th
    2024-07-04 Thu   closed            Independence Day
    2024-11-29 Fri   14:30Z → 18:00Z   early close, winter (EST)
    2024-12-25 Wed   closed            Christmas

`run_scheduler` loops until cancelled, so the tests below drive it with a clock
that only advances when the injected sleep is called. That makes a day of
schedule elapse instantly and deterministically — no wall-clock waiting, and no
dependence on how fast the machine is.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from atp_core.clock import TradingCalendar
from atp_worker.scheduler import (
    MAX_SLEEP_SECONDS,
    SESSION_SCAN_DAYS,
    next_due,
    run_scheduler,
)

if TYPE_CHECKING:
    from datetime import date

CAL = TradingCalendar("NYSE")

ORDINARY_OPEN = datetime(2024, 6, 3, 13, 30, tzinfo=UTC)
ORDINARY_CLOSE = datetime(2024, 6, 3, 20, 0, tzinfo=UTC)
MIDSESSION = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)
OVERNIGHT = datetime(2024, 6, 3, 2, 0, tzinfo=UTC)


async def nothing() -> None:
    """A job body for entries whose scheduling — not whose work — is under test."""


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"job": nothing, "trigger": "interval", "minutes": 5}
    return {**base, **overrides}


class _SchedulerStopError(Exception):
    """Breaks the driver's infinite loop once a test has seen enough."""


class FakeTime:
    """A clock advanced by its own sleep.

    The scheduler's only two dependencies on real time are `clock.now()` and the
    sleep it awaits. Wiring them to each other makes elapsed time a pure
    function of what the scheduler asked for, so a test can watch a whole day go
    by and assert on the exact instant a job fired.
    """

    def __init__(self, start: datetime, *, max_sleeps: int = 50) -> None:
        self._now = start
        self.sleeps: list[float] = []
        self._max_sleeps = max_sleeps

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > self._max_sleeps:
            raise _SchedulerStopError
        self._now += timedelta(seconds=seconds)


class TestCron:
    def test_still_ahead_today(self) -> None:
        due = next_due(entry(trigger="cron", hour=3, minute=30), OVERNIGHT, CAL)
        assert due == datetime(2024, 6, 3, 3, 30, tzinfo=UTC)

    def test_already_past_rolls_to_tomorrow(self) -> None:
        """02:00 seen from mid-session is tomorrow's 02:00, not a time in the
        past the loop would then fire immediately and repeatedly."""
        due = next_due(entry(trigger="cron", hour=2, minute=0), MIDSESSION, CAL)
        assert due == datetime(2024, 6, 4, 2, 0, tzinfo=UTC)

    def test_exactly_now_rolls_forward(self) -> None:
        """`next_due` is strictly after `now`. Returning `now` would run the job
        twice in the same second — the loop reschedules from the clock, so an
        inclusive boundary is a hot loop."""
        due = next_due(entry(trigger="cron", hour=2, minute=0), OVERNIGHT, CAL)
        assert due > OVERNIGHT
        assert due == datetime(2024, 6, 4, 2, 0, tzinfo=UTC)


class TestInterval:
    def test_plain_interval_is_just_now_plus_the_gap(self) -> None:
        assert next_due(entry(minutes=5), MIDSESSION, CAL) == MIDSESSION + timedelta(minutes=5)

    def test_market_hours_only_waits_for_the_open(self) -> None:
        """A 1-minute job at 02:00 must not schedule itself for 02:01, be
        skipped for being out of hours, and repeat that 690 times before the
        bell."""
        assert next_due(entry(minutes=1, market_hours_only=True), OVERNIGHT, CAL) == ORDINARY_OPEN

    def test_market_hours_only_inside_the_session_is_unaffected(self) -> None:
        due = next_due(entry(minutes=5, market_hours_only=True), MIDSESSION, CAL)
        assert due == MIDSESSION + timedelta(minutes=5)

    def test_an_interval_running_past_the_close_waits_for_the_next_open(self) -> None:
        """19:58 + 5 minutes is after the 20:00 close, so the next slot is
        tomorrow's open rather than 20:03 into a shut market."""
        late = datetime(2024, 6, 3, 19, 58, tzinfo=UTC)
        due = next_due(entry(minutes=5, market_hours_only=True), late, CAL)
        assert due == datetime(2024, 6, 4, 13, 30, tzinfo=UTC)


class TestSessionEdges:
    def test_before_the_open(self) -> None:
        """`rollover_daily_counters` runs 5 minutes before the bell."""
        due = next_due(entry(trigger="market_open", offset_minutes=-5), OVERNIGHT, CAL)
        assert due == ORDINARY_OPEN - timedelta(minutes=5)

    def test_after_the_close(self) -> None:
        """`generate_daily_report` runs 30 minutes after."""
        due = next_due(entry(trigger="market_close", offset_minutes=30), MIDSESSION, CAL)
        assert due == ORDINARY_CLOSE + timedelta(minutes=30)

    def test_an_early_close_moves_the_job_with_it(self) -> None:
        """The reason this is calendar-driven and not a fixed UTC time. On
        2024-07-03 the NYSE closes at 17:00Z, three hours early; a report
        pinned to 20:30Z would run three hours late, and any *pre*-close job
        pinned to a fixed time would fire after the market had already shut."""
        morning = datetime(2024, 7, 3, 12, 0, tzinfo=UTC)
        due = next_due(entry(trigger="market_close", offset_minutes=30), morning, CAL)
        assert due == datetime(2024, 7, 3, 17, 30, tzinfo=UTC)

    def test_the_same_offset_tracks_dst(self) -> None:
        """The open is 13:30Z in summer and 14:30Z in winter. A fixed UTC time
        would be an hour wrong for half the year — here it is neither."""
        summer = next_due(entry(trigger="market_open", offset_minutes=-5), OVERNIGHT, CAL)
        winter = next_due(
            entry(trigger="market_open", offset_minutes=-5),
            datetime(2024, 11, 29, 2, 0, tzinfo=UTC),
            CAL,
        )
        assert summer == datetime(2024, 6, 3, 13, 25, tzinfo=UTC)
        assert winter == datetime(2024, 11, 29, 14, 25, tzinfo=UTC)

    def test_a_holiday_is_skipped_to_the_next_session(self) -> None:
        """July 4th has no close to be 30 minutes after."""
        holiday = datetime(2024, 7, 4, 12, 0, tzinfo=UTC)
        due = next_due(entry(trigger="market_close", offset_minutes=30), holiday, CAL)
        assert due.date() == datetime(2024, 7, 5, tzinfo=UTC).date()

    def test_a_calendar_with_no_sessions_raises_rather_than_spinning(self) -> None:
        """The scan is bounded. A calendar that answers `None` forever is a bug
        worth an exception, not an infinite loop inside the worker."""

        class Shut:
            tz = CAL.tz

            def session_on(self, day: date) -> None:
                return None

        with pytest.raises(ValueError, match=f"within {SESSION_SCAN_DAYS} days"):
            next_due(entry(trigger="market_close", offset_minutes=30), MIDSESSION, Shut())  # type: ignore[arg-type]


class TestUnknownTrigger:
    def test_it_is_refused_by_name(self) -> None:
        """A typo in `SCHEDULE` must not silently mean 'never runs'."""
        with pytest.raises(ValueError, match="unknown scheduler trigger"):
            next_due(entry(trigger="hourly"), MIDSESSION, CAL)


class TestRunning:
    async def test_a_due_job_runs_and_is_rescheduled(self) -> None:
        calls: list[datetime] = []
        clock = FakeTime(MIDSESSION, max_sleeps=20)

        async def job() -> None:
            calls.append(clock.now())

        with pytest.raises(_SchedulerStopError):
            await run_scheduler(
                clock=clock,
                calendar=CAL,
                schedule=[entry(job=job, minutes=5)],
                sleep=clock.sleep,
            )

        assert len(calls) >= 2, "a rescheduled job must run again, not once"
        assert calls[1] - calls[0] >= timedelta(minutes=5)

    async def test_sleeps_are_capped_so_the_schedule_is_re_derived(self) -> None:
        """A job due tomorrow could be awaited in one long sleep, but then a
        stepped clock or a DST boundary is discovered a day late."""
        clock = FakeTime(MIDSESSION, max_sleeps=10)

        with pytest.raises(_SchedulerStopError):
            await run_scheduler(
                clock=clock,
                calendar=CAL,
                schedule=[entry(trigger="cron", hour=2, minute=0)],
                sleep=clock.sleep,
            )

        assert clock.sleeps, "the driver must wait rather than spin"
        assert max(clock.sleeps) <= MAX_SLEEP_SECONDS

    async def test_an_unimplemented_job_is_tried_once_and_left_alone(self) -> None:
        """Five of the six entries in `SCHEDULE` are still stubs. Retrying them
        every interval would bury the log in one repeated traceback — and this
        is not a failure, it is a job nobody has built."""
        calls: list[int] = []

        async def not_built() -> None:
            calls.append(1)
            raise NotImplementedError

        clock = FakeTime(MIDSESSION, max_sleeps=200)

        # With its only job dormant the driver parks rather than returning — a
        # responsibility that returned would read to the supervisor as a crash.
        # So the timeout here is the assertion that it parked.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                run_scheduler(
                    clock=clock,
                    calendar=CAL,
                    schedule=[entry(job=not_built, minutes=1)],
                    sleep=clock.sleep,
                ),
                timeout=0.25,
            )

        assert calls == [1]

    async def test_a_failing_job_is_rescheduled_and_the_driver_survives(self) -> None:
        """A nightly sweep that failed tonight must still run tomorrow, and it
        must not take the last working thing in the process down with it."""
        calls: list[int] = []

        async def flaky() -> None:
            calls.append(1)
            raise RuntimeError("vendor returned 500")

        clock = FakeTime(MIDSESSION, max_sleeps=30)

        with pytest.raises(_SchedulerStopError):
            await run_scheduler(
                clock=clock,
                calendar=CAL,
                schedule=[entry(job=flaky, minutes=1)],
                sleep=clock.sleep,
            )

        assert len(calls) >= 3, "a job that raised must keep its place in the schedule"

    async def test_a_market_hours_job_first_runs_after_the_bell(self) -> None:
        """Started overnight, it waits — it does not fire once into a shut
        market and count that as its run for the day."""
        calls: list[datetime] = []
        clock = FakeTime(OVERNIGHT, max_sleeps=400)

        async def job() -> None:
            calls.append(clock.now())

        with pytest.raises(_SchedulerStopError):
            await run_scheduler(
                clock=clock,
                calendar=CAL,
                schedule=[entry(job=job, minutes=1, market_hours_only=True)],
                sleep=clock.sleep,
            )

        assert calls, "the job never ran"
        assert calls[0] >= ORDINARY_OPEN
        assert CAL.is_open(calls[0])
