"""Queued jobs (arq) — work triggered by the API rather than the clock.

Backtests live here because they are long-running: minutes for a multi-year
minute-bar run. Running one inline would block an API worker for the duration.

**These functions are I/O and nothing else.** Read the row, load the bars, hand
the work to `atp_core.backtest.runner`, write the row. Every decision about what
the request means — which cost model, how the result serialises, what counts as
missing history — is in core, where it is tested without a database, a Redis or
an arq worker. A task that grew a judgement would be a judgement only reachable
by standing up three services.

The wiring is on `ctx`, put there once by `queue.on_startup`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from atp_core.backtest.ports import BacktestProgress
from atp_core.backtest.runner import (
    backfill_hint,
    missing_coverage,
    parse_spec_dates,
    result_to_storage,
    run_spec,
)
from atp_core.domain import Timeframe
from atp_core.errors import ATPError
from atp_core.logging import correlation_id, get_logger
from atp_core.risk.limits import DEFAULT_RISK_LIMITS, RiskLimits

if TYPE_CHECKING:
    from atp_core.backtest.engine import BacktestResult, ProgressCallback
    from atp_core.backtest.ports import BacktestRunSpec, StoredBacktestRun
    from atp_core.domain import Bar

log = get_logger(__name__)


async def run_backtest_task(ctx: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Execute a queued backtest and store the result.

    Four outcomes, and the row says which without a reader having to guess:

    - **No such run.** The job names a row that is not there, which means the
      create and the enqueue came apart. Nothing to write to, so this returns
      rather than raising — an arq failure here would be recorded only in arq's
      own result, which is not where anyone is looking.
    - **Not in flight.** The row is already `done` or `failed`, so this is a
      redelivery arriving after its own conclusion (a worker declared dead, its
      run swept, and then it recovered). The sweep's verdict stands.
    - **Failed.** Anything the run raises is written to `backtest_runs.error` in
      words. That is the whole reason this is a task and not a fire-and-forget:
      *a run stuck at "running" forever is the worst outcome for a user*, and the
      second worst is one that says `failed` with no reason.
    - **Done.** Metrics, equity curve and trades, in one transaction.

    The engine runs in a **thread**. It is a synchronous CPU-bound loop that
    would otherwise hold this process's event loop for its whole duration —
    which would stop arq answering its own health check, so an operator watching
    a perfectly healthy four-minute run would see a worker that had stopped
    responding and restart it.
    """
    runs = ctx["runs"]
    clock = ctx["clock"]

    # Every log line from this job carries the run id, including the ones from
    # inside core. Without it a worker running back-to-back jobs produces a log
    # in which no line says which backtest it is about.
    with correlation_id(run_id):
        run: StoredBacktestRun | None = await runs.get(run_id)
        if run is None:
            log.error(
                "backtest.run_missing",
                run_id=run_id,
                msg="queued job names a backtest_runs row that does not exist",
            )
            return {"run_id": run_id, "status": "missing"}

        if not run.is_in_flight:
            log.warning("backtest.run_not_in_flight", run_id=run_id, status=run.status)
            return {"run_id": run_id, "status": run.status}

        await runs.mark_running(run_id, at=clock.now())
        log.info(
            "backtest.running",
            run_id=run_id,
            strategy=run.spec.strategy_id,
            symbols=list(run.spec.symbols),
            timeframe=run.spec.timeframe,
        )

        try:
            result = await _execute(ctx, run.spec, run_id)
        except ATPError as exc:
            # Domain failures: a data gap, a lookahead violation, an unmodelled
            # signal action, a strategy that rejected its params. Every one of
            # these is something a person can read and act on, so the message
            # goes on the row as it is.
            await runs.fail(run_id, at=clock.now(), error=f"{type(exc).__name__}: {exc}")
            log.warning("backtest.failed", run_id=run_id, error=str(exc))
            return {"run_id": run_id, "status": "failed"}
        except Exception as exc:
            # Everything else. Recorded rather than allowed to propagate, for the
            # same reason: arq would mark the *job* failed and the row would stay
            # `running` until a sweep an hour later noticed. Re-raised after the
            # write so the failure still reaches arq's log and its metrics.
            await runs.fail(run_id, at=clock.now(), error=f"unexpected {type(exc).__name__}: {exc}")
            log.exception("backtest.crashed", run_id=run_id)
            raise

        metrics, curve, trades, warnings, totals = result_to_storage(result)
        await runs.finish(
            run_id,
            at=clock.now(),
            metrics=metrics,
            equity_curve=curve,
            trades=trades,
            warnings=warnings,
            totals=totals,
        )
        log.info(
            "backtest.done",
            run_id=run_id,
            bars=len(curve),
            trades=len(trades),
            warnings=len(warnings),
            total_return=str(result.total_return),
        )
        return {"run_id": run_id, "status": "done", "trades": len(trades)}


