"""The backtest CLI's argument handling and reporting.

Only the parts that decide something before any I/O happens. The run itself is
`BacktestEngine`, tested in `test_backtest_engine.py`; what is under test here
is that a mistyped argument is refused with a sentence an operator can act on,
rather than surfacing as a traceback from the config layer or — worse — as a
backtest of something they did not ask for.

`scripts/` is not an importable package, so the module is loaded by path.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import math
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest

from atp_core.backtest.ports import BacktestRunSpec, spec_to_json
from atp_core.backtest.runner import STOP_TYPES
from atp_core.persistence.backtests import _spec_from_json

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_backtest", REPO_ROOT / "scripts" / "run_backtest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_backtest"] = module
    spec.loader.exec_module(module)
    return module


cli = _load()

BASE = [
    "--strategy",
    "sma_crossover",
    "--symbols",
    "SPY",
    "--start",
    "2021-01-01",
    "--end",
    "2024-01-01",
]


def run(argv: list[str]) -> int:
    # `cli` is loaded by path (see the module docstring), so it is `Any` to mypy.
    return cast("int", asyncio.run(cli.main(argv)))


class TestArgumentRefusals:
    """Each of these must fail before touching the database. If one of them
    ever reaches `_load_bars`, the test hangs or errors on a missing DB rather
    than passing quietly — which is the signal we want."""

    def test_empty_symbols(self) -> None:
        with pytest.raises(SystemExit, match="--symbols is empty"):
            run(["--strategy", "sma_crossover", "--symbols", " , ", *BASE[4:]])

    def test_unknown_timeframe_lists_the_valid_ones(self) -> None:
        with pytest.raises(SystemExit, match="1m, 5m, 15m, 30m, 1h, 4h, 1d"):
            run([*BASE, "--timeframe", "3d"])

    def test_malformed_date(self) -> None:
        with pytest.raises(SystemExit, match="--start must be YYYY-MM-DD"):
            run(
                [
                    "--strategy",
                    "sma_crossover",
                    "--symbols",
                    "SPY",
                    "--start",
                    "01/01/2021",
                    "--end",
                    "2024-01-01",
                ]
            )

    def test_reversed_range(self) -> None:
        with pytest.raises(SystemExit, match="must be before"):
            run([*BASE[:4], "--start", "2024-01-01", "--end", "2021-01-01"])

    def test_params_must_be_json(self) -> None:
        with pytest.raises(SystemExit, match="--params must be valid JSON"):
            run([*BASE, "--params", "{not json}"])

    def test_params_must_be_an_object(self) -> None:
        with pytest.raises(SystemExit, match="must be a JSON object"):
            run([*BASE, "--params", "[1, 2]"])

    def test_non_positive_qty(self) -> None:
        with pytest.raises(SystemExit, match="--qty must be positive"):
            run([*BASE, "--qty", "0"])

    def test_non_positive_cash(self) -> None:
        with pytest.raises(SystemExit, match="--cash must be positive"):
            run([*BASE, "--cash", "-1"])

    def test_an_unknown_stop_type_is_refused_by_the_parser(self) -> None:
        """`choices` from `STOP_TYPES`, so the CLI cannot accept a name the
        engine has no `StopType` for — the refusal is the parser's, before any
        bars are loaded."""
        with pytest.raises(SystemExit):
            cli.parse_args([*BASE, "--stop", "trailing"])

    @pytest.mark.parametrize("stop_type", sorted(STOP_TYPES))
    def test_every_stop_the_engine_knows_is_offered(self, stop_type: str) -> None:
        """The parser's choices are `STOP_TYPES` itself. A type the engine
        gained and this did not would be unreachable from the one entry point
        an operator has."""
        assert cli.parse_args([*BASE, "--stop", stop_type]).stop == stop_type

    def test_the_stop_flags_reach_the_spec(self) -> None:
        """Parsed, not merely accepted: an argument the parser takes and the
        spec drops is a run protected differently from what was asked for."""
        args = cli.parse_args([*BASE, "--stop", "atr", "--stop-value", "2", "--stop-period", "20"])

        assert args.stop == "atr"
        assert args.stop_value == "2"
        assert args.stop_period == 20
        assert args.stop_bars == 0

    def test_no_stop_flag_means_no_stop(self) -> None:
        """The default is what every run this CLI has produced did: arm only
        what the strategy emits. `main` says so out loud rather than leaving an
        operator to infer it."""
        args = cli.parse_args(BASE)

        assert args.stop is None
        assert args.stop_bars == 0

    def test_unknown_strategy_names_the_registered_ones(self) -> None:
        """The registry is only populated by importing the examples package,
        which the CLI does. If that import is ever dropped, this fails with an
        empty list rather than the run silently finding no strategies."""
        with pytest.raises(SystemExit, match=r"unknown strategy 'nope'.*sma_crossover"):
            run(["--strategy", "nope", *BASE[2:]])

    def test_an_empty_strategy_is_not_reported_as_an_unknown_one(self) -> None:
        """`required=True` puts the flag on the command line; it does not stop
        `--strategy ""`, which is what an unset shell variable expands to.

        The registry answers a blank the same way it answers a typo — a failed
        lookup listing everything registered — so an operator who passed nothing
        was told their strategy was missing from the registry. Naming nothing
        and naming the wrong thing are different mistakes.
        """
        with pytest.raises(SystemExit, match=r"--strategy is empty"):
            run(["--strategy", "", *BASE[2:]])

    def test_a_padded_strategy_name_is_stripped_before_it_is_looked_up(self) -> None:
        """The refusal names `nope`, not `  nope  `, so the strip happens before
        the registry sees it.

        It has to happen before the *spec* too, and that is the less obvious
        half: `build_engine` resolves `spec.strategy_id` against the registry a
        second time, so a name stripped only for this function's own lookup
        would pass here and then fail inside the engine — the same misleading
        message, one layer deeper and past the point where a flag name could be
        mentioned. That path needs settings and a database, so the spec itself
        is asserted on the API's equivalent test rather than here.
        """
        with pytest.raises(SystemExit, match=r"unknown strategy 'nope'"):
            run(["--strategy", "  nope  ", *BASE[2:]])


class TestFormatting:
    def test_percentages_money_and_counts(self) -> None:
        assert cli._format(0.1041, "pct").strip() == "10.41%"
        assert cli._format(1234.5, "money").strip() == "1,234.50"
        assert cli._format(477, "int").strip() == "477"
        assert cli._format(0.3579, "num").strip() == "0.358"

    def test_infinity_is_spelled_out_not_printed_as_inf(self) -> None:
        """`profit_factor` is legitimately infinite when nothing lost. `inf` in
        a report column reads as a crash; the word does not."""
        assert cli._format(float("inf"), "num").strip() == "infinite"


class TestJsonSafety:
    """The CLI's `--out` file and the `backtest_runs.metrics` column are the same
    serialisation problem, so they share one function — `atp_core.backtest.runner
    .jsonable`, which the CLI imports. These exercise it through the CLI's own
    namespace, which is where a reader of this file expects to find it."""

    def test_infinity_becomes_null(self) -> None:
        """Infinity is a real metric value and not legal JSON. `json.dumps`
        would emit a bare `Infinity`, which most parsers reject — so the file
        would fail to load in exactly the tools meant to read it."""
        cleaned = cli.jsonable({"profit_factor": float("inf"), "sharpe": 0.5})
        assert cleaned == {"profit_factor": None, "sharpe": 0.5}
        assert json.dumps(cleaned, allow_nan=False)

    def test_nan_becomes_null(self) -> None:
        assert cli.jsonable({"x": math.nan}) == {"x": None}

    def test_nested_structures_are_cleaned(self) -> None:
        cleaned = cli.jsonable({"a": [{"b": float("-inf")}], "c": "ok"})
        assert cleaned == {"a": [{"b": None}], "c": "ok"}

    def test_ordinary_values_pass_through_untouched(self) -> None:
        payload = {"s": "SPY", "n": 3, "f": 1.5, "l": [1, 2]}
        assert cli.jsonable(payload) == payload


class TestHeadlineSet:
    def test_every_headline_key_exists_on_the_metric_set(self) -> None:
        """The report indexes `metrics[key]` directly, so a renamed metric
        would be a KeyError at the end of a long run rather than at import."""
        from atp_core.backtest.metrics import PerformanceMetrics

        fields = set(PerformanceMetrics.__slots__)
        assert {key for key, _, _ in cli._HEADLINE} <= fields


#: Every optional field off its default, so a field the export drops cannot pass
#: by coincidence. `test_the_sample_differs_from_every_default` guards that, for
#: the same reason `test_backtest_run_spec.py` guards its own copy: the original
#: dropped-field bug was invisible precisely because the values that went
#: missing were the ones a default would have supplied anyway.
EXPORTED = BacktestRunSpec(
    strategy_id="sma_crossover",
    symbols=("SPY", "QQQ"),
    start=datetime(2024, 1, 2, tzinfo=UTC),
    end=datetime(2024, 12, 31, tzinfo=UTC),
    timeframe="1h",
    starting_cash="250000.55",
    cost_model="zero",
    params={"fast_period": 10, "slow_period": 30},
    ruleset={"name": "a_rule_set", "universe": ["SPY"]},
    qty="42",
    sizing_method="risk_pct",
    sizing_value="0.01",
    stop_type="atr",
    stop_value="2.5",
    stop_period=21,
    stop_bars=7,
)


class _FakeResult:
    """Enough of `BacktestResult` for the report assembly, which reads two
    attributes of it and nothing else.

    A real result would need bars, an engine and a database to produce, and none
    of the three would make these assertions say anything more.
    """

    equity_curve: ClassVar[list[tuple[datetime, Decimal]]] = [
        (datetime(2024, 1, 2, tzinfo=UTC), Decimal("250000.55")),
        (datetime(2024, 1, 3, tzinfo=UTC), Decimal("250125.80")),
    ]

    def to_report(self) -> dict[str, Any]:
        return {
            "strategy": "sma_crossover",
            "symbols": ["SPY", "QQQ"],
            "timeframe": "1h",
            "start": "2024-01-02T00:00:00+00:00",
            "end": "2024-12-31T00:00:00+00:00",
            "ending_equity": "250125.80",
            "metrics": {"total_return": 0.0005},
            "warnings": [],
        }


class TestTheExportedSpec:
    """`--out` records what was asked for, not only what came back.

    The file used to carry the strategy, the universe and the window and stop
    there. Everything else an operator chose — the cost model, the sizing method
    and its value, the stop and its parameters, the strategy params — was
    reachable from the command line and recorded nowhere, so two runs that
    differed in how they were sized produced two files that looked comparable
    and were not. A `--zero-cost` run was indistinguishable from a costed one,
    which is the case docs/BACKTESTING.md is most insistent about.
    """

    def test_the_sample_differs_from_every_default(self) -> None:
        """Guards the guard, as in `test_backtest_run_spec.py`: a field sitting
        at its default would survive an export that dropped it."""
        for field in dataclasses.fields(BacktestRunSpec):
            if field.default is dataclasses.MISSING:
                continue
            assert getattr(EXPORTED, field.name) != field.default, (
                f"{field.name} is at its default, so no test here can tell "
                f"whether it survives the export"
            )

    def test_the_spec_travels_with_the_result(self) -> None:
        assert "spec" in cli.build_report(_FakeResult(), EXPORTED)

    @pytest.mark.parametrize("name", [f.name for f in dataclasses.fields(BacktestRunSpec)])
    def test_every_field_is_recorded(self, name: str) -> None:
        """Driven off `dataclasses.fields` rather than a list written by hand,
        so a field added to the spec cannot reach the engine while going missing
        from the file that claims to describe the run."""
        assert name in cli.build_report(_FakeResult(), EXPORTED)["spec"]

    def test_the_recorded_spec_round_trips(self) -> None:
        """The point of recording it: the file is enough to say what ran, well
        enough that the spec can be rebuilt from it and compared."""
        restored = _spec_from_json(
            EXPORTED.strategy_id, cli.build_report(_FakeResult(), EXPORTED)["spec"]
        )
        assert restored == EXPORTED

    def test_it_is_the_same_block_the_queued_path_stores(self) -> None:
        """The whole reason `spec_to_json` was hoisted out of the persistence
        adapter. A CLI run and a dashboard run have to describe themselves
        identically, or comparing one against the other means reconciling two
        formats first."""
        assert cli.build_report(_FakeResult(), EXPORTED)["spec"] == spec_to_json(EXPORTED)

    def test_it_survives_the_json_round_trip(self) -> None:
        """`--out` writes through `jsonable` with `allow_nan=False`. A spec
        holding something unserialisable would fail at the end of a long run."""
        written = json.loads(json.dumps(cli.jsonable(cli.build_report(_FakeResult(), EXPORTED))))
        assert written["spec"]["sizing_method"] == "risk_pct"
        assert written["spec"]["stop_type"] == "atr"
        assert written["spec"]["cost_model"] == "zero"


class TestTheRestOfTheReport:
    def test_the_spec_displaces_nothing(self) -> None:
        """The added key is additive. Anything already reading these files —
        a notebook, a saved comparison — keeps reading them."""
        report = cli.build_report(_FakeResult(), EXPORTED)
        for key in _FakeResult().to_report():
            assert key in report

    def test_the_curve_is_stringified_money(self) -> None:
        """Decimals, not floats: the curve that reaches a chart is the one the
        engine computed (CLAUDE.md §1.1)."""
        curve = cli.build_report(_FakeResult(), EXPORTED)["equity_curve"]
        assert curve[0] == ["2024-01-02T00:00:00+00:00", "250000.55"]
        assert all(isinstance(value, str) for _, value in curve)
