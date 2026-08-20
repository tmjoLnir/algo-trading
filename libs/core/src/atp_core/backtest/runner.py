"""Turning a stored request into a run, and a run into something storable.

The queue worker's job is I/O: read a row, load bars, write a row. Everything
between those — what the spec means, which cost model that name selects, how a
`BacktestResult` becomes three JSON columns — lives here, where it is testable
without a database, a Redis or an arq worker (CLAUDE.md §1.3).

This is also the second caller of the wiring `scripts/run_backtest.py` had to
itself. That matters more than it looks: a queued run and a CLI run that
assembled their engines separately would drift, and the drift would surface as
a dashboard reporting a different Sharpe from the terminal for the same
parameters — which is the kind of disagreement that makes a whole platform
untrustworthy rather than one screen wrong. ADR 0006's reasoning, third
application after `intended_price` and `atp_core.dashboard`.

**Nothing here reads a clock, a socket or a database.** `run` takes the bars it
is given.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from atp_core.analytics.performance import PerformanceAnalyzer, TradeRecord
from atp_core.backtest.costs import ZeroCostModel, alpaca_equities_default
from atp_core.backtest.engine import BacktestConfig, BacktestEngine, FixedQtySizer
from atp_core.domain import Timeframe
from atp_core.errors import ConfigError
from atp_core.risk.engine import RiskEngine
from atp_core.strategy import registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from atp_core.backtest.costs import CostModel
    from atp_core.backtest.engine import BacktestResult, ProgressCallback
    from atp_core.backtest.ports import BacktestRunSpec
    from atp_core.config import RiskLimits
    from atp_core.domain import Bar

#: Cost models a request may name, and what each builds.
#:
#: A registry rather than an `if` in the router, so the set a request may choose
#: from is the set the runner can build — the API validates a name against these
#: keys and cannot then be handed one it does not understand.
#:
#: `zero` is here and must stay awkward. docs/BACKTESTING.md is unambiguous that
#: a zero-cost result is not evidence about a strategy, and the CLI prints a
#: warning on every run that uses it. The queued path cannot print anything, so
#: the equivalent is `ZERO_COST_WARNING` below, attached to the run's own
#: warnings where whoever reads the result will see it.
COST_MODELS: dict[str, Callable[[], CostModel]] = {
    "alpaca_equities": alpaca_equities_default,
    "zero": ZeroCostModel,
}

#: The default, and it is deliberately not `zero`. A backtest that silently
#: ignored commission and slippage would flatter every strategy, and the default
#: is what most runs will use.
DEFAULT_COST_MODEL = "alpaca_equities"

#: Said on the result rather than at the call site, because a queued run has no
#: terminal to warn at and the person who needs telling is reading a screen
#: hours later.
ZERO_COST_WARNING = (
    "zero-cost model: this result is NOT evidence about this strategy "
    "(docs/BACKTESTING.md 'Ignoring costs')"
)

#: Said on every run, exactly as the CLI says it on every run. Sizing every
#: entry at the same share count ignores volatility, so the return is a property
#: of that number as much as of the strategy.
FIXED_QTY_WARNING = (
    "every entry was sized at {qty} shares. Real sizing is risk-based and "
    "equalises risk per trade (docs/RISK.md 'Position sizing') — treat this "
    "return as a property of that share count"
)

#: And this one, because an engine holding an empty rule chain refuses nothing.
NO_RISK_RULES_WARNING = (
    "no pre-trade risk rules were active — orders were routed through "
    "RiskEngine, but nothing refused them (roadmap Phase 3)"
)


def parse_spec_dates(spec: BacktestRunSpec) -> tuple[datetime, datetime]:
    """The window, rejected here if it is not a window.

    Both must be timezone-aware (CLAUDE.md §1.2). A naive datetime that reached
    the engine would be compared against aware bar timestamps and raise
    somewhere far less legible than this.
    """
    start, end = spec.start, spec.end
    for name, value in (("start", start), ("end", end)):
        if value.tzinfo is None:
            raise ConfigError(f"backtest {name} must be timezone-aware (CLAUDE.md §1.2)")
    if start >= end:
        raise ConfigError(f"backtest start must be before end ({start.date()} >= {end.date()})")
    return start, end


def build_engine(
    spec: BacktestRunSpec,
    *,
    limits: RiskLimits,
    on_progress: ProgressCallback | None = None,
) -> BacktestEngine:
    """Assemble the engine this spec describes.

    Every failure here is the *request's* fault and is raised as `ConfigError`,
    which is what lets the API answer 400 on the ones it can check before
    queueing and the worker record a readable reason on the ones it cannot.

    The risk chain is deliberately empty, and deliberately explicit —
    `RiskEngine(limits, rules=[])` rather than `default_rules()`, which raises
    until Phase 3. An unguarded engine has to be something a caller asked for in
    writing; this is the same line `scripts/run_backtest.py` takes and it carries
    the same warning onto the result.
    """
    start, end = parse_spec_dates(spec)

    try:
        timeframe = Timeframe(spec.timeframe)
    except ValueError:
        supported = ", ".join(t.value for t in Timeframe)
        raise ConfigError(f"timeframe must be one of: {supported}") from None

    if spec.cost_model not in COST_MODELS:
        known = ", ".join(sorted(COST_MODELS))
        raise ConfigError(f"cost_model must be one of: {known}") from None

    if not spec.symbols:
        raise ConfigError("a backtest needs at least one symbol")

    cash = _positive_decimal(spec.starting_cash, "starting_cash")
    qty = _positive_decimal(spec.qty, "qty")

    strategy_cls = registry.get(spec.strategy_id)  # raises StrategyError if unknown
    try:
        strategy = strategy_cls(dict(spec.params))
    except Exception as exc:  # a strategy validates its own params at construction
        raise ConfigError(f"strategy rejected its params: {exc}") from exc

    return BacktestEngine(
        strategy=strategy,
        config=BacktestConfig(
            symbols=list(spec.symbols),
            start=start,
            end=end,
            timeframe=timeframe,
            starting_cash=cash,
        ),
        cost_model=COST_MODELS[spec.cost_model](),
        risk_engine=RiskEngine(limits, rules=[]),
        position_sizer=FixedQtySizer(qty),
        on_progress=on_progress,
    )


def run_spec(
    spec: BacktestRunSpec,
    bars: dict[str, list[Bar]],
    *,
    limits: RiskLimits,
    on_progress: ProgressCallback | None = None,
) -> BacktestResult:
    """Build the engine and run it, with the caveats attached to the result.

    The three warnings ride on the result rather than being logged, because a
    queued run has no terminal and the person who needs them is reading a screen
    hours later — and the result they are most likely to be reading is the one
    that looks too good. `zero` goes first: it is the one that invalidates
    everything below it.
    """
    result = build_engine(spec, limits=limits, on_progress=on_progress).run(bars)

    if spec.cost_model == "zero":
        result.warnings.insert(0, ZERO_COST_WARNING)
    result.warnings.append(FIXED_QTY_WARNING.format(qty=spec.qty))
    result.warnings.append(NO_RISK_RULES_WARNING)
    return result


def result_to_storage(
    result: BacktestResult,
) -> tuple[dict[str, float], list[list[str]], list[dict[str, object]]]:
    """A finished run as the three JSON columns that hold it.

    Returned as one tuple because the three are written in one transaction: a
    `done` run whose metrics landed and whose equity curve did not would be a row
    claiming a result it cannot show.

    **Trades come from the same fold the live analytics use.**
    `PerformanceAnalyzer.build_trades` over the engine's own orders, rather than
    a second reconstruction written here. That is what makes a backtested trade
    and a live trade the same shape — the precondition for comparing them at all
    — and it is why the engine now sets `Order.purpose`: without it every exit in
    this table would read `signal`, including the stop-outs, which is a wrong
    label rather than a missing one.
    """
    metrics = _jsonable_metrics(result.metrics)
    curve = [[ts.isoformat(), str(equity)] for ts, equity in result.equity_curve]
    trades = [_jsonable_trade(trade) for trade in PerformanceAnalyzer().build_trades(result.orders)]
    return metrics, curve, trades


def jsonable(value: object) -> object:
    """Make a value JSON-legal, turning non-finite floats into None.

    Infinity is a legitimate metric value — `profit_factor` is infinite when
    nothing lost money, which means too few trades rather than a perfect strategy
    — and it is not legal JSON. `json.dumps` emits a bare `Infinity` that most
    parsers reject, so a value stored raw here would fail to load in exactly the
    tools meant to read it. None is the honest rendering: the dashboard shows a
    figure it does not have as `—` rather than as a number (docs/DASHBOARD.md).

    Lives here rather than in `scripts/run_backtest.py`, which is where it was
    written and where it is now imported from: the CLI's `--out` file and this
    table are the same serialisation problem, and two copies would be two
    chances for one of them to start emitting `Infinity`.
    """
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return None
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value


def _jsonable_metrics(metrics: dict[str, float]) -> dict[str, float]:
    """The metric set with non-finite values nulled.

    Typed as `dict[str, float]` while capable of holding None, which is the same
    compromise `BacktestResult.metrics` already makes: the column is JSON and a
    null in it is a value this platform has decided means "not available". A
    stricter type here would be honest about the None and would then disagree
    with the engine's own field for no gain.
    """
    cleaned = jsonable(dict(metrics))
    assert isinstance(cleaned, dict)  # a dict in gives a dict out
    return cleaned


def _jsonable_trade(trade: TradeRecord) -> dict[str, object]:
    """One `TradeRecord` as JSON.

    Decimals become strings and datetimes become ISO-8601, which is what every
    other money-carrying payload in this platform does (CLAUDE.md §4): the front
    end formats decimal *strings* and never parses one, so a trade P&L that
    arrived as a JSON float would be the one number on the screen that had been
    through IEEE 754.
    """
    out: dict[str, object] = {}
    for key, value in asdict(trade).items():
        if isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = jsonable(value)
    return out


def _positive_decimal(raw: str, field: str) -> Decimal:
    """A money-shaped string as a `Decimal`, refused if it is not positive.

    `Decimal(str)` rather than `Decimal(float)`: the string is what crossed the
    process boundary precisely so that nothing in this path ever holds the
    binary-float version of it (CLAUDE.md §1.1).
    """
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        raise ConfigError(f"{field} must be a decimal number, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{field} must be positive, got {raw}")
    return value


def suspicious(metrics: dict[str, float]) -> list[str]:
    """Reasons to distrust this result, in the words docs/BACKTESTING.md uses.

    Computed server-side and attached to the run, for the same reason the CLI
    prints them: a number a human has already read is a number they have already
    believed. The two thresholds are the document's own — under ~30 trades the
    statistics mean very little, and a Sharpe above 3 on a simple strategy is a
    bug until proven otherwise.

    Not a refusal. A suspicious result is still the result, and hiding it would
    be worse than labelling it.
    """
    notes: list[str] = []
    trades = metrics.get("num_trades") or 0
    sharpe = metrics.get("sharpe")

    if trades < 30:
        notes.append(
            f"only {int(trades)} trades — under about 30 the statistics above mean "
            "very little (docs/BACKTESTING.md 'Reading the result')"
        )
    if sharpe is not None and sharpe > 3:
        notes.append(
            f"a Sharpe of {sharpe:.2f} on a simple strategy is a bug until proven "
            "otherwise. Check fill timing first, then data alignment"
        )
    return notes


def missing_coverage(bars: dict[str, list[Bar]], symbols: tuple[str, ...]) -> list[str]:
    """Symbols with no stored bars in the requested window.

    Separate from the engine's own `_validate`, which raises `DataGapError` on
    the same condition, and the duplication is the point: this is asked *before*
    a job is queued so the answer is a 400 on the request rather than a failure
    four minutes into a run. The engine keeps its check because it must not
    depend on a caller having performed one.
    """
    return sorted(symbol for symbol in symbols if not bars.get(symbol))


def backfill_hint(missing: list[str], start: datetime) -> str:
    """The exact command that fixes missing history.

    The CLI names it; a queued run has to name it too, or the API's refusal is a
    dead end. Same shape as `scripts/run_backtest.py`'s message, deliberately.
    """
    return (
        f"No stored bars for {', '.join(missing)} in the requested window. "
        f"Backfill first: scripts/backfill_bars.py --symbols {','.join(missing)} "
        f"--start {start.date()}"
    )
