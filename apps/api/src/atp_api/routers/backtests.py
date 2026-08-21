"""Backtest endpoints — requirement #2.

Backtests are queued to the worker, not run inline: a multi-year minute-bar run
takes minutes and would block an API worker for the duration. The queue is arq
over the same Redis everything else here uses (ADR 0016), and the process that
consumes it is a third container — `queue` in `docker-compose.yml` — for the
reason stated there: a backtest must not be able to stall the process managing
open positions.

**This is the first endpoint in the platform that starts work.** Everything else
either reads something already computed or halts trading. It is still not a write
in the sense rule §1.5 governs: nothing here reaches a venue, and the broker the
engine fills against is simulated and lives inside the run. What it does place is
a *job*, and the three things that makes newly possible are worth naming, because
each was blocked on this file existing:

- `/analytics/live-vs-backtest` had one operand. Now there can be a stored
  backtest to compare a live strategy against.
- The promotion ratchet could not ask for "a completed backtest on record"
  (docs/SAFETY.md), because nothing could put one on record.
- docs/BACKTESTING.md's checklist asks whether individual trades were inspected.
  `/{run_id}/trades` is where that happens.

**Everything that can be judged from the request is judged before the job is
queued**, including whether the history exists. A run that fails four minutes in
because a symbol has no bars is a worse answer than a 400, and it is the single
most likely way a queued backtest fails.
"""

from __future__ import annotations

#: Imported at runtime, not behind `if TYPE_CHECKING`: FastAPI resolves a
#: handler's annotations when it wires the graph, and a name that exists only for
#: the type checker raises `NameError` on the first request.
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel, Field

from atp_api.deps import get_backtest_queue, get_backtest_repository, get_bar_repository, get_clock
from atp_core.backtest.ports import (
    STATUS_DONE,
    BacktestQueue,
    BacktestQueueError,
    BacktestRunSpec,
    StoredBacktestRun,
)
from atp_core.backtest.ports import BacktestRunRepository as RunRepository
from atp_core.backtest.runner import (
    COST_MODELS,
    DEFAULT_COST_MODEL,
    SIZING_METHODS,
    STOP_TYPES,
    backfill_hint,
    missing_coverage,
    resolve_stop_config,
    suspicious,
)
from atp_core.clock import Clock
from atp_core.data.ports import BarRepository
from atp_core.domain import Timeframe
from atp_core.errors import ATPError
from atp_core.logging import get_logger
from atp_core.persistence.backtests import new_run
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_core.strategy import registry

log = get_logger(__name__)

router = APIRouter(prefix="/backtests", tags=["backtests"])

#: Hard ceiling on one page of runs. A display, like `orders.MAX_LIMIT`.
MAX_LIMIT = 200

#: How many runs `POST /compare` will put side by side.
#:
#: Low on purpose, and the number is an argument rather than a capacity limit.
#: docs/BACKTESTING.md is explicit that comparing many variants and picking the
#: best is how overfitting happens — the winner of a 200-way sweep is the
#: luckiest parameter set. An endpoint that cheerfully ranked fifty runs would be
#: tooling for the mistake. Eight is enough to compare a handful of deliberate
#: variants and too few to sweep with.
MAX_COMPARE = 8

#: Symbols in one request. A backtest over a hundred names is a research job, and
#: this platform's queue runs one job at a time on one host (ADR 0011).
MAX_SYMBOLS = 25


class BacktestRequest(BaseModel):
    """What a caller may ask for.

    `cost_model` has no zero default and that is deliberate — docs/BACKTESTING.md
    is unambiguous that a zero-cost result is not evidence about a strategy, and
    a default that flattered every run would make the honest choice the one you
    had to remember.
    """

    strategy_id: str
    symbols: list[str]
    start: datetime
    end: datetime
    timeframe: str = "1d"
    starting_cash: Decimal = Decimal("100000")
    cost_model: str = DEFAULT_COST_MODEL  # never default to zero-cost
    #: Strategy parameters, validated by the strategy's own constructor before
    #: the job is queued — so a bad `fast`/`slow` pair is a 400 now rather than a
    #: failed run later.
    params: dict[str, Any] = Field(default_factory=dict)
    #: Shares per entry under the default `fixed_qty` sizing. Stored on the run,
    #: because a result whose share count nobody recorded cannot be compared
    #: with anything.
    qty: Decimal = Decimal("100")
    #: How a quantity is decided. `fixed_qty` remains the default so a request
    #: that names neither field is the run it has always been — not because it
    #: is the right way to size, which docs/RISK.md says is risk-based.
    sizing_method: str = "fixed_qty"
    #: What `sizing_method` reads; its meaning follows the method. Omitted means
    #: `qty`, which is what keeps an old request and a new `fixed_qty` one the
    #: same run.
    sizing_value: Decimal | None = None
    #: How every entry is protected, by `StopType` name. Omitted arms only what
    #: the strategy itself emits — which for a crossover is nothing, and which is
    #: what every run stored before this field did.
    stop_type: str = ""
    #: A multiple for `atr`/`chandelier`, a fraction or an amount for the rest.
    stop_value: Decimal | None = None
    #: ATR lookback, for the two types that need one.
    stop_period: int = 14
    #: Bars to hold, for a `time` stop. Refused rather than defaulted there.
    stop_bars: int = 0


