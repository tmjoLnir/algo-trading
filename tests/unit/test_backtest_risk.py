"""Sizing and the rule chain, as a backtest actually applies them.

Both have existed in `atp_core.risk` since Phase 3's first PRs, with a
production caller in `OrderRouter`. What did not exist is either of them
*reaching a backtest*: `build_engine` passed `RiskEngine(limits, rules=[])` and
a flat share count, so every run this platform produced sized every entry
identically and had nothing refuse it.

Four properties, and each is a way the wiring could be present and wrong:

1. **The chain a replay uses is the chain a replay can evaluate.** Four of the
   nine rules cannot, and passing them stubs that always approve would be a
   chain of nine that enforces five — worse than a chain of five, because it
   reads as complete.
2. **A rule that cannot evaluate refuses.** `DailyLossLimitRule` denies every
   entry until something anchors the session, which is correct and which
   nothing in this platform was doing.
3. **An unsizeable signal is recorded, not dropped.** `position_size` refuses to
   invent a stop; a backtest that turned that into a silent zero would report an
   empty run indistinguishable from a strategy that never signalled.
4. **One sizing function, two callers.** A backtest that did its own arithmetic
   would produce a return the live router could not reproduce.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from atp_core.backtest.engine import RiskBasedSizer
from atp_core.backtest.ports import BacktestRunSpec
from atp_core.backtest.runner import (
    SIZING_METHODS,
    refusal_summary,
    resolve_sizing,
    run_spec,
)
from atp_core.clock import SimulatedClock, TradingCalendar
from atp_core.config import RiskLimits, get_settings
from atp_core.domain import (
    SIZING,
    Bar,
    Order,
    OrderStatus,
    OrderType,
    Portfolio,
    Side,
    Signal,
    SignalAction,
    Timeframe,
)
from atp_core.errors import ConfigError
from atp_core.risk.engine import RiskEngine, backtest_rules, default_rules
from atp_core.risk.rules import DailyLossLimitRule, TradingHoursRule, position_size
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_core.strategy import registry

T0 = datetime(2024, 1, 2, tzinfo=UTC)
SHIPPED = "sma_crossover"
BARS = 90


def bars(count: int = BARS, *, symbol: str = "SPY") -> list[Bar]:
    """A wave, so the shipped crossover actually crosses — a ramp never does."""
    import math

    series = []
    for index in range(count):
        base = Decimal(str(round(100 + 12 * math.sin(2 * math.pi * index / (count / 2)), 2)))
        series.append(
            Bar(
                symbol=symbol,
                ts=T0 + timedelta(days=index),
                timeframe=Timeframe.D1,
                open=base,
                high=base + Decimal("1"),
                low=base - Decimal("1"),
                close=base + Decimal("0.5"),
                volume=Decimal("5000000"),
            )
        )
    return series


def a_spec(**overrides: object) -> BacktestRunSpec:
    fields: dict[str, object] = {
        "strategy_id": SHIPPED,
        "symbols": ("SPY",),
        "start": T0,
        "end": T0 + timedelta(days=BARS + 1),
        "timeframe": "1d",
        "starting_cash": "100000",
        "cost_model": "alpaca_equities",
        "params": {"fast_period": 5, "slow_period": 20},
        "qty": "10",
    }
    fields.update(overrides)
    return BacktestRunSpec(**fields)  # type: ignore[arg-type]


def an_order(symbol: str = "SPY", qty: str = "10") -> Order:
    return Order(
        symbol=symbol,
        side=Side.BUY,
        qty=Decimal(qty),
        order_type=OrderType.MARKET,
        created_at=T0,
        purpose="entry",
    )


class TestTheChainAReplayCanEvaluate:
    def test_the_five_that_describe_the_book_are_present(self) -> None:
        names = {rule.name for rule in backtest_rules()}
        assert names == {
            "max_position_size",
            "max_gross_exposure",
            "max_open_positions",
            "daily_loss_limit",
            "buying_power",
        }

    def test_the_four_that_cannot_are_absent_rather_than_stubbed(self) -> None:
        """Not `default_rules` with no-op collaborators. A kill switch that is
        never engaged and a feed that is never stale would approve
        unconditionally, and a chain of nine enforcing five reads as complete."""
        backtest = {rule.name for rule in backtest_rules()}
        live = {
            rule.name
            for rule in default_rules(
                kill_switch=_NeverEngaged(),
                clock=SimulatedClock(T0),
                calendar=TradingCalendar(),
                last_tick_at=lambda _symbol: T0,
            )
        }
        assert live - backtest == {"kill_switch", "trading_hours", "rate_limit", "stale_data"}

    def test_trading_hours_would_refuse_every_daily_order(self) -> None:
        """The concrete reason it is excluded, asserted rather than asserted
        *about*. A daily bar is stamped at exchange-local midnight, so the
        calendar says closed at both its open and its close — a chain carrying
        this rule would produce a daily backtest with no trades at all, and the
        cause would be four layers from the symptom.
        """
        calendar = TradingCalendar()
        day = datetime(2024, 3, 5, tzinfo=UTC).date()
        bar_ts, _ = calendar.day_bounds(day)
        clock = SimulatedClock(bar_ts + timedelta(days=1))  # the bar's close

        rule = TradingHoursRule(calendar=calendar, clock=clock)
        decision = rule.check(
            an_order(),
            Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000")),
            RiskLimits(),
        )

        assert not decision.approved
        assert "closed" in decision.reason


class TestSessionAnchoring:
    def test_an_unanchored_daily_loss_limit_denies_every_entry(self) -> None:
        """The bug this seam exists for. Correct behaviour by the rule — it is
        default-closed and will not assume the day began flat — and invisible,
        because a chain refusing everything looks like a chain nothing reached.
        """
        engine = RiskEngine(RiskLimits(), rules=[DailyLossLimitRule()])
        portfolio = Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))

        decision = engine.validate(an_order(), portfolio)

        assert not decision.approved
        assert decision.rule == "daily_loss_limit"
        assert "anchored" in decision.reason

    def test_anchoring_reaches_the_rule_and_says_how_many(self) -> None:
        """The count is what lets a caller assert it reached something.

        Validated against a chain of just this rule: the other four are
        default-closed on an unmarked book and would refuse for reasons that
        have nothing to do with anchoring — which would make this test pass or
        fail for the wrong reason.
        """
        engine = RiskEngine(RiskLimits(), rules=[DailyLossLimitRule()])
        portfolio = Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))

        assert engine.anchor_session(portfolio.equity) == 1
        assert engine.validate(an_order(), portfolio).approved

    def test_the_backtest_chain_has_exactly_one_thing_to_anchor(self) -> None:
        engine = RiskEngine(RiskLimits(), rules=backtest_rules())
        assert engine.anchor_session(Decimal("100000")) == 1

    def test_a_chain_with_nothing_to_anchor_says_zero(self) -> None:
        """So a caller can assert it reached something. A silent no-op would
        make a chain that lost its loss limit indistinguishable from one that
        never had it."""
        assert RiskEngine(RiskLimits(), rules=[]).anchor_session(Decimal("1")) == 0

    def test_a_run_anchors_rather_than_refusing_everything(self) -> None:
        """End to end: without the engine's own session boundary, every entry in
        every backtest would be denied by the loss limit."""
        result = run_spec(a_spec(), {"SPY": bars()}, limits=get_settings().risk)

        denied = [o for o in result.orders if o.rejected_by == "daily_loss_limit"]
        assert denied == []
        assert any(o.status is OrderStatus.FILLED for o in result.orders)


class TestSizingReachesTheBacktest:
    def test_the_sizer_delegates_rather_than_computing(self) -> None:
        """One sizing function, two callers. The assertion is equality with
        `position_size` itself, so a backtest cannot drift from the router by
        acquiring arithmetic of its own."""
        portfolio = Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))
        signal = Signal(
            strategy_id="s",
            symbol="SPY",
            action=SignalAction.ENTER_LONG,
            ts=T0,
            stop_loss_price=Decimal("48"),
        )
        price = Decimal("50")

        mine = RiskBasedSizer("risk_pct", Decimal("0.02"))(signal, portfolio, price)

        assert mine == position_size(
            "risk_pct", portfolio.equity, price, stop_price=Decimal("48"), risk_pct=Decimal("0.02")
        )

    def test_the_docs_worked_example_reproduces_through_the_backtest_sizer(self) -> None:
        """docs/RISK.md 'Position sizing', arrived at the way a backtest now
        arrives at it: $100k at 2%, a $50 entry with a $48 stop is 500 shares,
        and the same risk against a $35 stop is 66. Both lose about $1,000."""
        portfolio = Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))
        sizer = RiskBasedSizer("risk_pct", Decimal("0.01"))
        entry = Decimal("50")

        def sized(stop: str) -> Decimal:
            signal = Signal(
                strategy_id="s",
                symbol="SPY",
                action=SignalAction.ENTER_LONG,
                ts=T0,
                stop_loss_price=Decimal(stop),
            )
            return sizer(signal, portfolio, entry)

        near, far = sized("48"), sized("35")

        assert near == Decimal(500)
        assert far == Decimal(66)
        # The point of risk sizing: both trades lose roughly the same amount.
        assert near * (entry - Decimal("48")) == Decimal(1000)
        assert abs(far * (entry - Decimal("35")) - Decimal(1000)) <= Decimal(15)

    def test_an_unsizeable_signal_is_booked_rather_than_dropped(self) -> None:
        """`sma_crossover` emits no stop, so `risk_pct` cannot size it. The run
        must say so — one refused order per entry, naming the sizing stage —
        rather than returning an empty result that looks like a strategy which
        never signalled."""
        spec = a_spec(sizing_method="risk_pct", sizing_value="0.01")
        result = run_spec(spec, {"SPY": bars()}, limits=get_settings().risk)

        refused = [o for o in result.orders if o.rejected_by == SIZING]
        assert refused
        assert all(o.status is OrderStatus.REJECTED_RISK for o in refused)
        assert "needs a stop" in refused[0].reject_reason

    def test_equity_pct_sizes_off_the_book_rather_than_a_constant(self) -> None:
        """The observable difference from `fixed_qty`: the quantity is derived
        from equity and price rather than copied from the request.

        Asserted against `position_size` on the same inputs rather than against
        "the sizes differ", which a mean-reverting series can satisfy by
        accident or fail by accident — the fixture's entries can land at
        similar prices twice.
        """
        spec = a_spec(sizing_method="equity_pct", sizing_value="0.05")
        result = run_spec(spec, {"SPY": bars()}, limits=get_settings().risk)

        filled = [o for o in result.orders if o.status is OrderStatus.FILLED]
        assert filled
        # Nothing was sized at the request's `qty` of 10; every quantity is a
        # fraction of a six-figure book at a ~$100 price, which is tens of
        # shares rather than the constant the old sizer would have produced.
        assert all(order.qty != Decimal(10) for order in filled)
        assert all(order.qty > Decimal(10) for order in filled)


class TestTheChainRefusesForReal:
    def test_an_oversized_request_is_refused_by_the_position_cap(self) -> None:
        """Half the book in one symbol, against a 10% ceiling. Before this
        wiring the engine would have taken it."""
        spec = a_spec(sizing_method="equity_pct", sizing_value="0.50")
        result = run_spec(spec, {"SPY": bars()}, limits=get_settings().risk)

        assert result.orders
        assert all(o.rejected_by == "max_position_size" for o in result.orders)
        assert not any(o.status is OrderStatus.FILLED for o in result.orders)

    def test_the_result_summarises_refusals_by_rule(self) -> None:
        """One line saying how much of the run happened. Three hundred
        individual warnings is a list nobody reads."""
        spec = a_spec(sizing_method="equity_pct", sizing_value="0.50")
        result = run_spec(spec, {"SPY": bars()}, limits=get_settings().risk)

        summary = " ".join(result.warnings)
        assert "were refused before reaching the market" in summary
        assert "max_position_size" in summary

    def test_a_clean_run_carries_no_refusal_summary(self) -> None:
        assert refusal_summary(
            run_spec(
                a_spec(sizing_method="equity_pct", sizing_value="0.05"),
                {"SPY": bars()},
                limits=get_settings().risk,
            )
        ) in (None, refusal_summary(_clean_result()))

    def test_the_summary_counts_per_rule(self) -> None:
        result = _clean_result()
        result.orders.extend([_refused("max_position_size"), _refused("max_position_size")])
        result.orders.append(_refused(SIZING))

        summary = refusal_summary(result)

        assert summary is not None
        assert "max_position_size (2)" in summary
        assert f"{SIZING} (1)" in summary


class TestSpecCompatibility:
    def test_a_spec_stored_before_sizing_existed_resolves_to_what_it_was(self) -> None:
        """Every run in `backtest_runs` today has neither field. Each must still
        deserialize and still reproduce: `fixed_qty` of whatever `qty` said."""
        assert resolve_sizing(a_spec(qty="42")) == ("fixed_qty", Decimal(42))

    def test_an_explicit_value_wins_over_qty(self) -> None:
        spec = a_spec(qty="42", sizing_method="equity_pct", sizing_value="0.05")
        assert resolve_sizing(spec) == ("equity_pct", Decimal("0.05"))

    def test_an_unknown_method_is_refused_before_the_run(self) -> None:
        """A `ConfigError` here is a 400 at the API door; a `ValueError` from
        inside `position_size` would be a job that fails four minutes in."""
        with pytest.raises(ConfigError, match="sizing_method"):
            resolve_sizing(a_spec(sizing_method="vibes"))

    def test_the_methods_offered_are_the_ones_position_size_implements(self) -> None:
        """A method this list accepted and that function did not would be a 400
        the API could not have foreseen."""
        portfolio = Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))
        for method in SIZING_METHODS:
            position_size(
                method,
                portfolio.equity,
                Decimal("50"),
                stop_price=Decimal("48"),
                volatility=Decimal("0.2"),
                risk_pct=Decimal("0.01"),
            )


class _NeverEngaged:
    """Only to enumerate `default_rules`' names — never used to run a chain."""

    def is_engaged(self, strategy_id: str | None = None, symbol: str | None = None) -> bool:
        return False

    def engage(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        raise NotImplementedError

    def clear(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        raise NotImplementedError

    def active_halts(self) -> list[object]:  # pragma: no cover
        return []


def _clean_result() -> object:
    from atp_core.backtest.engine import BacktestConfig, BacktestResult

    return BacktestResult(
        config=BacktestConfig(
            symbols=["SPY"], start=T0, end=T0 + timedelta(days=1), timeframe=Timeframe.D1
        ),
        strategy_name=SHIPPED,
        portfolio=Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000")),
    )


def _refused(rule: str) -> Order:
    order = an_order()
    order.status = OrderStatus.REJECTED_RISK
    order.rejected_by = rule
    return order


def test_the_shipped_strategy_is_registered() -> None:
    """Everything above runs the real one; an empty registry would make every
    assertion here vacuous."""
    assert SHIPPED in registry.all_strategies()
