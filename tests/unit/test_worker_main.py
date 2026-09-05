"""The worker supervisor, and the watchlist it is configured from.

No sockets, no Redis, no database: `supervise()` takes the responsibilities it
runs as factories and the kill switch as a port, so every outcome below is
driven by ordinary coroutines (CLAUDE.md §1.7).

The distinction under test is the one the whole function exists for: a signal is
an ordinary shutdown and must *not* halt trading, while a responsibility ending
on its own must. Both directions matter. A supervisor that halts on every
routine restart makes a human clear a halt after each deploy, and one that stays
quiet when the ingestor dies leaves the rest of the system pricing against a
quote that stopped updating.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from atp_core.alerts.ports import Alert, Severity
from atp_core.risk.killswitch import HaltReason, HaltRecord, HaltScope
from atp_core.worker import DEFAULT_WORKER_CONFIG, WorkerConfig
from atp_core.worker.config import parse_symbol_list
from atp_worker.main import (
    HALT_ACTOR,
    WorkerError,
    _active_halts,
    _announce_death,
    _describe_halt,
    supervise,
)
from tests.fakes import FakeKillSwitch

if TYPE_CHECKING:
    from atp_worker.main import Responsibility


async def forever() -> None:
    """Runs until cancelled — what every real responsibility does."""
    await asyncio.Event().wait()


def raises(exc: BaseException) -> Responsibility:
    async def run() -> None:
        raise exc

    return run


async def returns_at_once() -> None:
    """The dangerous shape: finished, with nothing to show it went wrong."""
    return None


class Watcher:
    """A responsibility that records having been cancelled."""

    def __init__(self) -> None:
        self.cancelled = False

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class TestGracefulShutdown:
    async def test_a_signal_stops_everything_without_halting(self) -> None:
        """SIGTERM is a deploy, not an incident. Leaving a halt behind would
        make every routine restart a manual operation."""
        switch = FakeKillSwitch()
        stop = asyncio.Event()
        stop.set()

        await supervise(
            {"ingestor": forever, "scheduler": forever},
            stop_event=stop,
            kill_switch=switch,
        )

        assert switch.engagements == []
        assert switch.is_engaged() is False

    async def test_the_responsibilities_are_actually_cancelled(self) -> None:
        """Returning without cancelling would leave the market-data socket open,
        and Alpaca allows one per key — the next worker would be refused."""
        watchers = [Watcher(), Watcher()]
        stop = asyncio.Event()
        stop.set()

        await supervise(
            {f"task{i}": w.run for i, w in enumerate(watchers)},
            stop_event=stop,
            kill_switch=FakeKillSwitch(),
        )

        assert all(w.cancelled for w in watchers)


class TestSomethingDied:
    async def test_a_raising_responsibility_halts_globally(self) -> None:
        boom = RuntimeError("the feed gave up")
        switch = FakeKillSwitch()

        with pytest.raises(RuntimeError) as caught:
            await supervise(
                {"ingestor": raises(boom)},
                stop_event=asyncio.Event(),
                kill_switch=switch,
            )

        # The original exception, not a wrapper: whoever reads the crash needs
        # the traceback that caused it.
        assert caught.value is boom
        (scope, reason, actor, detail) = switch.engagements[0]
        assert scope == str(HaltScope.GLOBAL)
        assert reason == str(HaltReason.UNHANDLED_EXCEPTION)
        assert actor == HALT_ACTOR
        assert "ingestor" in detail

    async def test_a_responsibility_that_simply_returns_is_still_a_failure(self) -> None:
        """The quiet failure, and the reason `WorkerError` exists. Every
        responsibility here is written to run until cancelled, so one that
        returned did not finish its job — it stopped doing it. Nothing raised,
        so without this branch the worker would sit there with a dead ingestor
        and a healthy-looking process."""
        switch = FakeKillSwitch()

        with pytest.raises(WorkerError, match="should run until cancelled"):
            await supervise(
                {"ingestor": returns_at_once},
                stop_event=asyncio.Event(),
                kill_switch=switch,
            )

        assert switch.engagements, "a responsibility that stopped must still halt trading"
        assert switch.engagements[0][1] == str(HaltReason.UNHANDLED_EXCEPTION)

    async def test_the_survivors_are_cancelled_too(self) -> None:
        """A half-running worker is the state this supervisor exists to avoid."""
        survivor = Watcher()
        switch = FakeKillSwitch()

        with pytest.raises(RuntimeError):
            await supervise(
                {"ingestor": raises(RuntimeError("boom")), "scheduler": survivor.run},
                stop_event=asyncio.Event(),
                kill_switch=switch,
            )

        assert survivor.cancelled

    async def test_the_halt_detail_names_which_responsibility_ended(self) -> None:
        """An operator reading the halt record should not have to guess whether
        the feed died or the scheduler did."""
        switch = FakeKillSwitch()

        with pytest.raises(RuntimeError):
            await supervise(
                {"staleness_monitor": raises(RuntimeError("clock went backwards"))},
                stop_event=asyncio.Event(),
                kill_switch=switch,
            )

        detail = switch.engagements[0][3]
        assert "staleness_monitor" in detail
        assert "clock went backwards" in detail

    async def test_without_a_kill_switch_it_still_propagates(self) -> None:
        """`kill_switch=None` is a worker that cannot halt. It must still crash
        rather than swallow the failure — the log says TRADING IS NOT HALTED."""
        with pytest.raises(RuntimeError):
            await supervise(
                {"ingestor": raises(RuntimeError("boom"))},
                stop_event=asyncio.Event(),
                kill_switch=None,
            )


class TestWatchlist:
    """`normalise_symbols` / `parse_symbol_list` — what the ingestor subscribes.

    On `WorkerConfig` rather than `Settings` now: the watchlist is a stored row
    the dashboard writes, and its text box hands over the same comma-separated
    string the environment variable used to. The normalisation is unchanged, and
    so is every reason for it.
    """

    def test_empty_by_default(self) -> None:
        """No default universe: a worker that invented one would spend the
        single market-data connection on symbols nobody chose."""
        assert DEFAULT_WORKER_CONFIG.symbols == ()

    def test_upper_cased_and_stripped(self) -> None:
        """`symbol` is always an uppercase ticker here, and the ingestor rejects
        anything else — so `spy` typed into the box is normalised, not refused."""
        assert parse_symbol_list(" spy, QQQ ,iwm ") == ("SPY", "QQQ", "IWM")

    def test_deduplicated_in_first_seen_order(self) -> None:
        """A symbol listed twice would be subscribed twice and counted twice
        against the vendor's symbol limit."""
        assert parse_symbol_list("SPY,QQQ,spy") == ("SPY", "QQQ")

    def test_blank_entries_are_dropped(self) -> None:
        """A trailing comma is not a symbol, and `""` would fail the ingestor's
        uppercase check with a confusing message."""
        assert parse_symbol_list("SPY,,QQQ,") == ("SPY", "QQQ")

    def test_a_normalised_list_is_what_the_value_object_accepts(self) -> None:
        """The two halves have to agree: `WorkerConfig` refuses a lowercase
        ticker, so a parse that let one through would move the failure from the
        save to the worker's next boot."""
        assert WorkerConfig(symbols=parse_symbol_list("spy, qqq")).symbols == ("SPY", "QQQ")


