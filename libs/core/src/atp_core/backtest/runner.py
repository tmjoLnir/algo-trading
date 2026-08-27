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
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from pydantic import ValidationError

from atp_core.analytics.performance import PerformanceAnalyzer, TradeRecord
from atp_core.backtest.costs import ZeroCostModel, alpaca_equities_default
from atp_core.backtest.engine import BacktestConfig, BacktestEngine, RiskBasedSizer
from atp_core.domain import OrderStatus, Timeframe
from atp_core.domain.enums import StopType
from atp_core.errors import ATPError, ConfigError
from atp_core.risk.engine import RiskEngine, backtest_rules
from atp_core.risk.stops import StopConfig
from atp_core.strategy import RuleSet, compile_ruleset, registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from atp_core.backtest.costs import CostModel
    from atp_core.backtest.engine import BacktestResult, ProgressCallback
    from atp_core.backtest.ports import BacktestRunSpec
    from atp_core.config import RiskLimits
    from atp_core.domain import Bar
    from atp_core.strategy.base import Strategy

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

#: Said on every run that uses it, exactly as the CLI says it. Sizing every
#: entry at the same share count ignores volatility, so the return is a property
#: of that number as much as of the strategy. No longer said on *every* run —
#: it is now a statement about a choice rather than about the platform.
FIXED_QTY_WARNING = (
    "every entry was sized at {qty} shares. Real sizing is risk-based and "
    "equalises risk per trade (docs/RISK.md 'Position sizing') — treat this "
    "return as a property of that share count"
)

#: Attached only to a run that deliberately asked for no rules. It used to be
#: attached to all of them, because the engine was always built with an empty
#: chain; `backtest_rules()` is what changed that.
NO_RISK_RULES_WARNING = (
    "no pre-trade risk rules were active — orders were routed through "
    "RiskEngine, but nothing refused them"
)

#: The methods a request may name, and it is `position_size`'s own list rather
#: than a second copy: a method this accepted and that one did not would be a
#: 400 the API could not have foreseen, raised from inside the queue worker.
SIZING_METHODS: frozenset[str] = frozenset(
    {"fixed_qty", "fixed_notional", "equity_pct", "risk_pct", "volatility_target"}
)


def refusal_summary(result: BacktestResult) -> str | None:
    """What this run was refused, by which rule — or None if nothing was.

    **The most important line a result can carry now that the chain is real.**
    A run whose entries were mostly refused reports a return computed from the
    few that got through, and that number is not a statement about the strategy
    at all — it is a statement about a book the limits would not let it hold.
    The refusals were already in `result.orders`, individually, one warning per
    order; nobody reads three hundred warnings, and a reader who skims them
    cannot tell twenty refusals from two.

    Counted per rule for the reason `RiskEngine.validate` counts its metric that
    way: "risk denied 40 orders" is a curiosity, and "the position cap denied
    40" is a sizing bug with an address.
    """
    refused = [order for order in result.orders if order.status is OrderStatus.REJECTED_RISK]
    if not refused:
        return None
    by_rule = Counter(order.rejected_by or "unknown" for order in refused)
    breakdown = ", ".join(f"{rule} ({count})" for rule, count in by_rule.most_common())
    return (
        f"{len(refused)} of {len(result.orders)} orders were refused before reaching "
        f"the market — {breakdown}. The return below is what the orders that "
        f"survived produced, not what the strategy asked for"
    )


#: Stop types a request may name — `StopType`'s own members, so a name this
#: accepted and `StopManager` did not would be a failure raised from inside the
#: queue worker rather than a 400 at the door.
STOP_TYPES: frozenset[str] = frozenset(t.value for t in StopType)

#: The two types whose `value` is a *multiple* of ATR rather than a fraction or
#: an amount. `apps/worker._stop_config` makes the same split from one setting,
#: for the reason it states: separate fields would let a caller fill in the one
#: their configured type ignores.
_MULTIPLIER_TYPES: frozenset[StopType] = frozenset({StopType.ATR, StopType.CHANDELIER})


