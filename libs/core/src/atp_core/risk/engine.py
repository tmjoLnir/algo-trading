"""Pre-trade risk validation — requirement #3.

Every order passes through here before reaching a broker adapter (rule §1.5).
The engine is a chain of independent rules; the first rejection stops the chain.

Design note: rules are *deny*-oriented and default-closed. If a rule cannot
evaluate — a missing mark, an unreachable account — it rejects rather than
allows. An unpriced position is exactly when you least want to be trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from decimal import Decimal

    from atp_core.config import RiskLimits
    from atp_core.domain import Order, Portfolio


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The verdict, plus why — the reason is surfaced on the dashboard and
    logged, so a blocked strategy is diagnosable without attaching a debugger."""

    approved: bool
    rule: str = ""
    reason: str = ""
    adjusted_qty: Decimal | None = None  # rule may shrink rather than refuse

    @classmethod
    def allow(cls) -> RiskDecision:
        return cls(approved=True)

    @classmethod
    def deny(cls, rule: str, reason: str) -> RiskDecision:
        return cls(approved=False, rule=rule, reason=reason)


class RiskRule(Protocol):
    """One independent check."""

    @property
    def name(self) -> str: ...

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision: ...


class RiskEngine:
    """Runs the rule chain. The only gate between a signal and the market."""

    def __init__(self, limits: RiskLimits, rules: list[RiskRule] | None = None) -> None:
        self.limits = limits
        self.rules = rules if rules is not None else default_rules()

    def validate(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        """Approve, shrink, or reject.

        Rules run in order and the first denial wins, so cheap checks (kill
        switch, rate limit) belong before expensive ones (exposure maths).
        """
        raise NotImplementedError(
            "Run each rule; return the first denial. If a rule returns "
            "adjusted_qty, apply it and continue with the reduced order."
        )

    def validate_or_raise(self, order: Order, portfolio: Portfolio) -> None:
        """As `validate`, but raises `RiskLimitBreachedError` on denial."""
        raise NotImplementedError


def default_rules() -> list[RiskRule]:
    """The standard chain, cheapest and most decisive first."""
    raise NotImplementedError(
        "Order matters: KillSwitchRule, TradingHoursRule, RateLimitRule, "
        "MaxPositionSizeRule, MaxExposureRule, MaxOpenPositionsRule, "
        "DailyLossLimitRule, BuyingPowerRule, StaleDataRule."
    )
