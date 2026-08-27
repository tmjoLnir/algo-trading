"""The seam between asking for a backtest and running one.

Two ports, because a queued backtest has two pieces of state living in two
places for two different reasons.

**The run record** (`BacktestRunRepository`) is durable and belongs in Postgres.
A finished backtest is evidence: it is the thing a promotion to paper is
justified by, and the operand `/analytics/live-vs-backtest` compares a live
strategy against. Evidence that vanishes when Redis is flushed is not evidence.

**The queue and the progress** (`BacktestQueue`) are in flight and belong in
Redis. A job nobody has picked up yet and a run that is 40% through its timeline
are both statements about *right now*; neither survives being answered a day
late, and neither is worth a row.

The split has a consequence worth stating before someone treats it as a bug: the
two can disagree. A worker killed mid-run leaves a row saying `running` with no
job behind it, and nothing in Redis will ever contradict it — Redis holds no
record of a job that stopped existing. That is what `stale_running` exists for
and why the queue worker sweeps at startup: the durable half has to be corrected
by something that knows the ephemeral half is gone, because a run stuck at
`running` forever is the worst outcome a user of this can have.

Why ports at all, rather than a Redis client and a session in the router: the
API enqueues, the worker consumes, and neither may import the other
(`apps/*` → `libs/core`, never sideways). Core stays free of the sockets
(CLAUDE.md §1.3), and a test drives the whole POST path against a fake queue
with no Redis to connect to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


#: The states a run moves through, and the only four it can be in.
#:
#: `queued` → `running` → `done` | `failed`. There is no `cancelled`: nothing can
#: cancel a run, because arq cannot interrupt a job that is already executing and
#: an endpoint that reported a cancellation the worker went on ignoring would be
#: worse than no endpoint.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: The statuses that mean this run is still going to change. Read by the screen
#: to decide whether to keep polling, and by the startup sweep to decide which
#: rows a dead worker may have orphaned.
IN_FLIGHT = (STATUS_QUEUED, STATUS_RUNNING)


class BacktestQueueError(Exception):
    """The queue could not be reached, so the job was not accepted.

    Deliberately not an `ATPError` subclass in the way a domain failure is: this
    is infrastructure. It exists so the enqueue path can tell "Redis is down"
    apart from "your request is invalid", which are a 503 and a 400 and must not
    both surface as one.
    """


@dataclass(frozen=True, slots=True)
class BacktestRunSpec:
    """What was asked for, as stored on the run.

    Deliberately primitives — strings and stringified decimals — rather than
    `BacktestConfig`, which holds a `Timeframe` enum and `Decimal`s. This is what
    goes into a JSON column and comes back out of one, and it crosses a process
    boundary on the way: the API writes it, the queue worker reads it, and JSON
    is what both agree on. `atp_core.backtest.runner` is where it becomes a real
    `BacktestConfig` again, in exactly one place.

    `starting_cash` is a **string** for the same reason every monetary value in
    this platform crosses a boundary as one: JSON has one number type and it is a
    float. A starting cash of 100000.00000000001 would then propagate into every
    figure the run reports (CLAUDE.md §1.1).
    """

    strategy_id: str
    symbols: tuple[str, ...]
    start: datetime
    end: datetime
    timeframe: str
    starting_cash: str
    cost_model: str
    #: Strategy parameters, as the strategy's own `params_schema` describes them.
    params: dict[str, object] = field(default_factory=dict)
    #: The declarative rules this run executed, **snapshotted at queue time**.
    #: None for a coded strategy, where `strategy_id` names a registered class
    #: and this column cannot describe the logic.
    #:
    #: A copy rather than a reference, and that is the whole reason it is here.
    #: A rule set is editable in the UI — that is what it is *for* — so a run
    #: that recorded only `strategy_id` would report different numbers the next
    #: time it was replayed, silently, on the day somebody adjusted a threshold.
    #: `strategy_id` still carries the foreign key and answers "which strategy
    #: is this a run of"; this answers "what rules actually ran", and those stop
    #: being the same question the first time a rule set is edited.
    ruleset: dict[str, object] | None = None
    #: Shares per entry under `fixed_qty` sizing, and the value every run stored
    #: before `sizing_method` existed was sized by. Kept rather than folded into
    #: `sizing_value` so those rows still deserialize and still reproduce: a
    #: spec with neither new field is a `fixed_qty` run of `qty` shares, which is
    #: exactly what it was when it was written.
    qty: str = "100"
    #: How a quantity is decided — one of `risk.rules.position_size`'s methods.
    #: Defaults to `fixed_qty` for the back-compatibility above, *not* because it
    #: is the right way to size: docs/RISK.md is unambiguous that real sizing is
    #: risk-based, and a fixed share count makes the reported return a property
    #: of that number as much as of the strategy.
    sizing_method: str = "fixed_qty"
    #: What `sizing_method` reads, and its meaning follows the method — a share
    #: count for `fixed_qty`, an amount for `fixed_notional`, a fraction of
    #: equity for the rest. Empty means "use `qty`", which is what makes an old
    #: spec and a new `fixed_qty` one the same run.
    sizing_value: str = ""
    #: How every entry is protected, by `StopType` name. **Empty means the run
    #: arms only what its strategy asks for**, which is what this engine did
    #: unconditionally before stops were configurable — so a spec stored without
    #: it still reproduces exactly.
    #:
    #: Not a default of `atr`, even though that is `WORKER_STOP_TYPE`'s default
    #: and docs/RISK.md's recommendation. Changing the protection on a stored run
    #: would change its result, and a spec is a record of what was asked for.
    stop_type: str = ""
    #: What the stop type reads: a multiple for `atr` and `chandelier`, a
    #: fraction or an amount for the fixed and trailing kinds. One field for both
    #: because `apps/worker` uses one setting for both, and for the reason it
    #: gives — two would let a caller fill in the one their type ignores.
    stop_value: str = ""
    #: Lookback for the ATR the `atr` and `chandelier` types need.
    stop_period: int = 14
    #: How many bars a `time` stop holds for. Zero is "not configured", which
    #: that stop type refuses rather than defaults.
    stop_bars: int = 0


def spec_to_json(spec: BacktestRunSpec) -> dict[str, Any]:
    """The request as JSON — **every field, and that is the invariant rather
    than a detail.**

    Timestamps as ISO-8601 and money as a string, which is what
    `BacktestRunSpec` already holds: the type exists so that this conversion is
    a serialisation rather than a decision (see its docstring).

    Two callers, and the second is why this lives beside the dataclass rather
    than in the persistence adapter it was written in. `persistence.backtests`
    puts it in the `config` column; `scripts/run_backtest.py` puts it in the
    `--out` file. Those are one problem — recording what a run was a run *of* —
    and two copies would be two chances for one of them to start dropping a
    field. Same reasoning as `backtest.runner.jsonable`, hoisted out of that
    same script for that same reason.

    The every-field rule is not caution. This wrote nine of the spec's fifteen
    for a while, and the six it dropped were `sizing_method`, `sizing_value` and
    the four `stop_*` fields. Nothing failed visibly: the API validated a
    `risk_pct` request with an ATR stop, wrote a row that recorded neither, and
    the worker — which rebuilds the spec from that column and nothing else — ran
    it as `fixed_qty` with no stop. A run that silently ignores the sizing and
    the protection somebody chose is the same class of error as a backtest with
    no costs, and it looks exactly like a correct result.

    So: a field on the spec is a field here. `test_backtest_run_spec.py` asserts
    that against `dataclasses.fields`, which is what makes the next field
    impossible to forget rather than merely unlikely.

    Values are written **raw rather than resolved** — an empty `sizing_value`
    stays empty rather than becoming `qty`. The empty string is itself a fact
    about the run ("sized by `qty`", per that field's own docstring) and it
    round-trips; resolving it here would make the record disagree with what was
    asked for. The API's `BacktestSpecView` resolves for display, which is a
    different job with a different reader.
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
        "ruleset": dict(spec.ruleset) if spec.ruleset is not None else None,
        "qty": spec.qty,
        "sizing_method": spec.sizing_method,
        "sizing_value": spec.sizing_value,
        "stop_type": spec.stop_type,
        "stop_value": spec.stop_value,
        "stop_period": spec.stop_period,
        "stop_bars": spec.stop_bars,
    }