def resolve_stop_config(spec: BacktestRunSpec) -> StopConfig | None:
    """The protection this spec asks for, or None to arm only what signals carry.

    None is the honest answer for an unconfigured spec rather than a default
    `atr`: every run stored before these fields existed has no stop type, and
    giving it one would change what those runs report. A spec records what was
    asked for.

    **`broker_side` is False, and that is not a preference.** Live, a broker-side
    stop rests at the venue and fires without us — which is why `StrategyRunner`
    skips its own check for one. In a replay there is no venue: the engine *is*
    the fill model, and a config claiming the level was resting somewhere else
    would describe a protection nothing in the run provides.
    """
    if not spec.stop_type:
        return None
    if spec.stop_type not in STOP_TYPES:
        known = ", ".join(sorted(STOP_TYPES))
        raise ConfigError(f"stop_type must be one of: {known}")

    stop_type = StopType(spec.stop_type)
    if stop_type is StopType.TIME:
        if spec.stop_bars <= 0:
            raise ConfigError("a time stop needs a positive stop_bars")
        return StopConfig(stop_type=stop_type, bars=spec.stop_bars, broker_side=False)

    if not spec.stop_value:
        raise ConfigError(f"a {spec.stop_type} stop needs stop_value")
    value = _positive_decimal(spec.stop_value, "stop_value")
    if spec.stop_period <= 0:
        raise ConfigError(f"stop_period must be positive, got {spec.stop_period}")

    multiplied = stop_type in _MULTIPLIER_TYPES
    return StopConfig(
        stop_type=stop_type,
        value=None if multiplied else value,
        multiplier=value if multiplied else None,
        period=spec.stop_period,
        broker_side=False,
    )


def resolve_sizing(spec: BacktestRunSpec) -> tuple[str, Decimal]:
    """The sizing method this spec asks for, and the value it reads.

    `sizing_value` falls back to `qty`, which is what makes a run stored before
    either field existed reproduce exactly: no method named is `fixed_qty`, and
    no value named is the share count that spec already carried.

    Raises `ConfigError` rather than letting `position_size` raise `ValueError`
    deep inside the run, so a request naming a method that does not exist is a
    400 at the door instead of a job that fails four minutes later.
    """
    method = spec.sizing_method or "fixed_qty"
    if method not in SIZING_METHODS:
        known = ", ".join(sorted(SIZING_METHODS))
        raise ConfigError(f"sizing_method must be one of: {known}")
    raw = spec.sizing_value or spec.qty
    return method, _positive_decimal(raw, "sizing_value")


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


def _resolve_strategy(spec: BacktestRunSpec) -> Strategy:
    """The spec's strategy: a compiled rule set, or a registered class.

    **The rule set wins when the spec carries one**, and it is deliberately not
    a fallback for a name the registry does not know. A run that recorded rules
    executed those rules; reaching for the registry as well would let a coded
    strategy sharing the id decide what a stored run meant, which is the kind of
    ambiguity `registry.register` refuses duplicate names to prevent.

    `compile_ruleset` does not register what it builds, so the two paths cannot
    collide — and this staying the single place a spec becomes a strategy is
    what keeps a queued run and the CLI from reporting different numbers for the
    same parameters.
    """
    if spec.ruleset is not None:
        try:
            return compile_ruleset(RuleSet.model_validate(spec.ruleset))
        except ATPError as exc:
            # `InvalidRuleError` is already an `ATPError`; this names where the
            # spec came from, because by the time a worker reads it the rule set
            # is a JSON blob in a column rather than something anyone is editing.
            raise ConfigError(f"the run's stored rule set does not compile: {exc}") from exc
        except ValidationError as exc:
            raise ConfigError(f"the run's stored rule set is malformed: {exc}") from exc

    strategy_cls = registry.get(spec.strategy_id)  # raises StrategyError if unknown
    try:
        return strategy_cls(dict(spec.params))
    except Exception as exc:  # a strategy validates its own params at construction
        raise ConfigError(f"strategy rejected its params: {exc}") from exc


