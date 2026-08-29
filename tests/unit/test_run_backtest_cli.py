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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest

from atp_core.backtest.ports import BacktestRunSpec, spec_to_json
from atp_core.backtest.runner import (
    FIXED_QTY_WARNING,
    NO_STOP_WARNING,
    STOP_TYPES,
    ZERO_COST_WARNING,
    all_warnings,
    open_positions_note,
    refusal_summary,
    risk_chain_summary,
    run_spec,
)
from atp_core.config import get_settings
from atp_core.domain import Bar, Portfolio, Timeframe
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


#: A metric set with nothing to complain about: enough round trips for the
#: statistics to mean something, and a Sharpe below the "bug until proven
#: otherwise" line. Named because several tests below need the *absence* of a
#: derived warning, and a bare dict at each of them would drift.
_TRUSTWORTHY = {"total_return": 0.0005, "num_trades": 42, "sharpe": 1.3}

#: A real run, small. `EXPORTED` above describes a spec and never executes one,
#: which is all the report-assembly tests need; the caveats `run_spec` attaches
#: are properties of a run that happened, so the tests below have to run one.
_RUN_START = datetime(2024, 1, 2, tzinfo=UTC)
_RUN_BARS = 90


def _wave(count: int = _RUN_BARS) -> list[Bar]:
    """A wave, so the shipped crossover actually crosses — a ramp never does."""
    series = []
    for index in range(count):
        base = Decimal(str(round(100 + 12 * math.sin(2 * math.pi * index / (count / 2)), 2)))
        series.append(
            Bar(
                symbol="SPY",
                ts=_RUN_START + timedelta(days=index),
                timeframe=Timeframe.D1,
                open=base,
                high=base + Decimal("1"),
                low=base - Decimal("1"),
                close=base + Decimal("0.5"),
                # Synthetic bars have no corporate actions, so the adjusted close
                # is the close. Present rather than null because the engine prices
                # off adjusted closes and refuses a series without them.
                adj_close=base + Decimal("0.5"),
                volume=Decimal("5000000"),
            )
        )
    return series


def _runnable(**overrides: Any) -> BacktestRunSpec:
    """A spec that produces a real result and earns none of the three caveats:
    a costed model, sizing that is not a flat share count, and orders the chain
    lets through. A test asserting a caveat appears has turned it on itself.

    `equity_pct` rather than `risk_pct`: the latter sizes off a stop distance,
    and with no stop configured `position_sizing` refuses every entry — which
    makes the run carry a refusal summary and stop being the quiet baseline
    these tests measure against.
    """
    fields: dict[str, Any] = {
        "strategy_id": "sma_crossover",
        "symbols": ("SPY",),
        "start": _RUN_START,
        "end": _RUN_START + timedelta(days=_RUN_BARS + 1),
        "timeframe": "1d",
        "starting_cash": "100000",
        "cost_model": "alpaca_equities",
        "params": {"fast_period": 5, "slow_period": 20},
        "qty": "10",
        "sizing_method": "equity_pct",
        "sizing_value": "0.05",
    }
    fields.update(overrides)
    return BacktestRunSpec(**fields)


def _exported(spec: BacktestRunSpec) -> dict[str, Any]:
    """Run it and assemble the `--out` file, which is the pair under test."""
    result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)
    return cast("dict[str, Any]", cli.build_report(result, spec))


