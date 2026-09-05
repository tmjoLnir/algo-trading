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
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from atp_core.alerts.ports import Alert, Severity
from atp_core.clock import SimulatedClock, TradingCalendar
from atp_core.data.corporate_actions import Adjustment
from atp_core.domain import Order, OrderType, Portfolio, Position, Side, Timeframe
from atp_core.execution.reconciliation import Reconciler
from atp_core.risk.killswitch import HaltReason, HaltRecord, HaltScope
from atp_core.risk.rules import DAILY_LOSS_RULE
from atp_worker.runner import RunnerStats
from atp_worker.scheduler import (
    MAX_SLEEP_SECONDS,
    ROLLOVER_ACTOR,
    SCHEDULE,
    SESSION_SCAN_DAYS,
    SessionJobs,
    SessionWatch,
    _job_name,
    _report_adjustment,
    build_schedule,
    next_due,
    reconcile_with_broker,
    remind_about_halts,
    rollover_daily_counters,
    run_scheduler,
    summarise_the_session,
)
from tests.fakes import FakeBroker, FakeKillSwitch

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
        """Two of the three entries in `SCHEDULE` are still stubs. Retrying
        them every interval would bury the log in one repeated traceback — and
        this is not a failure, it is a job nobody has built."""
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


# ── the session-bound half ──────────────────────────────────────────────────


def _session(
    broker: FakeBroker,
    switch: FakeKillSwitch,
    portfolio: Portfolio,
    orders: list[Order] | None = None,
) -> SessionJobs:
    """A `SessionJobs` over the **real** `Reconciler`.

    A fake reconciler would let this suite assert that the job called something,
    which is not the property worth holding. What matters is that a divergence
    ends with trading halted, and that is the reconciler's own behaviour — so it
    is the reconciler that runs here, over a fake venue.
    """
    held = orders if orders is not None else []
    return SessionJobs(
        reconciler=Reconciler(broker, switch, SimulatedClock(MIDSESSION)),
        portfolio=portfolio,
        open_orders=lambda: list(held),
    )


def _book(**holdings: float) -> Portfolio:
    portfolio = Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))
    for symbol, qty in holdings.items():
        position = portfolio.position(symbol)
        position.qty = Decimal(str(qty))
        position.avg_entry_price = Decimal("100")
        position.last_price = Decimal("100")
    return portfolio


class TestBuildingTheSchedule:
    def test_without_a_session_nothing_reconciles(self) -> None:
        """A worker with no strategy holds no book and has no venue to compare
        it against. An entry failing every five minutes for want of either would
        be indistinguishable in the log from a venue that had gone away."""
        names = [_job_name(e) for e in build_schedule(None)]

        assert "reconcile_with_broker" not in names
        assert names == [_job_name(e) for e in SCHEDULE]

    def test_a_session_adds_the_five_minute_check(self) -> None:
        session = _session(FakeBroker(), FakeKillSwitch(), _book())
        schedule = build_schedule(session)
        reconcile = schedule[0]

        assert _job_name(reconcile) == "reconcile_with_broker"
        assert reconcile["trigger"] == "interval"
        assert reconcile["minutes"] == 5
        assert reconcile["market_hours_only"] is True

    def test_the_bound_job_is_still_named_in_the_log(self) -> None:
        """`functools.partial` carries no `__name__`. A scheduler that called
        every bound job "partial" would name nothing in the one log line an
        operator reads to see which responsibilities are running."""
        session = _session(FakeBroker(), FakeKillSwitch(), _book())

        assert _job_name(build_schedule(session)[0]) == "reconcile_with_broker"

    def test_a_bound_job_schedules_like_any_other(self) -> None:
        """`next_due` reaches for `entry["job"].__name__` on its error paths, so
        a partial must not be the thing that breaks scheduling arithmetic."""
        session = _session(FakeBroker(), FakeKillSwitch(), _book())
        due = next_due(build_schedule(session)[0], MIDSESSION, CAL)

        assert due == MIDSESSION + timedelta(minutes=5)


