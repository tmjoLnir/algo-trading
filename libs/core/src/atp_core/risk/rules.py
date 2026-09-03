"""The individual pre-trade rules.

Each is small, independently testable, and states in its docstring the failure
it exists to prevent. Add a rule here rather than adding a condition to an
existing one — a rule that checks two things reports the wrong reason for half
its rejections.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from atp_core.domain import Position, Side
from atp_core.risk.engine import RiskDecision

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

    from atp_core.clock import Clock, TradingCalendar
    from atp_core.domain import Order, Portfolio
    from atp_core.risk.killswitch import KillSwitch
    from atp_core.risk.limits import RiskLimits


def reduces_position(order: Order, portfolio: Portfolio) -> bool:
    """Whether this order shrinks the holding it touches.

    Not a property of the order: a sell is an exit when you are long and an
    entry when you are flat or short. `DailyLossLimitRule` turns on this
    distinction — it must block entries and never exits — so it is written once
    here rather than re-derived, subtly differently, in each rule that needs it.

    An order larger than the position it opposes still counts as reducing. It
    closes the position on its way through zero, and refusing it would trap the
    very position the limit is trying to let go of.
    """
    position = portfolio.positions.get(order.symbol)
    if position is None or position.is_flat:
        return False
    return position.is_long if order.side is Side.SELL else position.is_short


def reference_price(
    symbol: str, portfolio: Portfolio, limit_price: Decimal | None = None
) -> Decimal | None:
    """The best estimate of what an order in `symbol` will transact at, or None.

    A limit price is what we would pay at worst; otherwise the last mark is the
    only number available. None means the caller cannot evaluate, and
    default-closed means it refuses.

    Public and symbol-shaped rather than order-shaped so that position sizing,
    which runs *before* an `Order` exists, prices a trade the same way the rules
    that later judge it do. Sizing against one price and validating against
    another is how an order comes out just over a limit it was sized to sit
    under, and the disagreement would be invisible — both numbers look right on
    their own.
    """
    if limit_price is not None:
        return limit_price
    position = portfolio.positions.get(symbol)
    if position is not None and position.last_price is not None:
        return position.last_price
    return None


def _price_for(order: Order, portfolio: Portfolio) -> Decimal | None:
    """`reference_price` for an order that already exists."""
    return reference_price(order.symbol, portfolio, order.limit_price)


def project_pending(portfolio: Portfolio, pending: Iterable[Order]) -> Portfolio:
    """The book as it would stand if everything already in flight filled.

    **The rules measure a settled book, and orders are approved against it one
    at a time.** `Portfolio.cash` and `Portfolio.positions` move only when a
    fill lands, so a strategy that emits forty entries in one bar has every one
    of them judged against a book that contains none of the other thirty-nine.
    Each looks like 5% of equity against a 100% ceiling; together they are 200%.
    That is not hypothetical — a 40-symbol `buy_and_hold` replay filled all
    forty, ended at 1.97x gross exposure with cash at -97,046, and the exposure
    cap refused nothing.

    Every rule that describes the *shape of the book* is affected, not only the
    two that price it: `MaxOpenPositionsRule` counts positions, so a batch of
    entries submitted at nineteen open all pass; `MaxPositionSizeRule` reads one
    symbol's quantity, so two orders in the same name each pass at 6% of a 10%
    cap. Projecting the book once, here, fixes all four without any of them
    knowing that in-flight orders exist.

    **Reductions are not credited, and that asymmetry is the point.** A resting
    protective stop would, if counted as filled, *lower* the projected exposure
    and license a position the limits would otherwise refuse — a rule reasoning
    from an exit that has not happened, which is the inversion this module's
    default-closed posture exists to prevent. `reduces_position` already draws
    that line for `DailyLossLimitRule`; it draws it here too.

    **An unpriceable order still consumes its quantity.** Given no limit price
    and no mark there is no notional to add, so the projected position carries
    the quantity and no price — which puts the symbol in
    `Portfolio.unmarked_symbols`, and `_unpriced_book` then refuses on behalf of
    every rule that prices the book. Skipping it instead would under-count, and
    under-counting is the direction that approves what it should refuse.

    Returns `portfolio` itself when nothing is in flight, so the overwhelmingly
    common single-order path copies nothing. The projection is a read-only view
    for the chain: `avg_entry_price` and the P&L fields are deliberately left as
    they are, because no rule reads them and inventing a cost basis for a fill
    that has not happened would be a worse answer than an untouched one.
    """
    committed = [
        order
        for order in pending
        if order.remaining_qty > 0 and not reduces_position(order, portfolio)
    ]
    if not committed:
        return portfolio

    positions = {symbol: replace(position) for symbol, position in portfolio.positions.items()}
    cash = portfolio.cash

    for order in committed:
        position = positions.setdefault(order.symbol, Position(symbol=order.symbol))
        signed = order.remaining_qty * order.side.sign
        position.qty += signed
        price = reference_price(order.symbol, portfolio, order.limit_price)
        if price is None:
            continue
        # Exactly what a fill does to cash (`BacktestEngine._execute`), so a
        # projected buy leaves equity unchanged and moves only the exposure —
        # the quantity every percentage limit is a percentage *of* must not
        # drift just because an order is in flight.
        cash -= signed * price
        if position.last_price is None:
            position.last_price = price

    return replace(portfolio, cash=cash, positions=positions)


def _unpriced_book(rule: str, portfolio: Portfolio) -> RiskDecision | None:
    """Refuse while any open position lacks a mark.

    `Portfolio.equity` and `gross_exposure` both treat an unmarked position as
    worth zero, so every percentage limit computed from them comes out too
    small and approves what it should refuse. An unpriced book is exactly when
    you least want to be trading.
    """
    unmarked = portfolio.unmarked_symbols
    if unmarked:
        return RiskDecision.deny(rule, f"cannot value the book: no mark for {', '.join(unmarked)}")
    return None


@dataclass(slots=True)
class KillSwitchRule:
    """Refuses everything while the platform-wide halt is engaged.

    First in the chain: when a human hits stop, nothing else should get a vote.
    """

    switch: KillSwitch
    name: str = "kill_switch"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        if self.switch.is_engaged(order.strategy_id, order.symbol):
            return RiskDecision.deny(self.name, "trading is halted")
        return RiskDecision.allow()


@dataclass(slots=True)
class MaxPositionSizeRule:
    """Caps any single position at `max_position_pct` of equity.

    Prevents one conviction — or one sizing bug — from becoming the whole book.
    Checks the position *after* this order, not the order alone; three orders of
    4% each are a 12% position.
    """

    name: str = "max_position_size"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        if (denial := _unpriced_book(self.name, portfolio)) is not None:
            return denial
        price = _price_for(order, portfolio)
        if price is None:
            return RiskDecision.deny(self.name, f"no price available for {order.symbol}")

        equity = portfolio.equity
        if equity <= 0:
            return RiskDecision.deny(self.name, f"equity is {equity}")

        held = portfolio.position(order.symbol).qty
        # The position this order *leaves behind*, not the order on its own —
        # three orders of 4% each are a 12% position.
        resulting = abs(held + order.qty * order.side.sign) * price
        ceiling = limits.max_position_pct * equity
        if resulting > ceiling:
            return RiskDecision.deny(
                self.name,
                f"{order.symbol} would be {resulting:.2f} "
                f"({resulting / equity:.1%} of equity), over the "
                f"{limits.max_position_pct:.0%} cap of {ceiling:.2f}",
            )
        return RiskDecision.allow()


@dataclass(slots=True)
class MaxExposureRule:
    """Caps gross exposure — the leverage ceiling.

    Gross, not net: a long/short book that nets to zero still borrows money and
    still loses on both legs in a correlated shock.
    """

    name: str = "max_gross_exposure"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        if (denial := _unpriced_book(self.name, portfolio)) is not None:
            return denial
        price = _price_for(order, portfolio)
        if price is None:
            return RiskDecision.deny(self.name, f"no price available for {order.symbol}")

        equity = portfolio.equity
        if equity <= 0:
            return RiskDecision.deny(self.name, f"equity is {equity}")

        held = portfolio.position(order.symbol).qty
        # Gross: every leg adds, so a short growing more short consumes the
        # ceiling exactly as a long growing more long does.
        without = portfolio.gross_exposure - abs(held) * price
        resulting = without + abs(held + order.qty * order.side.sign) * price
        ceiling = limits.max_gross_exposure_pct * equity
        if resulting > ceiling:
            return RiskDecision.deny(
                self.name,
                f"gross exposure would be {resulting:.2f} "
                f"({resulting / equity:.1%} of equity), over the "
                f"{limits.max_gross_exposure_pct:.0%} cap of {ceiling:.2f}",
            )
        return RiskDecision.allow()


@dataclass(slots=True)
class DailyLossLimitRule:
    """Halts new entries once the day's drawdown exceeds `max_daily_loss_pct`.

    Exits must still be permitted — refusing to let a losing position close
    would turn a bad day into an unbounded one. Whether an order is an exit is
    `reduces_position(order, portfolio)` in this module, not a property of the
    order: a sell is an exit when you are long and an entry when you are flat.
    """

    name: str = "daily_loss_limit"
    #: Equity at the session's open. Anchored by whoever owns the session
    #: boundary, and persisted there so a mid-session restart does not re-anchor
    #: to a drawn-down number and silently grant the day a second allowance.
    day_start_equity: Decimal | None = None

    def anchor(self, equity: Decimal) -> None:
        """Set the day's starting point. Call once, at the session open."""
        self.day_start_equity = equity

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        # First, and before any other consideration: an exit is always allowed.
        # Refusing to let a losing position close turns a bad day into an
        # unbounded one, so this outranks even the checks below that would
        # otherwise refuse for want of information.
        if reduces_position(order, portfolio):
            return RiskDecision.allow()

        if self.day_start_equity is None:
            return RiskDecision.deny(
                self.name,
                "the day's starting equity has not been anchored, so the loss "
                "limit cannot be evaluated",
            )
        if self.day_start_equity <= 0:
            return RiskDecision.deny(self.name, f"day-start equity is {self.day_start_equity}")
        if (denial := _unpriced_book(self.name, portfolio)) is not None:
            return denial

        change = (portfolio.equity - self.day_start_equity) / self.day_start_equity
        if change <= -limits.max_daily_loss_pct:
            return RiskDecision.deny(
                self.name,
                f"down {change:.2%} on the day, at or past the "
                f"{limits.max_daily_loss_pct:.0%} limit — entries are blocked, "
                f"exits are not",
            )
        return RiskDecision.allow()


