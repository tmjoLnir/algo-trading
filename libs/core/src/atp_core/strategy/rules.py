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

from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from atp_core.domain.enums import StopType, Timeframe

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
        """Longest indicator lookback in the tree — the engine's warmup budget."""
        raise NotImplementedError("walk the condition tree, take max(period) + 1")


def compile_ruleset(spec: RuleSet) -> Any:
    """Turn a validated `RuleSet` into a `Strategy` instance.

    Returns `atp_core.strategy.base.Strategy`.
    """
    raise NotImplementedError("see docs/STRATEGY_AUTHORING.md 'Rule compilation'")
