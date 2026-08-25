"""`backtest_runs` against a real PostgreSQL.

These cannot be unit tests. What is under test is the database's behaviour, not
Python's:

- whether the **foreign key** actually refuses a run naming a strategy nothing
  has registered — the state a fresh install is in, since `WORKER_STRATEGY` is
  empty by default;
- whether `started_at` is genuinely nullable, which is the whole of migration
  `d7a1c9f4b208` and is what lets a queued run exist at all;
- whether the **conditional transitions** hold under the real `UPDATE ... WHERE
  status IN (...)`, so a redelivered arq job cannot overwrite a conclusion that
  has already been written;
- whether the JSON columns round-trip a metric bag, a curve of decimal *strings*
  and a trade list without any of it becoming a float.

A fake can be made to do all of that, which is exactly why the fake is not
evidence. `tests/fakes.FakeBacktestRunRepository` mirrors these rules; this is
what says the mirror is accurate.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
import pytest
import sqlalchemy

from atp_core.backtest.ports import BacktestRunSpec
from atp_core.clock import SimulatedClock
from atp_core.persistence.backtests import PostgresBacktestRunRepository, new_run
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.strategies import PostgresStrategyRepository
from atp_core.strategy.ports import StrategyRecord

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
STRATEGY = "sma_crossover"


def a_spec(strategy_id: str = STRATEGY) -> BacktestRunSpec:
    return BacktestRunSpec(
        strategy_id=strategy_id,
        symbols=("SPY", "QQQ"),
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2025, 1, 1, tzinfo=UTC),
        timeframe="1d",
        # Unrepresentable as a binary float on purpose: if anything in this path
        # turns it into one, it comes back as 100000.10000000001.
        starting_cash="100000.1",
        cost_model="alpaca_equities",
        params={"fast_period": 10, "slow_period": 30},
        qty="100",
    )


@pytest.fixture
async def clean_backtest_tables(migrated_db: str) -> AsyncIterator[str]:
    """Empty `backtest_runs` and the `strategies` rows it points at.

    Truncated before rather than after, so a failed test leaves its rows to be
    inspected instead of tidying away the evidence.
    """
    conn = await asyncpg.connect(migrated_db.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute("TRUNCATE TABLE backtest_runs, strategies CASCADE")
    finally:
        await conn.close()
    yield migrated_db


@pytest.fixture
async def repo(
    clean_backtest_tables: str,
) -> AsyncIterator[tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]]:
    engine = create_engine(clean_backtest_tables)
    try:
        factory = create_session_factory(engine)
        yield (
            PostgresBacktestRunRepository(factory),
            PostgresStrategyRepository(factory, SimulatedClock(T0)),
        )
    finally:
        await engine.dispose()


async def _registered(strategies: PostgresStrategyRepository) -> None:
    """The `strategies` row a backtest's foreign key needs.

    Written by the runner at its first session open in production. Here it stands
    for "a worker has loaded this strategy at least once", which is the real
    precondition for queueing a backtest against it.
    """
    await strategies.ensure(
        StrategyRecord(
            id=STRATEGY,
            name=STRATEGY,
            kind="coded",
            class_name="SmaCrossover",
            params={"fast_period": 10, "slow_period": 30},
            universe=("SPY", "QQQ"),
            timeframe="1d",
        )
    )


class TestTheForeignKey:
    async def test_a_run_naming_an_unregistered_strategy_is_refused(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """The realistic failure, not a hypothetical one.

        A strategy class is registered at import time; a `strategies` row is
        written by a worker at its first session open. With `WORKER_STRATEGY`
        empty by default, "the class exists and no row does" is the ordinary state
        of a fresh install — so this is the constraint `POST /backtests` turns
        into a 409 pointing at the Strategies tab.
        """
        runs, _ = repo

        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await runs.create(new_run("r1", a_spec("never-booted"), queued_at=T0))

    async def test_a_registered_strategy_takes_the_run(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        runs, strategies = repo
        await _registered(strategies)

        await runs.create(new_run("r1", a_spec(), queued_at=T0))

        assert (await runs.get("r1")) is not None


class TestAQueuedRunHasNotStarted:
    async def test_started_at_is_null_on_a_queued_row(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """Migration `d7a1c9f4b208`, demonstrated rather than asserted in Python.

        The column was `NOT NULL`, so before that migration this insert would
        fail outright and the only way to record a queued run was to stamp
        `started_at` with the current time — which would have made every run's
        duration include however long the queue was backed up.
        """
        runs, strategies = repo
        await _registered(strategies)

        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        stored = await runs.get("r1")

        assert stored is not None
        assert stored.status == "queued"
        assert stored.queued_at == T0
        assert stored.started_at is None
        assert stored.finished_at is None

    async def test_claiming_it_stamps_started_at_only(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        runs, strategies = repo
        await _registered(strategies)
        await runs.create(new_run("r1", a_spec(), queued_at=T0))

        await runs.mark_running("r1", at=T0 + timedelta(minutes=3))
        stored = await runs.get("r1")

        assert stored is not None
        assert stored.status == "running"
        assert stored.queued_at == T0
        assert stored.started_at == T0 + timedelta(minutes=3)
        assert stored.finished_at is None


class TestTheResultRoundTrips:
    async def test_metrics_a_curve_and_trades_come_back_as_stored(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """The JSON columns, including the part that matters: money stays a string.

        A curve stored as JSON numbers would come back as floats, and the chart
        the dashboard draws would be the first thing in this platform to render a
        balance that had been through IEEE 754 (CLAUDE.md §1.1).
        """
        runs, strategies = repo
        await _registered(strategies)
        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        await runs.mark_running("r1", at=T0)

        await runs.finish(
            "r1",
            at=T0 + timedelta(minutes=5),
            metrics={"sharpe": 1.25, "num_trades": 42, "profit_factor": None},  # type: ignore[dict-item]
            equity_curve=[[T0.isoformat(), "100000.10"], [T0.isoformat(), "100500.55"]],
            trades=[{"symbol": "SPY", "net_pnl": "500.45", "exit_reason": "stop_loss"}],
        )
        stored = await runs.get("r1")

        assert stored is not None
        assert stored.status == "done"
        assert stored.error is None
        assert stored.metrics == {"sharpe": 1.25, "num_trades": 42, "profit_factor": None}
        assert stored.equity_curve == [
            [T0.isoformat(), "100000.10"],
            [T0.isoformat(), "100500.55"],
        ]
        assert stored.trades == [{"symbol": "SPY", "net_pnl": "500.45", "exit_reason": "stop_loss"}]

    async def test_the_spec_survives_the_config_column(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """A result read a week later has to say what it was a result *of*.

        `starting_cash` is the assertion that earns its keep: a value chosen to be
        unrepresentable as a binary float, so anything in this path that made it a
        number would show up here rather than as a slightly wrong return six
        months later.

        The sizing and stop fields are asserted here because for a while they
        were the ones that did *not* survive, under a test with this exact name:
        `a_spec()` leaves them at their defaults, so every field this checked
        happened to be one of the nine that made it. They are set explicitly
        rather than by changing `a_spec`, which sixteen other cases use and
        which several of them expect to be an unsized, unstopped run.
        `tests/unit/test_backtest_run_spec.py` covers the serialisation itself;
        what this adds is that the real column behaves like the pure functions.
        """
        runs, strategies = repo
        await _registered(strategies)

        asked = dataclasses.replace(
            a_spec(),
            sizing_method="risk_pct",
            sizing_value="0.01",
            stop_type="atr",
            stop_value="2.5",
            stop_period=21,
            stop_bars=7,
        )
        await runs.create(new_run("r1", asked, queued_at=T0))
        stored = await runs.get("r1")

        assert stored is not None
        assert stored.spec.starting_cash == "100000.1"
        assert stored.spec.symbols == ("SPY", "QQQ")
        assert stored.spec.params == {"fast_period": 10, "slow_period": 30}
        assert stored.spec.cost_model == "alpaca_equities"
        assert stored.spec.qty == "100"
        # The six that a run is sized and protected by, and that the worker
        # rebuilds its engine from.
        assert stored.spec == asked

    async def test_a_failure_clears_any_partial_result(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """A chart of two of the five years somebody asked about is worse than no
        chart, because it renders."""
        runs, strategies = repo
        await _registered(strategies)
        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        await runs.mark_running("r1", at=T0)

        await runs.fail("r1", at=T0 + timedelta(minutes=1), error="DataGapError: no bars for QQQ")
        stored = await runs.get("r1")

        assert stored is not None
        assert stored.status == "failed"
        assert stored.error is not None
        assert "no bars for QQQ" in stored.error
        assert stored.metrics is None
        assert stored.equity_curve is None
        assert stored.trades is None


class TestConditionalTransitions:
    """The `WHERE status IN (...)` clauses, against the real thing.

    arq redelivers a job whose worker died before acknowledging it, so a second
    delivery is an ordinary event rather than a race to arbitrate. These are what
    say the conclusion already on the row wins.
    """

    async def test_a_finished_run_cannot_be_reopened(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        runs, strategies = repo
        await _registered(strategies)
        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        await runs.finish("r1", at=T0, metrics={"sharpe": 1.0}, equity_curve=[], trades=[])

        await runs.mark_running("r1", at=T0 + timedelta(hours=1))
        stored = await runs.get("r1")

        assert stored is not None
        assert stored.status == "done"
        assert stored.metrics == {"sharpe": 1.0}

    async def test_a_swept_run_keeps_its_reason_when_the_job_comes_back(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """A worker declared dead, its run swept, and then it recovered. The
        sweep's verdict is the one on the row."""
        runs, strategies = repo
        await _registered(strategies)
        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        await runs.fail("r1", at=T0, error="interrupted — the worker stopped")

        await runs.finish(
            "r1", at=T0 + timedelta(hours=1), metrics={"sharpe": 9.9}, equity_curve=[], trades=[]
        )
        stored = await runs.get("r1")

        assert stored is not None
        assert stored.status == "failed"
        assert stored.metrics is None

    async def test_a_transition_on_a_missing_run_is_a_no_op(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """No row to update, and no exception either.

        The task handles "no such run" itself, by looking first and saying so —
        an adapter that raised here would turn a legible log line into a crash.
        """
        runs, _ = repo

        await runs.mark_running("nope", at=T0)
        await runs.fail("nope", at=T0, error="whatever")

        assert (await runs.get("nope")) is None


class TestReads:
    async def test_newest_first_filtered_and_bounded(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """Ordered by `queued_at`, not `finished_at`.

        Half the rows have no `finished_at` — that is what a queue means — and
        ordering by a column half the table is null in would put the runs somebody
        is currently waiting on in an arbitrary place.
        """
        runs, strategies = repo
        await _registered(strategies)
        for index in range(5):
            await runs.create(
                new_run(f"r{index}", a_spec(), queued_at=T0 + timedelta(minutes=index))
            )

        listed = await runs.list_runs()
        assert [run.id for run in listed] == ["r4", "r3", "r2", "r1", "r0"]

        assert [run.id for run in await runs.list_runs(limit=2)] == ["r4", "r3"]
        assert await runs.list_runs(strategy_id="somebody-else") == []
        assert len(await runs.list_runs(strategy_id=STRATEGY)) == 5

    async def test_stale_running_finds_only_the_orphans(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """The query the startup sweep runs, and the three rows it must not touch.

        A queued run with no job is indistinguishable from one that is simply
        waiting, so failing it would be worse than leaving one that never runs.
        A finished run is finished. And a run that is merely slow must survive —
        `STALE_AFTER` is twice the job timeout for exactly that reason.
        """
        runs, strategies = repo
        await _registered(strategies)

        await runs.create(new_run("waiting", a_spec(), queued_at=T0))

        await runs.create(new_run("orphaned", a_spec(), queued_at=T0))
        await runs.mark_running("orphaned", at=T0)

        await runs.create(new_run("slow", a_spec(), queued_at=T0))
        await runs.mark_running("slow", at=T0 + timedelta(hours=3))

        await runs.create(new_run("finished", a_spec(), queued_at=T0))
        await runs.mark_running("finished", at=T0)
        await runs.finish("finished", at=T0, metrics={}, equity_curve=[], trades=[])

        stale = await runs.stale_running(older_than=T0 + timedelta(hours=1))

        assert stale == ["orphaned"]

    async def test_an_unknown_run_reads_as_none(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        runs, _ = repo

        assert (await runs.get("nope")) is None

    async def test_a_duplicate_id_is_refused(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """The id is minted per request and nothing retries a create, so a
        conflict means two requests generated one id — worth an exception rather
        than an upsert that silently overwrites somebody else's run."""
        runs, strategies = repo
        await _registered(strategies)
        await runs.create(new_run("r1", a_spec(), queued_at=T0))

        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await runs.create(new_run("r1", a_spec(), queued_at=T0))


class TestReadingOneStrategyInFull:
    """`get_stored`, which the queue endpoint uses to ask whether a strategy is
    declarative and to copy its rules onto the run.

    Its unit callers all go through `FakeStrategyRepository`, so this is the only
    thing that executes the query. `ruleset` comes back `None` here and that is
    not a weak assertion — nothing in the platform can write that column yet
    (`ensure` takes a `StrategyRecord`, which has no such field, and the adapter
    stores a hard-coded `None`), so `None` is the only value a real row can
    currently hold. The rules-carrying path is covered against the fake in
    `tests/unit/test_backtests_api.py`.
    """

    async def test_it_returns_the_whole_row(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        _, strategies = repo
        await _registered(strategies)

        stored = await strategies.get_stored(STRATEGY)

        assert stored is not None
        assert stored.id == STRATEGY
        assert stored.kind == "coded"
        assert stored.class_name == "SmaCrossover"
        assert stored.params == {"fast_period": 10, "slow_period": 30}
        assert stored.universe == ("SPY", "QQQ")
        assert stored.ruleset is None

    async def test_an_unknown_id_is_none_rather_than_an_error(
        self, repo: tuple[PostgresBacktestRunRepository, PostgresStrategyRepository]
    ) -> None:
        """The queue endpoint asks about every strategy it is given, including
        coded ones that have a row and ones that have none. A raise here would
        turn "this is not declarative" into a 500."""
        _, strategies = repo
        assert await strategies.get_stored("nothing_has_ever_run_this") is None
