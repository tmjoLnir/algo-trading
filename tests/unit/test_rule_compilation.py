"""Declarative rule sets — warmup arithmetic, compilation refusals, semantics.

A rule set is the path a strategy takes when nobody wrote code for it: it is
edited in a form, stored as a row, and then trades money. Nothing in git
reviews it, which puts the whole burden of "does this mean what it says" on the
compiler and on these tests.

Three things are load-bearing here and each has its own class below.

- **Warmup.** Understate it and the first signals of every run are computed on
  half-filled indicators and then taken as real (docs/STRATEGY_AUTHORING.md
  rule 5). The arithmetic is asserted against hand-computed numbers, not
  against itself.
- **Refusals.** A spec asking for something the interpreter cannot do must fail
  to compile. The failure mode being bought off here is the quiet one: a spec
  that runs, produces a plausible curve, and answered a different question.
- **Three-valued logic.** `None` means "cannot say yet", and collapsing it to
  `False` makes `none:` groups fire during warmup — before the indicator they
  negate exists.
"""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from atp_core.backtest.costs import ZeroCostModel
from atp_core.backtest.engine import (
    BacktestConfig,
    BacktestContext,
    BacktestEngine,
    FixedQtySizer,
)
from atp_core.clock import SimulatedClock
from atp_core.domain import (
    Bar,
    OrderStatus,
    Portfolio,
    Position,
    Side,
    SignalAction,
    Timeframe,
)
from atp_core.errors import InvalidRuleError
from atp_core.risk.engine import RiskDecision
from atp_core.strategy import RuleSet, compile_ruleset
from atp_core.strategy.examples.sma_crossover import SmaCrossover
from atp_core.strategy.rules import _Evaluation

if TYPE_CHECKING:
    from atp_core.domain import Order, Signal

START = datetime(2024, 1, 2, tzinfo=UTC)


# ── fixtures ────────────────────────────────────────────────────────────────


def bar(index: int, close: float, *, symbol: str = "SPY", volume: float = 1_000_000) -> Bar:
    price = Decimal(str(close))
    return Bar(
        symbol=symbol,
        ts=START + timedelta(days=index),
        timeframe=Timeframe.D1,
        # Deliberately not equal to the close. A fill at the decision bar's
        # close and one at the next bar's open are different numbers only if
        # the fixture makes them so, and the lookahead assertion below is
        # vacuous otherwise.
        open=price - Decimal("0.5"),
        high=price + Decimal(1),
        low=price - Decimal(1),
        close=price,
        volume=Decimal(str(volume)),
        # No corporate actions in a synthetic series, so the adjusted close is the
        # close. The engine refuses a series with none of them (CLAUDE.md §5).
        adj_close=price,
    )


def wave(count: int = 120, *, symbol: str = "SPY") -> list[Bar]:
    """Two full sine cycles — a shape a crossover can actually cross on.

    A monotonic ramp is the tempting fixture and the useless one: a fast SMA
    sits above a slow one from the first bar both exist and never crosses it, so
    every assertion about signals passes over an empty list.
    """
    return [
        bar(index, round(100 + 12 * math.sin(2 * math.pi * index / (count / 2)), 2), symbol=symbol)
        for index in range(count)
    ]


def ruleset(**overrides: Any) -> RuleSet:
    """A minimal valid spec: SMA(5) crossing SMA(20), long only."""
    spec: dict[str, Any] = {
        "name": "test_rules",
        "universe": ["SPY"],
        "timeframe": "1d",
        "entry_long": {
            "all": [
                {
                    "left": {"indicator": "sma", "period": 5},
                    "op": "crosses_above",
                    "right": {"indicator": "sma", "period": 20},
                }
            ]
        },
        "exit": {
            "any": [
                {
                    "left": {"indicator": "sma", "period": 5},
                    "op": "crosses_below",
                    "right": {"indicator": "sma", "period": 20},
                }
            ]
        },
        "risk": {"position_size": {"type": "fixed_qty", "value": 10}},
    }
    spec.update(overrides)
    return RuleSet.model_validate(spec)


def condition(left: Any, op: str, right: Any) -> dict[str, Any]:
    return {"left": left, "op": op, "right": right}


def indicator(name: str, period: int, **extra: Any) -> dict[str, Any]:
    return {"indicator": name, "period": period, **extra}


