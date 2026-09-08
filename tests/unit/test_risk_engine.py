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
from atp_core.domain import Order, OrderType, Portfolio, Side
from atp_core.errors import ConfigError, RiskLimitBreachedError
from atp_core.risk.engine import RiskBooks, RiskDecision, RiskEngine, RiskRule, default_rules
from atp_core.risk.killswitch import HaltReason, HaltRecord, HaltScope, HaltState
from atp_core.risk.limits import MAX_GROSS_CEILING, RiskLimits
from atp_core.risk.rules import (
    EXIT_BLIND_RULES,
    BuyingPowerRule,
    DailyLossLimitRule,
    KillSwitchRule,
    MaxExposureRule,
    MaxOpenPositionsRule,
    MaxPositionSizeRule,
    RateLimitRule,
    StaleDataRule,
    TradingHoursRule,
    closes_without_reversing,
    in_flight_by_symbol,
    position_size,
    project_pending,
    reduces_position,
    worst_resulting_qty,
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


def halted(reason: HaltReason = HaltReason.MANUAL, **unproven: tuple[str, ...]) -> FakeKillSwitch:
    """A halted switch, optionally carrying evidence.

    `unproven` is target → symbols, where the target is a symbol-scoped halt's
    target or `GLOBAL` for the global one, so a test can put the impugnment on
    whichever halt it means to.
    """
    switch = FakeKillSwitch()
    switch.engage(HaltScope.GLOBAL, reason, "test", unproven_symbols=unproven.pop("GLOBAL", ()))
    for target, symbols in unproven.items():
        switch.engage(HaltScope.SYMBOL, reason, "test", target=target, unproven_symbols=symbols)
    return switch


class UnreadableKillSwitch:
    """Redis is down: engaged, and knows nothing about any position."""

    def halt_state(self, strategy_id: str | None = None, symbol: str | None = None) -> HaltState:
        return HaltState(unreadable=True)

    def engage(self, *args: object, **kwargs: object) -> HaltRecord:  # pragma: no cover
        raise AssertionError("a rule never engages")

    def clear(self, *args: object, **kwargs: object) -> HaltRecord | None:  # pragma: no cover
        raise AssertionError("a rule never clears")

    def active_halts(self) -> list[HaltRecord]:  # pragma: no cover
        return []


class TestRiskRules:
    def test_kill_switch_blocks_an_entry(self) -> None:
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=True))
        decision = rule.check(order(), RiskBooks.of(portfolio()), limits())
        assert not decision.approved
        assert decision.rule == "kill_switch"

    def test_kill_switch_allows_when_clear(self) -> None:
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=False))
        assert rule.check(order(), RiskBooks.of(portfolio()), limits()).approved

    def test_kill_switch_blocks_adding_to_a_position_it_already_holds(self) -> None:
        """Halted, "I already own some" is not a reason to buy more."""
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=True))
        decision = rule.check(
            order(side=Side.BUY, qty=50), RiskBooks.of(portfolio(SPY=(100, 100))), limits()
        )
        assert not decision.approved
        assert decision.rule == "kill_switch"

    def test_kill_switch_lets_an_exit_out_of_a_long(self) -> None:
        """docs/SAFETY.md: "Halting stops new risk; flattening realises existing
        P&L." A halt that refused exits did both, and day 1 of the paper week
        survived 2h37m of one only because the book was empty throughout
        (docs/paper-week/day-1-review.md, F3)."""
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=True))
        decision = rule.check(
            order(side=Side.SELL, qty=40), RiskBooks.of(portfolio(SPY=(100, 100))), limits()
        )
        assert decision.approved

    def test_kill_switch_lets_an_exit_out_of_a_short(self) -> None:
        """Whether an order is an exit is not a property of its side."""
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=True))
        decision = rule.check(
            order(side=Side.BUY, qty=40), RiskBooks.of(portfolio(SPY=(-100, 100))), limits()
        )
        assert decision.approved

    def test_kill_switch_lets_an_exactly_sized_flatten_out(self) -> None:
        """`OrderRouter.flatten` sizes at exactly `abs(position.qty)`, so the
        boundary is the case that matters rather than an edge nobody hits."""
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=True))
        decision = rule.check(
            order(side=Side.SELL, qty=100), RiskBooks.of(portfolio(SPY=(100, 100))), limits()
        )
        assert decision.approved

    def test_kill_switch_refuses_an_exit_that_would_reverse_the_position(self) -> None:
        """The carve-out permits reducing, not trading, and this is where the
        difference is load-bearing.

        `reduces_position` counts an order larger than the position it opposes,
        deliberately, so `DailyLossLimitRule` cannot trap a holding it is trying
        to release. A halt cannot afford that reading: selling 250 against a long
        of 100 closes the long and *opens a short of 150* — new risk, taken while
        the platform is stopped. `KillSwitchRule` is stricter than the helper it
        shares for exactly this case, and nothing legitimate is refused by it.
        """
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=True))
        book = portfolio(SPY=(100, 100))
        # The premise: the shared helper does call this a reduction.
        assert reduces_position(order(side=Side.SELL, qty=250), book)

        decision = rule.check(order(side=Side.SELL, qty=250), RiskBooks.of(book), limits())

        assert not decision.approved
        assert decision.rule == "kill_switch"
        assert "reverse" in decision.reason

    def test_kill_switch_lets_a_protective_stop_through(self) -> None:
        """The failure this carve-out exists for, in its own test.

        docs/SAFETY.md layer 5 is "broker-side stops on every position" and it
        fails when the stop is "never placed after the entry fill". An entry
        that filled just before a halt had its protective child refused here, so
        the position ended up with no stop anywhere — layers 6 and 5 failing
        together. The stop is capped at the exposure held, so it can never trip
        the reversal guard above.
        """
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=True))
        stop = Order(
            symbol="SPY",
            side=Side.SELL,
            qty=Decimal(100),
            order_type=OrderType.STOP,
            stop_price=Decimal(95),
            strategy_id="test",
            parent_order_id="the-entry",
            purpose="stop_loss",
        )

        assert rule.check(stop, RiskBooks.of(portfolio(SPY=(100, 100))), limits()).approved

    def test_kill_switch_refuses_an_exit_in_a_symbol_it_does_not_hold(self) -> None:
        """Nothing to reduce is not a reduction. A sell into a flat book is a
        short entry, whatever the caller meant by it."""
        rule = KillSwitchRule(switch=FakeKillSwitch(engaged=True))
        decision = rule.check(order(side=Side.SELL, qty=100), RiskBooks.of(portfolio()), limits())
        assert not decision.approved

    def test_max_position_counts_existing_holding(self) -> None:
        """Three 4% orders must not become a 12% position."""
        rule = MaxPositionSizeRule()
        # 100k equity, 10% cap = 10,000. Already holding 40 @ 100 = 4,000.
        book = portfolio(cash=96_000, SPY=(40, 100))
        # +40 → 8,000, still inside.
        assert rule.check(order(qty=40), RiskBooks.of(book), limits()).approved
        # +80 → 12,000, over. The order alone is only 8,000 — the rule has to
        # be looking at the resulting position, not the order.
        denied = rule.check(order(qty=80), RiskBooks.of(book), limits())
        assert not denied.approved
        assert "of equity" in denied.reason

    def test_max_position_allows_a_full_exit_of_an_oversized_holding(self) -> None:
        """Selling down never breaches a size cap, however big the holding."""
        book = portfolio(cash=0, SPY=(500, 100))
        assert (
            MaxPositionSizeRule()
            .check(order(side=Side.SELL, qty=500), RiskBooks.of(book), limits())
            .approved
        )

    def test_gross_exposure_counts_shorts(self) -> None:
        """A market-neutral book still consumes buying power."""
        rule = MaxExposureRule()
        # Long 400 SPY and short 400 QQQ at 100: net zero, gross 80,000.
        book = portfolio(cash=100_000, SPY=(400, 100), QQQ=(-400, 100))
        assert book.net_exposure == 0
        assert book.gross_exposure == Decimal(80_000)
        # Equity is 100k cash + 40k - 40k = 100k, so the 100% cap is 100,000.
        assert rule.check(order(qty=100), RiskBooks.of(book), limits()).approved  # → 90,000
        denied = rule.check(order(qty=300), RiskBooks.of(book), limits())  # → 110,000
        assert not denied.approved
        assert "gross exposure" in denied.reason

    def test_gross_exposure_treats_growing_a_short_like_growing_a_long(self) -> None:
        book = portfolio(cash=100_000, QQQ=(-900, 100))
        denied = MaxExposureRule().check(
            order(symbol="QQQ", side=Side.SELL, qty=300), RiskBooks.of(book), limits()
        )
        assert not denied.approved

    def test_daily_loss_limit_blocks_entries(self) -> None:
        rule = DailyLossLimitRule()
        rule.anchor(Decimal(100_000))
        # Down 4% on the day, past the 3% limit.
        assert not rule.check(order(), RiskBooks.of(portfolio(cash=96_000)), limits()).approved

    def test_daily_loss_limit_allows_exits(self) -> None:
        """Critical: blocking an exit traps you in a losing position and turns
        a bad day into an unbounded one."""
        rule = DailyLossLimitRule()
        rule.anchor(Decimal(100_000))
        book = portfolio(cash=50_000, SPY=(100, 100))  # equity 60,000 — down 40%
        exit_order = order(side=Side.SELL, qty=100)
        assert reduces_position(exit_order, book)
        assert rule.check(exit_order, RiskBooks.of(book), limits()).approved

    def test_daily_loss_limit_allows_entries_while_inside_the_limit(self) -> None:
        rule = DailyLossLimitRule()
        rule.anchor(Decimal(100_000))
        assert rule.check(order(), RiskBooks.of(portfolio(cash=98_000)), limits()).approved

    def test_daily_loss_limit_denies_when_the_day_is_not_anchored(self) -> None:
        """Default-closed. An unanchored day cannot be evaluated, and guessing
        the anchor is how the day quietly gets a second allowance."""
        denied = DailyLossLimitRule().check(order(), RiskBooks.of(portfolio()), limits())
        assert not denied.approved
        assert "anchored" in denied.reason

    def test_daily_loss_limit_still_allows_an_exit_with_no_anchor(self) -> None:
        """The exit carve-out outranks the refusal to evaluate — an unanchored
        day must not be able to trap a position either."""
        book = portfolio(cash=0, SPY=(100, 100))
        assert (
            DailyLossLimitRule()
            .check(order(side=Side.SELL, qty=100), RiskBooks.of(book), limits())
            .approved
        )

    def test_rate_limit_stops_runaway_loop(self) -> None:
        clock = SimulatedClock(OPEN_HOURS)
        rule = RateLimitRule(clock=clock)
        capped = limits(max_orders_per_minute=3)
        for _ in range(3):
            assert rule.check(order(), RiskBooks.of(portfolio()), capped).approved
        denied = rule.check(order(), RiskBooks.of(portfolio()), capped)
        assert not denied.approved
        assert "last minute" in denied.reason

    def test_rate_limit_window_slides(self) -> None:
        clock = SimulatedClock(OPEN_HOURS)
        rule = RateLimitRule(clock=clock)
        capped = limits(max_orders_per_minute=2)
        assert rule.check(order(), RiskBooks.of(portfolio()), capped).approved
        assert rule.check(order(), RiskBooks.of(portfolio()), capped).approved
        assert not rule.check(order(), RiskBooks.of(portfolio()), capped).approved
        clock.set(OPEN_HOURS + timedelta(seconds=61))
        assert rule.check(order(), RiskBooks.of(portfolio()), capped).approved

    def test_stale_quote_blocks_order(self) -> None:
        clock = SimulatedClock(OPEN_HOURS)
        rule = StaleDataRule(
            clock=clock, last_tick_at=lambda _s: OPEN_HOURS - timedelta(seconds=45)
        )
        denied = rule.check(order(), RiskBooks.of(portfolio()), limits())
        assert not denied.approved
        assert "old" in denied.reason

    def test_fresh_quote_passes(self) -> None:
        clock = SimulatedClock(OPEN_HOURS)
        rule = StaleDataRule(clock=clock, last_tick_at=lambda _s: OPEN_HOURS - timedelta(seconds=5))
        assert rule.check(order(), RiskBooks.of(portfolio()), limits()).approved

    def test_a_feed_that_never_ticked_is_the_stalest_of_all(self) -> None:
        """The case a bare max-age comparison skips: there is no timestamp to
        be older than the limit."""
        rule = StaleDataRule(clock=SimulatedClock(OPEN_HOURS), last_tick_at=lambda _s: None)
        denied = rule.check(order(), RiskBooks.of(portfolio()), limits())
        assert not denied.approved
        assert "no market data" in denied.reason

    def test_trading_hours_blocks_outside_the_session(self) -> None:
        rule = TradingHoursRule(calendar=TradingCalendar(), clock=SimulatedClock(CLOSED))
        denied = rule.check(order(), RiskBooks.of(portfolio()), limits())
        assert not denied.approved
        assert "closed" in denied.reason

    def test_trading_hours_allows_inside_the_session(self) -> None:
        rule = TradingHoursRule(calendar=TradingCalendar(), clock=SimulatedClock(OPEN_HOURS))
        assert rule.check(order(), RiskBooks.of(portfolio()), limits()).approved

    def test_extended_hours_strategies_are_not_blocked(self) -> None:
        rule = TradingHoursRule(
            calendar=TradingCalendar(),
            clock=SimulatedClock(CLOSED),
            allow_extended_hours=True,
        )
        assert rule.check(order(), RiskBooks.of(portfolio()), limits()).approved

    def test_buying_power_rejects_what_cannot_be_paid_for(self) -> None:
        denied = BuyingPowerRule().check(
            order(qty=100, limit=100), RiskBooks.of(portfolio(cash=5_000)), limits()
        )
        assert not denied.approved
        assert "cash" in denied.reason

    def test_buying_power_never_blocks_a_sale(self) -> None:
        """A sale returns cash. Refusing one for want of buying power would be
        perverse, and would strand a position in an account with no cash."""
        book = portfolio(cash=0, SPY=(100, 100))
        assert (
            BuyingPowerRule()
            .check(order(side=Side.SELL, qty=100), RiskBooks.of(book), limits())
            .approved
        )

    def test_max_open_positions_blocks_a_new_symbol_at_the_limit(self) -> None:
        holdings = {f"S{i}": (10.0, 100.0) for i in range(3)}
        book = portfolio(cash=100_000, **holdings)
        capped = limits(max_open_positions=3)
        denied = MaxOpenPositionsRule().check(order(symbol="NEW"), RiskBooks.of(book), capped)
        assert not denied.approved
        # ...but adding to one already held does not increase the count.
        assert MaxOpenPositionsRule().check(order(symbol="S1"), RiskBooks.of(book), capped).approved

    def test_rule_that_cannot_evaluate_denies(self) -> None:
        """Default-closed: an unpriced position is when you least want to trade.

        An unmarked holding is valued at zero, so equity and gross exposure both
        come out too *small* — and a naive rule would compute a smaller
        percentage and approve. The inversion is the whole point of this test.
        """
        book = portfolio(cash=100_000, SPY=(1_000, 0))  # held, no mark
        assert book.unmarked_symbols == ["SPY"]

        for rule in (MaxPositionSizeRule(), MaxExposureRule()):
            decision = rule.check(order(symbol="QQQ"), RiskBooks.of(book), limits())
            assert not decision.approved, f"{rule.name} approved an unpriced book"
            assert "no mark" in decision.reason

    def test_no_price_anywhere_denies(self) -> None:
        """A market order on a symbol that has never printed has no price to
        measure against."""
        denied = MaxPositionSizeRule().check(order(limit=None), RiskBooks.of(portfolio()), limits())
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

            def check(self, o: Order, b: RiskBooks, l: RiskLimits) -> RiskDecision:  # noqa: E741
                return RiskDecision.shrink(self.name, "too big", Decimal(10))

        class Observer:
            name = "observer"

            def check(self, o: Order, b: RiskBooks, l: RiskLimits) -> RiskDecision:  # noqa: E741
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