class _FakeResult:
    """Enough of `BacktestResult` for the report assembly, which reads two
    attributes of it and nothing else.

    A real result would need bars, an engine and a database to produce, and none
    of the three would make these assertions say anything more.

    `metrics` and `warnings` are settable because the report now derives one
    from the other, so a fixed pair could only ever exercise one branch of it.
    """

    equity_curve: ClassVar[list[tuple[datetime, Decimal]]] = [
        (datetime(2024, 1, 2, tzinfo=UTC), Decimal("250000.55")),
        (datetime(2024, 1, 3, tzinfo=UTC), Decimal("250125.80")),
    ]

    def __init__(
        self,
        *,
        metrics: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self._metrics = {"total_return": 0.0005} if metrics is None else metrics
        self._warnings = [] if warnings is None else warnings

    def to_report(self) -> dict[str, Any]:
        return {
            "strategy": "sma_crossover",
            "symbols": ["SPY", "QQQ"],
            "timeframe": "1h",
            "start": "2024-01-02T00:00:00+00:00",
            "end": "2024-12-31T00:00:00+00:00",
            "ending_equity": "250125.80",
            "metrics": dict(self._metrics),
            "warnings": list(self._warnings),
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


class TestTheExportedWarnings:
    """`--out` records how far to trust the numbers, not only what the run did.

    The file used to carry `to_report()`'s warnings, which are what the run
    *did* — coverage shortfalls, refusals — and nothing about how far to trust
    the statistics printed beside them. The derived notes were computed and
    printed to the terminal, then dropped on the way to disk.

    The case that made it visible: a `buy_and_hold` export over twenty real
    symbols, whose `num_trades` was 0 because the strategy never sells, and
    with it nine placeholder-zero metrics. The dashboard's export of the same
    run said so; the CLI's said `"warnings": []`. Two files, identical in all
    1,525 equity points and all nineteen metrics, disagreeing about whether the
    result could be believed — and the one an operator archives is the one that
    stayed quiet.
    """

    def test_the_sample_size_caveat_reaches_the_file(self) -> None:
        report = cli.build_report(_FakeResult(metrics={"num_trades": 0}), EXPORTED)
        assert any("only 0 trades" in warning for warning in report["warnings"])

    def test_the_sharpe_caveat_reaches_the_file(self) -> None:
        """The other half of `suspicious`, and the one that fires on the result
        an operator is least inclined to question."""
        report = cli.build_report(_FakeResult(metrics={**_TRUSTWORTHY, "sharpe": 9.1}), EXPORTED)
        assert any("9.10" in warning for warning in report["warnings"])

    def test_it_is_the_set_the_queued_path_serves(self) -> None:
        """The point, and the same argument as `spec_to_json` above: one
        function decides what a finished run has to say about itself, or a CLI
        export and a dashboard export of one run caveat it differently."""
        result = _FakeResult(metrics={"num_trades": 3}, warnings=["coverage: SPY starts late"])
        assert cli.build_report(result, EXPORTED)["warnings"] == all_warnings(
            ["coverage: SPY starts late"], {"num_trades": 3}
        )

    def test_the_runs_own_warnings_survive(self) -> None:
        """Additive. The derived notes are prepended to what the engine
        recorded, never in place of it."""
        result = _FakeResult(metrics={"num_trades": 0}, warnings=["coverage: SPY starts late"])
        assert "coverage: SPY starts late" in cli.build_report(result, EXPORTED)["warnings"]

    def test_the_derived_notes_come_first(self) -> None:
        """`all_warnings`' ordering, asserted here because this file is the
        other place it has to hold: the engine's last line is the one that says
        how much of the run actually happened, and a note about sample size
        wedged after it would separate that line from the return it qualifies.
        """
        result = _FakeResult(metrics={"num_trades": 0}, warnings=["risk refused 4 orders"])
        assert cli.build_report(result, EXPORTED)["warnings"][-1] == "risk refused 4 orders"

    def test_a_trustworthy_run_gains_nothing(self) -> None:
        """The caveats are conditional, not decoration. A run with enough trades
        and a believable Sharpe exports exactly what the engine recorded."""
        result = _FakeResult(metrics=_TRUSTWORTHY, warnings=["coverage: SPY starts late"])
        assert cli.build_report(result, EXPORTED)["warnings"] == ["coverage: SPY starts late"]

    def test_it_survives_the_json_round_trip(self) -> None:
        """`--out` writes through `jsonable` with `allow_nan=False`, and the
        Sharpe note interpolates a float."""
        report = cli.build_report(_FakeResult(metrics={"num_trades": 0, "sharpe": 9.1}), EXPORTED)
        written = json.loads(json.dumps(cli.jsonable(report)))
        assert len(written["warnings"]) == 2
        assert all(isinstance(warning, str) for warning in written["warnings"])


class TestTheCaveatsTheRunAttaches:
    """`--out` carries the three caveats `run_spec` owns, not only the two the
    metrics imply.

    The CLI used to call `build_engine(...).run(...)` and state these three on
    screen itself: the zero-cost and fixed-qty notes above the run, the refusal
    summary below the table. None of them reached the file, so a `--zero-cost`
    export — a debugging run, by construction not evidence about anything —
    recorded the cost model in its `spec` block and said nothing about it in its
    `warnings`. The queued path has attached all three since it was written.
    """

    def test_a_zero_cost_run_says_so_in_the_file(self) -> None:
        """The one that matters most: this file is the one that reads as
        evidence when it is not."""
        assert ZERO_COST_WARNING in _exported(_runnable(cost_model="zero"))["warnings"]

    def test_a_costed_run_does_not(self) -> None:
        assert ZERO_COST_WARNING not in _exported(_runnable())["warnings"]

    def test_a_fixed_qty_run_says_so_in_the_file(self) -> None:
        """With the share count in it, because the caveat is that the return is
        a property of that number."""
        exported = _exported(_runnable(sizing_method="fixed_qty", sizing_value="10"))
        assert FIXED_QTY_WARNING.format(qty=Decimal("10")) in exported["warnings"]

    def test_a_run_sized_another_way_does_not(self) -> None:
        """It used to be said unconditionally. Saying it of an `equity_pct` run
        would be the file warning about something that did not happen."""
        assert not any("sized at" in w for w in _exported(_runnable())["warnings"])

    def test_the_zero_cost_caveat_leads(self) -> None:
        """`run_spec` inserts it first because it invalidates everything under
        it, and `all_warnings` puts the derived notes ahead of the stored ones.
        The first *stored* line is still the one that says the run was not real.
        """
        exported = _exported(_runnable(cost_model="zero"))
        derived = [w for w in exported["warnings"] if "docs/BACKTESTING.md 'Reading" in w]
        assert exported["warnings"][len(derived)] == ZERO_COST_WARNING

    def test_the_open_position_caveat_reaches_the_file(self) -> None:
        """ADR 0019's own case, one layer up. That ADR put `open_positions` and
        `unrealized_pnl` on the run so a reader *could* see the split; this is
        the sentence saying it matters, which the CLI printed under the table
        and recorded nowhere. A run that ends holding winners reports a gain its
        closed trades never made, and every trade statistic beside it counts
        closed round trips only.
        """
        spec = _runnable()
        result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)
        exported = cli.build_report(result, spec)["warnings"]

        assert result.portfolio.open_positions, "the baseline run should end holding one"
        assert open_positions_note(result) in exported

    def test_a_run_that_ends_flat_says_nothing_about_open_positions(self) -> None:
        """A real `Portfolio` rather than an extension of `_FakeResult`, which
        exists to be exactly what the report assembly reads and nothing more.
        `open_positions` is the property under test, so faking it would leave
        the test asserting against its own stub."""
        flat = SimpleNamespace(
            portfolio=Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))
        )

        assert not flat.portfolio.open_positions
        assert open_positions_note(cast("Any", flat)) is None

    def test_the_open_position_caveat_carries_decimal_money(self) -> None:
        """The unrealised figure is money, so it is formatted off the `Decimal`
        rather than through `float` (CLAUDE.md §1.1) — this string is the only
        place several readers will meet the number."""
        spec = _runnable()
        result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)
        note = open_positions_note(result)

        assert note is not None
        assert f"{result.unrealized_pnl:,.2f}" in note

    def test_the_missing_stop_caveat_reaches_the_file(self) -> None:
        """What a strategy was protected by is part of what it is. `_runnable`
        configures no stop, and `sma_crossover` emits no level of its own."""
        assert NO_STOP_WARNING in _exported(_runnable())["warnings"]

    def test_a_run_behind_a_stop_does_not(self) -> None:
        exported = _exported(_runnable(stop_type="atr", stop_value="2", stop_period=14))
        assert NO_STOP_WARNING not in exported["warnings"]

    def test_the_risk_chain_caveat_reaches_the_file(self) -> None:
        """The one every run earns, and the one the file most needed: an export
        full of refusals reads as a complete chain doing its job, and four of
        the nine rules were never consulted."""
        assert risk_chain_summary() in _exported(_runnable())["warnings"]

    def test_the_file_and_the_terminal_agree(self) -> None:
        """The whole point. Everything the CLI states in its own place is also
        in the file — `stated_separately` decides what the table skips, never
        what the run records."""
        spec = _runnable(cost_model="zero", sizing_method="fixed_qty", sizing_value="10")
        result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)
        exported = cli.build_report(result, spec)["warnings"]

        assert cli.stated_separately(spec, result) <= set(exported)