class TestReconcileWithBroker:
    async def test_a_matching_book_does_not_halt(self) -> None:
        broker = FakeBroker()
        broker.positions["SPY"] = Position(
            symbol="SPY", qty=Decimal(100), avg_entry_price=Decimal("100")
        )
        switch = FakeKillSwitch()

        await reconcile_with_broker(_session(broker, switch, _book(SPY=100)))

        assert switch.is_engaged() is False

    async def test_a_divergence_halts_trading(self) -> None:
        """The whole point, and the reason `halt_on_mismatch` is left at its
        default here. Sizing orders against a position we believe is 100 shares
        when it is 1,000 is how a small bug becomes a large loss, and it
        compounds with every subsequent order (docs/SAFETY.md layer 7)."""
        broker = FakeBroker()
        broker.positions["SPY"] = Position(
            symbol="SPY", qty=Decimal(1000), avg_entry_price=Decimal("100")
        )
        switch = FakeKillSwitch()

        await reconcile_with_broker(_session(broker, switch, _book(SPY=100)))

        assert switch.is_engaged() is True
        assert switch.engagements[-1][1] == HaltReason.RECONCILIATION_MISMATCH.value

    async def test_it_does_not_raise_on_a_divergence(self) -> None:
        """The driver reschedules a job that raised, but it also logs it as a
        failure. A halt is this job succeeding — it found what it was looking
        for — and reporting it as an error would put a traceback in the log
        every five minutes for as long as the halt stands."""
        broker = FakeBroker()
        broker.positions["QQQ"] = Position(
            symbol="QQQ", qty=Decimal(10), avg_entry_price=Decimal("100")
        )

        await reconcile_with_broker(_session(broker, FakeKillSwitch(), _book()))

    async def test_open_orders_are_read_at_each_run_not_at_wiring(self) -> None:
        """`SessionJobs.open_orders` is a callable for exactly this. A list
        captured when the worker booted would report every order placed since as
        an orphan, and orphans halt."""
        broker = FakeBroker()
        switch = FakeKillSwitch()
        held: list[Order] = []
        session = SessionJobs(
            reconciler=Reconciler(broker, switch, SimulatedClock(MIDSESSION)),
            portfolio=_book(),
            open_orders=lambda: list(held),
        )

        placed = broker._accept(
            Order(
                symbol="SPY",
                side=Side.BUY,
                qty=Decimal(10),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("99"),
            )
        )
        held.append(placed)

        await reconcile_with_broker(session)

        assert switch.is_engaged() is False, "a known order was reported as an orphan"


class RecordingAlerts:
    """An `AlertSink` that keeps what it was handed."""

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)


def _watch(
    switch: FakeKillSwitch | None = None,
    *,
    stats: RunnerStats | None = None,
) -> tuple[SessionWatch, RecordingAlerts]:
    alerts = RecordingAlerts()
    watch = SessionWatch(
        kill_switch=switch or FakeKillSwitch(),
        alerts=alerts,
        stats=(lambda: stats) if stats is not None else None,
    )
    return watch, alerts


def _halted(**overrides: Any) -> FakeKillSwitch:
    switch = FakeKillSwitch()
    fields: dict[str, Any] = {
        "scope": HaltScope.GLOBAL,
        "reason": HaltReason.DATA_FEED_LOST,
        "engaged_at": datetime(2024, 6, 3, 18, 46, tzinfo=UTC),
        "engaged_by": "staleness_monitor",
        "target": None,
    }
    fields.update(overrides)
    switch.halts = [HaltRecord(**fields)]
    return switch


class TestTheHaltReminder:
    """F8. A global halt stood for 2h37m on day 1 and produced exactly one
    alert — the one that engaged it. Nothing ever said it was *still* on."""

    async def test_it_says_nothing_when_nothing_is_halted(self) -> None:
        watch, alerts = _watch()

        await remind_about_halts(watch)

        assert alerts.sent == []

    async def test_a_standing_halt_is_repeated(self) -> None:
        watch, alerts = _watch(_halted())

        await remind_about_halts(watch)

        assert len(alerts.sent) == 1
        assert alerts.sent[0].severity is Severity.CRITICAL
        assert "staleness_monitor" in alerts.sent[0].body

    async def test_consecutive_reminders_do_not_collapse_into_one(self) -> None:
        """`key` is what a transport collapses repeats on (`alerts.ports`), so
        a fixed key would make the reminder mute itself after the first — which
        is precisely the silence being fixed."""
        watch, alerts = _watch(_halted())

        await remind_about_halts(watch)
        await remind_about_halts(watch)

        assert len({a.key for a in alerts.sent}) == 2

    async def test_it_reads_redis_every_time_rather_than_a_local_flag(self) -> None:
        """This is what "durable" means here: the state driving the reminder is
        the halt record, which survives the restart that killed five workers on
        day 1. A per-process flag would have died with each of them."""
        switch = _halted()
        watch, alerts = _watch(switch)

        await remind_about_halts(watch)
        switch.halts = []
        await remind_about_halts(watch)

        assert len(alerts.sent) == 1, "a cleared halt must stop the reminders"