class TestInFlightOrdersCountAgainstTheLimits:
    """A batch must not collectively breach a limit none of its orders breaches.

    The book moves when a fill lands, so an order approved and not yet filled is
    invisible to the next one. Every rule that describes the shape of the book
    reads settled state, so forty entries submitted in one bar are each measured
    against a book holding none of the other thirty-nine: each is 5% of equity
    against a 100% ceiling, and together they are 200%.

    Found in a real export. A `buy_and_hold` replay over forty symbols at
    `equity_pct 0.025`... at 0.05 filled all forty, ended at 1.97x gross
    exposure with cash at -97,046, and the exposure cap refused nothing. The
    same shape reaches production through `StrategyRunner._submit`, which loops
    signals through the router against one unrefreshed portfolio.
    """

    def _rules(self) -> list[RiskRule]:
        rules = default_rules(
            kill_switch=FakeKillSwitch(False),
            clock=SimulatedClock(OPEN_HOURS),
            calendar=TradingCalendar(),
            last_tick_at=lambda _s: OPEN_HOURS,
        )
        for rule in rules:
            if isinstance(rule, DailyLossLimitRule):
                rule.anchor(Decimal(100_000))
        return rules

    def _chain(self) -> RiskEngine:
        return RiskEngine(limits(), rules=self._rules())

    def _batch(self, count: int, *, qty: float = 50, price: float = 100) -> list[Order]:
        return [order(symbol=f"S{i:02d}", qty=qty, limit=price) for i in range(count)]

    def test_a_batch_cannot_collectively_breach_the_gross_exposure_cap(self) -> None:
        """The bug, stated as the fix. Twenty orders of 5,000 fill a 100,000
        book exactly; the twenty-first is over the cap and is refused, even
        though not one of the twenty-one is individually near it."""
        chain, book = self._chain(), portfolio()
        submitted = self._batch(20)

        twenty_first = order(symbol="S20", qty=50, limit=100)
        decision = chain.validate(twenty_first, book, submitted)

        assert not decision.approved
        assert decision.rule == "max_gross_exposure"

    def test_the_same_order_passes_while_the_batch_is_still_small(self) -> None:
        """The allow case, so the rule above is a limit and not a mute button."""
        chain, book = self._chain(), portfolio()
        assert chain.validate(order(symbol="S20"), book, self._batch(5)).approved

    def test_omitting_pending_is_the_bug_this_closes(self) -> None:
        """Pinned deliberately: the default is empty, and a caller that has
        orders in flight and does not pass them gets the old behaviour. That is
        what makes the two call sites in this platform load-bearing rather than
        decorative, and it is why this argument is not optional in spirit."""
        chain, book = self._chain(), portfolio()
        twenty_first = order(symbol="S20", qty=50, limit=100)

        assert chain.validate(twenty_first, book).approved
        assert not chain.validate(twenty_first, book, self._batch(20)).approved

    def test_pending_quantity_in_one_symbol_counts_against_the_position_cap(self) -> None:
        """`MaxPositionSizeRule` reads one symbol's quantity, so two orders in
        the same name at 6% of a 10% cap each pass alone and breach together."""
        chain, book = self._chain(), portfolio()
        first = order(symbol="SPY", qty=60, limit=100)  # 6,000 of a 10,000 cap

        assert chain.validate(order(symbol="SPY", qty=60, limit=100), book).approved
        decision = chain.validate(order(symbol="SPY", qty=60, limit=100), book, [first])
        assert not decision.approved
        assert decision.rule == "max_position_size"

    def test_pending_entries_count_against_the_open_position_cap(self) -> None:
        """`MaxOpenPositionsRule` counts positions rather than pricing them, so
        it is broken by the same seam and fixed by the same projection."""
        chain = RiskEngine(limits(max_open_positions=5), rules=self._rules())
        decision = chain.validate(
            order(symbol="S09", qty=1, limit=100), portfolio(), self._batch(5)
        )

        assert not decision.approved
        assert decision.rule == "max_open_positions"

    def test_pending_buys_consume_buying_power(self) -> None:
        """The other half of the 40-symbol failure: cash ended at -97,046
        because every order was priced against the opening balance.

        The exposure cap is lifted here so that buying power is the rule left
        to bind — each order is 9% of equity against a 10% position cap, and
        eleven of them exhaust the cash without any one being remarkable.

        Lifted to `MAX_GROSS_CEILING` rather than to an arbitrary 10, which
        `RiskLimits` refuses: 400% is the widest a US equities account can run
        at, and this needs only to clear the 99% the batch reaches.
        """
        chain = RiskEngine(limits(max_gross_exposure_pct=MAX_GROSS_CEILING), rules=self._rules())
        book = portfolio(cash=100_000)
        spent = self._batch(11, qty=90)  # 99,000 of 100,000

        decision = chain.validate(order(symbol="S11", qty=90, limit=100), book, spent)
        assert not decision.approved
        assert decision.rule == "buying_power"

    def test_a_resting_exit_is_not_credited(self) -> None:
        """The deliberate asymmetry. A protective stop counted as filled would
        *lower* projected exposure and license a position the limits would
        otherwise refuse — a rule reasoning from an exit that has not happened.
        """
        chain = self._chain()
        book = portfolio(cash=0, SPY=(1000.0, 100.0))  # 100,000 held, at the cap
        resting_stop = order(symbol="SPY", side=Side.SELL, qty=1000, limit=90)

        decision = chain.validate(order(symbol="QQQ", qty=10, limit=100), book, [resting_stop])
        assert not decision.approved, "the stop must not free up room it has not freed"
        assert decision.rule == "max_gross_exposure"

    def test_an_unpriceable_pending_order_refuses_rather_than_under_counts(self) -> None:
        """Default-closed. With no limit price and no mark there is no notional
        to add, so the projected position carries the quantity and no price —
        which `_unpriced_book` then refuses on. Skipping it would under-count,
        and under-counting approves what it should refuse."""
        chain, book = self._chain(), portfolio()
        unpriceable = order(symbol="XYZ", qty=10, limit=None)

        decision = chain.validate(order(qty=10), book, [unpriceable])
        assert not decision.approved
        assert "XYZ" in decision.reason

    def test_only_the_unfilled_remainder_of_a_partial_counts(self) -> None:
        """The filled half is already in the book; counting the whole order
        would double it and refuse trades that are within every limit."""
        chain, book = self._chain(), portfolio()
        half_filled = order(symbol="S00", qty=100, limit=100)
        half_filled.filled_qty = Decimal(50)

        assert chain.validate(order(symbol="S01", qty=50, limit=100), book, [half_filled]).approved