class RecordingAlerts:
    """An `AlertSink` that keeps what it was handed."""

    def __init__(self, *, explode: bool = False) -> None:
        self.sent: list[Alert] = []
        self._explode = explode

    def send(self, alert: Alert) -> None:
        if self._explode:
            raise RuntimeError("the transport is down")
        self.sent.append(alert)


def halt_record(
    scope: HaltScope = HaltScope.GLOBAL,
    *,
    target: str | None = None,
    reason: HaltReason = HaltReason.DATA_FEED_LOST,
    engaged_by: str = "staleness_monitor",
) -> HaltRecord:
    return HaltRecord(
        scope=scope,
        reason=reason,
        engaged_at=datetime(2024, 6, 3, 18, 46, tzinfo=UTC),
        engaged_by=engaged_by,
        target=target,
    )


class TestReadingTheHaltAtBoot:
    """F4. A worker restarted into a standing halt announced 'trading
    sma_crossover with paper money' three times on day 1, at INFO, while nothing
    could trade (docs/paper-week/day-1-review.md)."""

    def test_it_reports_every_scope_not_just_global(self) -> None:
        """A leftover symbol halt is the one that goes unnoticed: the loop runs
        and one name silently never trades."""
        switch = FakeKillSwitch()
        switch.halts = [halt_record(HaltScope.SYMBOL, target="SPY")]

        assert _active_halts(switch) == switch.halts

    def test_an_unreadable_switch_does_not_stop_the_worker_booting(self) -> None:
        """`active_halts` lets Redis errors propagate — right for a dashboard,
        wrong on a boot path. The switch fails closed on the same outage, so
        nothing trades either way, and a worker that cannot *describe* the halt
        is still better than no worker at all."""

        class Unreachable(FakeKillSwitch):
            def active_halts(self) -> list[HaltRecord]:
                raise ConnectionError("redis is gone")

        assert _active_halts(Unreachable()) == []

    def test_no_kill_switch_bound_is_not_an_error(self) -> None:
        assert _active_halts(None) == []

    def test_a_halt_is_described_with_who_and_when(self) -> None:
        """The two things anyone asks about a halt they did not place."""
        rendered = _describe_halt(halt_record(HaltScope.SYMBOL, target="SPY"))

        assert "symbol:SPY" in rendered
        assert "staleness_monitor" in rendered
        assert "2024-06-03T18:46:00+00:00" in rendered