class TestWhatTheTableRepeats:
    """The terminal says each caveat once, and the file keeps all of them.

    Routing the CLI through `run_spec` put three warnings onto the result that
    the CLI already prints in its own places. Left alone they would print twice
    in one run — which is why `stated_separately` exists, and why it filters the
    *printing* rather than trimming the result.
    """

    def test_it_skips_what_the_preamble_said(self) -> None:
        spec = _runnable(cost_model="zero")
        result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)
        assert ZERO_COST_WARNING in cli.stated_separately(spec, result)

    def test_it_skips_the_refusal_summary(self) -> None:
        """Printed under the table on its own, where the ten-warning cap that
        block applies cannot swallow the line saying how much of the run
        actually happened."""
        spec = _runnable(sizing_method="fixed_notional", sizing_value="100000000")
        result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)
        refusals = refusal_summary(result)

        assert refusals is not None, "the oversized order should have been refused"
        assert refusals in cli.stated_separately(spec, result)

    def test_it_claims_exactly_what_the_terminal_said_and_nothing_else(self) -> None:
        """This asserted an empty set while `_runnable` earned no caveat at all.
        It earns two now — it configures no stop and ends holding a position —
        and every run earns the risk-chain line, so the empty set stopped being
        the thing worth pinning. The exact set is the stronger assertion anyway:
        it catches a caveat this function claims the terminal said when the
        terminal did not, which is the failure that silently drops a warning
        from the table.
        """
        spec = _runnable()
        result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)

        assert cli.stated_separately(spec, result) == {
            risk_chain_summary(),
            NO_STOP_WARNING,
            open_positions_note(result),
        }

    def test_an_unnamed_sizing_method_is_not_claimed_as_said(self) -> None:
        """`resolve_sizing` reads an empty method as `fixed_qty`, so `run_spec`
        attaches the share-count caveat — but the preamble, which tests the
        argument, printed nothing. Claiming it here would filter the run's only
        mention of it out of the table and leave the terminal silent.

        Unreachable from the CLI itself, whose `--sizing` has a default and a
        choice list. Pinned because the two conditions are equivalent only for
        as long as that stays true.
        """
        spec = _runnable(sizing_method="", sizing_value="")
        result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)

        attached = FIXED_QTY_WARNING.format(qty=Decimal("10"))
        assert attached in result.warnings, "run_spec should have attached it"
        assert attached not in cli.stated_separately(spec, result)

    def test_the_engines_own_warnings_still_print(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The filter is for what the CLI says elsewhere, not a mute button. A
        coverage shortfall has no other place to appear."""
        spec = _runnable()
        result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)
        result.warnings.append("coverage: SPY starts late")
        cli._print_report("s", ["SPY"], result, result.metrics, Decimal("0"), set())

        assert "coverage: SPY starts late" in capsys.readouterr().out

    def test_the_three_new_caveats_reach_the_file_without_printing_twice(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The invariant the whole arrangement rests on, asserted on all three
        at once: each is on the result — so in `--out` and on a queued run —
        and none of them is in the table's warning block, because `main` prints
        each in its own place. Getting this wrong in the safe direction prints a
        caveat twice; getting it wrong the other way is how one disappears.
        """
        spec = _runnable()
        result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)
        cli._print_report(
            "s", ["SPY"], result, result.metrics, Decimal("0"), cli.stated_separately(spec, result)
        )
        printed = capsys.readouterr().out

        for caveat in (risk_chain_summary(), NO_STOP_WARNING, open_positions_note(result)):
            assert caveat in result.warnings
            assert caveat is not None
            assert caveat not in printed

    def test_a_filtered_warning_does_not_print(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The zero-cost line is on the result — and so in `--out` — while the
        preamble above the run is the only place the terminal says it."""
        spec = _runnable(cost_model="zero")
        result = run_spec(spec, {"SPY": _wave()}, limits=get_settings().risk)
        cli._print_report(
            "s", ["SPY"], result, result.metrics, Decimal("0"), cli.stated_separately(spec, result)
        )

        assert ZERO_COST_WARNING not in capsys.readouterr().out
        assert ZERO_COST_WARNING in result.warnings


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