class TestTheProjectionItself:
    def test_it_does_not_mutate_the_portfolio_it_is_given(self) -> None:
        """It runs inside `validate`, before rules that read the same object.
        A projection that wrote through would make the risk chain a mutator of
        the book it is judging."""
        book = portfolio(cash=50_000, SPY=(10.0, 100.0))
        projected = project_pending(book, [order(symbol="QQQ", qty=100, limit=100)])

        assert book.cash == Decimal(50_000)
        assert "QQQ" not in book.positions
        assert projected.cash == Decimal(40_000)
        assert projected.positions["QQQ"].qty == Decimal(100)

    def test_a_projected_buy_leaves_equity_unchanged(self) -> None:
        """Cash out, mark in — exactly what a fill does. Every percentage limit
        is a percentage *of* equity, so a projection that moved it would shift
        the ceiling as well as the measurement."""
        book = portfolio(cash=100_000)
        projected = project_pending(book, [order(symbol="SPY", qty=100, limit=100)])

        assert projected.equity == book.equity
        assert projected.gross_exposure == Decimal(10_000)

    def test_nothing_in_flight_returns_the_same_object(self) -> None:
        """The single-order path is the overwhelmingly common one and copies
        nothing."""
        book = portfolio()
        assert project_pending(book, []) is book

    def test_a_reducing_order_is_dropped_entirely(self) -> None:
        book = portfolio(cash=0, SPY=(100.0, 100.0))
        exit_order = order(symbol="SPY", side=Side.SELL, qty=100, limit=100)

        assert project_pending(book, [exit_order]) is book


