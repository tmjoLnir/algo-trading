"""Backtest run storage — `BacktestRunRepository` over PostgreSQL.

The `backtest_runs` table has existed since the initial migration with nothing
reading or writing it. That is why `/backtests` was a stub, why
`/analytics/live-vs-backtest` had no second operand, and why the promotion
ratchet on the strategies page could not require "a completed backtest on
record": all three were waiting on this file.

**Two processes write here, and that is deliberate.** The API writes exactly one
thing — `create`, the fact that somebody asked — and the queue worker writes
every transition after it. Those are disjoint columns at disjoint times, not two
processes computing one number, so it is not the problem ADR 0007 refuses. The
alternative would be the API waiting on the worker to acknowledge a job before
answering, which is a synchronous call into a queue whose whole purpose is that
nothing waits on it.

**Every transition is conditional on the status it expects.** `mark_running`
matches `queued` or `running`, `finish` and `fail` match a run that is not
already finished. That is not defensive habit: arq redelivers a job whose worker
died before acknowledging it, so a second delivery is an ordinary event, and the
correct response is to carry on rather than to overwrite a result that has
already landed. The `UPDATE ... WHERE status IN (...)` form makes the check and
the write one statement, so two workers racing on the same run cannot both win.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from atp_core.backtest.ports import (
    IN_FLIGHT,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    BacktestRunSpec,
    StoredBacktestRun,
)
from atp_core.logging import get_logger
from atp_core.persistence.db import session_scope
from atp_core.persistence.models import BacktestRunRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from atp_core.backtest.ports import BacktestRunRepository

log = get_logger(__name__)


class PostgresBacktestRunRepository:
    """`BacktestRunRepository` over the `backtest_runs` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, run: StoredBacktestRun) -> None:
        """Insert a queued run.

        A plain insert, not an upsert. The id is minted per request and nothing
        retries a create, so a primary-key conflict means two requests generated
        one id — a bug worth an exception rather than a silent overwrite of
        somebody else's run.

        `started_at` and `finished_at` go in as whatever the caller passed, which
        for a queued run is None on both. The column allows it as of migration
        `d7a1c9f4b208`; before that this table could not represent a queued run
        at all.
        """
        async with session_scope(self._session_factory) as session:
            session.add(
                BacktestRunRow(
                    id=run.id,
                    strategy_id=run.spec.strategy_id,
                    config=_spec_to_json(run.spec),
                    status=run.status,
                    metrics=run.metrics,
                    equity_curve=run.equity_curve,
                    trades=run.trades,
                    error=run.error,
                    queued_at=run.queued_at,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )
            )

    async def mark_running(self, run_id: str, *, at: datetime) -> None:
        """Claim the run.

        Matches `queued` **or** `running`, so a redelivered job re-stamps
        `started_at` and proceeds rather than being refused. Re-stamping is the
        right answer for a redelivery: the run that is going to produce the
        result is this one, and the duration worth reporting is its own.

        A run already `done` or `failed` is deliberately not matched. That is a
        job arriving after its own result — which happens if a worker was
        declared dead, its run swept, and then it recovered — and the sweep's
        verdict is the one on the row.
        """
        async with session_scope(self._session_factory) as session:
            await session.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id, BacktestRunRow.status.in_(IN_FLIGHT))
                .values(status=STATUS_RUNNING, started_at=at, error=None)
            )

    async def finish(
        self,
        run_id: str,
        *,
        at: datetime,
        metrics: dict[str, float],
        equity_curve: list[list[str]],
        trades: list[dict[str, object]],
    ) -> None:
        """Store the results and mark the run done, in one statement.

        One `UPDATE` rather than three, so there is no instant at which this row
        says `done` and carries only some of its result.
        """
        async with session_scope(self._session_factory) as session:
            await session.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id, BacktestRunRow.status.in_(IN_FLIGHT))
                .values(
                    status=STATUS_DONE,
                    metrics=metrics,
                    equity_curve=equity_curve,
                    trades=trades,
                    error=None,
                    finished_at=at,
                )
            )

    async def fail(self, run_id: str, *, at: datetime, error: str) -> None:
        """Record why a run did not produce a result.

        The results columns are explicitly cleared rather than left alone. A run
        that failed halfway has no result, and a partial equity curve sitting
        under a `failed` status is a chart of two of the five years somebody
        asked about — which is worse than no chart, because it renders.
        """
        async with session_scope(self._session_factory) as session:
            await session.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id, BacktestRunRow.status.in_(IN_FLIGHT))
                .values(
                    status=STATUS_FAILED,
                    error=error,
                    metrics=None,
                    equity_curve=None,
                    trades=None,
                    finished_at=at,
                )
            )

    async def get(self, run_id: str) -> StoredBacktestRun | None:
        async with session_scope(self._session_factory) as session:
            row = (
                await session.execute(select(BacktestRunRow).where(BacktestRunRow.id == run_id))
            ).scalar_one_or_none()
        return None if row is None else _to_stored(row)

    async def list_runs(
        self, *, strategy_id: str | None = None, limit: int = 50
    ) -> list[StoredBacktestRun]:
        """Runs newest first.

        Ordered by `queued_at` descending with the id as a tie-break, so two runs
        queued in the same microsecond keep a stable order between reads.
        Deliberately **not** by `finished_at`: a queued run has none, and
        ordering by a column half the rows are null in would put the runs
        somebody is currently waiting on in an arbitrary place.
        """
        query = select(BacktestRunRow)
        if strategy_id is not None:
            query = query.where(BacktestRunRow.strategy_id == strategy_id)
        query = query.order_by(BacktestRunRow.queued_at.desc(), BacktestRunRow.id.desc()).limit(
            limit
        )

        async with session_scope(self._session_factory) as session:
            rows = (await session.execute(query)).scalars().all()
        return [_to_stored(row) for row in rows]

    async def stale_running(self, *, older_than: datetime) -> list[str]:
        """Runs marked `running` since before `older_than`.

        Matched on `started_at`, which is exactly what that column is for now
        that it means "a worker claimed this" rather than "this row was written".
        Only `running` rows: a `queued` run with no job behind it is
        indistinguishable from one that is simply waiting, and failing a job
        about to run would be worse than leaving one that never will.
        """
        query = select(BacktestRunRow.id).where(
            BacktestRunRow.status == STATUS_RUNNING,
            BacktestRunRow.started_at.is_not(None),
            BacktestRunRow.started_at < older_than,
        )
        async with session_scope(self._session_factory) as session:
            return list((await session.execute(query)).scalars().all())


