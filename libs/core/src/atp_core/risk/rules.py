"""The individual pre-trade rules.

Each is small, independently testable, and states in its docstring the failure
it exists to prevent. Add a rule here rather than adding a condition to an
existing one — a rule that checks two things reports the wrong reason for half
its rejections.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.risk.engine import RiskDecision

if TYPE_CHECKING:
    from atp_core.config import RiskLimits
    from atp_core.domain import Order, Portfolio


@dataclass(slots=True)
class KillSwitchRule:
    """Refuses everything while the platform-wide halt is engaged.

    First in the chain: when a human hits stop, nothing else should get a vote.
    """

    name: str = "kill_switch"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        raise NotImplementedError


@dataclass(slots=True)
class MaxPositionSizeRule:
    """Caps any single position at `max_position_pct` of equity.

    Prevents one conviction — or one sizing bug — from becoming the whole book.
    Checks the position *after* this order, not the order alone; three orders of
    4% each are a 12% position.
    """

    name: str = "max_position_size"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        raise NotImplementedError


@dataclass(slots=True)
class MaxExposureRule:
    """Caps gross exposure — the leverage ceiling.

    Gross, not net: a long/short book that nets to zero still borrows money and
    still loses on both legs in a correlated shock.
    """

    name: str = "max_gross_exposure"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        raise NotImplementedError


@dataclass(slots=True)
class DailyLossLimitRule:
    """Halts new entries once the day's drawdown exceeds `max_daily_loss_pct`.

    Exits must still be permitted — refusing to let a losing position close
    would turn a bad day into an unbounded one. Check `order.reduces_position`
    before denying.
    """

    name: str = "daily_loss_limit"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        raise NotImplementedError


@dataclass(slots=True)
class RateLimitRule:
    """Caps orders per minute.

    This is the runaway-loop guard. A strategy bug that re-emits an entry every
    tick will otherwise submit thousands of orders in a minute and empty the
    account in fees alone — this has happened to real firms.
    """

    name: str = "rate_limit"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        raise NotImplementedError


@dataclass(slots=True)
class MaxOpenPositionsRule:
    name: str = "max_open_positions"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        raise NotImplementedError


@dataclass(slots=True)
class BuyingPowerRule:
    """Rejects what the account cannot pay for, before the broker does.

    Our own rejection is cheap and diagnosable; a stream of broker rejects can
    get API access throttled.
    """

    name: str = "buying_power"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        raise NotImplementedError


@dataclass(slots=True)
class TradingHoursRule:
    """Blocks orders outside the session unless explicitly extended-hours.

    A market order resting through the close fills at the open, potentially
    percentage points from where the strategy decided.
    """

    name: str = "trading_hours"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        raise NotImplementedError


@dataclass(slots=True)
class StaleDataRule:
    """Refuses to trade on a quote older than `max_age_seconds`.

    A frozen feed looks identical to a quiet market. Trading on a stale price is
    trading blind — this rule is why `StaleDataError` exists.
    """

    name: str = "stale_data"
    max_age_seconds: int = 30

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        raise NotImplementedError


def position_size(
    method: str,
    equity: Decimal,
    price: Decimal,
    stop_price: Decimal | None = None,
    risk_pct: Decimal = Decimal("0.01"),
) -> Decimal:
    """Turn intent into a quantity.

    The `risk_pct` method is the one that matters:

        qty = (equity × risk_pct) / |entry − stop|

    It equalises *risk*, not *notional*. A tight stop earns a bigger position and
    a wide stop a smaller one, so every trade loses roughly the same amount when
    it goes wrong regardless of the instrument's volatility. Sizing by fixed
    notional instead means your riskiest positions are silently your largest.

    Raises if `method` is risk-based and `stop_price` is None — sizing by risk
    without a stop is undefined, and defaulting it would hide the mistake.
    """
    raise NotImplementedError("see docs/RISK.md 'Position sizing'")