@dataclass(frozen=True, slots=True)
class StoredBacktestRun:
    """One `backtest_runs` row, as a reader should see it.

    Three timestamps, and the reason there are three rather than one is the
    whole of what a queue does to a request. `queued_at` is when somebody asked.
    `started_at` is when a worker picked it up, and is **None** while the job is
    still waiting — a queued run has not started, and stamping it at enqueue
    time would make every run's duration include however long the queue was
    backed up. `finished_at` is when it stopped, either way.

    `metrics`, `equity_curve` and `trades` are None until the run finishes, and
    stay None on a failure. A partial result is not a result: a strategy
    evaluated over two of its five years has a Sharpe, and it is not the Sharpe
    of anything anybody asked about.
    """

    id: str
    spec: BacktestRunSpec
    status: str
    error: str | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    #: The full metric set from `backtest.metrics.compute_all`, as JSON numbers.
    #: Floats, not decimals, and that is correct — these are statistics over a
    #: return series, not balances (CLAUDE.md §1.1). Infinity is a legitimate
    #: value for `profit_factor` and is stored as None, because it is not legal
    #: JSON; see `atp_core.backtest.runner`.
    metrics: dict[str, float] | None = None
    #: `[[iso timestamp, decimal string], ...]`. Money as strings, so the curve
    #: that reaches a chart is the one the engine computed.
    equity_curve: list[list[str]] | None = None
    #: The reconstructed round trips, one dict per trade. Same fold as the live
    #: analytics path (`PerformanceAnalyzer.build_trades`), so a backtested
    #: trade and a live one are the same shape and can be put side by side.
    trades: list[dict[str, object]] | None = None
    #: What the run itself said about its own result — refusals, coverage
    #: shortfalls, the caveats `run_spec` attaches. **None on a run stored
    #: before this column existed**, which is not the same as `[]`: the older
    #: row never recorded them, and reading its emptiness as "nothing was wrong"
    #: is the claim this whole field exists to stop being made silently.
    warnings: list[str] | None = None
    #: What the run made and what it did: `BacktestResult.totals()`, verbatim.
    #: Money as decimal strings, counts as integers — deliberately *not* in
    #: `metrics`, which is float by contract (ADR 0019). **None on a run stored
    #: before this column existed**, and it means it: those runs computed these
    #: figures and discarded them, so there is nothing to backfill from.
    totals: dict[str, object] | None = None

    @property
    def is_in_flight(self) -> bool:
        """True while this run is still going to change."""
        return self.status in IN_FLIGHT


