"""Declarative rule specs — "preset rules" (requirement #1) without writing code.

A rule set is YAML or JSON that the UI can render as a form, that validates
before it ever runs, and that is safe to store in a database. It compiles to the
same `Signal` stream a hand-written `Strategy` produces, so backtest, paper and
live treat both kinds identically.

    name: sma_crossover_spy
    universe: [SPY, QQQ]
    timeframe: 1d
    entry_long:
      all:
        - {left: {indicator: sma, period: 20}, op: crosses_above,
           right: {indicator: sma, period: 50}}
        - {left: {indicator: rsi, period: 14}, op: "<", right: {value: 70}}
    exit:
      any:
        - {left: {indicator: sma, period: 20}, op: crosses_below,
           right: {indicator: sma, period: 50}}
    risk:
      stop_loss: {type: atr, multiplier: 2.0, period: 14}
      take_profit: {type: fixed_pct, value: 0.06}
      position_size: {type: risk_pct, value: 0.01}

**Security note:** expressions are evaluated by an explicit interpreter over
this validated tree. Never `eval()` a user-supplied rule string — a rule set is
untrusted input that arrives over HTTP.
"""

from __future__ import annotations

import operator
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from atp_core.domain import Signal, SignalAction
from atp_core.domain.enums import StopType, Timeframe
from atp_core.errors import DataGapError, InvalidRuleError
from atp_core.indicators import dispatch
from atp_core.strategy.base import Strategy

if TYPE_CHECKING:
    from collections.abc import Callable

    from atp_core.domain import Bar
    from atp_core.strategy.context import StrategyContext

Comparator = Literal[
    "<",
    "<=",
    ">",
    ">=",
    "==",
    "!=",
    "crosses_above",  # was <= on the previous bar, is > on this one
    "crosses_below",
]


class IndicatorOperand(BaseModel):
    """A computed value: `{indicator: rsi, period: 14}`."""

    indicator: str
    period: int | None = None
    field: Literal["open", "high", "low", "close", "volume"] = "close"
    offset: int = Field(default=0, ge=0, description="bars back; 0 = current bar")
    params: dict[str, Any] = Field(default_factory=dict)


class PriceOperand(BaseModel):
    """A raw price field: `{price: close}`."""

    price: Literal["open", "high", "low", "close", "volume"]
    offset: int = Field(default=0, ge=0)


class ConstantOperand(BaseModel):
    value: Decimal


Operand = Annotated[
    IndicatorOperand | PriceOperand | ConstantOperand, Field(union_mode="left_to_right")
]


class Condition(BaseModel):
    """A single comparison."""

    left: Operand
    op: Comparator
    right: Operand


class ConditionGroup(BaseModel):
    """Boolean composition. Exactly one of `all` / `any` / `none` is set."""

    all: list["ConditionNode"] | None = None
    any: list["ConditionNode"] | None = None
    none: list["ConditionNode"] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ConditionGroup:
        set_fields = [f for f in ("all", "any", "none") if getattr(self, f) is not None]
        if len(set_fields) != 1:
            raise ValueError(
                f"a condition group needs exactly one of all/any/none, got {set_fields}"
            )
        return self


ConditionNode = Annotated[Condition | ConditionGroup, Field(union_mode="left_to_right")]
ConditionGroup.model_rebuild()


class StopSpec(BaseModel):
    type: StopType
    value: Decimal | None = None  # pct or absolute, by type
    multiplier: Decimal | None = None  # ATR / chandelier multiple
    period: int | None = None  # ATR lookback
    bars: int | None = None  # for StopType.TIME


