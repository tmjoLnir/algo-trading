"""A stored rule set reaching a run.

Step 2 of the work `test_backtest_run_spec.py` is step 1 of: the spec can now
carry the rules themselves, and `build_engine` compiles them instead of looking
a name up in the registry.

The reason the rules travel *with the run* rather than being fetched by name is
the one thing to hold onto here. A rule set is editable in the UI — that is what
it is for — so a run that recorded only `strategy_id` would replay differently
the day somebody adjusted a threshold, silently, with both numbers filed under
the same name. `test_the_run_keeps_the_rules_it_ran_on` is that property.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from atp_core.backtest.engine import BacktestEngine
from atp_core.backtest.ports import BacktestRunSpec
from atp_core.backtest.runner import _resolve_strategy, build_engine
from atp_core.config import get_settings
from atp_core.errors import ConfigError
from atp_core.persistence.backtests import _spec_from_json, _spec_to_json
from atp_core.strategy.examples import rsi_mean_reversion
from atp_core.strategy.examples.sma_crossover import SmaCrossover

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 12, 31, tzinfo=UTC)


def a_spec(**overrides: Any) -> BacktestRunSpec:
    fields: dict[str, Any] = {
        "strategy_id": "sma_crossover",
        "symbols": ("SPY",),
        "start": START,
        "end": END,
        "timeframe": "1d",
        "starting_cash": "100000",
        "cost_model": "zero",
    }
    fields.update(overrides)
    return BacktestRunSpec(**fields)


def shipped_rules() -> dict[str, Any]:
    """The RSI rule set as JSON, which is how a spec carries one."""
    return rsi_mean_reversion().model_dump(mode="json")


def through_the_column(spec: BacktestRunSpec) -> BacktestRunSpec:
    """The spec as the worker receives it — via `json`, the column's medium."""
    return _spec_from_json(spec.strategy_id, json.loads(json.dumps(_spec_to_json(spec))))


class TestResolvingTheStrategy:
    def test_a_spec_with_rules_compiles_them(self) -> None:
        strategy = _resolve_strategy(
            a_spec(strategy_id="rsi_mean_reversion", ruleset=shipped_rules())
        )
        assert strategy.name == "rsi_mean_reversion"
        assert strategy.warmup_bars == 200

    def test_a_spec_without_rules_still_uses_the_registry(self) -> None:
        """Every run stored today. The registry path is unchanged and is what
        the absent field means."""
        strategy = _resolve_strategy(a_spec(params={"fast_period": 20, "slow_period": 50}))
        assert isinstance(strategy, SmaCrossover)

    def test_the_rules_win_over_a_registered_name(self) -> None:
        """Not a fallback for a name the registry does not know.

        A run that recorded rules executed those rules. Consulting the registry
        as well would let a coded class sharing the id decide what a stored run
        meant — the ambiguity `register` refuses duplicate names to prevent.
        """
        spec = a_spec(strategy_id="sma_crossover", ruleset=shipped_rules())
        strategy = _resolve_strategy(spec)
        assert not isinstance(strategy, SmaCrossover)
        assert strategy.name == "rsi_mean_reversion"

    def test_malformed_rules_are_a_config_error(self) -> None:
        """A `ConfigError` is a 400 at the API door and a readable failure on the
        row; a `ValidationError` escaping from core would be a stack trace."""
        with pytest.raises(ConfigError, match="malformed"):
            _resolve_strategy(a_spec(ruleset={"name": "broken", "universe": []}))

    def test_rules_that_validate_but_cannot_compile_are_a_config_error(self) -> None:
        """`RuleSet` accepts an indicator name; `compile_ruleset` is what refuses
        one `indicators.dispatch` cannot compute."""
        rules = shipped_rules()
        rules["entry_long"]["all"][0]["left"]["indicator"] = "vwap"
        with pytest.raises(ConfigError, match="does not compile"):
            _resolve_strategy(a_spec(ruleset=rules))

    def test_an_empty_ruleset_is_not_treated_as_rules(self) -> None:
        """`{}` and absent mean the same thing — no rules — and the reader
        normalises one to the other so this cannot become "compile nothing"."""
        assert isinstance(_resolve_strategy(through_the_column(a_spec(ruleset={}))), SmaCrossover)


class TestThroughTheColumn:
    def test_the_run_keeps_the_rules_it_ran_on(self) -> None:
        """The whole reason the rules are on the spec.

        The worker rebuilds its engine from this column and nothing else, so
        what survives here is what executed — and it stays what executed after
        somebody edits the strategy row.
        """
        spec = a_spec(strategy_id="rsi_mean_reversion", ruleset=shipped_rules())
        assert through_the_column(spec).ruleset == shipped_rules()

    def test_the_worker_can_build_an_engine_from_it(self) -> None:
        spec = through_the_column(
            a_spec(
                strategy_id="rsi_mean_reversion",
                ruleset=shipped_rules(),
                sizing_method="risk_pct",
                sizing_value="0.01",
                stop_type="atr",
                stop_value="2.0",
                stop_period=14,
            )
        )
        engine = build_engine(spec, limits=get_settings().risk)
        assert isinstance(engine, BacktestEngine)
        assert engine.strategy.name == "rsi_mean_reversion"

    def test_the_specs_own_stop_reaches_the_engine(self) -> None:
        """Only true since step 1: these fields did not survive the column, so a
        rule set sized by `risk_pct` would have been refused at sizing for want
        of a stop the request had actually asked for."""
        spec = through_the_column(
            a_spec(ruleset=shipped_rules(), stop_type="atr", stop_value="2.0", stop_period=14)
        )
        engine = build_engine(spec, limits=get_settings().risk)
        assert engine.stop_config is not None
        assert engine.stop_config.period == 14
