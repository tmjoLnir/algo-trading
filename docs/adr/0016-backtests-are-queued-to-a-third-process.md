# 16. Backtests are queued to a third process

**Status:** Accepted · 2026-08-20

## Context

Requirement #2 asks for backtesting, and `docs/BACKTESTING.md` has documented
`POST /api/v1/backtests` as "not wired yet" since Phase 2. The engine has existed
and been trusted since #25; what was missing was a way to ask for a run from
anywhere but a terminal, and a place to keep the answer.

Three facts constrain any design here, and each rules something out.

**A backtest is minutes of synchronous, CPU-bound Python.** `BacktestEngine.run`
walks a merged timeline bar by bar; a multi-year minute-bar run over a handful of
symbols is hundreds of thousands of iterations with indicator maths on each. That
is not I/O-bound work that `await` helps with. It is a calculator.

**The API is the wrong place to run one.** An HTTP request that holds a uvicorn
worker for four minutes is a worker that serves nothing else, and the dashboard
polls `/dashboard/live` every five minutes from every open tab. The skeleton's
own docstring said so before any of this was built.

**The trading worker is also the wrong place.** This is the less obvious half.
`apps/worker` already supervises long-running responsibilities, so a queue there
looks like a natural fit — and it would be actively dangerous. That process owns
the market-data stream, the strategy loop and `StalenessMonitor`, and a four-minute
synchronous loop inside it blocks the event loop for the duration: no ticks
consumed, no bars stored, and the staleness watchdog eventually engaging
`DATA_FEED_LOST` because the feed looks dead. It is not dead. The process is busy
being a calculator, and the incident is a halt caused by somebody clicking a
button on a research screen.

There is also the question of where a *result* lives. `backtest_runs` has existed
in the schema since the initial migration with no reader and no writer, which is
why three other things are blocked: `/analytics/live-vs-backtest` has only one
operand, the promotion ratchet cannot require "a completed backtest on record"
(docs/SAFETY.md), and `docs/BACKTESTING.md`'s pre-belief checklist asks for
individual trades to be inspected with nothing able to show them.

## Decision

**A third process consumes an arq queue.** `apps/worker/queue.py`, run as
`python -m atp_worker.queue`, in its own container (`queue` in
`docker-compose.yml`) built from the same image as the trading worker. The API
records the request and enqueues; the queue worker executes and writes the result
back.

The pieces, and the reasoning that is not obvious from the code:

**The row is written before the job is enqueued.** A row with no job is a run
that shows up as `queued` and never progresses — visible, and re-queueable. A job
with no row is a worker that wakes up, cannot find what it was asked to do, and
has nowhere to write the failure. If the enqueue then fails, the API marks the row
`failed` and answers 503, because a run that says `queued` when nothing accepted
it is the one state a reader cannot act on.

**Durable state is in Postgres; in-flight state is in Redis.** The run record is
evidence — it is what a promotion is justified by — and evidence that vanishes
when Redis is flushed is not evidence. Progress is a statement about *right now*
and is worth no row: it lives under a TTL and is read only for a run whose status
says it is still going.

**A queued run has not started.** `backtest_runs.started_at` was `NOT NULL`, so
the only value the API could have written was the current time — making every
run's reported duration include however long the queue was backed up. Migration
`d7a1c9f4b208` adds `queued_at` and makes `started_at` nullable, so the row now
carries three timestamps that mean three different things.

**One attempt, and a startup sweep for the case that has no attempt left.** A
backtest is deterministic over stored bars, so a retry spends the same minutes to
reach the same failure — arq's default of five would do it four more times while
the queue backs up. `max_tries = 1` and `retry_jobs = False`. The failure that
this leaves uncovered is a worker killed mid-run: the job is gone from Redis, no
retry is coming, and the row says `running` forever, which the stub's own
docstring named as the worst outcome for a user. So the queue worker sweeps rows
left `running` beyond twice the job timeout at startup and fails them with a
reason that says *interrupted* rather than *failed* — the run did not fail, the
process running it stopped existing, and those want different responses.

**One job at a time.** `max_jobs = 1`. Each run saturates a core for minutes, and
ADR 0011 deploys this on one small VM; two concurrent runs make both take twice
as long and neither report progress smoothly. The queue is what makes serialising
them acceptable — the work is not lost, it waits.

**The engine runs in a thread**, via `asyncio.to_thread`. Even alone in this
process, a synchronous multi-minute loop stops arq answering its own health
check, and a worker that cannot say it is alive is one an operator restarts
mid-run.

**arq is a dependency of `libs/core`, not of `apps/worker` alone.** The API
enqueues and the worker consumes — two applications, and `apps/*` may not import
each other (CLAUDE.md §2). The adapter therefore lives in
`atp_core/persistence/jobs.py` with the other adapters, behind a `BacktestQueue`
port, and neither app knows the other exists. Using arq's own client rather than
writing to Redis directly is the same call ADR 0006 makes elsewhere: the consumer
is arq's worker, which reads what arq wrote, and a hand-rolled producer against
somebody else's consumer is a private wire format with two implementations that
can disagree silently on a version bump. The symptom would be a job that is
accepted and never runs.