class BacktestSpecView(BaseModel):
    """The request, echoed back on every run.

    On the run rather than only in the request's own response, because a result
    read a week later has to say what it was a result *of*. Every monetary value
    is a string, as everywhere else (docs/DASHBOARD.md).
    """

    strategy_id: str
    symbols: list[str]
    start: datetime
    end: datetime
    timeframe: str
    starting_cash: str
    cost_model: str
    params: dict[str, Any] = Field(default_factory=dict)
    qty: str
    #: Echoed back for the reason the rest of the spec is: a divergence between
    #: two runs of one strategy is usually a difference in how they were sized,
    #: and a reader comparing them cannot see that unless it travels with the
    #: result.
    sizing_method: str
    sizing_value: str
    #: Echoed for the reason the sizing fields are: two runs of one strategy that
    #: differ only in how their entries were protected are different results, and
    #: a reader comparing them cannot see that unless it travels with them.
    stop_type: str
    stop_value: str
    stop_period: int
    stop_bars: int


class BacktestProgressView(BaseModel):
    """How far a running job has got. Absent unless it is running and reported."""

    bars_done: int
    bars_total: int
    #: 0.0 → 1.0. Computed server-side because every client would compute it
    #: identically, and because a total of zero makes it a division rather than
    #: a fraction.
    fraction: float
    at: datetime


class BacktestOut(BaseModel):
    """One run.

    Three timestamps rather than one, and the reason is what a queue does to a
    request: `queued_at` is when it was asked for, `started_at` is null until a
    worker picked it up, `finished_at` is when it stopped either way. A single
    `started_at` — which is all this table used to have — would have made every
    run's duration include its queue wait.

    `metrics` is a bag of JSON floats and is **not** money. These are statistics
    over a return series (a Sharpe, a drawdown fraction, an expectancy in
    account currency), and the dashboard renders them through `src/lib/stats.ts`
    rather than the decimal formatter, which would claim a precision the numbers
    do not carry. A null value inside it is a metric that was infinite or
    undefined — `profit_factor` with no losing trade — and means "not available",
    never zero.
    """

    id: str
    strategy_id: str
    status: str
    spec: BacktestSpecView
    metrics: dict[str, float | None] | None = None
    error: str | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Only ever set while the run is in flight. A finished run's progress is its
    #: result, and the record expires.
    progress: BacktestProgressView | None = None
    #: Reasons to distrust this result, in docs/BACKTESTING.md's own words —
    #: too few trades, an implausible Sharpe. Server-side, because a number a
    #: human has already read is a number they have already believed.
    warnings: list[str] = Field(default_factory=list)


class BacktestListResponse(BaseModel):
    runs: list[BacktestOut]
    #: True when the page came back full. Stated rather than inferred: a list
    #: that stops at exactly the limit looks identical to one that ended.
    limit_reached: bool


class BacktestTradesResponse(BaseModel):
    """Every simulated trade in one run.

    The rows are the same shape as `/analytics/trades` because they come from the
    same fold — `PerformanceAnalyzer.build_trades`, over the engine's own orders.
    That is what makes a backtested trade and a live one comparable, and it is
    why the engine sets `Order.purpose`: without it every exit here would read
    `signal`, stop-outs included.
    """

    run_id: str
    trades: list[dict[str, Any]]