class TestTheTwoBooks:
    """A permission reads the settled book; a ceiling reads the committed one.

    ADR 0020 gave the whole chain the projected book, which is right for a
    ceiling and wrong for the three carve-outs that ask whether an order is an
    *exit*. A projected book answers that with a position that has not filled,
    so a working entry vouches for the order that opposes it. Every case below
    was approved by the chain before ADR 0027 and is refused now — and the
    allow-cases guard the opposite mistake, because a carve-out that stops
    working traps the position it exists to release.
    """

    @staticmethod
    def _books(settled: Portfolio, *pending: Order) -> RiskBooks:
        return RiskBooks(committed=project_pending(settled, pending), settled=settled)

    def test_a_halt_refuses_a_sell_that_only_a_working_buy_makes_look_like_an_exit(
        self,
    ) -> None:
        """The one that matters: flat account, one working BUY, platform halted.

        The projection carries a long of 100, so `reduces_position` called on it
        says this SELL is an exit and the halt lets it through — opening a short
        while trading is stopped. docs/SAFETY.md states the opposite guarantee
        without qualification, and Phase 3's roadmap tick rests on it.
        """
        settled = portfolio(SPY=(0.0, 100.0))
        books = self._books(settled, order(symbol="SPY", qty=100, limit=100))
        assert books.committed.position("SPY").qty == Decimal(100)

        decision = KillSwitchRule(switch=FakeKillSwitch(engaged=True)).check(
            order(symbol="SPY", side=Side.SELL, qty=100, limit=100), books, limits()
        )

        assert not decision.approved
        assert decision.reason == "trading is halted"

    def test_a_halt_still_lets_a_real_position_out(self) -> None:
        """The carve-out this must not break. A halt stops new risk; it does not
        trap a position — docs/SAFETY.md, and day 1's F3."""
        settled = portfolio(SPY=(100.0, 100.0))
        decision = KillSwitchRule(switch=FakeKillSwitch(engaged=True)).check(
            order(symbol="SPY", side=Side.SELL, qty=100, limit=100),
            self._books(settled),
            limits(),
        )

        assert decision.approved

    def test_a_halt_sizes_the_exit_against_the_settled_holding(self) -> None:
        """A working entry must not enlarge what a halt will let out. Held 40,
        another 60 in flight: selling 100 reverses into a short of 60 unless the
        rule measures the 40 that actually exist."""
        settled = portfolio(SPY=(40.0, 100.0))
        books = self._books(settled, order(symbol="SPY", qty=60, limit=100))

        decision = KillSwitchRule(switch=FakeKillSwitch(engaged=True)).check(
            order(symbol="SPY", side=Side.SELL, qty=100, limit=100), books, limits()
        )

        assert not decision.approved
        assert "would reverse the position" in decision.reason

    def test_the_daily_loss_limit_is_not_talked_past_by_a_working_entry(self) -> None:
        """Same shape, one layer down: the loss limit's exit carve-out asked of
        the projection approves a *new* entry past a breached limit."""
        settled = portfolio(SPY=(0.0, 100.0))
        books = self._books(settled, order(symbol="SPY", qty=100, limit=100))
        rule = DailyLossLimitRule(day_start_equity=Decimal(200_000))  # down 50%

        decision = rule.check(
            order(symbol="SPY", side=Side.SELL, qty=100, limit=100), books, limits()
        )

        assert not decision.approved
        assert decision.rule == "daily_loss_limit"

    def test_the_daily_loss_limit_still_lets_a_real_exit_out(self) -> None:
        settled = portfolio(SPY=(100.0, 100.0))
        rule = DailyLossLimitRule(day_start_equity=Decimal(200_000))

        assert rule.check(
            order(symbol="SPY", side=Side.SELL, qty=100, limit=100),
            self._books(settled),
            limits(),
        ).approved

    def test_buying_power_is_not_exempted_by_a_working_short(self) -> None:
        """A working SELL does not put cash in the account. Read off the
        projection it makes the opposing BUY look like an exit, which exempts a
        purchase from the one rule that exists to say it is unaffordable."""
        settled = portfolio(cash=10, SPY=(0.0, 100.0))
        books = self._books(settled, order(symbol="SPY", side=Side.SELL, qty=100, limit=1))

        decision = BuyingPowerRule().check(order(symbol="SPY", qty=100, limit=100), books, limits())

        assert not decision.approved
        assert "against 110.00 cash" in decision.reason

    def test_buying_power_still_exempts_a_genuine_reduction(self) -> None:
        settled = portfolio(cash=0, SPY=(-100.0, 100.0))

        assert (
            BuyingPowerRule()
            .check(order(symbol="SPY", qty=100, limit=100), self._books(settled), limits())
            .approved
        )