def build_engine(
    spec: BacktestRunSpec,
    *,
    limits: RiskLimits,
    on_progress: ProgressCallback | None = None,
    with_rules: bool = True,
) -> BacktestEngine:
    """Assemble the engine this spec describes.

    Every failure here is the *request's* fault and is raised as `ConfigError`,
    which is what lets the API answer 400 on the ones it can check before
    queueing and the worker record a readable reason on the ones it cannot.

    **The chain is `backtest_rules()`** — the five of the nine that a replay over
    bars can actually evaluate, with the four it cannot named and justified
    there rather than passed as stubs that always approve. Until now this built
    `RiskEngine(limits, rules=[])`, which refused nothing, and every result
    carried a warning saying so. A backtest with no risk chain reports returns
    from positions no live account would have been allowed to hold, which is the
    same class of flattery as running one with no costs.

    `rules=[]` is still reachable, and still deliberate: `with_rules=False` is
    how a caller asks for an engine that refuses nothing, and the warning goes
    back on the result when they do.
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
    method, value = resolve_sizing(spec)

    strategy = _resolve_strategy(spec)

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
        risk_engine=RiskEngine(limits, rules=backtest_rules() if with_rules else []),
        # `FixedQtySizer` is not reached even for `fixed_qty`: `position_size`
        # handles that method too, and routing every method through one function
        # is what stops a backtest and the live router disagreeing about a
        # quantity. The class stays for the engine's own mechanics tests.
        position_sizer=RiskBasedSizer(method, value),
        on_progress=on_progress,
        stop_config=resolve_stop_config(spec),
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
    method, value = resolve_sizing(spec)
    result = build_engine(spec, limits=limits, on_progress=on_progress).run(bars)

    if spec.cost_model == "zero":
        result.warnings.insert(0, ZERO_COST_WARNING)
    # Only when it is true. This used to be unconditional, because the engine
    # was always built with a flat share count; saying it on a `risk_pct` run
    # would now be the result warning about something that did not happen.
    if method == "fixed_qty":
        result.warnings.append(FIXED_QTY_WARNING.format(qty=value))
    # Last, and deliberately: it is the line that says how much of the run
    # above actually happened, so it should be the one still on screen when a
    # reader stops scrolling.
    if (refusals := refusal_summary(result)) is not None:
        result.warnings.append(refusals)
    return result


def result_to_storage(
    result: BacktestResult,
) -> tuple[
    dict[str, float], list[list[str]], list[dict[str, object]], list[str], dict[str, object]
]:
    """A finished run as the five JSON columns that hold it.

    Returned as one tuple because the five are written in one transaction: a
    `done` run whose metrics landed and whose equity curve did not would be a row
    claiming a result it cannot show.

    **`totals` is the money and the counts**, straight from
    `BacktestResult.totals()` rather than reassembled here. It is separate from
    `metrics` because `metrics` is float by contract and five of these are
    money, which must not be (CLAUDE.md §1.1) — and it is stored rather than
    derived because nothing downstream can recover `realized_pnl` from a metric
    set. A run that ends holding everything and one that banked the same return
    have identical metrics and opposite meanings (ADR 0019).

    **`warnings` is the run's own account of itself**, and it is here rather
    than recomputed on read because most of it is not a function of the metrics.
    `suspicious` can see that a result has too few trades; it cannot see that
    every order was refused before reaching the market, that four symbols had no
    history until eighteen months in, or that the costs were switched off — and
    an all-zero metric set looks the same whether a strategy was refused
    everything or never had an idea.

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
    return metrics, curve, trades, list(result.warnings), result.totals()


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


def all_warnings(stored: list[str] | None, metrics: dict[str, float] | None) -> list[str]:
    """Everything a finished run has to say about itself, in reading order.

    Two sources, and they answer different questions. `stored` is what the run
    *did* — orders refused, symbols whose history started late, costs switched
    off — recorded while it ran because none of it survives in the metric set.
    `suspicious` is how far to trust the statistics, and is derived from those
    statistics on every read so that a threshold this project revises applies to
    runs already on record.

    Concatenated rather than merged, and **the derived ones go first**. That is
    not cosmetic: `run_spec` ends `stored` with the refusal summary on purpose —
    "the line that says how much of the run above actually happened", which
    docs/BACKTESTING.md promises will be the last one — and appending to it
    would put a note about sample size between that line and the return it
    qualifies. Rendered above the numbers, last is nearest.

    It also reads in the right order: the statistical caveat states the symptom,
    and the run's own account of itself explains it.

    **A `stored` of None is not an empty run.** It is a row written before the
    column existed, whose warnings were computed and dropped; it gets exactly
    what it got before this function, and claims nothing about having been
    clean.
    """
    return [*suspicious(metrics or {}), *(stored or [])]


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