async def _execute(ctx: dict[str, Any], spec: BacktestRunSpec, run_id: str) -> BacktestResult:
    """Load the bars and run the engine off the event loop.

    Coverage is checked here as well as by the API before queueing, and the
    duplication is deliberate: the API's check is what turns missing history into
    a 400 on the request, and this one is what catches history that was there at
    request time and is not now — a restored database, a symbol whose bars were
    deleted. Neither can stand in for the other.
    """
    bars = await _load_bars(ctx, spec)
    missing = missing_coverage(bars, spec.symbols)
    if missing:
        raise _CoverageGapError(backfill_hint(missing, spec.start))

    return await asyncio.to_thread(
        run_spec,
        spec,
        bars,
        limits=await _limits(ctx),
        on_progress=progress_reporter(ctx, run_id),
    )


async def _limits(ctx: dict[str, Any]) -> RiskLimits:
    """The ceilings this backtest is measured against.

    The saved ones, read now rather than at process start: this worker outlives
    any number of edits, and a run queued this morning should be judged by the
    limits the platform was carrying this morning — not by whatever it started
    with days ago.

    They belong in a backtest at all because a strategy that only looks
    profitable through a ceiling it would breach in production is the result
    this platform most needs not to believe. Nothing saved means the defaults,
    which is what `.env.example` shipped and therefore what an unconfigured
    deployment has always tested against.
    """
    stored = await ctx["worker_config"].load()
    return DEFAULT_RISK_LIMITS if stored is None else stored.config.risk


def progress_reporter(ctx: dict[str, Any], run_id: str) -> ProgressCallback:
    """The engine's progress callback, bridged onto the async queue.

    The engine's loop is synchronous and running in a worker thread, so it cannot
    await. `run_coroutine_threadsafe` posts the write back onto the event loop
    this task is running on and does **not** wait for it: a progress report is a
    nicety and blocking the backtest on a Redis round trip every 500 bars would
    make the run's duration a property of the network.

    Errors are swallowed by `BacktestQueue.report` itself, which is where
    "failed" has a known meaning — the future returned here is deliberately
    dropped rather than checked.
    """
    loop = asyncio.get_running_loop()
    queue = ctx["queue"]

    def report(bars_done: int, bars_total: int) -> None:
        progress = BacktestProgress(
            run_id=run_id,
            bars_done=bars_done,
            bars_total=bars_total,
            at=ctx["clock"].now(),
        )
        asyncio.run_coroutine_threadsafe(queue.report(progress), loop)

    return report


async def _load_bars(ctx: dict[str, Any], spec: BacktestRunSpec) -> dict[str, list[Bar]]:
    """Stored bars for every symbol in the window.

    From the database, never the vendor. A backtest has to be reproducible, and
    re-fetching means today's answer can differ from yesterday's because the
    vendor restated something (docs/BACKTESTING.md) — the same rule the CLI
    follows, for the same reason.

    Sequential rather than gathered. `max_jobs` is 1 so there is no contention to
    win, and a `gather` over a large universe would open one pooled connection
    per symbol to save a few hundred milliseconds on a run measured in minutes.
    """
    start, end = parse_spec_dates(spec)
    timeframe = Timeframe(spec.timeframe)
    repository = ctx["bars"]
    return {
        symbol: await repository.get_bars(symbol, timeframe, start, end) for symbol in spec.symbols
    }


class _CoverageGapError(ATPError):
    """No stored bars for something the request asked about.

    An `ATPError` so the handler above records it as a readable reason rather
    than as a crash — this is the single most likely way a queued run fails, and
    its message is the backfill command that fixes it.
    """


async def backfill_symbol_task(ctx: dict[str, Any], symbol: str, start: str, end: str) -> int:
    """Fetch and store history for one symbol. Returns bars written.

    Still a stub, and now for a reason rather than by omission. Two things
    already do this: `scripts/backfill_bars.py` for an operator filling a range
    on purpose, and `scheduler.backfill_missing_bars` nightly for the gaps a feed
    outage left. A third caller would need a third answer to what happens when
    the vendor rate-limits it while a person is watching a progress bar, and
    nothing asks for one — no screen queues a backfill.

    It stays registered in `WorkerSettings.functions` deliberately: the queue is
    the right home for it when something does ask, and an unregistered name is a
    job that is accepted by Redis and rejected by the worker.
    """
    raise NotImplementedError


async def generate_report_task(
    ctx: dict[str, Any], report_type: str, params: dict[str, Any]
) -> str:
    """Render a report (CSV/PDF); return its storage key.

    Still a stub, and blocked on something other than effort: there is no
    storage. A key is only useful if something can fetch what it names, and this
    platform has no object store and no download endpoint — `/analytics/reports/
    daily` is a stub for its own reasons (roadmap Phase 5, "Daily report") and
    three of the four things that report wants are not gathered anywhere one
    query can reach.
    """
    raise NotImplementedError