@dataclass(frozen=True, slots=True)
class BacktestProgress:
    """How far a running job has got.

    Bars rather than a percentage, and both rather than one: a percentage alone
    cannot distinguish a run that is genuinely slow from one whose range turned
    out to hold forty bars. The screen shows a bar from `fraction` and the counts
    beside it.

    Ephemeral by construction — it lives in Redis under a TTL and is gone once
    the run finishes. A finished run's progress is its result.
    """

    run_id: str
    bars_done: int
    bars_total: int
    at: datetime

    @property
    def fraction(self) -> float:
        """0.0 → 1.0. Zero when the total is not known yet rather than
        undefined: a run whose bars are still loading has made no progress, and
        that is a truthful thing to render."""
        if self.bars_total <= 0:
            return 0.0
        return min(1.0, self.bars_done / self.bars_total)


class BacktestRunRepository(Protocol):
    """The `backtest_runs` table — the durable record of what was asked and got.

    Written by two processes, which is unusual in this platform and is deliberate
    here. The API writes exactly one thing, `create`, because the request is the
    fact it is authoritative about; the queue worker writes every subsequent
    transition, because it is the only thing that knows them. That is not the
    two-writer problem ADR 0007 refuses — these are disjoint sets of columns at
    disjoint times, not two processes computing the same number.
    """

    async def create(self, run: StoredBacktestRun) -> None:
        """Record a queued run.

        Called **before** the job is enqueued, not after, and the ordering is the
        point: a row with no job is a run that shows up as queued and never
        progresses, which is visible and recoverable. A job with no row is a
        worker that wakes up, cannot find what it was asked to do, and has
        nowhere to write the failure.

        Raises on a duplicate id. Nothing retries a create — the id is minted
        per request — so a conflict means two requests generated one id, which is
        a bug worth hearing about rather than an upsert to absorb.
        """
        ...

    async def mark_running(self, run_id: str, *, at: datetime) -> None:
        """Claim the run and stamp `started_at`.

        Called by the job that is about to execute it. Idempotent on a run
        already running: arq can deliver a job twice if a worker dies between
        picking it up and acknowledging it, and the correct response to the
        second delivery is to carry on, not to refuse.
        """
        ...

    async def finish(
        self,
        run_id: str,
        *,
        at: datetime,
        metrics: dict[str, float],
        equity_curve: list[list[str]],
        trades: list[dict[str, object]],
        warnings: list[str],
        totals: dict[str, object],
    ) -> None:
        """Store a completed run's results and mark it done.

        One call rather than a setter per field, because a run whose metrics
        landed and whose equity curve did not is a `done` run that lies about
        having a result. They arrive in one transaction or not at all.
        """
        ...

    async def fail(self, run_id: str, *, at: datetime, error: str) -> None:
        """Mark a run failed, with the reason on the row.

        The reason is stored rather than logged because the person who needs it
        is looking at a screen, not at the worker's stdout. A failed run with an
        empty `error` is indistinguishable from a bug in this platform, so
        callers must pass something a human can read.
        """
        ...

    async def get(self, run_id: str) -> StoredBacktestRun | None:
        """One run, or None if no such id."""
        ...

    async def list_runs(
        self, *, strategy_id: str | None = None, limit: int = 50
    ) -> list[StoredBacktestRun]:
        """Runs newest first, optionally for one strategy.

        Newest-first and bounded, like `OrderRepository.recent_orders` and for
        the same reason: this is a display. Nothing is matched against anything,
        so dropping the oldest rows loses rows off the bottom of a screen rather
        than changing a number.

        Ordered by `queued_at` descending with the id as a tie-break, so two runs
        queued in the same microsecond do not swap places between reads.
        """
        ...

    async def stale_running(self, *, older_than: datetime) -> list[str]:
        """Ids of runs marked `running` since before `older_than`.

        The one query here that exists for a failure rather than for a screen. A
        worker killed mid-run leaves a row saying `running` that nothing will
        ever revisit — the job is gone from Redis and no retry is coming — and a
        run stuck at `running` forever is the outcome the queue exists to avoid.
        The queue worker sweeps these at startup and fails them with a reason
        that says what happened.

        `queued` rows are deliberately **not** returned. A queued run with no job
        is possible (a crash between the create and the enqueue) but is
        indistinguishable from one that is simply waiting, and failing a job that
        is about to run would be worse than leaving one that never will.
        """
        ...