class PositionSizeSpec(BaseModel):
    """How much to buy.

    `risk_pct` is the one to reach for by default: it sizes so that hitting the
    stop loses a fixed fraction of equity, which keeps risk-per-trade constant
    across instruments of wildly different volatility. See docs/RISK.md.
    """

    #: A hard backstop, not a recommendation. docs/RISK.md gives 0.5–2% risk per
    #: trade and explains that above 2% a normal losing streak of 8–10 trades is
    #: an account-threatening event. This rejects an order of magnitude past
    #: that, on the grounds that anything beyond it is a typo rather than a
    #: choice — a misplaced decimal point turning 0.01 into 0.1 is the exact
    #: mistake worth catching at config time rather than at trade 3.
    MAX_RISK_PCT: ClassVar[Decimal] = Decimal("0.10")

    type: Literal["fixed_qty", "fixed_notional", "equity_pct", "risk_pct", "volatility_target"]
    value: Decimal = Field(gt=0)
    max_position_pct: Decimal | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def _value_suits_the_method(self) -> PositionSizeSpec:
        """`value` means a different thing per method, so it bounds differently.

        A share count of 500 is ordinary; a `risk_pct` of 500 would size the
        whole account into one trade fifty times over. Every other bound in this
        file is stated (`offset` is `ge=0`, `cooldown_bars` is `ge=0`), and this
        one was the gap.
        """
        if self.type in {"equity_pct", "risk_pct", "volatility_target"} and self.value > 1:
            raise ValueError(
                f"{self.type} takes a fraction of equity, got {self.value} — 0.01 is 1%, not 1"
            )
        if self.type == "risk_pct" and self.value > self.MAX_RISK_PCT:
            raise ValueError(
                f"risk_pct of {self.value} is past the {self.MAX_RISK_PCT} backstop. "
                f"docs/RISK.md gives 0.5-2% per trade: above 2%, a normal losing "
                f"streak of 8-10 trades threatens the account"
            )
        return self


class RiskSpec(BaseModel):
    stop_loss: StopSpec | None = None
    take_profit: StopSpec | None = None
    position_size: PositionSizeSpec
    max_holding_bars: int | None = None
    flatten_at_close: bool = False  # avoid overnight gap risk


class RuleSet(BaseModel):
    """A complete, executable strategy definition."""

    name: str
    description: str = ""
    universe: list[str] = Field(min_length=1)
    timeframe: Timeframe = Timeframe.D1

    entry_long: ConditionNode | None = None
    entry_short: ConditionNode | None = None
    exit: ConditionNode | None = None
    risk: RiskSpec

    max_concurrent_positions: int = Field(default=5, ge=1)
    cooldown_bars: int = Field(
        default=0, ge=0, description="bars to wait after an exit before re-entering"
    )

    @model_validator(mode="after")
    def _needs_an_entry(self) -> RuleSet:
        if self.entry_long is None and self.entry_short is None:
            raise ValueError("a rule set needs at least one of entry_long / entry_short")
        if self.exit is None and self.risk.stop_loss is None:
            raise ValueError(
                "a rule set with no exit condition and no stop loss can never close a "
                "position — refusing to run it"
            )
        return self

    @property
    def required_warmup(self) -> int:
        """Longest indicator lookback in the tree — the engine's warmup budget.

        Every operand states how much history it needs, and the answer is the
        largest: an `sma(50)` against an `sma(20)` needs 50 bars, not 70. Two
        additions to that base, both of which understate warmup if left out —
        and understating it is the failure `docs/STRATEGY_AUTHORING.md` rule 5
        names, where the first signals of a run are computed on half-filled
        indicators and taken as real:

        - **`offset` adds to the period.** `{indicator: sma, period: 20,
          offset: 3}` is the SMA(20) as it stood three bars ago, which needs 23
          bars, not 20.
        - **A crossing reads two bars.** `crosses_above` compares this bar's
          relationship against the previous one, so it needs one bar more than
          either operand alone. This is the `+ 1` that `SmaCrossover.warmup_bars`
          carries for exactly the same reason.

        The `risk` block's periods count too, which the phrase "in the tree" does
        not obviously cover. An ATR(50) stop under an SMA(5) entry genuinely
        needs 51 bars before it can place a level, and a warmup of 6 would spend
        the first 45 entries being refused at sizing for want of a stop — a run
        that looks like a strategy which never fired. Warmup is the point where
        *the spec* becomes meaningful, not just its conditions. `StopSpec.period`
        is an ATR lookback whatever the stop type reading it (`atr`,
        `chandelier`), so it is measured against `atr` and picks up the same
        extra Wilder bar an `atr` operand would.
        """
        trees = [t for t in (self.entry_long, self.entry_short, self.exit) if t is not None]
        needed = max((_bars_needed(tree) for tree in trees), default=0)
        for stop in (self.risk.stop_loss, self.risk.take_profit):
            if stop is not None and stop.period is not None:
                needed = max(needed, dispatch.min_bars("atr", stop.period))
        return needed


# ── the interpreter ─────────────────────────────────────────────────────────
#
# A rule set is untrusted input that arrives over HTTP, so it is walked by the
# explicit interpreter below and never handed to `eval()`. Everything here is a
# pure function of a validated tree and a list of bars.

#: Comparators that read two bars rather than one — see `required_warmup`.
_CROSSINGS: frozenset[str] = frozenset({"crosses_above", "crosses_below"})

