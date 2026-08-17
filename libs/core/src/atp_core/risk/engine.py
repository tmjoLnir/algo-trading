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

from atp_core.errors import ConfigError, RiskLimitBreachedError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from decimal import Decimal

    from atp_core.clock import Clock, TradingCalendar
    from atp_core.config import RiskLimits
    from atp_core.domain import Order, Portfolio
    from atp_core.risk.killswitch import KillSwitch


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

    @classmethod
    def shrink(cls, rule: str, reason: str, qty: Decimal) -> RiskDecision:
        """Approve, but only this much.

        The third verdict, and the one with no constructor until now: `allow`
        never set `adjusted_qty` and `deny` is terminal, so the shrink path
        `validate` documents was unreachable. None of the default rules shrink
        — refusing is clearer than silently trading a fraction of what a
        strategy asked for — but a custom rule can, and the engine applies it.
        """
        if qty <= 0:
            raise ValueError(f"a shrink must leave something to trade, got {qty}")
        return cls(approved=True, rule=rule, reason=reason, adjusted_qty=qty)


class RiskRule(Protocol):
    """One independent check."""

    @property
    def name(self) -> str: ...

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision: ...


class RiskEngine:
    """Runs the rule chain. The only gate between a signal and the market."""

    def __init__(self, limits: RiskLimits, rules: list[RiskRule] | None = None) -> None:
        if rules is None:
            raise ConfigError(
                "RiskEngine needs an explicit rule chain. Build one with "
                "default_rules(kill_switch, clock, calendar, last_tick_at) — four "
                "of the nine rules cannot evaluate without those, so there is no "
                "chain that can be assembled by omission. Pass rules=[] only if "
                "you deliberately want an engine that refuses nothing."
            )
        self.limits = limits
        self.rules = rules

    def validate(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        """Approve, shrink, or reject.

        Rules run in order and the first denial wins, so cheap checks (kill
        switch, rate limit) belong before expensive ones (exposure maths).

        A rule that shrinks rather than refuses **mutates the order**, so every
        later rule measures its limit against the quantity that would actually
        be sent. Checking a $50,000 exposure cap against an order a previous
        rule already cut to $5,000 would reject trades that are within every
        limit. `Order` is mutable state by design (`domain/order.py`), and the
        adjusted quantity is returned as well so a caller that kept its own
        reference sees the same number.

        An empty rule chain approves everything. That is only reachable by
        constructing `RiskEngine(limits, rules=[])` deliberately — `default_rules`
        raises until Phase 3 lands, so nothing gets an unguarded engine by
        omission.
        """
        adjusted: Decimal | None = None
        for rule in self.rules:
            decision = rule.check(order, portfolio, self.limits)
            if not decision.approved:
                return decision
            if decision.adjusted_qty is not None:
                adjusted = decision.adjusted_qty
                order.qty = adjusted
        return RiskDecision(approved=True, adjusted_qty=adjusted)

    def validate_or_raise(self, order: Order, portfolio: Portfolio) -> None:
        """As `validate`, but raises `RiskLimitBreachedError` on denial."""
        decision = self.validate(order, portfolio)
        if not decision.approved:
            raise RiskLimitBreachedError(decision.rule, decision.reason)


def default_rules(
    kill_switch: KillSwitch,
    clock: Clock,
    calendar: TradingCalendar,
    last_tick_at: Callable[[str], datetime | None],
) -> list[RiskRule]:
    """The standard chain, cheapest and most decisive first.

    Order matters twice over. Cheap and decisive checks come first so a halted
    platform costs one boolean rather than a book valuation. And because the
    first denial wins, the reason a human reads is the *most fundamental* one:
    an order placed while the kill switch is engaged should say "trading is
    halted", not "insufficient buying power", even when both are true.

    Four of the nine cannot evaluate without something outside the portfolio —
    a halt state, a clock, a calendar, a feed timestamp — so they are arguments
    rather than defaults. A chain that quietly dropped them would be a chain
    that stopped enforcing four things without anyone deciding to, which is why
    there is no no-argument version of this function.
    """
    # Imported here rather than at module scope: `rules` needs `RiskDecision`
    # from this module at runtime, and a top-level import in both directions
    # would not resolve.
    from atp_core.risk.rules import (
        BuyingPowerRule,
        DailyLossLimitRule,
        KillSwitchRule,
        MaxExposureRule,
        MaxOpenPositionsRule,
        MaxPositionSizeRule,
        RateLimitRule,
        StaleDataRule,
        TradingHoursRule,
    )

    return [
        KillSwitchRule(switch=kill_switch),
        TradingHoursRule(calendar=calendar, clock=clock),
        RateLimitRule(clock=clock),
        StaleDataRule(clock=clock, last_tick_at=last_tick_at),
        MaxPositionSizeRule(),
        MaxExposureRule(),
        MaxOpenPositionsRule(),
        DailyLossLimitRule(),
        BuyingPowerRule(),
    ]
