"""`BacktestRunSpec` through the `config` column and back.

This is the seam a queued backtest actually crosses. The API builds a spec and
writes it; the worker is handed a `run_id`, reads the row, and rebuilds the spec
from this column **and nothing else** — so whatever does not survive the trip
did not happen, whatever the request said.

For a while six of the fifteen fields did not survive. `sizing_method`,
`sizing_value` and the four `stop_*` fields were neither written nor read, so a
run queued as `risk_pct` with an ATR stop executed as `fixed_qty` with no stop.
Nothing failed: the API validated the stop config and discarded it, and the
result looked exactly like a correct one.

Two things hid it, and both are worth knowing when reading these tests:

- `FakeBacktestRunRepository` stores the `StoredBacktestRun` object in memory,
  so every unit test of the queue bypassed the serialiser entirely. The fake
  round-tripped the spec perfectly; the real adapter did not.
- The integration test that *does* use the real repository asserted on five
  fields, all of which happened to be among the nine that survived.

So these tests go at the private functions directly. That is the seam, and a
test that reached it through the repository would need a database to say
anything about pure serialisation.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest

from atp_core.backtest.ports import BacktestRunSpec, spec_to_json
from atp_core.persistence.backtests import _spec_from_json

START = datetime(2024, 1, 2, tzinfo=UTC)
END = datetime(2024, 12, 31, tzinfo=UTC)

#: Every optional field set to something that is **not** its default, so that a
#: field the serialiser drops cannot pass by coincidence.
#: `test_the_sample_differs_from_every_default` is what keeps that true.
FULLY_SPECIFIED = BacktestRunSpec(
    strategy_id="sma_crossover",
    symbols=("SPY", "QQQ"),
    start=START,
    end=END,
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

#: A row written before the sizing and stop fields were serialised. Not a
#: hypothetical: every run queued while `spec_to_json` dropped them looks
#: exactly like this.
LEGACY_CONFIG: dict[str, Any] = {
    "strategy_id": "sma_crossover",
    "symbols": ["SPY"],
    "start": START.isoformat(),
    "end": END.isoformat(),
    "timeframe": "1d",
    "starting_cash": "100000",
    "cost_model": "alpaca_equities",
    "params": {},
    "qty": "100",
}


def optional_fields() -> list[dataclasses.Field[Any]]:
    """The spec's fields that carry a default — the ones a row can omit."""
    return [f for f in dataclasses.fields(BacktestRunSpec) if f.default is not dataclasses.MISSING]


class TestTheRoundTrip:
    def test_the_sample_differs_from_every_default(self) -> None:
        """Guards the guard.

        Every case below rests on `FULLY_SPECIFIED` having no field left at its
        default. A field that matched its default would survive a serialiser
        that dropped it — passing while proving nothing, which is the exact
        failure mode that let the original bug through.
        """
        for field in optional_fields():
            value = getattr(FULLY_SPECIFIED, field.name)
            assert value != field.default, (
                f"{field.name} is at its default, so no test here can tell "
                f"whether it survives serialisation"
            )

    def test_every_field_survives(self) -> None:
        restored = _spec_from_json(FULLY_SPECIFIED.strategy_id, spec_to_json(FULLY_SPECIFIED))
        assert restored == FULLY_SPECIFIED

    @pytest.mark.parametrize("name", [f.name for f in dataclasses.fields(BacktestRunSpec)])
    def test_the_writer_records_every_field_the_spec_has(self, name: str) -> None:
        """Driven off `dataclasses.fields` rather than a list written by hand.

        This is the test that makes the next field impossible to forget rather
        than merely unlikely to be: adding one to `BacktestRunSpec` without
        teaching `spec_to_json` about it fails here, by name, before it can
        reach a run that quietly ignores it.
        """
        assert name in spec_to_json(FULLY_SPECIFIED)

    @pytest.mark.parametrize("field", optional_fields(), ids=lambda f: f.name)
    def test_each_field_round_trips_individually(self, field: dataclasses.Field[Any]) -> None:
        """Named per field, so a failure says which one was lost rather than
        that two specs differed."""
        restored = _spec_from_json(FULLY_SPECIFIED.strategy_id, spec_to_json(FULLY_SPECIFIED))
        assert getattr(restored, field.name) == getattr(FULLY_SPECIFIED, field.name)

    def test_the_window_keeps_its_timezone(self) -> None:
        """A naive datetime is rejected at the domain boundary (rule §1.2), so a
        window that came back naive would fail far from here."""
        restored = _spec_from_json(FULLY_SPECIFIED.strategy_id, spec_to_json(FULLY_SPECIFIED))
        assert restored.start.tzinfo is not None
        assert restored.start == START
        assert restored.end == END


class TestAnOlderRow:
    """Rows written before these fields were serialised still have to read."""

    def test_it_still_deserialises(self) -> None:
        restored = _spec_from_json("sma_crossover", LEGACY_CONFIG)
        assert restored.strategy_id == "sma_crossover"
        assert restored.symbols == ("SPY",)

    def test_it_reads_as_what_it_actually_ran(self) -> None:
        """`fixed_qty` with no stop — and that is the honest reading rather than
        a lossy one.

        A run queued while the writer dropped these fields *executed* as
        `fixed_qty` with no stop, because the worker rebuilt its spec from the
        same incomplete column. Reading it any other way would claim it ran as
        something it did not.
        """
        restored = _spec_from_json("sma_crossover", LEGACY_CONFIG)
        assert restored.sizing_method == "fixed_qty"
        assert restored.sizing_value == ""
        assert restored.stop_type == ""
        assert restored.stop_value == ""
        assert restored.stop_period == 14
        assert restored.stop_bars == 0

    def test_those_defaults_are_the_specs_own(self) -> None:
        """Stated in two places — here and on the dataclass — so pin them
        together. A reader default that drifted from the spec's would make a run
        mean one thing in memory and another after a round trip."""
        restored = _spec_from_json("sma_crossover", LEGACY_CONFIG)
        for field in optional_fields():
            if field.name in {"qty", "params"}:
                continue  # present in the legacy row, so not a default here
            assert getattr(restored, field.name) == field.default

    def test_a_row_with_no_window_is_refused(self) -> None:
        """Unchanged, and deliberately not tolerant: a run whose dates cannot be
        read is a row this platform cannot describe, and inventing a window
        would produce a screen full of confident wrong dates."""
        without_dates = {k: v for k, v in LEGACY_CONFIG.items() if k not in {"start", "end"}}
        with pytest.raises(KeyError):
            _spec_from_json("sma_crossover", without_dates)


class TestTheStrategyId:
    def test_it_is_taken_from_the_column_not_the_json(self) -> None:
        """Both hold it, and the column is the one with the foreign key on it.
        Reading the JSON copy would trust the half nothing constrains."""
        disagreeing = {**LEGACY_CONFIG, "strategy_id": "something_else"}
        assert _spec_from_json("sma_crossover", disagreeing).strategy_id == "sma_crossover"