@dataclass(slots=True)
class RateLimitRule:
    """Caps orders per minute.

    This is the runaway-loop guard. A strategy bug that re-emits an entry every
    tick will otherwise submit thousands of orders in a minute and empty the
    account in fees alone — this has happened to real firms.
    """

    clock: Clock
    name: str = "rate_limit"
    #: Submission times inside the trailing minute, oldest first.
    _recent: deque[datetime] = field(default_factory=deque, repr=False)

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        now = self.clock.now()
        cutoff = now - timedelta(seconds=60)
        while self._recent and self._recent[0] <= cutoff:
            self._recent.popleft()

        if len(self._recent) >= limits.max_orders_per_minute:
            return RiskDecision.deny(
                self.name,
                f"{len(self._recent)} orders in the last minute, at the limit of "
                f"{limits.max_orders_per_minute}",
            )

        # Counted on the attempt rather than on the eventual approval. A later
        # rule may still refuse this order, and that refusal does not make the
        # attempt free — a strategy looping on a rejection is the same runaway
        # this rule exists to stop.
        self._recent.append(now)
        return RiskDecision.allow()


@dataclass(slots=True)
class MaxOpenPositionsRule:
    """Caps how many symbols are held at once.

    Not a loss limit — a sprawl limit. Twenty positions is roughly what one
    person can actually watch; sixty is a book nobody is reading.
    """

    name: str = "max_open_positions"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        # Only an order that opens a symbol we do not already hold can add to
        # the count. Adding to an existing position, or closing one, never can.
        position = portfolio.positions.get(order.symbol)
        if position is not None and not position.is_flat:
            return RiskDecision.allow()

        open_count = len(portfolio.open_positions)
        if open_count >= limits.max_open_positions:
            return RiskDecision.deny(
                self.name,
                f"already holding {open_count} positions, at the limit of "
                f"{limits.max_open_positions}",
            )
        return RiskDecision.allow()


