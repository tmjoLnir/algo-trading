"""Risk engine — each rule blocks what it should and allows what it should.

Two things are being tested, and the second is the one that bites. A rule that
fails to block is an obvious bug. A rule that blocks something it should have
allowed is the subtle one: `DailyLossLimitRule` refusing an exit would trap a
losing position and turn a bad day into an unbounded one, which is why every
rule here has both an allow case and a deny case.

The engine is deny-oriented and default-closed. Where a rule cannot evaluate —
an unmarked position, a feed that has never ticked, a day with no anchor — the
expected answer is refusal, and that is asserted rather than assumed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from atp_core.clock import SimulatedClock, TradingCalendar
from atp_core.config import RiskLimits
from atp_core.domain import Order, Portfolio, Side
from atp_core.errors import ConfigError, RiskLimitBreachedError
from atp_core.risk.engine import RiskDecision, RiskEngine, RiskRule, default_rules
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
    position_size,
    reduces_position,
)
from atp_core.strategy.rules import PositionSizeSpec
from tests.fakes import FakeKillSwitch

OPEN_HOURS = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)  # 10:00 New York, a Tuesday
CLOSED = datetime(2024, 1, 2, 2, 0, tzinfo=UTC)  # 21:00 New York the evening before


def limits(**overrides: object) -> RiskLimits:
    """Explicit values — never inherited from the environment, or a stray
    RISK_* export would quietly change what these tests assert."""
    base: dict[str, object] = {
        "max_position_pct": Decimal("0.10"),
        "max_gross_exposure_pct": Decimal("1.00"),
        "max_daily_loss_pct": Decimal("0.03"),
        "max_orders_per_minute": 30,
        "max_open_positions": 20,
        "max_quote_age_seconds": 30,
    }
    base.update(overrides)
    return RiskLimits(**base)  # type: ignore[arg-type]


def portfolio(cash: float = 100_000, **holdings: tuple[float, float]) -> Portfolio:
    """`holdings` is symbol → (qty, mark). A mark of 0 means *unmarked*."""
    book = Portfolio(cash=Decimal(str(cash)), starting_equity=Decimal(str(cash)))
    for symbol, (qty, mark) in holdings.items():
        position = book.position(symbol)
        position.qty = Decimal(str(qty))
        position.avg_entry_price = Decimal(str(mark or 100))
        position.last_price = Decimal(str(mark)) if mark else None
    return book


def order(
    symbol: str = "SPY", side: Side = Side.BUY, qty: float = 100, limit: float | None = 100
) -> Order:
    return Order(
        symbol=symbol,
        side=side,
        qty=Decimal(str(qty)),
        limit_price=Decimal(str(limit)) if limit is not None else None,
        strategy_id="test",
    )


class TestRiskRules:
    def test_kill_switch_blocks_everything(self) -> None:
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=True))
        decision = rule.check(order(), portfolio(), limits())
        assert not decision.approved
        assert decision.rule == "kill_switch"

    def test_kill_switch_allows_when_clear(self) -> None:
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=False))
        assert rule.check(order(), portfolio(), limits()).approved

    def test_max_position_counts_existing_holding(self) -> None:
        """Three 4% orders must not become a 12% position."""
        rule = MaxPositionSizeRule()
        # 100k equity, 10% cap = 10,000. Already holding 40 @ 100 = 4,000.
        book = portfolio(cash=96_000, SPY=(40, 100))
        # +40 → 8,000, still inside.
        assert rule.check(order(qty=40), book, limits()).approved
        # +80 → 12,000, over. The order alone is only 8,000 — the rule has to
        # be looking at the resulting position, not the order.
        denied = rule.check(order(qty=80), book, limits())
        assert not denied.approved
        assert "of equity" in denied.reason

    def test_max_position_allows_a_full_exit_of_an_oversized_holding(self) -> None:
        """Selling down never breaches a size cap, however big the holding."""
        book = portfolio(cash=0, SPY=(500, 100))
        assert MaxPositionSizeRule().check(order(side=Side.SELL, qty=500), book, limits()).approved

    def test_gross_exposure_counts_shorts(self) -> None:
        """A market-neutral book still consumes buying power."""
        rule = MaxExposureRule()
        # Long 400 SPY and short 400 QQQ at 100: net zero, gross 80,000.
        book = portfolio(cash=100_000, SPY=(400, 100), QQQ=(-400, 100))
        assert book.net_exposure == 0
        assert book.gross_exposure == Decimal(80_000)
        # Equity is 100k cash + 40k - 40k = 100k, so the 100% cap is 100,000.
        assert rule.check(order(qty=100), book, limits()).approved  # → 90,000
        denied = rule.check(order(qty=300), book, limits())  # → 110,000
        assert not denied.approved
        assert "gross exposure" in denied.reason

    def test_gross_exposure_treats_growing_a_short_like_growing_a_long(self) -> None:
        book = portfolio(cash=100_000, QQQ=(-900, 100))
        denied = MaxExposureRule().check(
            order(symbol="QQQ", side=Side.SELL, qty=300), book, limits()
        )
        assert not denied.approved

    def test_daily_loss_limit_blocks_entries(self) -> None:
        rule = DailyLossLimitRule()
        rule.anchor(Decimal(100_000))
        # Down 4% on the day, past the 3% limit.
        assert not rule.check(order(), portfolio(cash=96_000), limits()).approved

    def test_daily_loss_limit_allows_exits(self) -> None:
        """Critical: blocking an exit traps you in a losing position and turns
        a bad day into an unbounded one."""
        rule = DailyLossLimitRule()
        rule.anchor(Decimal(100_000))
        book = portfolio(cash=50_000, SPY=(100, 100))  # equity 60,000 — down 40%
        exit_order = order(side=Side.SELL, qty=100)
        assert reduces_position(exit_order, book)
        assert rule.check(exit_order, book, limits()).approved

    def test_daily_loss_limit_allows_entries_while_inside_the_limit(self) -> None:
        rule = DailyLossLimitRule()
        rule.anchor(Decimal(100_000))
        assert rule.check(order(), portfolio(cash=98_000), limits()).approved

    def test_daily_loss_limit_denies_when_the_day_is_not_anchored(self) -> None:
        """Default-closed. An unanchored day cannot be evaluated, and guessing
        the anchor is how the day quietly gets a second allowance."""
        denied = DailyLossLimitRule().check(order(), portfolio(), limits())
        assert not denied.approved
        assert "anchored" in denied.reason

    def test_daily_loss_limit_still_allows_an_exit_with_no_anchor(self) -> None:
        """The exit carve-out outranks the refusal to evaluate — an unanchored
        day must not be able to trap a position either."""
        book = portfolio(cash=0, SPY=(100, 100))
        assert DailyLossLimitRule().check(order(side=Side.SELL, qty=100), book, limits()).approved

    def test_rate_limit_stops_runaway_loop(self) -> None:
        clock = SimulatedClock(OPEN_HOURS)
        rule = RateLimitRule(clock=clock)
        capped = limits(max_orders_per_minute=3)
        for _ in range(3):
            assert rule.check(order(), portfolio(), capped).approved
        denied = rule.check(order(), portfolio(), capped)
        assert not denied.approved
        assert "last minute" in denied.reason

    def test_rate_limit_window_slides(self) -> None:
        clock = SimulatedClock(OPEN_HOURS)
        rule = RateLimitRule(clock=clock)
        capped = limits(max_orders_per_minute=2)
        assert rule.check(order(), portfolio(), capped).approved
        assert rule.check(order(), portfolio(), capped).approved
        assert not rule.check(order(), portfolio(), capped).approved
        clock.set(OPEN_HOURS + timedelta(seconds=61))
        assert rule.check(order(), portfolio(), capped).approved

    def test_stale_quote_blocks_order(self) -> None:
        clock = SimulatedClock(OPEN_HOURS)
        rule = StaleDataRule(
            clock=clock, last_tick_at=lambda _s: OPEN_HOURS - timedelta(seconds=45)
        )
        denied = rule.check(order(), portfolio(), limits())
        assert not denied.approved
        assert "old" in denied.reason

    def test_fresh_quote_passes(self) -> None:
        clock = SimulatedClock(OPEN_HOURS)
        rule = StaleDataRule(clock=clock, last_tick_at=lambda _s: OPEN_HOURS - timedelta(seconds=5))
        assert rule.check(order(), portfolio(), limits()).approved

    def test_a_feed_that_never_ticked_is_the_stalest_of_all(self) -> None:
        """The case a bare max-age comparison skips: there is no timestamp to
        be older than the limit."""
        rule = StaleDataRule(clock=SimulatedClock(OPEN_HOURS), last_tick_at=lambda _s: None)
        denied = rule.check(order(), portfolio(), limits())
        assert not denied.approved
        assert "no market data" in denied.reason

    def test_trading_hours_blocks_outside_the_session(self) -> None:
        rule = TradingHoursRule(calendar=TradingCalendar(), clock=SimulatedClock(CLOSED))
        denied = rule.check(order(), portfolio(), limits())
        assert not denied.approved
        assert "closed" in denied.reason

    def test_trading_hours_allows_inside_the_session(self) -> None:
        rule = TradingHoursRule(calendar=TradingCalendar(), clock=SimulatedClock(OPEN_HOURS))
        assert rule.check(order(), portfolio(), limits()).approved

    def test_extended_hours_strategies_are_not_blocked(self) -> None:
        rule = TradingHoursRule(
            calendar=TradingCalendar(),
            clock=SimulatedClock(CLOSED),
            allow_extended_hours=True,
        )
        assert rule.check(order(), portfolio(), limits()).approved

    def test_buying_power_rejects_what_cannot_be_paid_for(self) -> None:
        denied = BuyingPowerRule().check(order(qty=100, limit=100), portfolio(cash=5_000), limits())
        assert not denied.approved
        assert "cash" in denied.reason

    def test_buying_power_never_blocks_a_sale(self) -> None:
        """A sale returns cash. Refusing one for want of buying power would be
        perverse, and would strand a position in an account with no cash."""
        book = portfolio(cash=0, SPY=(100, 100))
        assert BuyingPowerRule().check(order(side=Side.SELL, qty=100), book, limits()).approved

    def test_max_open_positions_blocks_a_new_symbol_at_the_limit(self) -> None:
        holdings = {f"S{i}": (10.0, 100.0) for i in range(3)}
        book = portfolio(cash=100_000, **holdings)
        capped = limits(max_open_positions=3)
        denied = MaxOpenPositionsRule().check(order(symbol="NEW"), book, capped)
        assert not denied.approved
        # ...but adding to one already held does not increase the count.
        assert MaxOpenPositionsRule().check(order(symbol="S1"), book, capped).approved

    def test_rule_that_cannot_evaluate_denies(self) -> None:
        """Default-closed: an unpriced position is when you least want to trade.

        An unmarked holding is valued at zero, so equity and gross exposure both
        come out too *small* — and a naive rule would compute a smaller
        percentage and approve. The inversion is the whole point of this test.
        """
        book = portfolio(cash=100_000, SPY=(1_000, 0))  # held, no mark
        assert book.unmarked_symbols == ["SPY"]

        for rule in (MaxPositionSizeRule(), MaxExposureRule()):
            decision = rule.check(order(symbol="QQQ"), book, limits())
            assert not decision.approved, f"{rule.name} approved an unpriced book"
            assert "no mark" in decision.reason

    def test_no_price_anywhere_denies(self) -> None:
        """A market order on a symbol that has never printed has no price to
        measure against."""
        denied = MaxPositionSizeRule().check(order(limit=None), portfolio(), limits())
        assert not denied.approved
        assert "no price" in denied.reason


class TestReducesPosition:
    def test_a_sell_is_an_exit_only_when_long(self) -> None:
        long_book = portfolio(SPY=(100, 100))
        assert reduces_position(order(side=Side.SELL), long_book)
        assert not reduces_position(order(side=Side.BUY), long_book)

    def test_a_buy_is_an_exit_when_short(self) -> None:
        short_book = portfolio(SPY=(-100, 100))
        assert reduces_position(order(side=Side.BUY), short_book)
        assert not reduces_position(order(side=Side.SELL), short_book)

    def test_nothing_reduces_a_flat_position(self) -> None:
        assert not reduces_position(order(side=Side.SELL), portfolio())

    def test_an_order_that_flips_through_zero_still_reduces(self) -> None:
        """It closes the position on its way past, and refusing it would trap
        the very holding the limit is trying to release."""
        assert reduces_position(order(side=Side.SELL, qty=300), portfolio(SPY=(100, 100)))


class TestChain:
    def _rules(self, engaged: bool = False) -> list[RiskRule]:
        return default_rules(
            kill_switch=FakeKillSwitch(engaged),
            clock=SimulatedClock(OPEN_HOURS),
            calendar=TradingCalendar(),
            last_tick_at=lambda _s: OPEN_HOURS,
        )

    def _anchored_rules(
        self, engaged: bool = False, anchor: Decimal = Decimal(100_000)
    ) -> list[RiskRule]:
        rules = self._rules(engaged)
        for rule in rules:
            if isinstance(rule, DailyLossLimitRule):
                rule.anchor(anchor)
        return rules

    def _chain(self, engaged: bool = False, *, anchored: bool = True) -> RiskEngine:
        rules = self._anchored_rules(engaged) if anchored else self._rules(engaged)
        return RiskEngine(limits(), rules=rules)

    def test_the_chain_refuses_entries_until_the_day_is_anchored(self) -> None:
        """Not an oversight — the point. A chain assembled and left unanchored
        cannot evaluate the loss limit, and default-closed means it refuses
        rather than trading blind. Whoever builds the chain owns anchoring it
        at the session open."""
        decision = self._chain(anchored=False).validate(order(qty=10), portfolio())
        assert not decision.approved
        assert decision.rule == "daily_loss_limit"

    def test_default_chain_has_all_nine_rules_kill_switch_first(self) -> None:
        names = [r.name for r in self._chain().rules]
        assert len(names) == 9
        assert names[0] == "kill_switch"
        assert set(names) == {
            "kill_switch",
            "trading_hours",
            "rate_limit",
            "stale_data",
            "max_position_size",
            "max_gross_exposure",
            "max_open_positions",
            "daily_loss_limit",
            "buying_power",
        }

    def test_the_first_denial_wins_and_it_is_the_most_fundamental_one(self) -> None:
        """A halted platform says "trading is halted", not "insufficient buying
        power", even when both are true. The reason a human reads has to be the
        one that actually matters."""
        engine = self._chain(engaged=True)
        decision = engine.validate(order(qty=10_000, limit=100), portfolio(cash=1))
        assert not decision.approved
        assert decision.rule == "kill_switch"

    def test_an_empty_chain_approves_but_must_be_asked_for(self) -> None:
        assert RiskEngine(limits(), rules=[]).validate(order(), portfolio()).approved
        with pytest.raises(ConfigError, match="explicit rule chain"):
            RiskEngine(limits())

    def test_validate_or_raise(self) -> None:
        engine = self._chain(engaged=True)
        with pytest.raises(RiskLimitBreachedError, match="kill_switch"):
            engine.validate_or_raise(order(), portfolio())
        self._chain().validate_or_raise(order(qty=10, limit=100), portfolio())

    def test_a_shrink_is_applied_and_later_rules_see_the_smaller_order(self) -> None:
        """Otherwise a 50,000 exposure cap gets measured against an order a
        previous rule already cut to 5,000, and refuses trades that are inside
        every limit."""
        seen: list[Decimal] = []

        class Shrinker:
            name = "shrinker"

            def check(self, o: Order, p: Portfolio, l: RiskLimits) -> RiskDecision:  # noqa: E741
                return RiskDecision.shrink(self.name, "too big", Decimal(10))

        class Observer:
            name = "observer"

            def check(self, o: Order, p: Portfolio, l: RiskLimits) -> RiskDecision:  # noqa: E741
                seen.append(o.qty)
                return RiskDecision.allow()

        engine = RiskEngine(limits(), rules=[Shrinker(), Observer()])
        placed = order(qty=100)
        decision = engine.validate(placed, portfolio())

        assert decision.approved
        assert decision.adjusted_qty == Decimal(10)
        assert seen == [Decimal(10)]
        assert placed.qty == Decimal(10)

    def test_a_shrink_to_nothing_is_not_a_shrink(self) -> None:
        with pytest.raises(ValueError, match="leave something to trade"):
            RiskDecision.shrink("r", "why", Decimal(0))

    @pytest.mark.parametrize(
        ("rule_name", "build"),
        [
            # 20,000 into one symbol against a 10% cap on 100k.
            (
                "max_position_size",
                lambda: (order(qty=200, limit=100), portfolio(cash=100_000), limits(), None),
            ),
            # Ten symbols already at 10,000 each: no single position breaches
            # the 10% cap, but an eleventh takes gross past 100% of equity.
            (
                "max_gross_exposure",
                lambda: (
                    order(qty=100, limit=100),
                    portfolio(cash=0, **{f"S{i}": (100.0, 100.0) for i in range(10)}),
                    limits(),
                    None,
                ),
            ),
            (
                "max_open_positions",
                lambda: (
                    order(symbol="NEW", qty=1, limit=100),
                    portfolio(cash=100_000, **{f"S{i}": (1.0, 100.0) for i in range(20)}),
                    limits(),
                    None,
                ),
            ),
            (
                "daily_loss_limit",
                lambda: (order(qty=10), portfolio(cash=90_000), limits(), Decimal(100_000)),
            ),
            # Needs a gross cap above 100% to be reachable at all — see the
            # test below this one.
            (
                "buying_power",
                lambda: (
                    order(qty=10, limit=100),
                    portfolio(cash=50, QQQ=(500, 100)),
                    limits(max_gross_exposure_pct=Decimal(2)),
                    None,
                ),
            ),
        ],
    )
    def test_each_limit_is_refused_by_its_own_rule(self, rule_name: str, build: object) -> None:
        """The first half of the phase's proposed *Verifiable:* line. Breaching
        one limit must be attributed to the rule that owns it — a rejection
        blamed on the wrong rule sends whoever reads it to the wrong config.
        """
        placed, book, rule_limits, anchor = build()  # type: ignore[operator]
        # Anchor the day to the book itself unless the case is about the loss
        # limit, so that every other case breaches exactly one thing.
        rules = self._anchored_rules(anchor=anchor if anchor is not None else book.equity)
        engine = RiskEngine(rule_limits, rules=rules)
        decision = engine.validate(placed, book)
        assert not decision.approved
        assert decision.rule == rule_name, f"blamed {decision.rule!r}, expected {rule_name!r}"

    def test_buying_power_is_unreachable_on_a_long_only_book_at_100_percent_gross(self) -> None:
        """Worth pinning, because it looks like a gap and is not.

        For a long-only book, equity is cash plus the value of the positions, so
        the headroom under a 100% gross cap is *exactly* the cash. Gross runs
        before buying power and the two bind identically, so buying power only
        becomes the operative limit under margin or with shorts — where equity
        and gross come apart.

        If someone later reorders the chain and this starts failing, the
        question to ask is whether buying power should run first, not whether
        this test is wrong.
        """
        book = portfolio(cash=50, QQQ=(500, 100))
        placed = order(qty=10, limit=100)

        flat_day = book.equity
        at_100 = RiskEngine(limits(), rules=self._anchored_rules(anchor=flat_day)).validate(
            placed, book
        )
        assert at_100.rule == "max_gross_exposure"

        on_margin = RiskEngine(
            limits(max_gross_exposure_pct=Decimal(2)),
            rules=self._anchored_rules(anchor=flat_day),
        ).validate(placed, book)
        assert on_margin.rule == "buying_power"

    def test_no_configuration_of_the_chain_can_refuse_an_exit(self) -> None:
        """The second half, and the clause worth failing a build over. Against
        a book that breaches every limit at once — down 40% on the day, no cash,
        an oversized position — the order that closes it must still pass."""
        book = portfolio(cash=0, SPY=(500, 100))
        rules = self._rules()
        for rule in rules:
            if isinstance(rule, DailyLossLimitRule):
                rule.anchor(Decimal(1_000_000))  # a catastrophic day

        engine = RiskEngine(limits(), rules=rules)
        exit_order = order(side=Side.SELL, qty=500)
        assert reduces_position(exit_order, book)
        assert engine.validate(exit_order, book).approved

        # And the same book refuses an entry, so the pass above is the exit
        # carve-out rather than a chain that approves everything.
        assert not engine.validate(order(side=Side.BUY, qty=500), book).approved


EQUITY = Decimal(100_000)


class TestPositionSizing:
    def test_risk_pct_equalises_risk_not_notional(self) -> None:
        """$100k equity, 1% risk: $50 entry/$48 stop → 500 shares;
        $50 entry/$35 stop → 66 shares. Both lose $1,000 if stopped."""
        tight = position_size("risk_pct", EQUITY, Decimal(50), Decimal(48), Decimal("0.01"))
        wide = position_size("risk_pct", EQUITY, Decimal(50), Decimal(35), Decimal("0.01"))

        assert tight == Decimal(500)
        assert wide == Decimal(66)

        # The whole point, stated as the invariant rather than the quantities:
        # both lose about the same if stopped, despite one being 7.5x the other
        # in notional. Rounding down costs the wide one at most one share of it.
        assert tight * Decimal(2) == Decimal(1_000)
        assert Decimal(985) <= wide * Decimal(15) <= Decimal(1_000)

        # Under fixed notional the volatile name would get the same $25,000 and
        # lose $7,500 on the same stop — precisely backwards.
        by_notional = position_size("fixed_notional", EQUITY, Decimal(50), risk_pct=Decimal(25_000))
        assert by_notional * Decimal(15) == Decimal(7_500)

    def test_risk_pct_without_stop_raises(self) -> None:
        """Undefined — must raise rather than silently defaulting."""
        with pytest.raises(ValueError, match="needs a stop"):
            position_size("risk_pct", EQUITY, Decimal(50))

    def test_a_stop_at_the_entry_price_raises(self) -> None:
        """Zero risk per share makes the position unbounded, and a division by
        zero here would surface as an inscrutable traceback rather than the
        configuration error it is."""
        with pytest.raises(ValueError, match="risk per share is zero"):
            position_size("risk_pct", EQUITY, Decimal(50), Decimal(50))

    def test_fixed_qty_is_the_value_itself(self) -> None:
        assert position_size("fixed_qty", EQUITY, Decimal(50), risk_pct=Decimal(250)) == 250

    def test_fixed_notional_divides_by_price(self) -> None:
        assert position_size(
            "fixed_notional", EQUITY, Decimal(50), risk_pct=Decimal(10_000)
        ) == Decimal(200)

    def test_equity_pct_is_volatility_blind(self) -> None:
        """5% of $100k at $50 is 100 shares whatever the instrument does — the
        documented weakness of the method, pinned so it is not mistaken for a
        bug later."""
        assert position_size(
            "equity_pct", EQUITY, Decimal(50), risk_pct=Decimal("0.05")
        ) == Decimal(100)

    def test_volatility_target_shrinks_the_more_volatile_name(self) -> None:
        """Same target, twice the volatility, half the position."""
        calm = position_size(
            "volatility_target",
            EQUITY,
            Decimal(50),
            risk_pct=Decimal("0.10"),
            volatility=Decimal("0.20"),
        )
        wild = position_size(
            "volatility_target",
            EQUITY,
            Decimal(50),
            risk_pct=Decimal("0.10"),
            volatility=Decimal("0.40"),
        )
        assert calm == Decimal(1_000)
        assert wild == Decimal(500)

    def test_volatility_target_without_volatility_raises(self) -> None:
        """Same shape as risk_pct without a stop: the input the method is
        defined by is missing, so it refuses rather than inventing one."""
        with pytest.raises(ValueError, match="needs the instrument's volatility"):
            position_size("volatility_target", EQUITY, Decimal(50), risk_pct=Decimal("0.10"))

    def test_sizes_round_down_never_up(self) -> None:
        """A sizing function must not hand back more risk than it was asked
        for. 66.67 becomes 66, which is also what docs/RISK.md's own worked
        example does."""
        assert position_size(
            "fixed_notional", EQUITY, Decimal(3), risk_pct=Decimal(100)
        ) == Decimal(33)

    def test_unknown_method_names_the_supported_ones(self) -> None:
        with pytest.raises(ValueError, match="unknown position sizing method"):
            position_size("kelly", EQUITY, Decimal(50))

    @pytest.mark.parametrize(
        ("equity", "price", "value", "match"),
        [
            (Decimal(0), Decimal(50), Decimal("0.01"), "equity"),
            (Decimal(100), Decimal(0), Decimal("0.01"), "price"),
            (Decimal(100), Decimal(50), Decimal(0), "must be positive"),
        ],
    )
    def test_degenerate_inputs_raise(
        self, equity: Decimal, price: Decimal, value: Decimal, match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            position_size("equity_pct", equity, price, risk_pct=value)


class TestPositionSizeSpecBounds:
    """Config-time validation. A misplaced decimal point is the mistake worth
    catching here rather than at trade 3."""

    def test_a_sane_risk_pct_is_accepted(self) -> None:
        assert PositionSizeSpec(type="risk_pct", value=Decimal("0.01")).value == Decimal("0.01")

    def test_a_fraction_above_one_is_refused(self) -> None:
        with pytest.raises(ValidationError, match=re.escape("0.01 is 1%, not 1")):
            PositionSizeSpec(type="risk_pct", value=Decimal(2))

    def test_risk_per_trade_past_the_backstop_is_refused(self) -> None:
        """0.95 was the audit's example of what nothing rejected."""
        with pytest.raises(ValidationError, match="losing streak"):
            PositionSizeSpec(type="risk_pct", value=Decimal("0.95"))

    def test_a_share_count_is_not_bounded_like_a_fraction(self) -> None:
        """500 shares is ordinary; 500 as a risk fraction would not be."""
        assert PositionSizeSpec(type="fixed_qty", value=Decimal(500)).value == Decimal(500)

    def test_a_non_positive_value_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            PositionSizeSpec(type="fixed_qty", value=Decimal(0))