class BacktestQueue(Protocol):
    """Handing a run to whatever will execute it, and asking how far it got.

    One implementation over arq and Redis (`persistence/jobs.py`). The port is
    what keeps `apps/api` from importing `apps/worker` to reach a task function,
    and what lets the whole enqueue path be tested without a Redis.
    """

    async def enqueue(self, run_id: str) -> None:
        """Submit the run for execution.

        Raises `BacktestQueueError` if the queue cannot be reached. Deliberately
        not swallowed: a request that answered 202 while the job went nowhere
        would leave a row queued forever, and the caller's only honest reply is
        that the platform could not accept the work.

        Idempotent on `run_id` — the job id is derived from it — so a retried
        request cannot put the same run on the queue twice. That is the same
        reasoning as `client_order_id` (CLAUDE.md §1.4), one layer up: the
        expensive duplicate here is four minutes of CPU and two contradictory
        result rows rather than a duplicate position.
        """
        ...

    async def report(self, progress: BacktestProgress) -> None:
        """Publish how far a run has got. Best-effort.

        Never raises for a store that is unreachable, and that is the one place
        in this file where swallowing an error is right: progress is a nicety,
        the run is the work, and a backtest that died at 80% because it could not
        say it was at 80% would be an absurd way to lose four minutes of compute.
        """
        ...

    async def progress(self, run_id: str) -> BacktestProgress | None:
        """The latest published progress, or None.

        None is ordinary and means several different things — the job has not
        started, it finished (progress expires), or it never reported. All three
        read the same to a caller, which is why the *status* on the row is what
        the screen branches on and this only ever refines it.
        """
        ...