_COMPARISONS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


def _operand_bars(operand: Operand) -> int:
    """How many bars this operand needs before it can produce a value.

    Asked of `dispatch` rather than assumed to be the period, because `rsi` and
    `atr` need one bar more than theirs. Assuming the period there is not an
    off-by-one in a warmup count: `_RuleSetStrategy` sizes its history window
    off this number, so a window one bar short of what `rsi` needs answers None
    for the entire run rather than only during warmup, and the rule never fires.
    """
    if isinstance(operand, IndicatorOperand):
        return dispatch.min_bars(operand.indicator, operand.period or 0) + operand.offset
    if isinstance(operand, PriceOperand):
        return operand.offset + 1  # offset 0 still needs the current bar
    return 0  # a constant needs no history


def _bars_needed(node: ConditionNode) -> int:
    """The deepest history requirement anywhere under `node`."""
    if isinstance(node, ConditionGroup):
        children = _children(node)
        return max((_bars_needed(child) for child in children), default=0)
    extra = 1 if node.op in _CROSSINGS else 0
    return max(_operand_bars(node.left), _operand_bars(node.right)) + extra


def _children(group: ConditionGroup) -> list[ConditionNode]:
    """The one populated branch of a group. The validator guarantees exactly one."""
    for branch in (group.all, group.any, group.none):
        if branch is not None:
            return branch
    return []  # unreachable: `_exactly_one` rejects a group with none set


def _label(operand: Operand) -> str:
    """A stable, human-readable name — the key an operand's value is traced under.

    Doubles as the memo key, which is why it has to encode everything that
    changes the number: `sma(20)` and `sma(20)[-3]` are different values of the
    same indicator and must not collide.
    """
    if isinstance(operand, ConstantOperand):
        return str(operand.value)
    base = (
        f"{operand.indicator}({operand.period})"
        if isinstance(operand, IndicatorOperand)
        else operand.price
    )
    return f"{base}[-{operand.offset}]" if operand.offset else base


