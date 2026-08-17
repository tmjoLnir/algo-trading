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
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

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
    return asyncio.run(cli.main(argv))


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

    def test_unknown_strategy_names_the_registered_ones(self) -> None:
        """The registry is only populated by importing the examples package,
        which the CLI does. If that import is ever dropped, this fails with an
        empty list rather than the run silently finding no strategies."""
        with pytest.raises(SystemExit, match=r"unknown strategy 'nope'.*sma_crossover"):
            run(["--strategy", "nope", *BASE[2:]])


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
    def test_infinity_becomes_null(self) -> None:
        """Infinity is a real metric value and not legal JSON. `json.dumps`
        would emit a bare `Infinity`, which most parsers reject — so the file
        would fail to load in exactly the tools meant to read it."""
        cleaned = cli._jsonable({"profit_factor": float("inf"), "sharpe": 0.5})
        assert cleaned == {"profit_factor": None, "sharpe": 0.5}
        assert json.dumps(cleaned, allow_nan=False)

    def test_nan_becomes_null(self) -> None:
        assert cli._jsonable({"x": math.nan}) == {"x": None}

    def test_nested_structures_are_cleaned(self) -> None:
        cleaned = cli._jsonable({"a": [{"b": float("-inf")}], "c": "ok"})
        assert cleaned == {"a": [{"b": None}], "c": "ok"}

    def test_ordinary_values_pass_through_untouched(self) -> None:
        payload = {"s": "SPY", "n": 3, "f": 1.5, "l": [1, 2]}
        assert cli._jsonable(payload) == payload


class TestHeadlineSet:
    def test_every_headline_key_exists_on_the_metric_set(self) -> None:
        """The report indexes `metrics[key]` directly, so a renamed metric
        would be a KeyError at the end of a long run rather than at import."""
        from atp_core.backtest.metrics import PerformanceMetrics

        fields = set(PerformanceMetrics.__slots__)
        assert {key for key, _, _ in cli._HEADLINE} <= fields