@dataclass(slots=True)
class BuyingPowerRule:
    """Rejects what the account cannot pay for, before the broker does.

    Our own rejection is cheap and diagnosable; a stream of broker rejects can
    get API access throttled.
    """

    name: str = "buying_power"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        # An order that reduces a holding returns cash rather than consuming it,
        # and refusing one for want of buying power would be perverse.
        if reduces_position(order, portfolio):
            return RiskDecision.allow()

        price = _price_for(order, portfolio)
        if price is None:
            return RiskDecision.deny(self.name, f"no price available for {order.symbol}")

        cost = order.qty * price
        if cost > portfolio.cash:
            return RiskDecision.deny(
                self.name,
                f"{order.symbol} would cost {cost:.2f} against {portfolio.cash:.2f} cash",
            )
        return RiskDecision.allow()


@dataclass(slots=True)
class TradingHoursRule:
    """Blocks orders outside the session unless explicitly extended-hours.

    A market order resting through the close fills at the open, potentially
    percentage points from where the strategy decided.
    """

    calendar: TradingCalendar
    clock: Clock
    name: str = "trading_hours"
    #: Set only for a strategy that genuinely trades the pre- and post-market
    #: and has priced the wider spreads there in.
    allow_extended_hours: bool = False

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        if self.allow_extended_hours:
            return RiskDecision.allow()
        now = self.clock.now()
        if not self.calendar.is_open(now):
            return RiskDecision.deny(
                self.name,
                f"{self.calendar.exchange} is closed at {now.isoformat()}",
            )
        return RiskDecision.allow()