def group(*conditions: Any, branch: str = "all") -> dict[str, Any]:
    return {branch: list(conditions)}


#: An exit that needs no history, so a warmup assertion is driven only by the
#: tree it is about. `ruleset()`'s default exit needs 21 bars and would
#: otherwise be the number every short entry tree appeared to produce.
NO_HISTORY_EXIT: dict[str, Any] = group(condition({"value": "1"}, "<", {"value": "2"}))


def counting_spec(**risk: Any) -> RuleSet:
    """A spec whose conditions never fire and whose warmup is zero.

    What is left is the bar counting — `max_holding_bars`, `cooldown_bars` —
    with nothing else able to produce a signal, so the bar a signal lands on is
    the count itself rather than the count plus a warmup nobody wrote down.
    """
    never = group(condition({"value": "1"}, ">", {"value": "2"}))
    return ruleset(
        entry_long=never,
        exit=never,
        risk={"position_size": {"type": "fixed_qty", "value": 10}, **risk},
    )


class _AllowAllRisk:
    """The surface `BacktestEngine` uses of a `RiskEngine`, and only that.

    The same double `test_backtest_engine.py` carries, for the same reason:
    these tests are about what a rule set decides, not about how a risk rule
    sizes a book. `tests/unit/test_risk_engine.py` owns the rules.
    """

    def anchor_session(self, equity: Decimal) -> int:
        return 1

    def validate(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        return RiskDecision.allow()


def run_backtest(spec: RuleSet, bars: list[Bar], *, symbol: str = "SPY") -> Any:
    """A full run, for the cases that need orders to actually fill.

    `drive` never fills, so a position never opens and nothing that keys off one
    — exits, cooldowns — can be observed through it.
    """
    config = BacktestConfig(
        symbols=[symbol],
        start=bars[0].ts,
        # The fixture's own span. Asked for a year it does not have, the engine
        # warns about coverage — true, and noise in tests about compilation.
        end=bars[-1].ts,
        timeframe=Timeframe.D1,
        starting_cash=Decimal(100_000),
    )
    return BacktestEngine(
        strategy=compile_ruleset(spec),
        config=config,
        cost_model=ZeroCostModel(),
        risk_engine=_AllowAllRisk(),
        position_sizer=FixedQtySizer(Decimal(10)),
    ).run({symbol: bars})


def drive(
    spec: RuleSet,
    bars: list[Bar],
    *,
    portfolio: Portfolio | None = None,
    symbol: str = "SPY",
) -> list[Signal]:
    """Feed every bar through a compiled spec, collecting what it emits.

    The real `BacktestContext` rather than a fake, because its cursor is the
    lookahead guarantee: a strategy driven through it cannot read a bar that has
    not closed, so a test that passes here passes for the right reason.
    """
    strategy = compile_ruleset(spec)
    book = (
        portfolio
        if portfolio is not None
        else Portfolio(cash=Decimal(100_000), starting_equity=Decimal(100_000))
    )
    clock = SimulatedClock(START)
    ctx = BacktestContext({symbol: bars}, book, clock, (symbol,))

    emitted: list[Signal] = []
    for index, current in enumerate(bars):
        clock.set(current.ts)
        ctx.advance(symbol, index)
        emitted.extend(strategy.on_bar(ctx, current))
    return emitted


# ── warmup ──────────────────────────────────────────────────────────────────


class TestRequiredWarmup:
    """Hand-computed numbers, so a regression in the walk cannot agree with it.

    Every case pins `exit=NO_HISTORY_EXIT` so the number under assertion comes
    from the tree the test is named after. Left on the helper's default exit,
    which needs 21 bars, a broken walk over short entry trees would still
    "produce" 21 and every one of these would pass.
    """

    def test_the_longest_lookback_wins_rather_than_the_sum(self) -> None:
        """SMA(50) against SMA(20) needs 50 bars of history, not 70.

        The two indicators read the *same* series; they do not each consume
        their own slice of it.
        """
        spec = ruleset(
            entry_long=group(
                condition({"indicator": "sma", "period": 20}, ">", indicator("sma", 50))
            ),
            exit=NO_HISTORY_EXIT,
        )
        assert spec.required_warmup == 50

    def test_a_crossing_costs_one_extra_bar(self) -> None:
        """The `+1` that `SmaCrossover.warmup_bars` carries, for its reason: a
        crossing compares this bar's ordering against the previous bar's, so it
        reads one bar further back than either operand alone."""
        crossing = ruleset(
            entry_long=group(
                condition(indicator("sma", 50), "crosses_above", indicator("sma", 20))
            ),
            exit=NO_HISTORY_EXIT,
        )
        level = ruleset(
            entry_long=group(condition(indicator("sma", 50), ">", indicator("sma", 20))),
            exit=NO_HISTORY_EXIT,
        )
        assert crossing.required_warmup == 51
        assert level.required_warmup == 50

    def test_it_agrees_with_the_hand_written_strategy_it_mirrors(self) -> None:
        """`SmaCrossover(20, 50)` declares 51. The rule set spelling the same
        strategy must not declare something else — two warmups for one strategy
        would have the declarative and coded versions trade different bars."""
        coded = SmaCrossover({"fast_period": 20, "slow_period": 50})
        declared = ruleset(
            entry_long=group(
                condition(indicator("sma", 20), "crosses_above", indicator("sma", 50))
            ),
            exit=NO_HISTORY_EXIT,
        )
        assert declared.required_warmup == coded.warmup_bars

    def test_offset_adds_to_the_period(self) -> None:
        """The SMA(20) as it stood 3 bars ago needs 23 bars, not 20."""
        spec = ruleset(
            entry_long=group(
                condition({"indicator": "sma", "period": 20, "offset": 3}, ">", {"value": "100"})
            ),
            exit=NO_HISTORY_EXIT,
        )
        assert spec.required_warmup == 23

    def test_a_price_operand_needs_its_offset_plus_the_current_bar(self) -> None:
        spec = ruleset(
            entry_long=group(condition({"price": "close", "offset": 4}, ">", {"value": "1"})),
            exit=NO_HISTORY_EXIT,
        )
        assert spec.required_warmup == 5

    def test_constants_alone_need_no_history(self) -> None:
        spec = ruleset(
            entry_long=group(condition({"value": "1"}, "<", {"value": "2"})),
            exit=NO_HISTORY_EXIT,
        )
        assert spec.required_warmup == 0

    def test_an_atr_stop_counts_even_though_no_condition_mentions_it(self) -> None:
        """An ATR(50) stop under an SMA(5) entry needs 51 bars before it can
        place a level. A warmup of 6 would spend the first 45 entries refused at
        sizing for want of a stop — a run indistinguishable from a strategy that
        never signalled (`test_backtest_risk.py` pins that refusal).

        51 and not 50: `StopSpec.period` is an ATR lookback, so it carries the
        same extra Wilder bar an `atr` operand does.
        """
        spec = ruleset(
            entry_long=group(condition(indicator("sma", 5), ">", {"value": "1"})),
            exit=NO_HISTORY_EXIT,
            risk={
                "position_size": {"type": "risk_pct", "value": "0.01"},
                "stop_loss": {"type": "atr", "multiplier": "2.0", "period": 50},
            },
        )
        assert spec.required_warmup == 51

    def test_a_take_profit_period_counts_too(self) -> None:
        spec = ruleset(
            entry_long=group(condition(indicator("sma", 5), ">", {"value": "1"})),
            exit=NO_HISTORY_EXIT,
            risk={
                "position_size": {"type": "fixed_qty", "value": 10},
                "take_profit": {"type": "atr", "multiplier": "3.0", "period": 30},
            },
        )
        assert spec.required_warmup == 31

    def test_it_walks_every_tree_not_only_the_entry(self) -> None:
        spec = ruleset(
            entry_long=group(condition(indicator("sma", 5), ">", {"value": "1"})),
            exit=group(condition(indicator("ema", 90), ">", {"value": "55"}), branch="any"),
        )
        assert spec.required_warmup == 90

    @pytest.mark.parametrize(("name", "expected"), [("sma", 14), ("ema", 14), ("stddev", 14)])
    def test_most_indicators_need_exactly_their_period(self, name: str, expected: int) -> None:
        spec = ruleset(
            entry_long=group(condition(indicator(name, 14), ">", {"value": "1"})),
            exit=NO_HISTORY_EXIT,
        )
        assert spec.required_warmup == expected

    @pytest.mark.parametrize("name", ["rsi", "atr"])
    def test_wilder_indicators_need_one_bar_more_than_their_period(self, name: str) -> None:
        """RSI(14) needs 15 bars, not 14 — Wilder's smoothing averages the
        *differences* between consecutive closes, and 14 differences span 15
        bars. `ta.rsi` and `ta.atr` both refuse a 14-long series.

        The cost of getting this wrong is not one bar of warmup. The compiled
        strategy sizes its history window off this number, and a window fixed at
        14 never grows — so `dispatch.compute` answers None for the whole run
        and the rule never fires once. An empty result, from a spec that reads
        like it should trade.
        """
        spec = ruleset(
            entry_long=group(condition(indicator(name, 14), ">", {"value": "1"})),
            exit=NO_HISTORY_EXIT,
        )
        assert spec.required_warmup == 15

    def test_it_reaches_the_short_entry_as_well(self) -> None:
        spec = ruleset(
            entry_long=group(condition(indicator("sma", 5), ">", {"value": "1"})),
            entry_short=group(condition(indicator("ema", 120), "<", {"value": "1"})),
            exit=NO_HISTORY_EXIT,
        )
        assert spec.required_warmup == 120

    def test_it_recurses_into_nested_groups(self) -> None:
        spec = ruleset(
            entry_long={
                "all": [
                    condition(indicator("sma", 5), ">", {"value": "1"}),
                    {
                        "any": [
                            condition(indicator("rsi", 14), "<", {"value": "30"}),
                            {"none": [condition(indicator("ema", 200), "<", {"value": "1"})]},
                        ]
                    },
                ]
            },
            exit=NO_HISTORY_EXIT,
        )
        assert spec.required_warmup == 200


class TestCompilationRefusals:
    """Each of these compiles happily if the check is removed, and then trades.

    That is the point: none of them is a crash. A spec asking for `field: high`
    and silently getting closes produces a full equity curve that answers a
    question nobody asked, and nothing downstream can tell.
    """

    def test_an_unknown_indicator_is_refused_at_compile_time(self) -> None:
        spec = ruleset(
            entry_long={
                "all": [condition({"indicator": "vwap", "period": 20}, ">", {"value": "1"})]
            }
        )
        with pytest.raises(InvalidRuleError, match="unknown indicator 'vwap'"):
            compile_ruleset(spec)

    def test_the_refusal_names_what_is_available(self) -> None:
        """A typo'd name should not send the author reading source to find the
        list of real ones."""
        spec = ruleset(
            entry_long={
                "all": [condition({"indicator": "smaa", "period": 20}, ">", {"value": "1"})]
            }
        )
        with pytest.raises(InvalidRuleError, match="sma"):
            compile_ruleset(spec)

    def test_an_indicator_without_a_period_is_refused_rather_than_defaulted(self) -> None:
        """`ctx.indicator` falls back to 14 for a caller that omits one. A rule
        set must not: an `sma` silently given a period nobody chose is a
        different strategy than the one that was approved."""
        spec = ruleset(entry_long={"all": [condition({"indicator": "sma"}, ">", {"value": "1"})]})
        with pytest.raises(InvalidRuleError, match="needs a period"):
            compile_ruleset(spec)

    @pytest.mark.parametrize("period", [0, -5])
    def test_a_nonsense_period_is_refused(self, period: int) -> None:
        """`ta.sma(values, 0)` averages the whole series rather than raising —
        an SMA(0) would run, and mean something else entirely."""
        spec = ruleset(
            entry_long={
                "all": [condition({"indicator": "sma", "period": period}, ">", {"value": "1"})]
            }
        )
        with pytest.raises(InvalidRuleError, match="period of at least 1"):
            compile_ruleset(spec)

    def test_an_indicator_on_a_field_other_than_close_is_refused(self) -> None:
        """`indicators.dispatch` computes on closes. Accepting `field: high` and
        handing back the SMA of closes is the silent-wrong-answer case."""
        spec = ruleset(
            entry_long={
                "all": [
                    condition(
                        {"indicator": "sma", "period": 20, "field": "high"}, ">", {"value": "1"}
                    )
                ]
            }
        )
        with pytest.raises(InvalidRuleError, match="not available"):
            compile_ruleset(spec)

    def test_extra_indicator_params_are_refused(self) -> None:
        """Honouring one here rather than in `dispatch` is how a backtest and
        the live runner come to compute different numbers (ADR 0006)."""
        spec = ruleset(
            entry_long={
                "all": [
                    condition(
                        {"indicator": "ema", "period": 20, "params": {"smoothing": 3}},
                        ">",
                        {"value": "1"},
                    )
                ]
            }
        )
        with pytest.raises(InvalidRuleError, match="takes no extra params"):
            compile_ruleset(spec)

    def test_an_empty_condition_group_is_refused(self) -> None:
        """`all: []` is vacuously true. As an entry that means "enter on every
        bar", which nobody writes on purpose and which costs real money."""
        spec = ruleset(entry_long={"all": []})
        with pytest.raises(InvalidRuleError, match="empty condition group"):
            compile_ruleset(spec)

    def test_flatten_at_close_is_refused_rather_than_ignored(self) -> None:
        """A strategy never reads the clock (CLAUDE.md §1.5), so it cannot know
        where the session ends. Ignoring the flag would carry overnight risk the
        spec explicitly asked not to have."""
        spec = ruleset(
            risk={"position_size": {"type": "fixed_qty", "value": 10}, "flatten_at_close": True}
        )
        with pytest.raises(InvalidRuleError, match="flatten_at_close"):
            compile_ruleset(spec)

    def test_a_refusal_names_which_tree_it_came_from(self) -> None:
        spec = ruleset(
            exit={"any": [condition({"indicator": "nope", "period": 5}, ">", {"value": "1"})]}
        )
        with pytest.raises(InvalidRuleError, match="exit:"):
            compile_ruleset(spec)


# ── semantics ───────────────────────────────────────────────────────────────


class TestSignals:
    """What a compiled spec emits, and when."""

    def test_a_crossing_fires_on_the_transition_not_on_every_bar(self) -> None:
        """The mistake `docs/STRATEGY_AUTHORING.md` rule 6 exists for.

        Comparing only the current bar re-fires an entry on every bar that fast
        stays above slow, which on this fixture is most of them. Over two sine
        cycles the fast SMA crosses up twice, so there are two long entries —
        not the fifty-odd bars it spends above.
        """
        entries = [s for s in drive(ruleset(), wave()) if s.action is SignalAction.ENTER_LONG]
        assert len(entries) == 2

    def test_it_does_not_re_enter_something_it_already_holds(self) -> None:
        """Authoring rule 7, asserted by alternation rather than by count: a buy
        is only ever followed by a sell.

        Through the engine, because the position it must not re-enter only
        exists once an order has filled.
        """
        sides = [
            order.side
            for order in run_backtest(ruleset(), wave()).orders
            if order.status is OrderStatus.FILLED
        ]
        assert sides
        for first, second in itertools.pairwise(sides):
            assert first is not second

    def test_an_exit_is_only_emitted_against_a_position(self) -> None:
        """Driven with no position ever opening: the exit condition is true on
        the down-crosses, and must produce nothing."""
        spec = ruleset(entry_long=group(condition({"value": "1"}, ">", {"value": "2"})))
        assert drive(spec, wave()) == []

    def test_a_short_entry_emits_enter_short(self) -> None:
        spec = ruleset(
            entry_long=None,
            entry_short=group(
                condition(indicator("sma", 5), "crosses_below", indicator("sma", 20))
            ),
        )
        shorts = [s for s in drive(spec, wave()) if s.action is SignalAction.ENTER_SHORT]
        assert len(shorts) == 2

    def test_long_wins_when_both_entries_would_fire(self) -> None:
        """Both trees true on the same bar is a contradictory spec, and one
        signal has to be chosen. Long is taken, deterministically, rather than
        emitting both and letting the engine open and immediately reverse."""
        always = group(condition({"value": "2"}, ">", {"value": "1"}))
        spec = ruleset(entry_long=always, entry_short=always, exit=NO_HISTORY_EXIT)
        emitted = drive(spec, wave(30))
        assert emitted[0].action is SignalAction.ENTER_LONG

    def test_nothing_fires_before_the_window_is_full(self) -> None:
        """`ctx.history` refuses a short series, and a half-length window is
        exactly the partial indicator warmup exists to avoid trading on."""
        spec = ruleset(
            entry_long=group(condition(indicator("sma", 40), ">", {"value": "1"})),
            exit=NO_HISTORY_EXIT,
        )
        assert drive(spec, wave(30)) == []

    def test_a_wilder_indicator_actually_fires(self) -> None:
        """The regression test for the window bug: RSI(14) over a 60-bar series
        must produce a signal. Sized at 14 rather than 15, `dispatch.compute`
        answers None on every bar and this list is empty forever."""
        spec = ruleset(
            entry_long=group(condition(indicator("rsi", 14), "<", {"value": "45"})),
            exit=group(condition(indicator("rsi", 14), ">", {"value": "55"}), branch="any"),
        )
        assert drive(spec, wave(60))

    def test_the_reason_carries_the_values_it_was_decided_on(self) -> None:
        """Authoring rule 4: `reason` is what a human gets when they ask the
        dashboard why a trade is on. "entry_long" does not answer that."""
        first = drive(ruleset(), wave())[0]
        assert "entry_long" in first.reason
        assert "crosses_above" in first.reason
        assert "sma(5)=" in first.reason
        assert "sma(20)=" in first.reason

    def test_the_indicators_map_carries_every_operand_and_the_close(self) -> None:
        first = drive(ruleset(), wave())[0]
        assert set(first.indicators) == {"sma(5)", "sma(20)", "close"}
        assert first.indicators["sma(5)"] > first.indicators["sma(20)"]

    def test_the_signal_is_stamped_with_the_strategy_and_the_bar(self) -> None:
        first = drive(ruleset(), wave())[0]
        assert first.strategy_id == "test_rules"
        assert first.symbol == "SPY"
        assert first.ts.tzinfo is not None

    def test_a_symbol_outside_the_universe_is_ignored(self) -> None:
        """A run whose symbols do not meet the spec's universe takes no trades.
        It looks identical to a strategy that never signalled, which is why the
        docstring on `on_bar` names `universe` as the first thing to check."""
        spec = ruleset(universe=["QQQ"])
        assert drive(spec, wave(), symbol="SPY") == []

    def test_it_carries_no_stop_level_of_its_own(self) -> None:
        """The boundary this compiler keeps: levels come from the run's stop
        config, through `_with_derived_stop`. Emitting one here as well would
        give a single stop two sources that could disagree."""
        spec = ruleset(
            risk={
                "position_size": {"type": "fixed_qty", "value": 10},
                "stop_loss": {"type": "fixed_pct", "value": "0.05"},
            }
        )
        entries = [s for s in drive(spec, wave()) if s.is_entry]
        assert entries
        assert all(s.stop_loss_price is None for s in entries)


class TestThreeValuedLogic:
    """`None` is "cannot say yet", and it has to stay distinct from `False`.

    Driven against `_Evaluation` directly, which is the only way to reach it.
    `on_bar` sizes its window off `required_warmup` and returns early until that
    many bars exist, so by the time a tree is walked every operand in it has the
    history it needs — the property `test_the_window_resolves_every_operand`
    pins. These cases feed a short series past that guard to check what the
    interpreter does when an operand genuinely cannot answer, because "then it
    cannot happen" is a claim that stops being true the first time an indicator
    is added that fails for some other reason.
    """

    #: Ten bars: an SMA(4) resolves over them, an SMA(40) cannot.
    BARS = wave(10)

    def verdict(self, tree: dict[str, Any]) -> bool | None:
        node = RuleSet.model_validate({**ruleset().model_dump(), "entry_long": tree}).entry_long
        assert node is not None
        return _Evaluation(self.BARS).verdict(node)

    KNOWN_TRUE = condition(indicator("sma", 4), ">", {"value": "1"})
    KNOWN_FALSE = condition(indicator("sma", 4), "<", {"value": "1"})
    UNKNOWN = condition(indicator("sma", 40), ">", {"value": "1"})

    def test_the_premise_holds(self) -> None:
        """If these three stop meaning what their names say, every case below
        is asserting something else."""
        assert self.verdict(group(self.KNOWN_TRUE)) is True
        assert self.verdict(group(self.KNOWN_FALSE)) is False
        assert self.verdict(group(self.UNKNOWN)) is None

    def test_an_unknown_operand_does_not_satisfy_a_none_group(self) -> None:
        """The case that makes this three-valued rather than two.

        `none: [rsi(14) < 30]` reads as "not oversold". Collapse the unknown RSI
        of an early bar to `False` and the group answers `True` — so the spec
        enters on the first bar of every run, on the grounds that a number which
        does not exist yet is not below 30.
        """
        assert self.verdict({"none": [self.UNKNOWN]}) is None

    def test_a_none_group_holds_only_once_every_child_is_definitely_false(self) -> None:
        assert self.verdict({"none": [self.KNOWN_FALSE]}) is True
        assert self.verdict({"none": [self.KNOWN_TRUE]}) is False

    def test_one_definite_failure_settles_an_all_group(self) -> None:
        """No need to wait on the unknown sibling: `all` is already false."""
        assert self.verdict({"all": [self.KNOWN_FALSE, self.UNKNOWN]}) is False

    def test_an_all_group_stays_unknown_while_a_child_is(self) -> None:
        assert self.verdict({"all": [self.KNOWN_TRUE, self.UNKNOWN]}) is None

    def test_one_definite_success_settles_an_any_group(self) -> None:
        """The mirror: `any` is true whatever the unknown sibling turns out to be."""
        assert self.verdict({"any": [self.KNOWN_TRUE, self.UNKNOWN]}) is True

    def test_an_any_group_stays_unknown_while_a_child_is(self) -> None:
        assert self.verdict({"any": [self.KNOWN_FALSE, self.UNKNOWN]}) is None

    def test_a_crossing_is_unknown_when_only_the_previous_bar_is_missing(self) -> None:
        """An SMA(10) over exactly 10 bars has a value now and none a bar ago,
        so whether it *crossed* is unanswerable even though it is not."""
        crossing = condition(indicator("sma", 10), "crosses_above", {"value": "1"})
        assert (
            _Evaluation(self.BARS).value(
                RuleSet.model_validate({**ruleset().model_dump(), "entry_long": group(crossing)})
                .entry_long.all[0]
                .left
            )
            is not None
        )  # type: ignore[union-attr,index]
        assert self.verdict(group(crossing)) is None

    def test_the_window_resolves_every_operand(self) -> None:
        """Why the cases above are defensive rather than load-bearing.

        `required_warmup` is the deepest requirement in the spec, and `on_bar`
        will not evaluate anything until it has that many bars — so the first
        bar past warmup resolves every operand, including the extra one a
        crossing and a Wilder indicator each need. A window that did not would
        answer None for the whole run, not just its start.
        """
        spec = ruleset(
            entry_long=group(
                condition(indicator("rsi", 14), "crosses_above", indicator("sma", 20)),
                condition({"price": "close", "offset": 3}, ">", {"value": "1"}),
            ),
            exit=NO_HISTORY_EXIT,
        )
        bars = wave(spec.required_warmup)
        walk = _Evaluation(bars)
        assert walk.verdict(spec.entry_long) is not None  # type: ignore[arg-type]


class TestGating:
    """`cooldown_bars`, `max_holding_bars` and `max_concurrent_positions`.

    All three are counting, not pricing, which is why the strategy owns them:
    they need bars and positions, both of which a strategy may read, and no
    price level and no risk machinery, which it may not touch.
    """

    def test_cooldown_blocks_a_re_entry_until_it_has_elapsed(self) -> None:
        """Through the engine, not `drive`: a cooldown starts at an *exit*, and
        nothing exits in a run where no order ever fills."""
        without = _entry_fills(run_backtest(ruleset(), wave()))
        with_cooldown = _entry_fills(run_backtest(ruleset(cooldown_bars=60), wave()))
        assert without == 2
        assert with_cooldown == 1

    def test_a_cooldown_short_enough_to_elapse_blocks_nothing(self) -> None:
        assert _entry_fills(run_backtest(ruleset(cooldown_bars=2), wave())) == 2

    def test_max_holding_bars_exits_a_position_that_has_run_long(self) -> None:
        """A bar-count exit, and the only exit this compiler places itself."""
        emitted = drive(counting_spec(max_holding_bars=5), wave(40), portfolio=_holding_book())
        assert emitted
        assert emitted[0].action is SignalAction.EXIT
        assert "held 5 bars" in emitted[0].reason

    def test_max_holding_bars_counts_from_when_the_position_appeared(self) -> None:
        """The book is already long on bar 0, so bar 6 is the fifth bar after
        the one this first observed it — and the close proves which bar it is."""
        bars = wave(40)
        emitted = drive(counting_spec(max_holding_bars=5), bars, portfolio=_holding_book())
        assert emitted[0].indicators["close"] == pytest.approx(float(bars[5].close))

    def test_a_position_inside_its_limit_is_left_alone(self) -> None:
        assert drive(counting_spec(max_holding_bars=99), wave(40), portfolio=_holding_book()) == []

    def test_max_concurrent_positions_caps_new_entries(self) -> None:
        """Counted off the portfolio each bar rather than tracked, so a position
        closed by a stop — which the strategy never hears about — frees its slot
        on the next bar instead of holding it for the rest of the run."""
        spec = ruleset(universe=["SPY", "QQQ"], max_concurrent_positions=1)
        assert drive(spec, wave(), portfolio=_holding_book("QQQ")) == []

    def test_a_free_slot_still_admits_an_entry(self) -> None:
        spec = ruleset(universe=["SPY", "QQQ"], max_concurrent_positions=2)
        assert [s for s in drive(spec, wave(), portfolio=_holding_book("QQQ")) if s.is_entry]


class TestThroughTheEngine:
    """The point of compiling to a `Strategy`: nothing downstream knows.

    A rule set reaches the engine through the same path `SmaCrossover` does —
    the same warmup discard, the same next-bar fill, the same risk gate.
    """

    def test_a_compiled_spec_completes_a_round_trip(self) -> None:
        result = run_backtest(ruleset(), wave())
        filled = [o for o in result.orders if o.status is OrderStatus.FILLED]
        assert [o.side for o in filled][:2] == [Side.BUY, Side.SELL]
        assert not result.warnings

    def test_it_fills_at_the_next_bar_open_not_this_bar_close(self) -> None:
        """The lookahead invariant, asserted for a rule set specifically.

        Pinned to the exact bar rather than to "some open": the signal's own
        timestamp gives the bar it was decided on, and the fill has to land on
        the *following* bar's open. A strategy that could transact at the close
        it decided on has invented a profitable strategy that loses money in
        production (docs/BACKTESTING.md).
        """
        bars = wave()
        decided_at = drive(ruleset(), bars)[0].ts
        index = next(i for i, b in enumerate(bars) if b.ts == decided_at)

        first_fill = next(
            o for o in run_backtest(ruleset(), bars).orders if o.status is OrderStatus.FILLED
        )
        assert first_fill.avg_fill_price == bars[index + 1].open
        assert first_fill.avg_fill_price != bars[index].close

    def test_the_engine_discards_what_the_spec_signals_during_warmup(self) -> None:
        """`warmup_bars` is read off the spec, so a spec needing 20 bars trades
        no earlier than one hand-written to need 20."""
        strategy = compile_ruleset(ruleset())
        assert strategy.warmup_bars == ruleset().required_warmup == 21

    def test_a_rule_set_is_not_added_to_the_registry(self) -> None:
        """`register` refuses a duplicate name to keep results unambiguous, and
        a stored rule set is compiled afresh on every run of it — registering
        would fail the second time. It is identified by its row, not by the
        process-global registry."""
        from atp_core.strategy import all_strategies

        compile_ruleset(ruleset(name="not_registered"))
        compile_ruleset(ruleset(name="not_registered"))
        assert "not_registered" not in all_strategies()

    def test_the_repr_does_not_dump_the_whole_spec(self) -> None:
        """`Strategy.__repr__` prints params, and a rule set's params are the
        serialised spec — a screenful of JSON on every log line carrying one."""
        text = repr(compile_ruleset(ruleset()))
        assert "test_rules" in text
        assert "crosses_above" not in text

    def test_the_params_still_carry_the_whole_spec(self) -> None:
        """Trimmed in the repr, not in the data: a stored row recording `{}`
        would claim the strategy was configured with nothing."""
        assert compile_ruleset(ruleset()).params["universe"] == ["SPY"]


def _holding_book(symbol: str = "SPY") -> Portfolio:
    """A book already long `symbol`, for the exit-side and slot-count cases."""
    book = Portfolio(cash=Decimal(100_000), starting_equity=Decimal(100_000))
    book.positions[symbol] = Position(
        symbol=symbol, qty=Decimal(10), avg_entry_price=Decimal(100), last_price=Decimal(100)
    )
    return book


def _entry_fills(result: Any) -> int:
    return sum(
        1
        for order in result.orders
        if order.status is OrderStatus.FILLED and order.side is Side.BUY
    )