class TestACeilingDoesNotRefuseWhatShrinksTheBook:
    """`max_position_size` and `max_gross_exposure` measure the committed book,
    and so refused an order that made that book *smaller*.

    Held 40 with 200 more in flight, a genuine `SELL 40` leaves 200 — over the
    cap, so the cap refused the exit and left the position on. That is the same
    failure the kill switch's carve-out exists to prevent, one rule along. The
    The exemption is `closes_without_reversing`, and it is neither of the two
    obvious predicates. `reduces_position` is quantity-blind and would let a
    `SELL 300` against a long of 100 skip the cap entirely. A pure magnitude
    comparison — "does this leave less behind" — has a hole in the middle, and
    `TestAReversalIsNotAReduction` below is that hole.
    """

    @staticmethod
    def _books(settled: Portfolio, *pending: Order) -> RiskBooks:
        return RiskBooks(committed=project_pending(settled, pending), settled=settled)

    def test_the_position_cap_allows_an_exit_that_closes_into_the_position(
        self,
    ) -> None:
        settled = portfolio(SPY=(40.0, 100.0))
        books = self._books(settled, order(symbol="SPY", qty=200, limit=100))
        assert books.committed.position("SPY").qty == Decimal(240)

        assert (
            MaxPositionSizeRule()
            .check(order(symbol="SPY", side=Side.SELL, qty=40, limit=100), books, limits())
            .approved
        )

    def test_the_position_cap_still_refuses_a_reversal(self) -> None:
        """`SELL 500` against 240 committed leaves a short of 260 — *more* than
        is held, so it is an entry wearing an exit's clothes and meets the cap."""
        settled = portfolio(SPY=(40.0, 100.0))
        books = self._books(settled, order(symbol="SPY", qty=200, limit=100))

        decision = MaxPositionSizeRule().check(
            order(symbol="SPY", side=Side.SELL, qty=500, limit=100), books, limits()
        )

        assert not decision.approved
        assert decision.rule == "max_position_size"

    def test_the_position_cap_still_refuses_an_order_that_grows_the_book(self) -> None:
        settled = portfolio(SPY=(40.0, 100.0))
        books = self._books(settled, order(symbol="SPY", qty=200, limit=100))

        assert (
            not MaxPositionSizeRule()
            .check(order(symbol="SPY", qty=40, limit=100), books, limits())
            .approved
        )

    def test_the_exposure_cap_allows_what_shrinks_it(self) -> None:
        settled = portfolio(SPY=(40.0, 100.0))
        books = self._books(settled, order(symbol="SPY", qty=200, limit=100))

        assert (
            MaxExposureRule()
            .check(
                order(symbol="SPY", side=Side.SELL, qty=40, limit=100),
                books,
                limits(max_gross_exposure_pct=Decimal("0.05"), max_position_pct=Decimal("0.05")),
            )
            .approved
        )

    def test_the_exposure_cap_still_refuses_a_reversal(self) -> None:
        settled = portfolio(SPY=(40.0, 100.0))
        books = self._books(settled, order(symbol="SPY", qty=200, limit=100))

        assert (
            not MaxExposureRule()
            .check(
                order(symbol="SPY", side=Side.SELL, qty=500, limit=100),
                books,
                limits(max_gross_exposure_pct=Decimal("0.05"), max_position_pct=Decimal("0.05")),
            )
            .approved
        )

    def test_an_unmarked_holding_elsewhere_cannot_refuse_a_reduction(self) -> None:
        """The exemption is asked *before* the book is valued, deliberately.

        A stop reducing SPY does not need IWM to have a mark, and refusing it
        because IWM has none leaves a position naked — docs/SAFETY.md's layers 5
        and 6 failing together, which is the pairing docs/RISK.md warns about.
        """
        settled = portfolio(SPY=(100.0, 100.0), IWM=(50.0, 0.0))  # IWM unmarked
        books = RiskBooks.of(settled)
        exit_order = order(symbol="SPY", side=Side.SELL, qty=100, limit=100)

        assert MaxPositionSizeRule().check(exit_order, books, limits()).approved
        assert MaxExposureRule().check(exit_order, books, limits()).approved
        # ...and an order that grows the book is still refused for want of the mark.
        entry = order(symbol="SPY", qty=100, limit=100)
        assert (
            MaxPositionSizeRule()
            .check(entry, books, limits())
            .reason.startswith("cannot value the book")
        )


class TestTheEngineHandsBothBooksToTheChain:
    def test_validate_gives_a_permission_the_settled_book(self) -> None:
        """End to end through `RiskEngine.validate`, which is the only place the
        two books are built — a rule tested in isolation proves nothing about
        what the engine actually passes it."""
        settled = portfolio(SPY=(0.0, 100.0))
        working = order(symbol="SPY", qty=100, limit=100)
        engine = RiskEngine(limits(), rules=[KillSwitchRule(switch=FakeKillSwitch(engaged=True))])

        decision = engine.validate(
            order(symbol="SPY", side=Side.SELL, qty=100, limit=100), settled, [working]
        )

        assert not decision.approved
        assert decision.rule == "kill_switch"

    def test_validate_gives_a_ceiling_the_committed_book(self) -> None:
        """The other half, and the one ADR 0020 bought: two entries in one name
        must be measured together even though neither has settled."""
        settled = portfolio(SPY=(0.0, 100.0))
        working = order(symbol="SPY", qty=60, limit=100)
        engine = RiskEngine(limits(), rules=[MaxPositionSizeRule()])

        decision = engine.validate(order(symbol="SPY", qty=60, limit=100), settled, [working])

        assert not decision.approved
        assert decision.rule == "max_position_size"

    def test_of_makes_both_books_the_same_book(self) -> None:
        book = portfolio()
        books = RiskBooks.of(book)

        assert books.committed is book
        assert books.settled is book


