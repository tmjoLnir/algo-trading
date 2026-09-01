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
from typing import TYPE_CHECKING

import pytest

from atp_core.risk.killswitch import HaltReason, HaltScope
from atp_core.worker import DEFAULT_WORKER_CONFIG, WorkerConfig
from atp_core.worker.config import parse_symbol_list
from atp_worker.main import HALT_ACTOR, WorkerError, supervise
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
