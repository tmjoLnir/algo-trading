"""The queued backtest, from job to stored result.

`run_backtest_task` against fakes: no Redis, no Postgres, no arq. The engine is
real, over a handful of synthetic bars, because the thing worth testing is that a
job produces a *stored result* — and a fake engine would leave the serialisation,
which is where the interesting failures are, untested.

Four properties, and the first is the one the stub's docstring named:

1. **A run never stays at `running`.** Every path out of this task writes a
   terminal status with a reason, and the one path that cannot — the process
   being killed — is what `sweep_interrupted` exists for.
2. **The task and the enqueue agree on the job's name.** The producer enqueues by
   string precisely so it need not import the consumer, which means a rename on
   either side is a job accepted by Redis and rejected by the worker.
3. **A redelivered job does not overwrite a conclusion.** arq redelivers a job
   whose worker died before acknowledging it.
4. **Progress that cannot be published does not fail the run.** Losing four
   minutes of compute because a status update failed would be absurd.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from arq.connections import RedisSettings
from arq.worker import create_worker

from atp_core.backtest.engine import PROGRESS_EVERY
from atp_core.backtest.ports import BacktestRunSpec
from atp_core.clock import SystemClock
from atp_core.config import get_settings
from atp_core.domain import Bar, Timeframe
from atp_core.persistence.backtests import new_run
from atp_core.persistence.jobs import QUEUE_NAME, RUN_BACKTEST_TASK
from atp_core.strategy import registry
from atp_worker.queue import (
    INTERRUPTED_ERROR,
    JOB_TIMEOUT_SECONDS,
    STALE_AFTER,
    WorkerSettings,
    sweep_interrupted,
)
from atp_worker.tasks import run_backtest_task
from tests.fakes import FakeBacktestQueue, FakeBacktestRunRepository, a_totals

T0 = datetime(2024, 1, 2, tzinfo=UTC)
RUN_ID = "run-1"
SHIPPED = "sma_crossover"

#: Enough bars that the shipped strategy warms up (slow_period + 1) and can then
#: cross. Fewer and the run is legitimate but takes no trades, which tests a
#: different thing.
BARS = 90


def bars(count: int = BARS, *, symbol: str = "SPY") -> list[Bar]:
    """A synthetic daily series that oscillates.

    A **wave**, not a ramp, and the shape is what makes this fixture useful.

    On a monotonically rising series a fast SMA sits above a slow one from the
    first bar both can be computed and never crosses it, so `sma_crossover`
    produces no signal at all — no entry, no order, no trade, and every assertion
    about the trade table passes over an empty list. It has to go *down* first for
    an up-cross to happen, and then down again for the exit.

    Two full cycles over `BARS`, so the crossings land after the strategy's
    warmup rather than inside it.
    """
    series = []
    for index in range(count):
        # Decimal via `str`, never `Decimal(float)`: a price is money (rule §1.1),
        # and the sine is only choosing which price.
        base = Decimal(str(round(100 + 12 * math.sin(2 * math.pi * index / (count / 2)), 2)))
        series.append(
            Bar(
                symbol=symbol,
                ts=T0 + timedelta(days=index),
                timeframe=Timeframe.D1,
                open=base,
                high=base + Decimal("1"),
                low=base - Decimal("1"),
                close=base + Decimal("0.5"),
                volume=Decimal("5000000"),
                # A synthetic series has no corporate actions, so the adjusted
                # close is the close. Present rather than null because the engine
                # prices off adjusted closes and refuses a series without them.
                adj_close=base + Decimal("0.5"),
            )
        )
    return series


class FakeBarRepository:
    def __init__(self, series: dict[str, list[Bar]] | None = None) -> None:
        self.series = series if series is not None else {"SPY": bars()}
        self.error: Exception | None = None

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        if self.error is not None:
            raise self.error
        return list(self.series.get(symbol, []))


def a_spec(**overrides: Any) -> BacktestRunSpec:
    fields: dict[str, Any] = {
        "strategy_id": SHIPPED,
        "symbols": ("SPY",),
        "start": T0,
        "end": T0 + timedelta(days=BARS + 1),
        "timeframe": "1d",
        "starting_cash": "100000",
        "cost_model": "alpaca_equities",
        "params": {"fast_period": 5, "slow_period": 20},
        "qty": "10",
    }
    fields.update(overrides)
    return BacktestRunSpec(**fields)


@pytest.fixture
def runs() -> FakeBacktestRunRepository:
    return FakeBacktestRunRepository()


@pytest.fixture
def queue() -> FakeBacktestQueue:
    return FakeBacktestQueue()


@pytest.fixture
def bar_repo() -> FakeBarRepository:
    return FakeBarRepository()


@pytest.fixture
def ctx(
    runs: FakeBacktestRunRepository, queue: FakeBacktestQueue, bar_repo: FakeBarRepository
) -> dict[str, Any]:
    return {
        "runs": runs,
        "queue": queue,
        "bars": bar_repo,
        "clock": SystemClock(),
        "settings": get_settings(),
    }


async def queued(runs: FakeBacktestRunRepository, **overrides: Any) -> None:
    await runs.create(new_run(RUN_ID, a_spec(**overrides), queued_at=T0))


class TestTheContractWithTheProducer:
    def test_the_queue_worker_has_a_populated_registry(self) -> None:
        """Importing `atp_worker.queue` must be enough to know the strategies.

        `@register` runs at import time, so a process that has never imported a
        strategy module has an empty registry — and this worker would fail every
        queued run with "unknown strategy" while the API, which does import them,
        accepted the request at the door. That is the least debuggable shape this
        failure has, and it is what this test caught.
        """
        assert SHIPPED in registry.all_strategies()

    def test_the_task_name_the_api_enqueues_is_the_one_registered(self) -> None:
        """A rename on either side is a job that is accepted and never runs.

        `atp_core.persistence.jobs` enqueues by string so the API need not import
        this module; that indirection is exactly what a compiler cannot check, so
        it is checked here.
        """
        assert run_backtest_task.__name__ == RUN_BACKTEST_TASK

    def test_the_task_is_registered_with_the_worker(self) -> None:
        """arq rejects a job naming a function that is not on the whitelist."""
        assert run_backtest_task in WorkerSettings.functions

    async def test_a_failed_backtest_is_not_retried(self) -> None:
        """A backtest is deterministic over stored bars, so a retry spends the
        same minutes to reach the same failure — four more times, on arq's
        default, while the queue backs up behind it.

        Asserted on a **constructed worker**, not on the settings class, and the
        difference is the point: `arq.worker.get_kwargs` filters
        `WorkerSettings.__dict__` down to the names `Worker` actually accepts, so
        a misspelled setting is silently discarded and the default applies. This
        is what notices that; reading `WorkerSettings.max_tries` would pass
        against a setting arq never received.
        """
        worker = create_worker(
            WorkerSettings,  # type: ignore[arg-type]
            redis_settings=RedisSettings.from_dsn("redis://localhost:6379/0"),
        )

        assert worker.max_tries == 1
        assert worker.retry_jobs is False
        # One at a time: each run saturates a core for minutes, and two on a
        # single-vCPU host (ADR 0011) make both take twice as long.
        assert worker.max_jobs == 1
        assert worker.queue_name == QUEUE_NAME
        # Generous, because what it bounds is a legitimate multi-year minute-bar
        # run — and the sweep threshold has to stay comfortably above it so a
        # slow run is never swept out from under itself.
        assert worker.job_timeout_s == JOB_TIMEOUT_SECONDS
        assert STALE_AFTER.total_seconds() > worker.job_timeout_s


class TestASuccessfulRun:
    async def test_it_finishes_with_metrics_a_curve_and_trades(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        await queued(runs)

        result = await run_backtest_task(ctx, RUN_ID)

        assert result["status"] == "done"
        stored = runs.runs[RUN_ID]
        assert stored.status == "done"
        assert stored.error is None
        assert stored.metrics is not None
        assert stored.equity_curve is not None
        assert stored.trades is not None
        # One equity point per bar in the merged timeline.
        assert len(stored.equity_curve) == BARS

    async def test_it_stamps_started_and_finished(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """`started_at` is written by the worker that claimed it, which is the
        whole point of the column being nullable."""
        await queued(runs)
        assert runs.runs[RUN_ID].started_at is None

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.started_at is not None
        assert stored.finished_at is not None
        assert stored.finished_at >= stored.started_at

    async def test_money_in_the_stored_curve_is_a_string(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """Decimal to the pixels, with no float in between (CLAUDE.md §1.1)."""
        await queued(runs)

        await run_backtest_task(ctx, RUN_ID)

        curve = runs.runs[RUN_ID].equity_curve or []
        assert all(isinstance(point[1], str) for point in curve)

    async def test_stored_trades_carry_a_real_exit_reason(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """The reason the engine now sets `Order.purpose`.

        `PerformanceAnalyzer.build_trades` reads `purpose` to label an exit. With
        the engine leaving it at its `entry` default, every exit here — stop-outs
        included — would read `signal`: a *wrong* label rather than a missing one,
        on the field that decides whether a strategy's stops are misplaced.

        Asserted as "never unknown" rather than as a specific reason, because
        which exits this synthetic series produces is not the property under
        test.
        """
        await queued(runs)

        await run_backtest_task(ctx, RUN_ID)

        trades = runs.runs[RUN_ID].trades or []
        assert trades, "the wave series should close at least one round trip"
        assert all(trade["exit_reason"] != "unknown" for trade in trades)
        # Not the `entry` default read back as "signal" for something that was
        # not a signal exit: every reason here is one the engine actually chose.
        assert all(
            trade["exit_reason"] in {"signal", "stop_loss", "take_profit", "time", "manual"}
            for trade in trades
        )

    async def test_metrics_are_json_legal(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """Infinity is a real metric value — `profit_factor` with nothing lost —
        and is not legal JSON. Stored as None, which the dashboard renders as
        `—` rather than as a number."""
        import json

        await queued(runs)

        await run_backtest_task(ctx, RUN_ID)

        metrics = runs.runs[RUN_ID].metrics or {}
        assert json.dumps(metrics, allow_nan=False)


class TestProgress:
    async def test_it_reports_the_whole_timeline_by_the_end(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository, queue: FakeBacktestQueue
    ) -> None:
        """The last report is always the full count.

        Not whatever the last multiple of `PROGRESS_EVERY` happened to be — a bar
        stuck at 96% on a finished run is a support question.
        """
        await queued(runs)

        await run_backtest_task(ctx, RUN_ID)

        assert queue.reports, "a run reported no progress at all"
        last = queue.reports[-1]
        assert last.bars_done == last.bars_total == BARS
        assert last.fraction == 1.0

    async def test_a_short_run_still_reports_a_start_and_an_end(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository, queue: FakeBacktestQueue
    ) -> None:
        """`PROGRESS_EVERY` is 500, so a 90-bar run hits no interval report at
        all — and would show nothing without the unconditional first and last."""
        assert BARS < PROGRESS_EVERY
        await queued(runs)

        await run_backtest_task(ctx, RUN_ID)

        assert queue.reports[0].bars_done == 0
        assert queue.reports[-1].bars_done == BARS

    async def test_a_run_completes_when_progress_cannot_be_published(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository, queue: FakeBacktestQueue
    ) -> None:
        """Losing minutes of compute because a status update failed would be an
        absurd trade. `BacktestQueue.report` swallows it, which is the one place
        in that adapter where swallowing is right."""
        queue.report_error = ConnectionError("redis is gone")
        await queued(runs)

        await run_backtest_task(ctx, RUN_ID)

        assert runs.runs[RUN_ID].status == "done"
        assert queue.reports == []


class TestFailurePaths:
    async def test_missing_history_fails_the_run_with_the_backfill_command(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository, bar_repo: FakeBarRepository
    ) -> None:
        """The API checks this before queueing; this catches history that was
        there at request time and is not now — a restored database, deleted bars.
        Neither check can stand in for the other.
        """
        bar_repo.series = {}
        await queued(runs)

        result = await run_backtest_task(ctx, RUN_ID)

        assert result["status"] == "failed"
        stored = runs.runs[RUN_ID]
        assert stored.status == "failed"
        assert "backfill_bars.py" in (stored.error or "")

    async def test_a_domain_failure_lands_on_the_row_in_words(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository, bar_repo: FakeBarRepository
    ) -> None:
        """A run stuck at `running` forever is the worst outcome; one that says
        `failed` with no reason is the second worst."""
        # Unsorted bars: `BacktestEngine._validate` raises `DataGapError`.
        series = bars()
        bar_repo.series = {"SPY": [series[5], *series[:5], *series[6:]]}
        await queued(runs)

        result = await run_backtest_task(ctx, RUN_ID)

        assert result["status"] == "failed"
        assert "DataGapError" in (runs.runs[RUN_ID].error or "")

    async def test_a_partial_result_is_not_kept_on_a_failure(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository, bar_repo: FakeBarRepository
    ) -> None:
        """A chart of two of the five years somebody asked about is worse than no
        chart, because it renders."""
        bar_repo.series = {}
        await queued(runs)

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.metrics is None
        assert stored.equity_curve is None
        assert stored.trades is None

    async def test_an_unexpected_error_is_recorded_and_then_re_raised(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository, bar_repo: FakeBarRepository
    ) -> None:
        """Recorded first, so the row does not sit at `running` until a sweep an
        hour later notices; re-raised after, so the failure still reaches arq's
        log and its metrics.
        """
        bar_repo.error = RuntimeError("the database fell over")
        await queued(runs)

        with pytest.raises(RuntimeError, match="the database fell over"):
            await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.status == "failed"
        assert "unexpected RuntimeError" in (stored.error or "")

    async def test_a_job_naming_no_row_returns_rather_than_raising(
        self, ctx: dict[str, Any]
    ) -> None:
        """Nothing to write the failure to. Raising would record it only in arq's
        own result, which is not where anybody is looking."""
        result = await run_backtest_task(ctx, "never-created")

        assert result["status"] == "missing"


class TestRedelivery:
    async def test_a_job_arriving_after_its_own_conclusion_changes_nothing(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository, queue: FakeBacktestQueue
    ) -> None:
        """A worker declared dead, its run swept, and then it recovered.

        The sweep's verdict is the one on the row, so a late job must not restart
        the work or overwrite the reason.
        """
        await queued(runs)
        await runs.fail(RUN_ID, at=T0, error=INTERRUPTED_ERROR)

        result = await run_backtest_task(ctx, RUN_ID)

        assert result["status"] == "failed"
        assert runs.runs[RUN_ID].error == INTERRUPTED_ERROR
        assert queue.reports == [], "a concluded run was executed again"


class TestTheStartupSweep:
    async def test_it_fails_a_run_a_dead_worker_left_running(
        self, runs: FakeBacktestRunRepository
    ) -> None:
        """The only thing that ever corrects such a row.

        The job is gone from Redis, no retry is coming, and nothing else in the
        platform looks at `backtest_runs`.
        """
        await queued(runs)
        await runs.mark_running(RUN_ID, at=T0)

        swept = await sweep_interrupted(runs, T0 + timedelta(hours=1), at=T0 + timedelta(hours=2))

        assert swept == [RUN_ID]
        stored = runs.runs[RUN_ID]
        assert stored.status == "failed"
        # Says what happened rather than "failed": the run did not fail, the
        # process running it stopped existing, and those want different responses.
        assert "interrupted" in (stored.error or "")
        assert "queue it again" in (stored.error or "")

    async def test_it_leaves_a_run_that_is_merely_slow(
        self, runs: FakeBacktestRunRepository
    ) -> None:
        """A sweep is only ever wrong in one direction. `STALE_AFTER` is twice
        the job timeout so a legitimate long run is never swept out from under
        itself."""
        await queued(runs)
        await runs.mark_running(RUN_ID, at=T0)

        swept = await sweep_interrupted(runs, T0 - timedelta(hours=1), at=T0)

        assert swept == []
        assert runs.runs[RUN_ID].status == "running"

    async def test_it_leaves_a_queued_run_alone(self, runs: FakeBacktestRunRepository) -> None:
        """A queued run with no job is possible and is indistinguishable from one
        that is simply waiting. Failing a job that is about to run would be worse
        than leaving one that never will."""
        await queued(runs)

        swept = await sweep_interrupted(runs, T0 + timedelta(days=1), at=T0 + timedelta(days=1))

        assert swept == []
        assert runs.runs[RUN_ID].status == "queued"

    async def test_it_leaves_a_finished_run_alone(self, runs: FakeBacktestRunRepository) -> None:
        await queued(runs)
        await runs.mark_running(RUN_ID, at=T0)
        await runs.finish(
            RUN_ID, at=T0, metrics={}, equity_curve=[], trades=[], warnings=[], totals=a_totals()
        )

        assert await sweep_interrupted(runs, T0 + timedelta(days=1), at=T0) == []
        assert runs.runs[RUN_ID].status == "done"


class TestWarningsTravelWithTheResult:
    async def test_a_zero_cost_run_says_it_is_not_evidence(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """The CLI prints this on every zero-cost run. A queued run has no
        terminal, so the equivalent rides on the result — where whoever reads it
        hours later will see it."""
        from atp_core.backtest.runner import run_spec

        spec = a_spec(cost_model="zero")
        result = run_spec(spec, {"SPY": bars()}, limits=get_settings().risk)

        assert "NOT evidence" in result.warnings[0]

    async def test_a_fixed_qty_run_states_its_share_count(self, ctx: dict[str, Any]) -> None:
        """Still said, because it is still true of a run sized this way: sizing
        every entry identically ignores volatility, so the return is a property
        of the share count as much as of the strategy."""
        from atp_core.backtest.runner import run_spec

        result = run_spec(a_spec(), {"SPY": bars()}, limits=get_settings().risk)

        assert "sized at 10 shares" in " ".join(result.warnings)

    async def test_a_risk_sized_run_does_not_claim_a_share_count(self, ctx: dict[str, Any]) -> None:
        """The warning is about a choice now, not about the platform. Saying it
        on a run that sized by risk would be the result warning about something
        that did not happen."""
        from atp_core.backtest.runner import run_spec

        spec = a_spec(sizing_method="equity_pct", sizing_value="0.05")
        result = run_spec(spec, {"SPY": bars()}, limits=get_settings().risk)

        assert "sized at" not in " ".join(result.warnings)

    async def test_no_run_claims_an_empty_rule_chain_any_more(self, ctx: dict[str, Any]) -> None:
        """This warning rode on every result while `build_engine` passed
        `rules=[]`. It does not any more, and a run still carrying it would mean
        the chain had been dropped again."""
        from atp_core.backtest.runner import run_spec

        result = run_spec(a_spec(), {"SPY": bars()}, limits=get_settings().risk)

        assert "no pre-trade risk rules" not in " ".join(result.warnings)


class TestTheRunKeepsItsMoney:
    """The nine figures a run produces about the money it made.

    `BacktestResult.to_report()` has always had them and the CLI's `--out` JSON
    has always carried them. A *queued* run had none of them at any layer: the
    engine computed them and `result_to_storage` returned metrics, the curve,
    the trades and the warnings.

    The gap that matters is the second test. A run that ends holding everything
    and one that banked the same return have identical metric sets — every
    per-trade statistic counts closed round trips — and they mean opposite
    things. `num_trades: 0` is the only hint the metric set carries, and it says
    something different: that the statistics rest on no closed trips, which
    reads as "does not trade much" rather than "you are still holding all of
    it".
    """

    async def test_the_money_reaches_the_row(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        await queued(runs)

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.totals is not None
        assert set(stored.totals) == {
            "starting_equity",
            "ending_equity",
            "total_return",
            "realized_pnl",
            "unrealized_pnl",
            "fees",
            "open_positions",
            "orders",
            "filled_orders",
            "signals",
        }

    async def test_a_return_that_is_all_mark_says_so(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """`buy_and_hold` never closes, so every penny of its return is a mark.

        This is the shape of the run that motivated the column: 202.8% reported,
        none of it realised, twenty positions still open, and a dashboard
        reading "202.8% return" with nothing to say otherwise.
        """
        await queued(runs, strategy_id="buy_and_hold")

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.metrics is not None
        assert stored.metrics["num_trades"] == 0  # says nothing about *why*
        assert stored.totals is not None

        realised = Decimal(str(stored.totals["realized_pnl"]))
        unrealised = Decimal(str(stored.totals["unrealized_pnl"]))
        starting = Decimal(str(stored.totals["starting_equity"]))
        ending = Decimal(str(stored.totals["ending_equity"]))

        # Nothing was banked. Asserted at the cent rather than at zero: sizing
        # divides, so a fill price carries the Decimal context's full 28 digits
        # and the remainder that defines `realized_pnl` keeps a residue some
        # twenty orders of magnitude below a cent. It is not a balance anybody
        # can hold, and rounding it away is what every reader of this figure
        # does anyway.
        assert abs(realised) < Decimal("0.005")
        assert unrealised != 0
        assert int(str(stored.totals["open_positions"])) > 0

        # The invariant that makes the split trustworthy rather than two
        # separate sums that can drift apart: `realized_pnl` is computed as the
        # remainder, so the two halves add back to the change in equity exactly.
        assert realised + unrealised == ending - starting

    def test_money_is_a_string_and_a_count_is_an_integer(self) -> None:
        """The reason this is a column of its own rather than more `metrics`.

        `metrics` is float by contract — those are statistics over a return
        series. Five of these are balances, and a balance that round-tripped
        through a JSON number would no longer be exact (CLAUDE.md §1.1).
        """
        from atp_core.backtest.runner import run_spec

        totals = run_spec(a_spec(), {"SPY": bars()}, limits=get_settings().risk).totals()

        for key in (
            "starting_equity",
            "ending_equity",
            "total_return",
            "realized_pnl",
            "unrealized_pnl",
            "fees",
        ):
            assert isinstance(totals[key], str), f"{key} must cross as a decimal string"
        for key in ("open_positions", "orders", "filled_orders", "signals"):
            assert isinstance(totals[key], int), f"{key} is a count"

    def test_the_cli_and_the_stored_row_cannot_disagree(self) -> None:
        """One assembly, two callers — ADR 0006's rule, applied to these nine.

        A second copy in `to_report` would be a second chance for a queued run
        and a CLI run to report different money for the same backtest.
        """
        from atp_core.backtest.runner import run_spec

        result = run_spec(a_spec(), {"SPY": bars()}, limits=get_settings().risk)

        report = result.to_report()

        assert result.totals().items() <= report.items()

    async def test_they_are_written_in_the_same_call_as_the_rest(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """A `done` row reporting a return with no way to say how much of it was
        banked is the row this column exists to stop existing."""
        await queued(runs)

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.status == "done"
        assert stored.metrics is not None
        assert stored.totals is not None

    async def test_a_failed_run_keeps_none_of_it(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """Cleared with the rest: the figures described a result this run does
        not have."""
        await queued(runs, symbols=("NOPE",))

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.status == "failed"
        assert stored.totals is None


class TestTheRunKeepsItsWarnings:
    """What the run said about itself has to reach the row.

    Everything here was computed on every queued run before this existed and
    then dropped: `result_to_storage` returned metrics, curve and trades, and
    `BacktestResult.warnings` went nowhere. The API filled the hole by deriving
    warnings from the metric set on read, which can only ever produce the two
    caveats that *are* functions of the metrics.

    The gap that matters is the first test below. A run whose every order was
    refused has the same all-zero metric set as one that never signalled, so
    nothing derived from metrics can tell them apart — and they call for
    opposite responses.
    """

    async def test_a_run_whose_orders_were_all_refused_says_so_on_the_row(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """`risk_pct` sizing with no stop anywhere: the exact shape of the run
        that motivated this. Every entry is refused for want of a distance to
        measure risk against, and the result is all zeros."""
        await queued(runs, sizing_method="risk_pct", sizing_value="0.01")

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.metrics is not None
        assert stored.metrics["num_trades"] == 0  # indistinguishable from idle
        assert stored.warnings is not None
        assert any("refused before reaching the market" in w for w in stored.warnings)
        assert any("position_sizing" in w for w in stored.warnings)

    async def test_the_zero_cost_caveat_is_stored_rather_than_only_printed(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """A queued run has no terminal, so this is the only place it can land."""
        await queued(runs, cost_model="zero")

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.warnings is not None
        assert any("NOT evidence" in w for w in stored.warnings)

    async def test_the_fixed_qty_caveat_travels_with_the_return_it_qualifies(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        await queued(runs)  # a_spec sizes by a flat share count

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.warnings is not None
        assert any("sized at 10 shares" in w for w in stored.warnings)

    async def test_they_are_written_in_the_same_call_as_the_rest_of_the_result(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """Not a second `UPDATE`. A `done` row carrying metrics and no warnings
        would claim a clean result it never had."""
        await queued(runs, cost_model="zero")

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.status == "done"
        assert stored.metrics is not None
        assert stored.equity_curve is not None
        assert stored.trades is not None
        assert stored.warnings is not None

    async def test_a_failed_run_keeps_none_of_them(
        self, ctx: dict[str, Any], runs: FakeBacktestRunRepository
    ) -> None:
        """Cleared with the rest of the result. They caveated a result this run
        does not have, and `error` is the sentence that replaces them."""
        await queued(runs, symbols=("NOPE",))

        await run_backtest_task(ctx, RUN_ID)

        stored = runs.runs[RUN_ID]
        assert stored.status == "failed"
        assert stored.error is not None
        assert stored.warnings is None