class TestAnnouncingADeath:
    """F8. Three workers died in 158 seconds on day 1 and sent zero alerts
    between them — the halt's own notification had already fired, and `engage`
    dedups on the Redis record."""

    def test_a_death_alerts_even_though_the_halt_already_did(self) -> None:
        alerts = RecordingAlerts()

        _announce_death(alerts, "ingestor", "the feed gave up")

        assert len(alerts.sent) == 1
        assert alerts.sent[0].severity is Severity.CRITICAL
        assert "ingestor" in alerts.sent[0].title

    def test_the_key_is_the_responsibility_so_two_deaths_are_two_alerts(self) -> None:
        """A halt is one condition however often it is re-engaged; a process
        death is a new event every time. Keying these on the halt would have
        collapsed day 1's crash loop into the silence it actually produced."""
        alerts = RecordingAlerts()

        _announce_death(alerts, "ingestor", "first")
        _announce_death(alerts, "strategy_runner", "second")

        assert {a.key for a in alerts.sent} == {
            "worker.died.ingestor",
            "worker.died.strategy_runner",
        }

    def test_a_broken_transport_does_not_replace_the_error_being_raised(self) -> None:
        """This runs on the way out of a worker that is already failing."""
        _announce_death(RecordingAlerts(explode=True), "ingestor", "the feed gave up")

    def test_no_sink_bound_is_not_an_error(self) -> None:
        _announce_death(None, "ingestor", "the feed gave up")

    async def test_supervise_alerts_when_a_responsibility_ends(self) -> None:
        alerts = RecordingAlerts()

        with pytest.raises(RuntimeError):
            await supervise(
                {"ingestor": raises(RuntimeError("the feed gave up"))},
                stop_event=asyncio.Event(),
                kill_switch=FakeKillSwitch(),
                alerts=alerts,
            )

        assert [a.key for a in alerts.sent] == ["worker.died.ingestor"]

    async def test_a_clean_shutdown_alerts_nobody(self) -> None:
        """A signal is an ordinary restart. Alerting on one would train an
        operator to ignore the alerts that matter."""
        alerts = RecordingAlerts()
        stop = asyncio.Event()
        stop.set()

        await supervise({"ingestor": Watcher().run}, stop_event=stop, alerts=alerts)

        assert alerts.sent == []
