"""The arq worker — a third process, and why it is not the second.

`atp_worker.main` supervises the things that must run continuously: the market
data stream, the strategy loop, the schedule. This runs the things somebody
asked for. They are separate processes on purpose, and the reason is not
tidiness:

**A backtest is CPU-bound and long.** A multi-year minute-bar run is minutes of
solid Python in a synchronous loop. Executed inside the trading worker it would
block that process's event loop for the duration — no ticks consumed, no bars
stored, no staleness monitor running, and `StalenessMonitor` would eventually
halt trading because the feed looked dead. It is not dead; the process is busy
being a calculator. A strategy must not be able to stall HTTP requests
(`docker-compose.yml` on the api/worker split), and a backtest must not be able
to stall the strategy.

**It is also why `run_backtest_task` hands the engine to a thread.** Even alone
in this process, a synchronous multi-minute loop stops arq answering its own
health check, and a worker that cannot say it is alive is one an operator
restarts mid-run.

Run it with:

    python -m atp_worker.queue

and **not** with `arq atp_worker.queue.WorkerSettings`, which is arq's usual
entry point. That form reads `WorkerSettings.__dict__`, so the Redis address
would have to be a class attribute evaluated at *import* time — which means a
module that cannot be imported without configuration, and a `get_settings()`
cached before anything has had a chance to set the environment. Every other
process here reads its settings inside its entry point (`atp_worker.main`,
`atp_api.main.lifespan`), and this one matches them: `run()` below builds the
worker with the address it just read.

`on_startup` opens the pools once for the process rather than per job — a
backtest that opened a connection pool to run would pay for it on every job and
leave it for the server to time out.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from arq.connections import RedisSettings
from arq.worker import create_worker

from atp_core.clock import SystemClock
from atp_core.config import get_settings
from atp_core.logging import configure as configure_logging
from atp_core.logging import get_logger
from atp_core.persistence.backtests import PostgresBacktestRunRepository
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.jobs import QUEUE_NAME, ArqBacktestQueue
from atp_core.persistence.redis_client import close_redis, create_redis

#: `@register` runs at import time, so a process that has never imported a
#: strategy module has an EMPTY registry — and this worker would then fail every
#: single queued run with "unknown strategy", however valid the request was. The
#: API's own validation would have passed it: that process imports the examples
#: for its `/strategies` and `/backtests` endpoints, so a request accepted at the
#: door would die in here, which is the least debuggable shape this failure has.
#:
#: The live worker imports it via `trading.py` and the CLI imports it directly,
#: for exactly this reason. `tests/unit/test_backtest_queue.py` runs a real engine
#: through this task, which is what noticed the omission.
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_worker.tasks import backfill_symbol_task, generate_report_task, run_backtest_task

if TYPE_CHECKING:
    from atp_core.backtest.ports import BacktestRunRepository

log = get_logger(__name__)

#: How long one backtest may take before arq gives up on it.
#:
#: Generous, because the thing it is bounding is a legitimate multi-year
#: minute-bar run and killing one of those at ten minutes would make the queue
#: useless for exactly the runs it exists for. A run that exceeds this is a
#: symbol universe or a range nobody meant to ask for, and the timeout is what
#: stops it holding the only worker slot forever.
JOB_TIMEOUT_SECONDS = 3600

#: One attempt. A backtest is deterministic over stored bars, so a retry spends
#: the same minutes to reach the same failure — and arq's default of five would
#: do it four more times while the queue backs up behind it. The failure is
#: recorded on the row with its reason, which is the outcome a person can act on.
#:
#: The one class of failure a retry *would* fix is a transient database blip
#: while reading the config. That is seconds of work, and losing it costs a
#: person one button press; auto-retrying an hour of compute to save that press
#: is the wrong trade.
MAX_TRIES = 1

#: How many backtests run at once in this process. One, and that is a decision:
#: each saturates a core for minutes, so two concurrent runs on a single-vCPU
#: host (ADR 0011) make both take twice as long and neither report progress
#: smoothly. The queue is what makes serialising them acceptable — the work is
#: not lost, it waits.
MAX_JOBS = 1

#: A run whose `started_at` is older than this, still marked `running`, is
#: assumed to belong to a worker that died. Sweeping is the only thing that ever
#: corrects such a row: the job is gone from Redis, no retry is coming, and a run
#: stuck at `running` forever is the worst outcome this queue can produce.
#:
#: Comfortably longer than `JOB_TIMEOUT_SECONDS`, so a run that is merely slow is
#: never swept out from under itself. A sweep is only ever wrong in one
#: direction, and this is which direction.
STALE_AFTER = timedelta(seconds=JOB_TIMEOUT_SECONDS * 2)

#: What the sweep writes as the reason. Says what happened rather than "failed":
#: the run did not fail, the process running it stopped existing, and those want
#: different responses from a reader.
INTERRUPTED_ERROR = (
    "interrupted — the worker running this backtest stopped before it finished "
    "(restart, deploy, or the process was killed). Nothing is wrong with the "
    "request; queue it again."
)


async def on_startup(ctx: dict[str, Any]) -> None:
    """Open the pools, wire the adapters, and clear the wreckage of a dead run.

    Everything a task needs goes on `ctx` once, so a job is pure work: no task in
    this module builds an engine, a session factory or a Redis client.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    engine = create_engine(settings.database_url)
    redis = create_redis(settings.redis_url)
    clock = SystemClock()
    session_factory = create_session_factory(engine)

    ctx["settings"] = settings
    ctx["clock"] = clock
    ctx["db_engine"] = engine
    ctx["redis"] = redis
    ctx["runs"] = PostgresBacktestRunRepository(session_factory)
    ctx["bars"] = PostgresBarRepository(session_factory)
    ctx["queue"] = ArqBacktestQueue(redis, settings.redis_url, clock)

    log.info("queue.ready", run_mode=settings.run_mode, queue=QUEUE_NAME, max_jobs=MAX_JOBS)

    # After the log line, so a sweep that cannot reach the database does not make
    # the process look like it never started. Not fatal for the same reason: this
    # worker's job is to run backtests, and refusing to accept any because a
    # tidy-up query failed would be the wrong trade.
    try:
        await sweep_interrupted(ctx["runs"], clock.now() - STALE_AFTER, at=clock.now())
    except Exception as exc:
        log.warning("queue.sweep_failed", error=str(exc))


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Release everything `on_startup` opened."""
    queue = ctx.get("queue")
    if isinstance(queue, ArqBacktestQueue):
        await queue.aclose()
    redis = ctx.get("redis")
    if redis is not None:
        await close_redis(redis)
    engine = ctx.get("db_engine")
    if engine is not None:
        await engine.dispose()
    log.info("queue.stopped")


async def sweep_interrupted(
    runs: BacktestRunRepository, older_than: datetime, *, at: datetime
) -> list[str]:
    """Fail every run a dead worker left marked `running`.

    Runs at startup rather than on a schedule, because the event that creates
    these rows *is* a worker stopping — so the next worker starting is exactly
    when there is something to correct and someone to correct it. A periodic
    sweep would find the same rows later and no sooner.

    Returns the ids it failed, so a test can assert on them and the log can count
    them.
    """
    stale = await runs.stale_running(older_than=older_than)
    for run_id in stale:
        await runs.fail(run_id, at=at, error=INTERRUPTED_ERROR)
    if stale:
        log.warning("queue.swept_interrupted_runs", count=len(stale), run_ids=stale)
    return stale


class WorkerSettings:
    """What this worker runs and how it is bounded.

    A plain class of attributes, read by `create_worker` in `run()` below.
    `redis_settings` is deliberately absent — it is passed at startup, because
    reading configuration at import is the thing this module does not do.

    `functions` is the whitelist: a job naming anything not on it is rejected by
    arq rather than executed, which is what makes the queue's name-based dispatch
    safe. `atp_core.persistence.jobs` enqueues by string precisely so the
    producer need not import this module, and this list is the other half of that
    contract.

    `keep_result` is short. The result a person reads is the `backtest_runs` row,
    which is durable; arq's own result record is only how a CLI watcher learns a
    job finished, and keeping the same answer twice with one copy able to expire
    is how the two come to disagree.
    """

    functions: ClassVar[list[Any]] = [
        run_backtest_task,
        backfill_symbol_task,
        generate_report_task,
    ]
    queue_name = QUEUE_NAME
    on_startup = on_startup
    on_shutdown = on_shutdown
    max_jobs = MAX_JOBS
    job_timeout = JOB_TIMEOUT_SECONDS
    max_tries = MAX_TRIES
    keep_result = 300
    health_check_interval = 30
    #: A failed job is not retried, so this must be False as well: `retry_jobs`
    #: is what re-queues a job whose worker died mid-execution, and re-running an
    #: hour of deterministic compute on a redelivery is the same wrong trade
    #: `MAX_TRIES` refuses. The row is swept and says why instead.
    retry_jobs = False


def run() -> None:
    """Process entry point.

    Settings are read here, not at import, so this module can be imported by a
    test without a `.env` and without poisoning the `get_settings()` cache.
    `Worker.run` installs its own signal handlers and finishes the job in flight
    before exiting, which is why the container sends SIGTERM and waits.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    worker = create_worker(
        WorkerSettings,  # type: ignore[arg-type]
        redis_settings=RedisSettings.from_dsn(settings.redis_url),
    )
    worker.run()


if __name__ == "__main__":
    run()