**The job is named by string.** `RUN_BACKTEST_TASK = "run_backtest_task"` in
core, matched against `WorkerSettings.functions` by a unit test, because a rename
on either side is otherwise a job Redis accepts and the worker rejects.

**`GET /backtests/compare`, not `POST`.** The skeleton specified a POST and this
deviates, for ADR 0009's reason: `require_write_scope` decides from the method, so
as a POST the comparison would be refused with 403 to exactly the reader it is for
— somebody watching the book who wants to know which of two backtests did better.
That ADR's argument is that authorisation is about the *act*, and this handler
performs none. The alternative was a second entry in `READ_ONLY_MAY_CALL`, whose
one existing entry is there for a domain rule about halting; widening it to
accommodate a verb choice would be weakening a guardrail.

## Consequences

- **`/analytics/live-vs-backtest` has a second operand.** It stays a stub — it is
  its own roadmap item with its own semantics — but the reason it was blocked is
  gone. So is the reason the promotion ratchet could not ask for a completed
  backtest.
- **A new container.** `make up` starts five services rather than four, and
  `docker-compose.prod.yml` gains the same three corrections `worker` has.
- **The engine now sets `Order.purpose`.** Found by building this: the engine
  never set it, so every order it produced defaulted to `"entry"` — and
  `PerformanceAnalyzer.build_trades`, which the queue reuses so a backtested trade
  and a live one are the same shape, labelled every exit as an exit "by signal",
  stop-outs and targets included. That is a *wrong* label rather than a missing
  one, on the field that decides whether a strategy's stops are misplaced. It is a
  live-vs-backtest divergence in the same family as the take-profit one recorded
  against `StrategyRunner` in #58.
- **The engine gained an optional progress callback.** A callback rather than
  anything the engine does itself, because reporting means writing somewhere and
  core writes nowhere (CLAUDE.md §1.3). It is called every 500 bars; the CLI
  passes none.
- **The CLI and the queue now assemble their engines through one function.**
  `atp_core.backtest.runner.build_engine`. Two call sites wiring their own would
  drift, and the symptom would be the dashboard reporting a different Sharpe from
  the terminal for the same parameters — a disagreement that makes a platform
  untrustworthy rather than a screen wrong.
- **A queued run cannot be cancelled.** arq cannot interrupt a job already
  executing, and an endpoint that reported a cancellation the worker went on
  ignoring would be worse than no endpoint. `max_jobs = 1` and a bounded job
  timeout are what keep a mistaken run from holding the queue indefinitely.
- **The coverage check is duplicated**, in the API before queueing and in the
  worker before running. Neither can stand in for the other: the first turns
  missing history into a 400 naming the exact `backfill_bars.py` command, and the
  second catches history that was there at request time and is not now.

## Alternatives considered

**Run it inline in the request.** Simplest, and it is what the stub's own
docstring rules out. A four-minute request holds a uvicorn worker, and the client
has to hold a connection open across it. There is no version of this that works
for a minute-bar run.

**A background task in the API process** (`BackgroundTasks`, or an asyncio task).
Avoids a new container and keeps the request short. Rejected: the work still runs
inside the API's event loop, so it blocks the process serving the dashboard — and
a result in flight is lost entirely on any deploy, with nothing to sweep it,
because there is no queue that remembers the request.

**The existing trading worker.** No new container, and it already supervises
long-running work. Rejected on the ground stated in Context, which is the whole
reason there is a third process: it would let a research screen halt trading.

**Celery, RQ, or a database-polling queue of our own.** arq was already declared
in `apps/worker`'s dependencies and is asyncio-native over the Redis this stack
already runs, which matters because the surrounding code is `async` and the
adapters are shared with the API. Celery brings a synchronous worker model and a
much larger operational surface for one job type. A hand-rolled poller over
Postgres is tempting — it would need no new dependency and would put the queue in
the same transaction as the row — and is the alternative worth revisiting if the
queue ever needs to survive a Redis flush. It was refused here because it means
writing and owning visibility timeouts, redelivery and worker liveness, which is
exactly the code arq already has tested.

**Publish an intent on Redis pub/sub**, as the API does for other worker
interactions. Rejected on `atp_core.channels`'s own terms: pub/sub has no
persistence and no delivery guarantee, so a request sent while the queue worker
was restarting would be lost with the caller having been told 202.

**Store the result in Redis and let the client poll for it.** Would need no
migration. Rejected: a backtest result is evidence with a long life — it is the
thing `/analytics/live-vs-backtest` compares against months later, and the thing
a promotion cites — and a result whose lifetime is a cache eviction policy cannot
serve that.

**Store trades by recomputing them on read**, as ADR 0015 does for live trades.
Not available: that reconstruction folds the *orders table*, and a backtest's
orders exist only inside the process that produced them. So the trades are
stored, in the same transaction as the metrics and the curve.