class BacktestEquityCurveResponse(BaseModel):
    """The equity curve as `[iso timestamp, decimal string]` pairs.

    Money as strings, so the curve a chart receives is the one the engine
    computed. `toChartNumber` is the single place the front end is allowed to
    make one a float, for geometry (docs/DASHBOARD.md).
    """

    run_id: str
    points: list[list[str]]


class BacktestComparisonResponse(BaseModel):
    """Metrics side by side, with the reason not to trust the winner."""

    runs: list[BacktestOut]
    #: metric name → run id → value. Transposed here rather than in the client
    #: because a comparison table is read by row, and every client would
    #: otherwise pivot the same list identically.
    metrics: dict[str, dict[str, float | None]]
    #: Said on every comparison, not just large ones. See `MAX_COMPARE`.
    overfitting_warning: str


#: The sentence above. A constant so the same words reach the API's schema, the
#: response and the screen.
OVERFITTING_WARNING = (
    "Comparing variants and picking the best is how overfitting happens: the "
    "winner of a sweep is usually the luckiest parameter set, not the best one. "
    "Disclose how many variants you tried (docs/BACKTESTING.md 'Overfitting')."
)


def _to_spec_view(spec: BacktestRunSpec) -> BacktestSpecView:
    return BacktestSpecView(
        strategy_id=spec.strategy_id,
        symbols=list(spec.symbols),
        start=spec.start,
        end=spec.end,
        timeframe=spec.timeframe,
        starting_cash=spec.starting_cash,
        cost_model=spec.cost_model,
        params=dict(spec.params),
        qty=spec.qty,
        sizing_method=spec.sizing_method or "fixed_qty",
        # Resolved rather than echoed raw: an old run stores an empty
        # `sizing_value` and was sized by `qty`, and serving the empty string
        # would show a reader a field the run did not actually use.
        sizing_value=spec.sizing_value or spec.qty,
        stop_type=spec.stop_type,
        stop_value=spec.stop_value,
        stop_period=spec.stop_period,
        stop_bars=spec.stop_bars,
    )


def _to_view(run: StoredBacktestRun, progress: BacktestProgressView | None = None) -> BacktestOut:
    return BacktestOut(
        id=run.id,
        strategy_id=run.spec.strategy_id,
        status=run.status,
        spec=_to_spec_view(run.spec),
        metrics=dict(run.metrics) if run.metrics is not None else None,
        error=run.error,
        queued_at=run.queued_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        progress=progress,
        # Only for a finished run. A queued one has no metrics to be suspicious
        # of, and a failed one has an `error`, which is the more useful sentence.
        warnings=suspicious(run.metrics) if run.status == STATUS_DONE and run.metrics else [],
    )


async def _progress_view(
    queue: BacktestQueue, run: StoredBacktestRun
) -> BacktestProgressView | None:
    """The in-flight progress for a run, if there is any to read.

    Asked only for a run that is actually in flight, so a list of a hundred
    finished runs costs no Redis round trips at all. A finished run's progress
    record has expired anyway, and reading it would answer None slowly.
    """
    if not run.is_in_flight:
        return None
    progress = await queue.progress(run.id)
    if progress is None:
        return None
    return BacktestProgressView(
        bars_done=progress.bars_done,
        bars_total=progress.bars_total,
        fraction=progress.fraction,
        at=progress.at,
    )


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(run_id: str) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_404_NOT_FOUND, detail=f"no backtest run {run_id!r}"
    )