def _spec_to_json(spec: BacktestRunSpec) -> dict[str, Any]:
    """The request as it goes into the `config` column.

    Timestamps as ISO-8601 and money as a string, which is what
    `BacktestRunSpec` already holds — the type exists so that this conversion is
    a serialisation rather than a decision (see its docstring).

    **Every field, and that is the invariant rather than a detail.** This wrote
    nine of the spec's fifteen for a while, and the six it dropped were
    `sizing_method`, `sizing_value` and the four `stop_*` fields. Nothing failed
    visibly: the API validated a `risk_pct` request with an ATR stop, wrote a
    row that recorded neither, and the worker — which rebuilds the spec from
    this column and nothing else — ran it as `fixed_qty` with no stop. A run
    that silently ignores the sizing and the protection somebody chose is the
    same class of error as a backtest with no costs, and it looks exactly like a
    correct result.

    So: a field on the spec is a field here. `test_backtest_run_spec.py` asserts
    that against `dataclasses.fields`, which is what makes the next field
    impossible to forget rather than merely unlikely.
    """
    return {
        "strategy_id": spec.strategy_id,
        "symbols": list(spec.symbols),
        "start": spec.start.isoformat(),
        "end": spec.end.isoformat(),
        "timeframe": spec.timeframe,
        "starting_cash": spec.starting_cash,
        "cost_model": spec.cost_model,
        "params": dict(spec.params),
        "qty": spec.qty,
        "sizing_method": spec.sizing_method,
        "sizing_value": spec.sizing_value,
        "stop_type": spec.stop_type,
        "stop_value": spec.stop_value,
        "stop_period": spec.stop_period,
        "stop_bars": spec.stop_bars,
    }


