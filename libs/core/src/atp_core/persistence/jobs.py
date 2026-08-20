"""The job queue — `BacktestQueue` over arq and Redis.

Two halves of one port, over one Redis, for two different reasons.

**Enqueue goes through arq**, not through a list this module pushes onto itself.
arq's job envelope — the serialisation, the score, the `job_id` deduplication
key — is arq's business, and the consumer is `arq`'s own worker, which will read
what arq wrote and not what we guessed it writes. A hand-rolled producer against
somebody else's consumer is a private wire format with two implementations that
can disagree on a version bump, and it would disagree silently: the symptom is a
job that is accepted and never runs.

**Progress is a plain Redis key**, because arq has nowhere to put it. A job's
result is written when the job ends; the whole point of progress is that it is
readable while the job is still going. So the task writes a small JSON blob under
a TTL and the API reads it.

This is why `arq` is a dependency of `libs/core` rather than of `apps/worker`
alone. The API enqueues and the worker consumes — two applications, and
`apps/*` may not import each other (CLAUDE.md §2). Core holds the adapter, both
apps hold the port, and neither knows the other exists. Nothing here is imported
by anything in `domain/`, `strategy/`, `risk/` or `backtest/` except the port
they share, so core stays pure where it matters (CLAUDE.md §1.3): this file is an
adapter and lives with the other adapters.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from arq import create_pool
from arq.connections import RedisSettings

from atp_core.backtest.ports import BacktestProgress, BacktestQueueError
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from arq import ArqRedis
    from redis.asyncio import Redis

    from atp_core.backtest.ports import BacktestQueue
    from atp_core.clock import Clock

log = get_logger(__name__)

#: The arq queue the backtest jobs go on, named by us rather than left at arq's
#: default. One name in one place, for the same reason `atp_core.channels` exists:
#: a producer and a consumer that each default to something are two things that
#: agree until one of them is configured.
QUEUE_NAME = "atp:jobs"

#: The registered name of the task that runs a backtest. A string, because the
#: producer must not import the consumer — this is the whole reason the port
#: exists. It matches the function's name in `apps/worker/queue.py`, and
#: `tests/unit/test_backtest_queue.py` asserts that it still does, because a
#: rename on one side is otherwise a job that is enqueued and never picked up.
RUN_BACKTEST_TASK = "run_backtest_task"

#: How long a progress record outlives its last update.
#:
#: Long enough that a slow bar does not make a running job look like it stopped
#: reporting, short enough that a finished run's last progress does not linger
#: behind the result. It is not a heartbeat and must not be read as one — a run
#: whose progress has expired is not thereby dead, which is what the *status* on
#: the row is for.
PROGRESS_TTL_SECONDS = 3600


def _progress_key(run_id: str) -> str:
    return f"atp:backtest:progress:{run_id}"


def _job_id(run_id: str) -> str:
    """The arq job id for a run.

    Derived from the run id rather than random, which is what makes `enqueue`
    idempotent: arq refuses a job whose id is already queued or in progress, so a
    retried request cannot put the same run on the queue twice. Same reasoning as
    `client_order_id` one layer up (CLAUDE.md §1.4) — the duplicate here costs
    minutes of CPU and produces two rows racing to write one result.
    """
    return f"backtest:{run_id}"


class ArqBacktestQueue:
    """`BacktestQueue` over arq, sharing the caller's Redis for progress.

    Takes an already-open `Redis` for the progress half and opens its own arq
    pool lazily for the enqueue half. Two clients against one server, and the
    split is not gratuitous: `arq.create_pool` returns an `ArqRedis` configured
    the way arq needs it, and the application's own client is opened by its
    lifespan and closed by it. Borrowing one for the other would put arq's
    expectations on a connection the rest of the app shares.

    The pool is opened on first use rather than in a constructor, because a
    constructor cannot be awaited and a dependency built per request must not
    open a connection per request. `aclose` releases it.
    """

    def __init__(self, redis: Redis, redis_url: str, clock: Clock) -> None:
        self._redis = redis
        self._redis_url = redis_url
        self._clock = clock
        self._pool: ArqRedis | None = None

    async def _arq(self) -> ArqRedis:
        if self._pool is None:
            try:
                self._pool = await create_pool(
                    RedisSettings.from_dsn(self._redis_url), default_queue_name=QUEUE_NAME
                )
            except Exception as exc:
                raise BacktestQueueError(f"could not reach the job queue: {exc}") from exc
        return self._pool

    async def aclose(self) -> None:
        """Release the arq pool, if one was opened."""
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None

    async def enqueue(self, run_id: str) -> None:
        """Put the run on the queue.

        `enqueue_job` returns None when a job with this id already exists, which
        is not an error and is not silently ignored either: it means a retry
        arrived and the original is still queued, so the caller's run is going to
        be executed exactly once, which is what it asked for. Logged at info
        because it is the idempotency working, and a reader looking at why a
        second request produced no second run needs to find it stated.
        """
        pool = await self._arq()
        try:
            job = await pool.enqueue_job(
                RUN_BACKTEST_TASK, run_id, _job_id=_job_id(run_id), _queue_name=QUEUE_NAME
            )
        except Exception as exc:
            raise BacktestQueueError(f"could not enqueue the backtest: {exc}") from exc

        if job is None:
            log.info("backtest.enqueue_deduplicated", run_id=run_id, job_id=_job_id(run_id))
        else:
            log.info("backtest.enqueued", run_id=run_id, job_id=job.job_id)

    async def report(self, progress: BacktestProgress) -> None:
        """Publish progress. Never raises.

        The one place in this file that swallows an error, and the reason is
        proportion: the run is the work and this is a nicety. A backtest that
        died at 80% because it could not say it was at 80% would be an absurd way
        to lose four minutes of compute — and the caller is the engine's progress
        callback, which is documented as not catching anything, precisely so that
        the swallowing happens here where "failed" has a known meaning.
        """
        payload = json.dumps(
            {
                "run_id": progress.run_id,
                "bars_done": progress.bars_done,
                "bars_total": progress.bars_total,
                "at": progress.at.isoformat(),
            }
        )
        try:
            await self._redis.set(_progress_key(progress.run_id), payload, ex=PROGRESS_TTL_SECONDS)
        except Exception as exc:
            # Debug, not warning. A progress write failing on every bar of a long
            # run would otherwise fill the log with the same line hundreds of
            # times and bury whatever else the run said.
            log.debug("backtest.progress_unreported", run_id=progress.run_id, error=str(exc))

    async def progress(self, run_id: str) -> BacktestProgress | None:
        """The latest published progress, or None if there is none to read.

        A malformed payload reads as None rather than raising. Nothing but this
        module writes this key, so a value that will not parse is a bug here or a
        rolling deploy mid-format-change, and neither is worth turning a
        dashboard poll into a 500 — the status on the row is the answer that
        matters and this only refines it.
        """
        raw = await self._redis.get(_progress_key(run_id))
        if raw is None:
            return None
        try:
            return _parse_progress(raw)
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("backtest.progress_unreadable", run_id=run_id, error=str(exc))
            return None

    def progress_for(self, run_id: str, bars_done: int, bars_total: int) -> BacktestProgress:
        """A progress record stamped with this queue's clock.

        Here rather than at the call site so that nothing constructing one reads
        the wall clock itself (CLAUDE.md §1.2) — including the engine's progress
        callback, which runs inside a simulated-clock backtest and must stamp
        these with *real* time, because they describe how long the machine has
        been working and not what the market was doing.
        """
        return BacktestProgress(
            run_id=run_id, bars_done=bars_done, bars_total=bars_total, at=self._clock.now()
        )


def _parse_progress(raw: bytes | str) -> BacktestProgress:
    data: dict[str, Any] = json.loads(raw)
    return BacktestProgress(
        run_id=str(data["run_id"]),
        bars_done=int(data["bars_done"]),
        bars_total=int(data["bars_total"]),
        at=datetime.fromisoformat(data["at"]),
    )


def _typecheck(queue: ArqBacktestQueue) -> BacktestQueue:
    """Structural conformance, checked by mypy rather than asserted at runtime."""
    return queue