class _Evaluation:
    """One walk of one condition tree against one symbol's visible history.

    Holds three things that all have to agree with each other: the memo (so a
    crossing does not recompute an indicator it already has), the trace (the
    values that end up on `Signal.indicators`), and the bars everything is
    measured over. Keeping them on one object rather than threading three dicts
    through the recursion is the whole reason this is a class.

    **Three-valued on purpose.** `None` is "cannot say yet", not "no" — an
    indicator returns it for the whole of warmup, and collapsing that to `False`
    would make `none: [rsi < 30]` fire on bar 1 of every run, before there is an
    RSI to be under 30. So unknown propagates: a group answers `True` or `False`
    only when the unknown children could not have changed it, and the caller
    treats anything that is not exactly `True` as no signal.
    """

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self._memo: dict[tuple[str, int], float | None] = {}
        #: label → value, for `Signal.indicators` and the `reason` string.
        self.values: dict[str, float] = {}

    def value(self, operand: Operand, back: int = 0) -> float | None:
        """This operand's value, `back` extra bars into the past.

        `back` is 1 for the previous-bar leg of a crossing. It is kept separate
        from `operand.offset` so that `{offset: 3} crosses_above ...` means bars
        3 and 4 back, which is what "crossed, as of three bars ago" has to mean.

        Only the current-bar leg is traced. The previous one is machinery — a
        dashboard reading "why is this trade on?" wants `sma(20)=101.2`, not the
        two numbers whose ordering flipped.
        """
        key = (_label(operand), back)
        if key not in self._memo:
            self._memo[key] = self._compute(operand, back)
        found = self._memo[key]
        if back == 0 and found is not None and not isinstance(operand, ConstantOperand):
            self.values[key[0]] = found
        return found

    def _compute(self, operand: Operand, back: int) -> float | None:
        if isinstance(operand, ConstantOperand):
            # float, not Decimal: this feeds indicator maths and comparisons,
            # not a ledger (CLAUDE.md §1.1). Nothing here tracks a balance.
            return float(operand.value)

        offset = operand.offset + back
        # Truncated from the BACK, never the front. `ta.sma(closes[:-1], n)` is
        # the previous bar's SMA — the same slice `SmaCrossover` takes — whereas
        # dropping leading bars would reseed Wilder's smoothing in `rsi` and
        # `atr` and quietly answer a different number.
        window = self._bars[: len(self._bars) - offset] if offset else self._bars
        if not window:
            return None

        if isinstance(operand, PriceOperand):
            return float(getattr(window[-1], operand.price))
        # `period` is not None: `_refuse_what_cannot_run` rejects a spec without
        # one before any of this runs.
        return dispatch.compute(operand.indicator, window, operand.period or 0)

    def verdict(self, node: ConditionNode) -> bool | None:
        """Does this subtree hold on the current bar? `None` while unknowable."""
        if isinstance(node, ConditionGroup):
            return self._group(node)
        return self._compare(node)

    def _group(self, group: ConditionGroup) -> bool | None:
        verdicts = [self.verdict(child) for child in _children(group)]
        unknown = any(v is None for v in verdicts)
        if group.all is not None:
            if any(v is False for v in verdicts):
                return False  # one definite failure settles it, unknowns or not
            return None if unknown else True
        if group.any is not None:
            if any(v is True for v in verdicts):
                return True
            return None if unknown else False
        # `none` — the negation of `any`.
        if any(v is True for v in verdicts):
            return False
        return None if unknown else True

    def _compare(self, condition: Condition) -> bool | None:
        left, right = self.value(condition.left), self.value(condition.right)
        if left is None or right is None:
            return None
        if condition.op not in _CROSSINGS:
            return _COMPARISONS[condition.op](left, right)

        # A crossing is a transition, not a level (STRATEGY_AUTHORING rule 6):
        # comparing only the current bar would re-fire an entry on every bar
        # that fast stays above slow.
        was_left, was_right = (
            self.value(condition.left, back=1),
            self.value(condition.right, back=1),
        )
        if was_left is None or was_right is None:
            return None
        if condition.op == "crosses_above":
            return was_left <= was_right and left > right
        return was_left >= was_right and left < right

    def describe(self, node: ConditionNode) -> str:
        """The subtree rendered with the values it was decided on.

        This is `Signal.reason`, which the authoring guide calls not optional:
        it is what a human gets when they ask the dashboard why a trade is on,
        and what makes a losing run diagnosable months later. "sma(20)=101.2
        crosses_above sma(50)=100.8" answers that; "entry_long" does not.
        """
        if isinstance(node, ConditionGroup):
            branch = "all" if node.all is not None else "any" if node.any is not None else "none"
            rendered = ", ".join(self.describe(child) for child in _children(node))
            return f"{branch}({rendered})"
        return f"{self._shown(node.left)} {node.op} {self._shown(node.right)}"

    def _shown(self, operand: Operand) -> str:
        label = _label(operand)
        if isinstance(operand, ConstantOperand):
            return label
        found = self.values.get(label)
        # `:.6g` rather than a fixed number of places: this renders an RSI of
        # 28.4 and a share price of 4218.75 in the same string.
        return label if found is None else f"{label}={found:.6g}"


# ── compilation ─────────────────────────────────────────────────────────────


def _refuse_what_cannot_run(spec: RuleSet) -> None:
    """Reject, at compile time, every part of a spec this cannot honour.

    The alternative is ignoring those parts silently, and a spec that asks for
    `field: high` and gets closes is worse than one that fails to compile: it
    runs, produces a plausible equity curve, and is wrong in a way nothing in
    the result would show. Pydantic validated the spec's *shape* — this checks
    what the interpreter can actually execute.
    """
    for tree, where in (
        (spec.entry_long, "entry_long"),
        (spec.entry_short, "entry_short"),
        (spec.exit, "exit"),
    ):
        if tree is not None:
            _refuse_node(tree, where)

    if spec.risk.flatten_at_close:
        raise InvalidRuleError(
            "flatten_at_close is not modelled yet: a strategy cannot see the "
            "session end (it never reads the clock, CLAUDE.md §1.5) and guessing "
            "one would exit at a different bar in a backtest than in production"
        )


def _refuse_node(node: ConditionNode, where: str) -> None:
    if isinstance(node, ConditionGroup):
        children = _children(node)
        if not children:
            # `all: []` is vacuously true, which as an entry means "enter on
            # every bar". Nobody writes that on purpose, and the cost of
            # reading it charitably is a run that trades every bar it can.
            raise InvalidRuleError(f"{where}: an empty condition group is never what was meant")
        for child in children:
            _refuse_node(child, where)
        return
    for operand in (node.left, node.right):
        _refuse_operand(operand, where)


