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

from atp_core import metrics
from atp_core.errors import ConfigError, RiskLimitBreachedError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
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


class SessionAnchored(Protocol):
    """A rule that measures something against where the session started.

    `DailyLossLimitRule` is the only one today, and it is *default-closed* about
    it: with no anchor it denies every entry rather than assuming the day began
    flat. That is the right instinct and it made the missing call invisible —
    a chain that refuses everything and a chain that is not reached at all look
    identical from outside, and nothing in this platform had ever called
    `anchor` in production.

    So the anchoring is a named seam on the engine (`anchor_session`) rather
    than something each caller remembers to do to one rule it happens to know
    about. A caller that owns a session boundary calls it; a rule that has one
    implements this.
    """

    def anchor(self, equity: Decimal) -> None: ...


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

    def validate(
        self, order: Order, portfolio: Portfolio, pending: Iterable[Order] = ()
    ) -> RiskDecision:
        """Approve, shrink, or reject.

        **`pending` is what the caller has already committed and not yet seen
        settle**, and passing it is what stops a batch of orders collectively
        breaching a limit none of them breaches alone. The book moves on a fill,
        so without it every order in one bar is judged against a book holding
        none of the others: forty entries at 5% of equity each pass a 100% cap
        and land at 200%. `rules.project_pending` explains the failure and the
        conservatism; this is the only place it is applied, so every rule gets
        the corrected book without knowing in-flight orders exist.

        It defaults to empty, which is exactly today's behaviour for a caller
        that has nothing in flight — but a caller that *does* and omits it gets
        the old bug back, silently. The two callers in this platform both pass
        it: `BacktestEngine` its resting orders, `OrderRouter` whatever the
        runner believes is working at the venue.

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
        constructing `RiskEngine(limits, rules=[])` deliberately — the two chain
        builders below both return rules, and `RiskEngine(limits)` with none at
        all raises, so nothing gets an unguarded engine by omission.
        """
        # Imported here rather than at module scope for the reason
        # `default_rules` gives: `rules` needs `RiskDecision` from this module
        # at runtime, and a top-level import in both directions would not
        # resolve.
        from atp_core.risk.rules import project_pending

        book = project_pending(portfolio, pending)

        adjusted: Decimal | None = None
        for rule in self.rules:
            decision = rule.check(order, book, self.limits)
            if not decision.approved:
                # Counted with the rule that refused, which is the only label on
                # these worth having: "risk denied 40 orders today" is a
                # curiosity, and "the exposure cap denied 40" is a position
                # sizing bug.
                metrics.risk_checked("denied", rule=decision.rule)
                return decision
            if decision.adjusted_qty is not None:
                adjusted = decision.adjusted_qty
                order.qty = adjusted
        metrics.risk_checked("shrunk" if adjusted is not None else "approved")
        return RiskDecision(approved=True, adjusted_qty=adjusted)

    def validate_or_raise(
        self, order: Order, portfolio: Portfolio, pending: Iterable[Order] = ()
    ) -> None:
        """As `validate`, but raises `RiskLimitBreachedError` on denial."""
        decision = self.validate(order, portfolio, pending)
        if not decision.approved:
            raise RiskLimitBreachedError(decision.rule, decision.reason)

    def anchor_session(self, equity: Decimal) -> int:
        """Tell every session-aware rule where the trading day started.

        **Called by whoever owns the session boundary**: the live runner at each
        open, the backtest engine at each new session in the replay. Returns how
        many rules were anchored, which is what lets a caller assert it reached
        something rather than trusting that it did.

        This exists because nothing was calling it. `default_rules()` has always
        included `DailyLossLimitRule`, that rule denies every entry until it is
        anchored, and no production path ever anchored one — so the live chain
        was configured to refuse every entry it ever saw. It went unnoticed
        because the rule is default-closed and correct to be: a chain refusing
        everything for want of an anchor is indistinguishable, from outside,
        from a chain nothing has reached yet, and nothing has traded paper.

        Idempotent in the sense that matters and *not* in the sense that does
        not: calling it twice in one session re-anchors to a possibly drawn-down
        number and silently grants the day a second allowance, which is the
        mistake `DailyLossLimitRule.day_start_equity` warns about. Call it on
        the boundary, not on the loop.
        """
        anchored = 0
        for rule in self.rules:
            anchor = getattr(rule, "anchor", None)
            if callable(anchor):
                anchor(equity)
                anchored += 1
        return anchored


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


#: The four rules `backtest_rules` leaves out, by name and in the order its
#: docstring explains them.
#:
#: Declared here rather than written out wherever a result needs to name them,
#: because a backtest has to be able to say *which* four went unevaluated and a
#: second list would drift from this one silently — the rules would still be
#: absent and the sentence describing them would stop being true.
#: `test_the_four_that_cannot_are_absent_rather_than_stubbed` pins it against
#: the difference between the two chains rather than against itself.
REPLAY_BLIND_RULES: tuple[str, ...] = (
    "trading_hours",
    "stale_data",
    "kill_switch",
    "rate_limit",
)


def backtest_rules() -> list[RiskRule]:
    """The chain a replay over bars can actually evaluate.

    Five of the nine, and **the four that are absent are absent by decision
    rather than by omission** — which is the distinction `default_rules` exists
    to enforce, applied honestly rather than dodged by passing stubs that always
    approve. A no-op kill switch and a `last_tick_at` returning the current bar
    would let this call `default_rules` and claim nine rules; all four would
    approve unconditionally, and the chain would be theatre with a longer name.

    What each of the four would actually be measuring:

    - **`trading_hours`** asks the calendar whether the market is open at
      `clock.now()`. A daily bar is stamped at exchange-local *midnight*
      (docs/DATA.md), so the calendar says closed at both its open and its close
      — the rule would refuse every order in every daily backtest. And it could
      never do useful work anyway: every stored bar is a session by
      construction, so the honest answer is always "open".
    - **`stale_data`** measures a quote against a feed clock. The bar series
      *is* the feed here; freshness is zero by construction and there is no
      staleness for the rule to find.
    - **`kill_switch`** reads a halt an operator engaged. A replay has no
      operator and no halt state.
    - **`rate_limit`** is the runaway-loop guard — a bug that re-emits an order
      every tick. The event loop here emits at most one order per signal per
      bar and cannot run away, and simulated time advances a whole bar between
      orders, so the trailing minute means something different from what the
      rule was written against.

    What is left is every rule that is a statement about the *shape of the
    book*, and those are the ones a backtest most needs: they are what turn "the
    strategy said buy" into "and the account could actually hold that". A
    backtest that ignored them reports returns from positions no live account
    would have been allowed to take.

    `DailyLossLimitRule` denies every entry until something calls
    `RiskEngine.anchor_session` — deliberately, and the caller here is the
    engine's own session boundary.
    """
    from atp_core.risk.rules import (
        BuyingPowerRule,
        DailyLossLimitRule,
        MaxExposureRule,
        MaxOpenPositionsRule,
        MaxPositionSizeRule,
    )

    return [
        MaxPositionSizeRule(),
        MaxExposureRule(),
        MaxOpenPositionsRule(),
        DailyLossLimitRule(),
        BuyingPowerRule(),
    ]