class TestTheSessionSummary:
    """F8. Day 1 ran ten hours, submitted zero orders, spent its last 74
    minutes halted, and told nobody any of it."""

    async def test_it_reports_the_numbers(self) -> None:
        watch, alerts = _watch(stats=RunnerStats(evaluations=390, orders_submitted=0))

        await summarise_the_session(watch)

        assert "0 orders submitted" in alerts.sent[0].body
        assert "390 evaluations" in alerts.sent[0].body

    async def test_a_day_that_ended_halted_is_critical_not_informational(self) -> None:
        """A halt still standing at the close is something to act on before
        tomorrow's open, not something to read in the morning."""
        watch, alerts = _watch(_halted(), stats=RunnerStats())

        await summarise_the_session(watch)

        assert alerts.sent[0].severity is Severity.CRITICAL
        assert "STILL HALTED" in alerts.sent[0].title

    async def test_an_ordinary_day_is_informational(self) -> None:
        watch, alerts = _watch(stats=RunnerStats(orders_submitted=4))

        await summarise_the_session(watch)

        assert alerts.sent[0].severity is Severity.INFO
        assert "Not halted." in alerts.sent[0].body

    async def test_a_data_only_worker_still_reports(self) -> None:
        """The worker that owed somebody a message on day 1 was the one *not*
        trading, so no strategy must not mean no summary."""
        watch, alerts = _watch(_halted())

        await summarise_the_session(watch)

        assert "no strategy ran today" in alerts.sent[0].body


class TestSchedulingTheEscalations:
    def test_they_are_scheduled_without_a_trading_session(self) -> None:
        watch, _ = _watch()
        names = [_job_name(e) for e in build_schedule(None, watch)]

        assert "remind_about_halts" in names
        assert "summarise_the_session" in names
        assert "reconcile_with_broker" not in names

    def test_the_reminder_runs_in_session_only(self) -> None:
        """Reminding an operator at 03:00 that the platform is not trading is
        how an alert channel gets muted."""
        watch, _ = _watch()
        reminder = next(
            e for e in build_schedule(None, watch) if _job_name(e) == "remind_about_halts"
        )

        assert reminder["trigger"] == "interval"
        assert reminder["market_hours_only"] is True

    def test_the_summary_lands_at_the_close(self) -> None:
        watch, _ = _watch()
        summary = next(
            e for e in build_schedule(None, watch) if _job_name(e) == "summarise_the_session"
        )

        assert summary["trigger"] == "market_close"
        assert summary["offset_minutes"] == 0

    def test_without_a_watch_nothing_escalates(self) -> None:
        names = [_job_name(e) for e in build_schedule(None)]

        assert names == [_job_name(e) for e in SCHEDULE]


class TestTheDailyLossRollover:
    """F10. The stub promised three things; two were already being done
    elsewhere, and the third could not be built because nothing ever engaged a
    daily-loss halt to clear (docs/paper-week/day-1-review.md)."""

    @staticmethod
    def _halt(**overrides: Any) -> FakeKillSwitch:
        switch = FakeKillSwitch()
        fields: dict[str, Any] = {
            "scope": HaltScope.GLOBAL,
            "reason": HaltReason.DAILY_LOSS_LIMIT,
            "engaged_at": datetime(2024, 6, 2, 19, 30, tzinfo=UTC),  # yesterday
            "engaged_by": DAILY_LOSS_RULE,
            "target": None,
        }
        fields.update(overrides)
        switch.halts = [HaltRecord(**fields)]
        return switch

    async def test_yesterdays_daily_loss_halt_is_released(self) -> None:
        switch = self._halt()
        watch, alerts = _watch(switch)

        await rollover_daily_counters(watch)

        assert switch.clears == [(str(HaltScope.GLOBAL), ROLLOVER_ACTOR, None)]
        assert alerts.sent[0].severity is Severity.INFO

    async def test_a_halt_breached_today_is_kept(self) -> None:
        """A limit hit this morning is not yesterday's halt, and clearing it
        would hand the day a second allowance against a drawn-down account."""
        switch = self._halt(engaged_at=datetime.now(UTC))
        watch, _ = _watch(switch)

        await rollover_daily_counters(watch)

        assert switch.clears == []

    async def test_a_manual_halt_is_never_touched(self) -> None:
        """The narrowness is the whole defence. docs/RISK.md is explicit that
        clearing is deliberate where engaging is reflexive, and this is the only
        automated clear in the platform."""
        switch = self._halt(reason=HaltReason.MANUAL, engaged_by="jo")
        watch, _ = _watch(switch)

        await rollover_daily_counters(watch)

        assert switch.clears == []

    async def test_a_daily_loss_reason_a_human_chose_is_not_touched_either(self) -> None:
        """`scripts/halt.py --reason daily_loss_limit` is a person's decision.
        Only a halt the risk chain itself engaged is released here."""
        switch = self._halt(engaged_by="jo")
        watch, _ = _watch(switch)

        await rollover_daily_counters(watch)

        assert switch.clears == []

    async def test_a_feed_halt_standing_beside_it_survives(self) -> None:
        """Releasing the loss halt must not resume trading through an outage."""
        switch = self._halt()
        switch.halts.append(
            HaltRecord(
                scope=HaltScope.GLOBAL,
                reason=HaltReason.DATA_FEED_LOST,
                engaged_at=datetime(2024, 6, 2, 19, 45, tzinfo=UTC),
                engaged_by="staleness_monitor",
            )
        )
        watch, _ = _watch(switch)

        await rollover_daily_counters(watch)

        assert len(switch.clears) == 1, "only the daily-loss halt is released"

    async def test_nothing_halted_is_a_no_op(self) -> None:
        watch, alerts = _watch()

        await rollover_daily_counters(watch)

        assert alerts.sent == []

    def test_it_runs_before_the_open(self) -> None:
        """So a halt released here is released while nothing is trading."""
        watch, _ = _watch()
        job = next(
            e for e in build_schedule(None, watch) if _job_name(e) == "rollover_daily_counters"
        )

        assert job["trigger"] == "market_open"
        assert job["offset_minutes"] == -5

    def test_it_is_no_longer_a_dormant_stub(self) -> None:
        """It sat in `SCHEDULE` raising `NotImplementedError`, so the driver
        marked it dormant after one attempt and it never ran again."""
        assert "rollover_daily_counters" not in [_job_name(e) for e in SCHEDULE]