def _validated_spec(payload: BacktestRequest) -> BacktestRunSpec:
    """The request as a spec, or a 400 explaining what is wrong with it.

    Everything checkable without touching the database is checked here. The
    strategy is *constructed* rather than merely looked up, because a strategy
    validates its params in its constructor — so an impossible parameter pair is
    a 400 on the request rather than a run that fails immediately and needs a
    human to read a stack trace to find out why.
    """
    symbols = tuple(dict.fromkeys(s.strip().upper() for s in payload.symbols if s.strip()))
    if not symbols:
        raise _bad_request("symbols is empty")
    if len(symbols) > MAX_SYMBOLS:
        raise _bad_request(f"at most {MAX_SYMBOLS} symbols per run, got {len(symbols)}")

    if payload.start.tzinfo is None or payload.end.tzinfo is None:
        raise _bad_request("start and end must be timezone-aware (CLAUDE.md §1.2)")
    if payload.start >= payload.end:
        raise _bad_request(
            f"start must be before end ({payload.start.date()} >= {payload.end.date()})"
        )

    try:
        timeframe = Timeframe(payload.timeframe)
    except ValueError:
        supported = ", ".join(t.value for t in Timeframe)
        raise _bad_request(f"timeframe must be one of: {supported}") from None

    if payload.cost_model not in COST_MODELS:
        raise _bad_request(f"cost_model must be one of: {', '.join(sorted(COST_MODELS))}")
    if payload.sizing_method not in SIZING_METHODS:
        raise _bad_request(f"sizing_method must be one of: {', '.join(sorted(SIZING_METHODS))}")
    if payload.sizing_value is not None and payload.sizing_value <= 0:
        raise _bad_request(f"sizing_value must be positive, got {payload.sizing_value}")
    if payload.stop_type and payload.stop_type not in STOP_TYPES:
        raise _bad_request(f"stop_type must be one of: {', '.join(sorted(STOP_TYPES))}")
    if payload.stop_value is not None and payload.stop_value <= 0:
        raise _bad_request(f"stop_value must be positive, got {payload.stop_value}")
    if payload.starting_cash <= 0:
        raise _bad_request(f"starting_cash must be positive, got {payload.starting_cash}")
    if payload.qty <= 0:
        raise _bad_request(f"qty must be positive, got {payload.qty}")

    try:
        strategy_cls = registry.get(payload.strategy_id)
    except ATPError as exc:
        # Names the registered strategies, which is what makes a typo
        # self-correcting. The registry is populated by importing
        # `strategy.examples` at the top of this module — without that line this
        # would refuse every strategy with total confidence.
        raise _bad_request(str(exc)) from None

    try:
        strategy_cls(dict(payload.params))
    except ATPError as exc:
        raise _bad_request(f"strategy rejected its params: {exc}") from None
    except (TypeError, ValueError) as exc:
        raise _bad_request(f"strategy rejected its params: {exc}") from None

    spec = BacktestRunSpec(
        strategy_id=payload.strategy_id,
        symbols=symbols,
        start=payload.start,
        end=payload.end,
        timeframe=timeframe.value,
        # Str, not float. The `Decimal` pydantic parsed is exact; `str` keeps it
        # that way across the JSON column and into the worker (CLAUDE.md §1.1).
        starting_cash=str(payload.starting_cash),
        cost_model=payload.cost_model,
        params=dict(payload.params),
        qty=str(payload.qty),
        sizing_method=payload.sizing_method,
        # Str, not float, for the reason `starting_cash` is one: this crosses a
        # JSON column and a process boundary, and a fraction of equity is
        # exactly the kind of value a binary float rounds visibly.
        sizing_value="" if payload.sizing_value is None else str(payload.sizing_value),
        stop_type=payload.stop_type,
        stop_value="" if payload.stop_value is None else str(payload.stop_value),
        stop_period=payload.stop_period,
        stop_bars=payload.stop_bars,
    )

    # The cross-field stop rules — an `atr` with no multiple, a `time` with no
    # bar count — live in the resolver rather than being restated here, so the
    # set this accepts is the set the worker can build. Called for its refusal
    # and its result discarded: `build_engine` calls it again in the worker,
    # which is where the config is actually needed.
    try:
        resolve_stop_config(spec)
    except ATPError as exc:
        raise _bad_request(str(exc)) from None
    return spec


async def _require_coverage(bars: BarRepository, spec: BacktestRunSpec) -> None:
    """Refuse the request if the history it needs is not stored.

    **The most valuable check in this file**, and the reason the stub's docstring
    asked for it: without it the failure arrives minutes later, from a different
    process, as a row that says `failed`. Here it is a 400 naming the exact
    `backfill_bars.py` command that fixes it, which is what the CLI does and what
    makes the refusal actionable rather than a dead end.

    It reads the bars it is checking for. That is the same query the worker will
    run — duplicated work on the happy path, in exchange for the only honest
    answer to "does this history exist": a `count` would need a second query
    shape to maintain and would still not prove the series is non-empty for
    *every* symbol, which is the condition the engine actually raises on.

    Not a guarantee. History present now can be gone by the time the job runs, so
    the worker checks again — neither check can stand in for the other.
    """
    timeframe = Timeframe(spec.timeframe)
    loaded = {
        symbol: await bars.get_bars(symbol, timeframe, spec.start, spec.end)
        for symbol in spec.symbols
    }
    missing = missing_coverage(loaded, spec.symbols)
    if missing:
        raise _bad_request(backfill_hint(missing, spec.start))