@dataclass(slots=True)
class StaleDataRule:
    """Refuses to trade on a quote older than `max_age_seconds`.

    A frozen feed looks identical to a quiet market. Trading on a stale price is
    trading blind — this rule is why `StaleDataError` exists.
    """

    clock: Clock
    #: symbol → when data for it was last seen, or None if never.
    last_tick_at: Callable[[str], datetime | None]
    name: str = "stale_data"

    def check(self, order: Order, portfolio: Portfolio, limits: RiskLimits) -> RiskDecision:
        seen = self.last_tick_at(order.symbol)
        if seen is None:
            # Never having seen a price is the most stale a feed can be, and it
            # is the case a max-age comparison would silently skip.
            return RiskDecision.deny(self.name, f"no market data has arrived for {order.symbol}")

        age = (self.clock.now() - seen).total_seconds()
        # The limit lives in RiskLimits rather than on this rule, so an operator
        # can tune it without editing code.
        if age > limits.max_quote_age_seconds:
            return RiskDecision.deny(
                self.name,
                f"{order.symbol} data is {age:.0f}s old, over the "
                f"{limits.max_quote_age_seconds}s limit",
            )
        return RiskDecision.allow()


#: Methods whose value is a fraction of equity rather than a count or an amount.
FRACTIONAL_METHODS = frozenset({"equity_pct", "risk_pct", "volatility_target"})


def position_size(
    method: str,
    equity: Decimal,
    price: Decimal,
    stop_price: Decimal | None = None,
    risk_pct: Decimal = Decimal("0.01"),
    volatility: Decimal | None = None,
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

    `risk_pct` carries `PositionSizeSpec.value`, whose meaning follows `method`:
    a share count for `fixed_qty`, an amount for `fixed_notional`, a fraction of
    equity for the rest. The parameter keeps its name because the fraction is
    what it is for the default method and the one worth naming.

    `volatility` is the instrument's own volatility, in the same units as the
    target — needed only by `volatility_target`, and required there for the same
    reason a stop is required by `risk_pct`.

    **Rounded down to whole shares.** docs/RISK.md's own worked example rounds
    that way (a $15 stop on $1,000 of risk gives 66 shares, not 66.67), and
    down rather than to-nearest because a sizing function should never hand back
    more risk than it was asked for.
    """
    if equity <= 0:
        raise ValueError(f"cannot size against equity of {equity}")
    if price <= 0:
        raise ValueError(f"cannot size at a price of {price}")
    if risk_pct <= 0:
        raise ValueError(f"position size value must be positive, got {risk_pct}")

    if method == "fixed_qty":
        raw = risk_pct
    elif method == "fixed_notional":
        raw = risk_pct / price
    elif method == "equity_pct":
        raw = equity * risk_pct / price
    elif method == "risk_pct":
        if stop_price is None:
            raise ValueError(
                "risk_pct sizing needs a stop: the quantity is defined by the "
                "distance to it, and defaulting that distance would silently "
                "size every trade as though its stop were somewhere it is not"
            )
        per_share_risk = abs(price - stop_price)
        if per_share_risk == 0:
            raise ValueError(
                f"stop price {stop_price} equals the entry price, so the risk "
                f"per share is zero and the position size is unbounded"
            )
        raw = equity * risk_pct / per_share_risk
    elif method == "volatility_target":
        if volatility is None or volatility <= 0:
            raise ValueError(
                f"volatility_target sizing needs the instrument's volatility; got {volatility}"
            )
        # Scale exposure so a more volatile instrument gets proportionally less
        # of the book — the same equalising instinct as risk_pct, applied to
        # ongoing variance rather than to a single stop distance.
        raw = equity * (risk_pct / volatility) / price
    else:
        supported = "fixed_qty, fixed_notional, equity_pct, risk_pct, volatility_target"
        raise ValueError(f"unknown position sizing method {method!r}; supported: {supported}")

    return raw.to_integral_value(rounding=ROUND_DOWN)