class TestTheDailyReportJob:
    """The stub is gone, and what replaced it reports what it cannot measure."""

    def test_it_is_no_longer_in_the_dormant_list(self) -> None:
        """It sat in `SCHEDULE` raising `NotImplementedError`, so the driver
        marked it dormant after one attempt and it never ran again."""
        assert "generate_daily_report" not in [_job_name(e) for e in SCHEDULE]

    def test_it_runs_after_the_close_and_after_the_summary(self) -> None:
        """The two are not the same message. `summarise_the_session` fires at
        the bell and says whether to act tonight; this is the day written down
        half an hour later."""
        watch, _ = _watch()
        schedule = build_schedule(None, watch)
        report = next(e for e in schedule if _job_name(e) == "generate_daily_report")
        summary = next(e for e in schedule if _job_name(e) == "summarise_the_session")

        assert report["trigger"] == "market_close"
        assert report["offset_minutes"] > summary["offset_minutes"]


class TestTheCorporateActionsJob:
    def test_it_is_no_longer_in_the_dormant_list(self) -> None:
        assert "apply_corporate_actions" not in [_job_name(e) for e in SCHEDULE]

    def test_it_runs_well_before_the_open(self) -> None:
        """Far enough out that a split-shaped move is alerted with time to act
        before the reconciler halts on the mismatch it causes."""
        watch, _ = _watch()
        job = next(
            e for e in build_schedule(None, watch) if _job_name(e) == "apply_corporate_actions"
        )

        assert job["trigger"] == "market_open"
        assert job["offset_minutes"] <= -60

    def test_a_split_like_move_reaches_a_human(self) -> None:
        """The point of running an hour early: the reconciliation halt an hour
        later is then expected and explained rather than a mystery."""
        watch, alerts = _watch()

        _report_adjustment(watch, Adjustment("SPY", Decimal(4), 20, 20, None), Timeframe.D1)

        assert len(alerts.sent) == 1
        assert alerts.sent[0].severity is Severity.CRITICAL
        assert "SPY" in alerts.sent[0].title

    def test_a_dividend_sized_move_does_not(self) -> None:
        """Real, worth recording, not worth a phone call at 08:30. Alerting on
        both would train an operator to ignore the one that matters."""
        watch, alerts = _watch()

        _report_adjustment(watch, Adjustment("SPY", Decimal("1.004"), 20, 20, None), Timeframe.D1)

        assert alerts.sent == []

    def test_an_inconsistent_move_is_alerted_without_naming_a_factor_to_act_on(self) -> None:
        """Not a smaller version of a detected split: the history moved in a way
        one corporate action cannot account for, so the alert says the bars
        disagree rather than handing over a number to adopt a share count from."""
        watch, alerts = _watch()

        _report_adjustment(watch, Adjustment("SPY", Decimal(4), 20, 11, None), Timeframe.D1)

        assert len(alerts.sent) == 1
        assert alerts.sent[0].severity is Severity.CRITICAL
        assert "inconsistent" in alerts.sent[0].title
        assert "11 of 20" in alerts.sent[0].body