@router.post("", response_model=BacktestOut, status_code=202)
async def run_backtest(
    payload: BacktestRequest,
    runs: Annotated[RunRepository, Depends(get_backtest_repository)],
    queue: Annotated[BacktestQueue, Depends(get_backtest_queue)],
    bars: Annotated[BarRepository, Depends(get_bar_repository)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> BacktestOut:
    """Queue a run. Returns immediately with status `queued`.

    Validate data coverage BEFORE queueing — telling the user up front that
    history is missing beats a job that fails four minutes in.

    **The row is written before the job is enqueued**, and the order is the whole
    of the failure design. A row with no job is a run that shows up as queued and
    never progresses: visible, and re-queueable. A job with no row is a worker
    that wakes up, cannot find what it was asked to do, and has nowhere to write
    the failure. So if the enqueue fails, this answers 503 and the row is left
    marked failed rather than sitting queued forever behind a job that does not
    exist.

    202, not 201. Nothing has been created that the caller asked for — the result
    does not exist yet — and the `Location`-shaped answer is the run id in the
    body, which the client polls.
    """
    spec = _validated_spec(payload)
    await _require_coverage(bars, spec)

    now = clock.now()
    run = new_run(str(uuid.uuid4()), spec, queued_at=now)

    try:
        await runs.create(run)
    except Exception as exc:
        # A foreign key refusing an unregistered strategy lands here: the
        # registry knows the class and `strategies` has no row for it because no
        # worker has ever loaded it. That is a real and confusing state, and it
        # is exactly what the strategies page exists to show — so the message
        # says which half is missing rather than reporting a constraint name.
        log.warning("backtest.create_failed", strategy=spec.strategy_id, error=str(exc))
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"could not record a run for {spec.strategy_id!r}. A backtest needs a "
                "row in `strategies`, which a worker writes the first time it loads "
                "the strategy — see the Strategies tab for what has and has not run."
            ),
        ) from exc

    try:
        await queue.enqueue(run.id)
    except BacktestQueueError as exc:
        # The row exists and nothing is going to run it, so it is failed here
        # rather than left queued. A run that says `queued` when no queue
        # accepted it is the one state a reader cannot act on.
        await runs.fail(run.id, at=clock.now(), error=f"could not be queued: {exc}")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"the job queue is not reachable, so this run was not started: {exc}",
        ) from exc

    log.info(
        "backtest.queued",
        run_id=run.id,
        strategy=spec.strategy_id,
        symbols=list(spec.symbols),
        timeframe=spec.timeframe,
    )
    return _to_view(run)


@router.get("", response_model=BacktestListResponse)
async def list_backtests(
    runs: Annotated[RunRepository, Depends(get_backtest_repository)],
    queue: Annotated[BacktestQueue, Depends(get_backtest_queue)],
    strategy_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
) -> BacktestListResponse:
    """Runs newest first, optionally for one strategy.

    Progress is attached only to the runs that are in flight, so a page of
    finished runs costs no Redis round trips.
    """
    stored = await runs.list_runs(strategy_id=strategy_id, limit=limit)
    views = [_to_view(run, await _progress_view(queue, run)) for run in stored]
    return BacktestListResponse(runs=views, limit_reached=len(stored) >= limit)