class TestWhatCanRefuseAnExit:
    """`EXIT_BLIND_RULES` is quoted by five documents and three docstrings, and
    has been wrong in all of them at least once. Derived here from the real
    chain rather than trusted, exactly as `REPLAY_BLIND_RULES` is."""

    @staticmethod
    def _chain() -> list[RiskRule]:
        """Every default rule, each configured to refuse whatever it can."""
        return default_rules(
            kill_switch=FakeKillSwitch(engaged=True),
            clock=SimulatedClock(CLOSED),  # outside the session
            calendar=TradingCalendar(),
            last_tick_at=lambda _symbol: None,  # never ticked: maximally stale
        )

    def test_exactly_three_rules_can_refuse_a_reduction(self) -> None:
        """A pure exit — 100 held, 100 sold — put to every rule with each one
        set up to refuse if it is able to.

        The book is deliberately hostile: no cash, a position far over the
        tightened caps, an unmarked second holding, a halt engaged, the session
        shut, and a feed that has never ticked. Every rule that *can* say no to
        a reduction says no here.

        Run twice, taking the union, because `RateLimitRule` refuses only once
        its window is full — the first pass consumes its single slot. A
        one-pass version of this test silently reported two rules and passed
        for the wrong reason.
        """
        settled = portfolio(cash=0, SPY=(100.0, 100.0), IWM=(50.0, 0.0))
        exit_order = order(symbol="SPY", side=Side.SELL, qty=100, limit=100)
        tight = limits(
            max_position_pct=Decimal("0.01"),
            max_gross_exposure_pct=Decimal("0.01"),
            max_orders_per_minute=1,
        )

        chain = self._chain()
        refused = {
            rule.name
            for _pass in range(2)
            for rule in chain
            if not rule.check(exit_order, RiskBooks.of(settled), tight).approved
        }

        assert refused == set(EXIT_BLIND_RULES)

    def test_the_list_names_rules_the_chain_actually_has(self) -> None:
        """A typo here would silently shrink the set the test above compares
        against, and the assertion would still pass."""
        names = {rule.name for rule in self._chain()}

        assert set(EXIT_BLIND_RULES) <= names
        assert len(EXIT_BLIND_RULES) == len(set(EXIT_BLIND_RULES))


class TestAReversalIsNotAReduction:
    """The hole the first version of the ceiling exemption had, pinned open.

    `closes_without_reversing` shipped as `increases_exposure`: a pure magnitude
    comparison asking whether the order left *more* of the symbol behind. An
    adversarial review of that diff found what the framing could not see —
    `SELL 400` against a long of 200 leaves a short of 200, no larger, so the
    magnitude test exempted it. That is not a reduction. It is a brand-new
    opposite-side position opened at full size, and these tests run it with the
    symbol already at 25% of equity against a 10% cap, which is exactly the
    state the cap exists to refuse from.

    Every quantity from "closes part of it" to "reverses past it" is asserted,
    because the defect lived in the middle of that range and both ends were
    already covered.
    """

    @staticmethod
    def _over_cap_book() -> Portfolio:
        """SPY at 25% of equity against a 10% cap — the book after a mark moved."""
        book = portfolio(cash=0, SPY=(200.0, 100.0), IWM=(200.0, 300.0))
        assert book.equity == Decimal(80_000)
        assert Decimal(200) * Decimal(100) / book.equity == Decimal("0.25")
        return book

    @pytest.mark.parametrize(
        ("qty", "exempt", "what"),
        [
            (100, True, "closes half the long"),
            (200, True, "closes the long exactly, to flat"),
            (201, False, "one share past flat is already a short"),
            (300, False, "reverses to a short of 100"),
            (400, False, "reverses to a short of 200 — same size, opposite side"),
            (500, False, "reverses to a short of 300"),
        ],
    )
    def test_no_reversal_is_exempt_from_the_ceilings(
        self, qty: int, exempt: bool, what: str
    ) -> None:
        """The predicate itself. Exemption stops the moment the order would
        carry the position through flat — at 201, not at 401 where a magnitude
        comparison put it."""
        assert (
            closes_without_reversing(
                order(symbol="SPY", side=Side.SELL, qty=qty, limit=100), self._over_cap_book()
            )
            is exempt
        ), f"SELL {qty} {what}"

    @pytest.mark.parametrize(
        ("qty", "allowed", "what"),
        [
            (100, True, "exempt: closes into the long"),
            (200, True, "exempt: closes to flat"),
            (201, True, "not exempt, but a 1-share short is inside the cap"),
            (300, False, "a short of 100 is 12.5% of equity, over the 10% cap"),
            (400, False, "a short of 200 is 25% — the case the magnitude test let through"),
            (500, False, "a short of 300 is 37.5%"),
        ],
    )
    def test_the_position_cap_measures_every_reversal_it_does_not_exempt(
        self, qty: int, allowed: bool, what: str
    ) -> None:
        """And the verdict, which is not the same question. Losing the exemption
        means the cap gets to *evaluate* the order, not that it refuses it: a
        reversal small enough to sit inside the ceiling is still fine."""
        decision = MaxPositionSizeRule().check(
            order(symbol="SPY", side=Side.SELL, qty=qty, limit=100),
            RiskBooks.of(self._over_cap_book()),
            limits(),
        )

        assert decision.approved is allowed, f"SELL {qty} — {what}"

    @pytest.mark.parametrize(("qty", "allowed"), [(200, True), (400, False)])
    def test_the_exposure_cap_refuses_every_reversal(self, qty: int, allowed: bool) -> None:
        """Gross exposure is unchanged by a same-size flip, which is precisely
        why a magnitude test could not refuse one."""
        book = self._over_cap_book()
        decision = MaxExposureRule().check(
            order(symbol="SPY", side=Side.SELL, qty=qty, limit=100),
            RiskBooks.of(book),
            limits(max_gross_exposure_pct=Decimal("0.10"), max_position_pct=Decimal("0.10")),
        )

        assert decision.approved is allowed

    def test_the_predicate_agrees_with_the_kill_switch(self) -> None:
        """`closes_without_reversing` is the same line `KillSwitchRule` draws.
        Drawn twice, the two would drift; this asserts they have not."""
        book = self._over_cap_book()
        halted = KillSwitchRule(switch=FakeKillSwitch(engaged=True))

        for qty in (100, 200, 201, 300, 400, 500):
            sell = order(symbol="SPY", side=Side.SELL, qty=qty, limit=100)
            assert (
                closes_without_reversing(sell, book)
                is halted.check(sell, RiskBooks.of(book), limits()).approved
            ), f"disagreement at SELL {qty}"

    def test_a_short_book_reverses_the_same_way(self) -> None:
        """The long case is the readable one; the short case is where a sign
        error would hide."""
        book = portfolio(cash=0, SPY=(-200.0, 100.0))

        for qty, allowed in ((200, True), (400, False)):
            decision = MaxPositionSizeRule().check(
                order(symbol="SPY", side=Side.BUY, qty=qty, limit=100),
                RiskBooks.of(book),
                limits(),
            )
            assert decision.approved is allowed, f"BUY {qty} against a short of 200"