def _refuse_operand(operand: Operand, where: str) -> None:
    if not isinstance(operand, IndicatorOperand):
        return
    name = operand.indicator
    if name not in dispatch.KNOWN_INDICATORS:
        # At compile time rather than on bar 1: a typo'd indicator should fail
        # when the rule set is saved, not halfway through a backtest.
        raise InvalidRuleError(
            f"{where}: unknown indicator {name!r}; known: {sorted(dispatch.KNOWN_INDICATORS)}"
        )
    if operand.period is None or operand.period < 1:
        # Not defaulted. `ctx.indicator` falls back to 14 for a caller that
        # omits one, but an `sma` silently given a period nobody chose is a
        # different strategy than the one that was approved.
        raise InvalidRuleError(
            f"{where}: indicator {name!r} needs a period of at least 1, got {operand.period!r}"
        )
    if operand.field != "close":
        raise InvalidRuleError(
            f"{where}: {name!r} on {operand.field!r} is not available — "
            f"`indicators.dispatch` computes on closes (`atr` reads high/low "
            f"internally). Use a `price` operand for a raw field"
        )
    if operand.params:
        raise InvalidRuleError(
            f"{where}: {name!r} takes no extra params, got {sorted(operand.params)}. "
            f"Adding one means teaching `indicators.dispatch`, so that a backtest "
            f"and the live runner keep computing the same number (ADR 0006)"
        )