#: Declared BEFORE `/{run_id}`, and that is load-bearing rather than tidy.
#: FastAPI matches routes in registration order, so with this second a request
#: for `/backtests/compare` would be handled by `get_backtest` with
#: `run_id="compare"` and answer 404 — a working endpoint made unreachable by
#: the order of two decorators. `tests/unit/test_backtests_api.py` pins it.
@router.get("/compare", response_model=BacktestComparisonResponse)
async def compare_backtests(
    runs: Annotated[RunRepository, Depends(get_backtest_repository)],
    run_ids: Annotated[list[str], Query()],
) -> BacktestComparisonResponse:
    """Metrics side by side.

    Beware: comparing many variants and picking the best is how overfitting
    happens. The winner of a 200-way parameter sweep is usually the luckiest
    parameter set, not the best one. See docs/BACKTESTING.md.

    That warning is in the response as well as in this docstring, because the
    docstring is read by whoever writes the client and the response is read by
    whoever is about to promote a strategy. `MAX_COMPARE` is the same argument
    expressed as a limit: this endpoint compares a handful of deliberate variants
    and is deliberately useless for sweeping.

    Only finished runs are compared. A queued or failed run has no metrics, and
    including it as a column of nulls would put a run that produced no answer
    beside runs that did, in a table read to pick a winner.

    **A GET, where the skeleton specified `POST /compare`**, and the reason is
    ADR 0009 rather than taste. `require_write_scope` decides from the method, so
    as a POST this would be refused with 403 to exactly the reader it is for —
    somebody watching the book who wants to know which of two backtests did
    better. That ADR's whole argument is that authorisation is about the *act*,
    and this handler performs none: it reads rows and pivots them. The
    alternative was a third entry in `deps.READ_ONLY_MAY_CALL`, whose one
    existing entry is there for a domain rule about halting — widening it for a
    read expressed with the wrong verb would be weakening a guardrail to
    accommodate a method choice.

    `run_ids` is therefore a repeated query parameter: `?run_ids=a&run_ids=b`.
    `MAX_COMPARE` keeps that comfortably short.
    """
    wanted = list(dict.fromkeys(run_ids))
    if not wanted:
        raise _bad_request("run_ids is empty")
    if len(wanted) > MAX_COMPARE:
        raise _bad_request(
            f"at most {MAX_COMPARE} runs at a time, got {len(wanted)}. Comparing many "
            "variants and picking the best is how overfitting happens "
            "(docs/BACKTESTING.md)"
        )

    found: list[StoredBacktestRun] = []
    for run_id in wanted:
        run = await runs.get(run_id)
        if run is None:
            raise _not_found(run_id)
        found.append(run)

    unfinished = [run.id for run in found if run.status != STATUS_DONE or not run.metrics]
    if unfinished:
        raise _bad_request(
            f"these runs have no metrics to compare: {', '.join(unfinished)}. "
            "Only a completed run has a result."
        )

    # Every metric any run reports, so a run missing one shows a gap rather than
    # silently dropping the row. Sorted, because a dict of metrics has no
    # meaningful order and a table whose rows moved between requests is unusable.
    names = sorted({name for run in found if run.metrics for name in run.metrics})
    return BacktestComparisonResponse(
        runs=[_to_view(run) for run in found],
        metrics={name: {run.id: (run.metrics or {}).get(name) for run in found} for name in names},
        overfitting_warning=OVERFITTING_WARNING,
    )


@router.get("/{run_id}", response_model=BacktestOut)
async def get_backtest(
    run_id: str,
    runs: Annotated[RunRepository, Depends(get_backtest_repository)],
    queue: Annotated[BacktestQueue, Depends(get_backtest_queue)],
) -> BacktestOut:
    """One run, with its live progress if it is still going.

    Deliberately does **not** carry the equity curve or the trades. Those are
    their own endpoints because they are large — a five-year daily curve is 1,250
    points and a minute run is hundreds of thousands — and this is the response a
    client polls every few seconds while a run is in flight.
    """
    run = await runs.get(run_id)
    if run is None:
        raise _not_found(run_id)
    return _to_view(run, await _progress_view(queue, run))


@router.get("/{run_id}/trades", response_model=BacktestTradesResponse)
async def get_backtest_trades(
    run_id: str,
    runs: Annotated[RunRepository, Depends(get_backtest_repository)],
) -> BacktestTradesResponse:
    """Every simulated trade. Inspecting individual trades is how you catch a
    backtest that is "profitable" because of one impossible fill.

    A run with no trades stored answers with an empty list rather than a 404, and
    the distinction the *status* carries is what tells them apart: a `done` run
    with no trades took none, which is a result, and a `queued` one has not
    produced any yet. Both are honest empties; a 404 would say the run does not
    exist.
    """
    run = await runs.get(run_id)
    if run is None:
        raise _not_found(run_id)
    return BacktestTradesResponse(run_id=run_id, trades=list(run.trades or []))


@router.get("/{run_id}/equity-curve", response_model=BacktestEquityCurveResponse)
async def get_backtest_equity_curve(
    run_id: str,
    runs: Annotated[RunRepository, Depends(get_backtest_repository)],
) -> BacktestEquityCurveResponse:
    """The run's equity curve, as timestamp/decimal-string pairs."""
    run = await runs.get(run_id)
    if run is None:
        raise _not_found(run_id)
    return BacktestEquityCurveResponse(run_id=run_id, points=list(run.equity_curve or []))