class TestTheExemptionIsAnExitQuestionToo:
    """The ceiling exemption asks "is this an exit?", so it reads the settled
    book like every other exit question on this chain.

    It shipped reading the committed one, which is ADR 0027's own defect
    reintroduced one rule along: with a flat account and a working `BUY 100`,
    the projection carries a long of 100, so `SELL 100` reads as closing it and
    the cap stands aside — for an order that opens a short of 100 if that BUY
    never fills. Three independent reviewers found it on the same diff.
    """

    @staticmethod
    def _books(settled: Portfolio, *pending: Order) -> RiskBooks:
        return RiskBooks(committed=project_pending(settled, pending), settled=settled)

    def test_a_working_entry_does_not_exempt_the_order_that_opposes_it(self) -> None:
        settled = portfolio(SPY=(0.0, 100.0))  # flat
        books = self._books(settled, order(symbol="SPY", qty=100, limit=100))
        sell = order(symbol="SPY", side=Side.SELL, qty=100, limit=100)

        assert books.committed.position("SPY").qty == Decimal(100)
        assert closes_without_reversing(sell, books.committed) is True, "the phantom long"
        assert closes_without_reversing(sell, books.settled) is False, "what the rule must ask"

    def test_the_cap_judges_that_order_rather_than_exempting_it(self) -> None:
        """Not exempt means measured, not refused — here the resulting committed
        position is flat, so the cap approves. What matters is that it *looked*."""
        settled = portfolio(cash=0, SPY=(0.0, 100.0), IWM=(200.0, 300.0))
        books = self._books(settled, order(symbol="SPY", qty=700, limit=100))
        sell = order(symbol="SPY", side=Side.SELL, qty=100, limit=100)

        # Committed carries a long of 700; SELL 100 leaves 600, well over the cap.
        decision = MaxPositionSizeRule().check(sell, books, limits())

        assert not decision.approved
        assert decision.rule == "max_position_size"

    def test_a_genuine_exit_behind_a_working_entry_is_still_exempt(self) -> None:
        """The case #142 existed to fix must survive the correction."""
        settled = portfolio(SPY=(40.0, 100.0))
        books = self._books(settled, order(symbol="SPY", qty=200, limit=100))

        assert (
            MaxPositionSizeRule()
            .check(order(symbol="SPY", side=Side.SELL, qty=40, limit=100), books, limits())
            .approved
        )

    def test_the_exposure_cap_asks_the_same_book(self) -> None:
        settled = portfolio(SPY=(0.0, 100.0))
        books = self._books(settled, order(symbol="SPY", qty=100, limit=100))
        sell = order(symbol="SPY", side=Side.SELL, qty=100, limit=100)

        assert closes_without_reversing(sell, books.settled) is False
        assert MaxExposureRule().check(sell, books, limits()).approved  # judged, and fine


class TestAWorkingReversalIsVisibleToTheCeilings:
    """ADR 0028. `project_pending` drops a working reversal — its predicate is
    quantity-blind — so the committed book says long 100 while a `SELL 300`
    works and the fill would leave a short of 200.

    The reversal is *not* fixed by projecting it into the committed book, and
    the reason is arithmetic. `Portfolio` welds quantity, mark and cash into one
    equity, so showing the short moves market value and the only way to hold
    equity still is to credit cash the account has not been paid — which is the
    number `BuyingPowerRule` reads. Every candidate value either loosens buying
    power or manufactures a drawdown. So the bound is a bound, not a book.
    """

    @staticmethod
    def _books(settled: Portfolio, *pending: Order) -> RiskBooks:
        return RiskBooks(
            committed=project_pending(settled, pending),
            settled=settled,
            in_flight=in_flight_by_symbol(pending),
        )

    def test_the_committed_book_still_hides_the_reversal(self) -> None:
        """Pinning the premise, so this class fails loudly if the projection is
        ever changed to carry reversals after all."""
        settled = portfolio(SPY=(100.0, 100.0))
        books = self._books(settled, order(symbol="SPY", side=Side.SELL, qty=300, limit=100))

        assert books.committed.position("SPY").qty == Decimal(100)

    def test_the_bound_sees_it(self) -> None:
        settled = portfolio(SPY=(100.0, 100.0))
        books = self._books(settled, order(symbol="SPY", side=Side.SELL, qty=300, limit=100))

        # A further SELL 1 lands on a book that may be short 200, not long 100.
        assert worst_resulting_qty(
            order(symbol="SPY", side=Side.SELL, qty=1, limit=100), books
        ) == Decimal(201)

    def test_the_position_cap_refuses_what_the_reversal_makes_too_big(self) -> None:
        # Equity 100,000, so the 10% cap is 10,000.
        #
        # The order under test must not itself be an exit, or ADR 0027's
        # exemption allows it before the cap measures anything — `SELL 50`
        # against a settled long of 100 closes into the position and is exempt,
        # correctly. `SELL 150` carries it through flat, so the cap gets to look:
        # 5,000 against the committed book (long 100 -> short 50) and 35,000
        # against the book the working reversal can produce (short 200 -> 350).
        # The cap sits between the two, which is the whole point of the case.
        settled = portfolio(cash=90_000, SPY=(100.0, 100.0))
        working = order(symbol="SPY", side=Side.SELL, qty=300, limit=100)

        entry = order(symbol="SPY", side=Side.SELL, qty=150, limit=100)
        assert MaxPositionSizeRule().check(entry, RiskBooks.of(settled), limits()).approved
        assert (
            not MaxPositionSizeRule().check(entry, self._books(settled, working), limits()).approved
        )

    def test_the_exposure_cap_counts_a_reversal_in_another_symbol(self) -> None:
        """The per-symbol correction: a reversal working in IWM raises the gross
        exposure an order in SPY is measured against."""
        # Equity 100,000, ceiling 15,000. HEAD measures 12,000; the reversal
        # IWM can produce adds 100 shares at its mark, taking it to 22,000.
        settled = portfolio(cash=89_000, SPY=(10.0, 100.0), IWM=(100.0, 100.0))
        working = order(symbol="IWM", side=Side.SELL, qty=300, limit=100)
        tight = limits(max_gross_exposure_pct=Decimal("0.15"), max_position_pct=Decimal("0.15"))
        entry = order(symbol="SPY", qty=10, limit=100)

        assert MaxExposureRule().check(entry, RiskBooks.of(settled), tight).approved
        assert not MaxExposureRule().check(entry, self._books(settled, working), tight).approved

    def test_nothing_in_flight_is_exactly_head(self) -> None:
        """`RiskBooks.of` carries no in-flight summary, so every number the two
        ceilings compute is the one they computed before ADR 0028."""
        settled = portfolio(SPY=(100.0, 100.0))
        entry = order(symbol="SPY", qty=40, limit=100)

        assert worst_resulting_qty(entry, RiskBooks.of(settled)) == Decimal(140)

    def test_the_outcome_where_nothing_fills_is_always_covered(self) -> None:
        """A cancel, a reject, an expiry, a DAY limit dying at the close. The
        settled holding is a candidate outcome in its own right, so the bound
        never assumes a working order fills."""
        settled = portfolio(SPY=(100.0, 100.0))
        books = self._books(settled, order(symbol="SPY", side=Side.SELL, qty=300, limit=100))

        # 100 held + a BUY 40 that lands after the reversal is cancelled.
        assert worst_resulting_qty(order(symbol="SPY", qty=40, limit=100), books) >= Decimal(140)

    def test_a_resting_reduction_still_does_not_lower_a_ceiling(self) -> None:
        """ADR 0020's asymmetry, restated as a property of the outcome: an
        outcome showing less of the symbol than is held is not considered, so a
        resting protective stop cannot license a position the cap would refuse."""
        settled = portfolio(SPY=(100.0, 100.0))
        stop = order(symbol="SPY", side=Side.SELL, qty=100, limit=95)
        books = self._books(settled, stop)

        assert worst_resulting_qty(order(symbol="SPY", qty=40, limit=100), books) == Decimal(140)

    def test_an_unvaluable_working_quantity_elsewhere_is_refused(self) -> None:
        """Default-closed, for `_unpriced_book`'s reason: a working quantity
        nobody can value is the same problem as a held one, and it does not
        reach `unmarked_symbols` while the projected position nets to flat."""
        # IWM flat and unmarked, with a BUY 100 and a SELL 100 working. Neither
        # reduces a flat position, so the projection carries both and they net
        # to flat — the symbol never reaches `unmarked_symbols`, so
        # `_unpriced_book` does not fire and this branch is the only guard left.
        settled = portfolio(cash=0, SPY=(100.0, 100.0))
        books = self._books(
            settled,
            order(symbol="IWM", qty=100, limit=None),
            order(symbol="IWM", side=Side.SELL, qty=100, limit=None),
        )

        decision = MaxExposureRule().check(order(symbol="SPY", qty=10, limit=100), books, limits())

        assert not decision.approved
        assert "cannot value the working quantity in IWM" in decision.reason