def _spec_from_json(strategy_id: str, config: dict[str, Any]) -> BacktestRunSpec:
    """The `config` column back into a spec.

    `strategy_id` is taken from its own column rather than from the JSON. Both
    hold it and the column is the one with the foreign key on it, so the column
    is the authority; reading the JSON copy would be trusting the half nothing
    constrains.

    Tolerant of a missing optional field and not of a missing window: a run
    whose dates cannot be read is a row this platform cannot describe, and
    inventing a window for it would produce a screen full of confident wrong
    dates.

    **The defaults here are the ones a row written before a field existed
    means.** Every one matches `BacktestRunSpec`'s own default, and each of
    those was chosen so that an older row still reproduces: `fixed_qty` is what
    every run was sized by before `sizing_method` existed, and an empty
    `stop_type` arms only what the strategy itself asks for, which is what the
    engine did unconditionally before stops were configurable.

    One consequence is worth stating plainly, because someone will notice it.
    Rows written while `_spec_to_json` was dropping these fields come back as
    `fixed_qty` with no stop **even if the request that created them asked for
    `risk_pct` and an ATR stop** — the ask was never recorded and cannot be
    recovered. That is the honest reading rather than a lossy one: those runs
    *executed* as `fixed_qty` with no stop, because the worker read the same
    incomplete column. The row reproduces what ran, which is what a run record
    is for.
    """
    return BacktestRunSpec(
        strategy_id=strategy_id,
        symbols=tuple(config.get("symbols") or ()),
        start=datetime.fromisoformat(config["start"]),
        end=datetime.fromisoformat(config["end"]),
        timeframe=str(config.get("timeframe", "1d")),
        starting_cash=str(config.get("starting_cash", "0")),
        cost_model=str(config.get("cost_model", "alpaca_equities")),
        params=dict(config.get("params") or {}),
        qty=str(config.get("qty", "100")),
        sizing_method=str(config.get("sizing_method", "fixed_qty")),
        sizing_value=str(config.get("sizing_value", "")),
        stop_type=str(config.get("stop_type", "")),
        stop_value=str(config.get("stop_value", "")),
        stop_period=int(config.get("stop_period", 14)),
        stop_bars=int(config.get("stop_bars", 0)),
    )


def _to_stored(row: BacktestRunRow) -> StoredBacktestRun:
    return StoredBacktestRun(
        id=row.id,
        spec=_spec_from_json(row.strategy_id, dict(row.config or {})),
        status=row.status,
        error=row.error,
        queued_at=row.queued_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        metrics=dict(row.metrics) if row.metrics is not None else None,
        equity_curve=list(row.equity_curve) if row.equity_curve is not None else None,
        trades=list(row.trades) if row.trades is not None else None,
    )


def new_run(run_id: str, spec: BacktestRunSpec, *, queued_at: datetime) -> StoredBacktestRun:
    """A run as it looks the moment it is asked for.

    Here rather than in the router so that the initial state of a run is defined
    once — the API and any test that needs a queued run build it the same way,
    and `status` cannot start as anything but `queued`.
    """
    return StoredBacktestRun(
        id=run_id,
        spec=spec,
        status=STATUS_QUEUED,
        error=None,
        queued_at=queued_at,
        started_at=None,
        finished_at=None,
    )


def _typecheck(repo: PostgresBacktestRunRepository) -> BacktestRunRepository:
    """Structural conformance, checked by mypy rather than asserted at runtime."""
    return repo