class _RuleSetStrategy(Strategy):
    """A `RuleSet` running as an ordinary `Strategy`.

    The point of compiling rather than interpreting somewhere special: what
    comes out is a `Strategy` like any other, so the backtest engine, the paper
    runner and live all drive it through the identical path they drive
    `SmaCrossover` through. There is no "declarative mode" anywhere downstream.

    **What this does not do is as deliberate as what it does.** The spec's
    `risk` block — stop levels, take-profits, position sizing — is not touched
    here beyond `required_warmup`. A strategy never sizes a position and never
    places a protective level (CLAUDE.md §1.5, and the reason `Strategy` has no
    field for either); the sizer, `StopManager` and the engine's `stop_config`
    own those. Deriving a stop here *as well* would give one level two sources
    that could disagree, which is the divergence ADR 0006 exists to prevent.
    Wiring `spec.risk` into a run's configuration is a separate change, and
    until it lands a rule set's stops have to be passed to the run explicitly.

    What it does own is every exit that is a matter of *counting*: the
    condition tree, `cooldown_bars`, `max_holding_bars` and
    `max_concurrent_positions` need no price level and no risk machinery, only
    bars and positions, both of which a strategy may read.
    """

    def __init__(self, spec: RuleSet) -> None:
        self.spec = spec
        self._universe = frozenset(spec.universe)
        # At least the current bar, even for a spec of pure constants.
        self._window = max(spec.required_warmup, 1)
        self.on_start()
        # Last, because it calls `validate_params`, which reads `self.spec`.
        super().__init__(params=spec.model_dump(mode="json"))

    def on_start(self) -> None:
        """Reset the bar counters (authoring rule 2: no state a restart keeps).

        A runner that restarts mid-session starts counting from zero, so a
        position already open is treated as having been entered on the first bar
        seen after the restart. That understates its age, which delays a
        `max_holding_bars` exit rather than bringing one forward — the safe
        direction of a bound whose reference point was lost.
        """
        #: symbol → bars seen this session. The clock a strategy is allowed.
        self._seen: dict[str, int] = {}
        self._entered_at: dict[str, int] = {}
        self._exited_at: dict[str, int] = {}

    @property
    def warmup_bars(self) -> int:
        return self.spec.required_warmup

    def __repr__(self) -> str:
        """The spec's shape, not the spec.

        `Strategy.__repr__` prints `params`, and a rule set's params are the
        whole serialised spec — a screenful of nested JSON on every log line
        that carries a strategy. The params stay complete because a stored row
        recording `{}` would claim the strategy was configured with nothing;
        it is only the repr that is trimmed.
        """
        return (
            f"<{type(self).__name__} name={self.name!r} "
            f"universe={sorted(self._universe)} warmup={self.warmup_bars}>"
        )

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> list[Signal]:
        if bar.symbol not in self._universe:
            # A run whose symbols do not meet the spec's universe takes no
            # trades. That is the spec being explicit about what it trades, not
            # the engine being wrong — but it does look identical to a strategy
            # that never signalled, so `universe` is the first thing to check
            # when a rule-set run comes back empty.
            return []

        index = self._seen.get(bar.symbol, 0) + 1
        self._seen[bar.symbol] = index
        position = ctx.position(bar.symbol)
        self._track(bar.symbol, index, flat=position.is_flat)

        try:
            bars = ctx.history(bar.symbol, self.spec.timeframe, self._window)
        except DataGapError:
            # The ordinary state during warmup. `history` refuses a short series
            # rather than returning one, and a half-length window is exactly the
            # partial indicator this is here to avoid trading on.
            return []

        if not position.is_flat:
            return self._exit_signals(ctx, bar, bars, index)
        return self._entry_signals(ctx, bar, bars, index)

    def _track(self, symbol: str, index: int, *, flat: bool) -> None:
        """Note when this symbol went flat or non-flat.

        Read off the observed position rather than off the signals emitted,
        because a signal is a request: it fills a bar later, at a price that may
        never come, and it can be refused by risk or by sizing. Counting a
        holding period from an entry that never filled would time out a position
        that was never opened.
        """
        if flat:
            if self._entered_at.pop(symbol, None) is not None:
                self._exited_at[symbol] = index
        else:
            self._entered_at.setdefault(symbol, index)

    def _exit_signals(
        self, ctx: StrategyContext, bar: Bar, bars: list[Bar], index: int
    ) -> list[Signal]:
        entered = self._entered_at.get(bar.symbol)
        limit = self.spec.risk.max_holding_bars
        if limit is not None and entered is not None and index - entered >= limit:
            held = index - entered
            return [self._signal(SignalAction.EXIT, ctx, bar, f"held {held} bars ≥ {limit}", {})]

        if self.spec.exit is None:
            return []  # `_needs_an_entry` guarantees a stop loss is carrying it
        walk = _Evaluation(bars)
        if walk.verdict(self.spec.exit) is not True:
            return []
        return [
            self._signal(
                SignalAction.EXIT, ctx, bar, f"exit: {walk.describe(self.spec.exit)}", walk.values
            )
        ]

    def _entry_signals(
        self, ctx: StrategyContext, bar: Bar, bars: list[Bar], index: int
    ) -> list[Signal]:
        exited = self._exited_at.get(bar.symbol)
        if exited is not None and index - exited < self.spec.cooldown_bars:
            return []
        if self._open_positions(ctx) >= self.spec.max_concurrent_positions:
            return []

        for tree, action, where in (
            (self.spec.entry_long, SignalAction.ENTER_LONG, "entry_long"),
            (self.spec.entry_short, SignalAction.ENTER_SHORT, "entry_short"),
        ):
            if tree is None:
                continue
            walk = _Evaluation(bars)
            if walk.verdict(tree) is True:
                reason = f"{where}: {walk.describe(tree)}"
                return [self._signal(action, ctx, bar, reason, walk.values)]
        return []

    def _open_positions(self, ctx: StrategyContext) -> int:
        """How many of the universe are currently held.

        Counted off the portfolio each bar rather than tracked, so that a
        position closed by a stop — which the strategy never hears about —
        frees its slot on the next bar instead of holding it for the rest of
        the run.
        """
        return sum(1 for symbol in self._universe if not ctx.position(symbol).is_flat)

    def _signal(
        self,
        action: SignalAction,
        ctx: StrategyContext,
        bar: Bar,
        reason: str,
        values: dict[str, float],
    ) -> Signal:
        return Signal(
            strategy_id=self.name,
            symbol=bar.symbol,
            action=action,
            ts=ctx.now,
            reason=reason,
            indicators={**values, "close": float(bar.close)},
        )


def compile_ruleset(spec: RuleSet) -> Strategy:
    """Turn a validated `RuleSet` into a `Strategy` instance.

    A fresh subclass per spec, rather than one class carrying the name on the
    instance, because `name` is a `ClassVar` that the registry, the database and
    every `Signal.strategy_id` key off. Shadowing it per-instance would type as
    a mistake and read as one.

    The class is deliberately **not** registered: `registry.register` refuses a
    duplicate name to keep backtest results unambiguous, and compiling the same
    stored rule set twice — which happens on every run of it — would trip that
    on the second call. A rule set is identified by its database row, not by the
    process-global registry, which is for classes that ship in the repo.
    """
    _refuse_what_cannot_run(spec)

    class _Compiled(_RuleSetStrategy):
        name: ClassVar[str] = spec.name
        description: ClassVar[str] = spec.description

    _Compiled.__name__ = _Compiled.__qualname__ = f"RuleSet_{spec.name}"
    return _Compiled(spec)