class TestTheCarveOutIsVoidWhereTheQuantityIsUnproven:
    """ADR 0029. The exit carve-out rests on there being a position here to
    close; a halt carrying an impugnment is the platform saying it cannot prove
    that. These are the tests that keep the exception narrow — because the
    tempting version of it, keyed on `HaltReason`, refuses every exit and every
    protective stop in the book on a dollar of late-settling fees.
    """

    def test_an_exit_is_refused_in_an_unproven_symbol(self) -> None:
        """The defect this closes. `flatten` sizes at `abs(position.qty)`, and
        under a reconciliation mismatch that number is the one in doubt: sell
        300 against a book that says 300 and a venue that says 100, and the
        flatten opens a short of 200 while the platform is halted."""
        rule = KillSwitchRule(switch=halted(HaltReason.RECONCILIATION_MISMATCH, GLOBAL=("SPY",)))

        decision = rule.check(
            order(side=Side.SELL, qty=100), RiskBooks.of(portfolio(SPY=(100, 100))), limits()
        )

        assert not decision.approved
        assert decision.rule == "kill_switch"
        assert "unproven" in decision.reason
        assert "broker" in decision.reason, "an operator needs the way out, not just the refusal"

    def test_an_exit_is_still_permitted_in_a_symbol_the_halt_does_not_impugn(self) -> None:
        """The half that keeps this an exception rather than a reversal of the
        carve-out. One unproven symbol must not close the book."""
        rule = KillSwitchRule(switch=halted(HaltReason.RECONCILIATION_MISMATCH, GLOBAL=("QQQ",)))

        decision = rule.check(
            order(side=Side.SELL, qty=100), RiskBooks.of(portfolio(SPY=(100, 100))), limits()
        )

        assert decision.approved

    def test_a_halt_that_impugns_nothing_still_lets_exits_out(self) -> None:
        """The `RECONCILIATION_MISMATCH` trap, and the reason the carve-out is
        keyed on evidence rather than on the reason.

        `Reconciler.is_clean` covers cash and orphaned orders with a $1.00
        tolerance, so a mismatch of late-settling fees engages this exact reason
        while impugning no position at all. Keyed on `HaltReason`, that dollar
        would refuse every exit and every protective stop platform-wide,
        unattended, every five minutes.
        """
        rule = KillSwitchRule(switch=halted(HaltReason.RECONCILIATION_MISMATCH))

        decision = rule.check(
            order(side=Side.SELL, qty=100), RiskBooks.of(portfolio(SPY=(100, 100))), limits()
        )

        assert decision.approved

    def test_an_entry_is_refused_the_same_way_it_always_was(self) -> None:
        """The impugnment narrows the carve-out. It does not change the default,
        and the denial an operator reads should still be the plain one."""
        rule = KillSwitchRule(switch=halted(HaltReason.RECONCILIATION_MISMATCH, GLOBAL=("QQQ",)))

        decision = rule.check(order(side=Side.BUY, qty=100), RiskBooks.of(portfolio()), limits())

        assert not decision.approved
        assert decision.reason == "trading is halted"

    def test_a_protective_stop_in_an_unproven_symbol_is_refused_too(self) -> None:
        """The uncomfortable case, stated rather than hidden.

        A stop is exactly the order the carve-out was widened for, and this
        refuses one. It is still the right answer: a stop is sized off the same
        `Position.qty` the reconcile just disputed, so placing it against an
        unproven quantity is how a stop becomes a short. The alert names the
        symbol and sends the operator to the broker's UI precisely because this
        path leaves a position uncovered and a human has to know.
        """
        rule = KillSwitchRule(switch=halted(HaltReason.BROKER_UNREACHABLE, GLOBAL=("SPY",)))
        stop = Order(
            symbol="SPY",
            side=Side.SELL,
            qty=Decimal(100),
            order_type=OrderType.STOP,
            stop_price=Decimal(95),
            strategy_id="test",
            parent_order_id="the-entry",
            purpose="stop_loss",
        )

        assert not rule.check(stop, RiskBooks.of(portfolio(SPY=(100, 100))), limits()).approved

    def test_evidence_on_a_symbol_scoped_halt_reaches_the_rule(self) -> None:
        """`halt_state` composes every halt covering the order, so the
        impugnment can be on any of them. A test that only ever put it on the
        global halt would not notice a rule reading one record."""
        rule = KillSwitchRule(switch=halted(HaltReason.RECONCILIATION_MISMATCH, SPY=("SPY",)))

        decision = rule.check(
            order(side=Side.SELL, qty=100), RiskBooks.of(portfolio(SPY=(100, 100))), limits()
        )

        assert not decision.approved
        assert "unproven" in decision.reason

    def test_an_unreachable_switch_still_lets_a_genuine_exit_out(self) -> None:
        """docs/SAFETY.md layers 5 and 6, not failing together.

        A Redis outage fails closed — every entry is refused — but it is not
        evidence about any position. Treating "cannot read the halt record" as
        "cannot prove the book" would refuse every protective stop on every
        Redis blip, which is layer 5 taken down by a layer 6 fault that says
        nothing about the book at all.
        """
        rule = KillSwitchRule(switch=UnreadableKillSwitch())

        assert not rule.check(order(side=Side.BUY), RiskBooks.of(portfolio()), limits()).approved
        assert rule.check(
            order(side=Side.SELL, qty=100), RiskBooks.of(portfolio(SPY=(100, 100))), limits()
        ).approved
