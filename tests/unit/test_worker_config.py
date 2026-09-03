"""`WorkerConfig` — what a worker trades, and every refusal it carries.

The value object is the *only* place these rules live, and that is the property
worth testing hardest. The dashboard refuses a bad edit and the worker refuses
to boot on a bad row; if those two disagreed, the failure mode is specific and
nasty — a configuration that saves cleanly and then kills the process at its
next restart, discovered by an operator who has just been told the save worked.
So each rule is asserted here once, and both ends get it by construction.

The second theme is that **a refusal must say which field and why**. These are
sentences the API puts in a 400 body and the worker puts in the reason it will
not start, so "invalid configuration" would be a failure of this module rather
than a terse message.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from atp_core.errors import ConfigError
from atp_core.risk.limits import DEFAULT_RISK_LIMITS, RiskLimits
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_core.strategy import registry
from atp_core.worker import DEFAULT_WORKER_CONFIG, SIZING_METHODS, STOP_TYPES, WorkerConfig
from atp_core.worker.config import (
    _SIZING_VALUES,
    _STOP_VALUES,
    MAX_SYMBOLS,
    normalise_symbols,
    parse_strategy_params,
    parse_symbol_list,
    strategy_options,
)


class TestTheVocabularyMatchesThePlatform:
    """These lists are restated rather than imported, so something must check.

    `PositionSizeSpec` and `StopType` are the things that actually consume these
    strings. A method added to one and not the other would be a dropdown option
    the router cannot honour, or a stored value the form cannot show.
    """

    def test_every_sizing_method_is_one_position_size_spec_accepts(self) -> None:
        from atp_core.strategy.rules import PositionSizeSpec

        accepted = set(PositionSizeSpec.model_fields["type"].annotation.__args__)  # type: ignore[union-attr]
        assert accepted == _SIZING_VALUES

    def test_every_stop_type_is_a_member_of_the_enum(self) -> None:
        from atp_core.domain import StopType

        assert {member.value for member in StopType} == _STOP_VALUES

    def test_every_accepted_value_has_a_dropdown_option(self) -> None:
        """An option missing from the catalogue is a value the form cannot set
        and the screen renders as a blank select."""
        assert {o.value for o in SIZING_METHODS} == _SIZING_VALUES
        assert {o.value for o in STOP_TYPES} == _STOP_VALUES

    def test_every_option_explains_itself(self) -> None:
        """A `<select>` of six stop types tells a reader nothing about which to
        pick, which is the whole reason each option carries prose."""
        for option in (*SIZING_METHODS, *STOP_TYPES):
            assert option.help.strip(), option.value


class TestTheDefaultsAreInert:
    def test_nothing_is_traded_and_nothing_is_ingested(self) -> None:
        """A worker that starts trading because it was deployed, rather than
        because somebody chose to, is the accident these two prevent."""
        assert DEFAULT_WORKER_CONFIG.strategy == ""
        assert DEFAULT_WORKER_CONFIG.symbols == ()
        assert DEFAULT_WORKER_CONFIG.trades is False

    def test_the_live_lock_is_closed(self) -> None:
        assert DEFAULT_WORKER_CONFIG.allow_live_orders is False

    def test_the_rest_are_the_documented_defaults(self) -> None:
        """The same values `.env.example` shipped, so an install that upgrades
        and saves nothing behaves exactly as it did."""
        assert DEFAULT_WORKER_CONFIG.sizing_method == "risk_pct"
        assert DEFAULT_WORKER_CONFIG.sizing_value == Decimal("0.01")
        assert DEFAULT_WORKER_CONFIG.stop_type == "atr"
        assert DEFAULT_WORKER_CONFIG.stop_multiplier == Decimal("2")
        assert DEFAULT_WORKER_CONFIG.stop_period == 14
        assert DEFAULT_WORKER_CONFIG.max_silence_seconds == 60


class TestSymbols:
    def test_a_lowercase_ticker_is_refused_by_name(self) -> None:
        """`Instrument` refuses one a layer down, so accepting it here would
        only defer the failure to the first subscription."""
        with pytest.raises(ConfigError, match="spy"):
            WorkerConfig(symbols=("spy",))

    def test_a_duplicate_is_refused(self) -> None:
        """Subscribed twice, counted twice against the vendor's symbol limit."""
        with pytest.raises(ConfigError, match="twice"):
            WorkerConfig(symbols=("SPY", "SPY"))

    def test_a_paste_sized_watchlist_is_refused(self) -> None:
        with pytest.raises(ConfigError, match=str(MAX_SYMBOLS)):
            WorkerConfig(symbols=tuple(f"S{n}" for n in range(MAX_SYMBOLS + 1)))

    def test_normalising_keeps_the_order_that_was_typed(self) -> None:
        """A watchlist that reshuffles itself on save reads as the platform
        having changed something it did not."""
        assert normalise_symbols([" qqq ", "SPY", "qqq", ""]) == ("QQQ", "SPY")

    def test_parsing_a_text_box_is_the_same_normalisation(self) -> None:
        assert parse_symbol_list(" spy, QQQ ,, iwm ") == ("SPY", "QQQ", "IWM")

    def test_with_symbols_produces_a_normalised_copy(self) -> None:
        assert DEFAULT_WORKER_CONFIG.with_symbols(["spy"]).symbols == ("SPY",)


class TestSizing:
    def test_a_fraction_above_the_backstop_is_refused(self) -> None:
        """docs/RISK.md gives 0.5–2% risk per trade. An order of magnitude past
        that is a misplaced decimal point rather than a choice, and catching it
        at the moment of typing is the point of having the rule here."""
        with pytest.raises(ConfigError, match="10%"):
            WorkerConfig(sizing_method="risk_pct", sizing_value=Decimal("0.5"))

    def test_a_share_count_is_not_bounded_by_the_fraction_rule(self) -> None:
        """500 shares is ordinary; 500 as a `risk_pct` would size the account
        into one trade fifty times over. The bound belongs to the method."""
        assert WorkerConfig(
            sizing_method="fixed_qty", sizing_value=Decimal("500")
        ).sizing_value == Decimal("500")

    def test_zero_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="positive"):
            WorkerConfig(sizing_value=Decimal("0"))

    def test_an_unknown_method_names_the_ones_that_exist(self) -> None:
        with pytest.raises(ConfigError, match="risk_pct"):
            WorkerConfig(sizing_method="martingale")  # type: ignore[arg-type]


class TestStops:
    def test_a_fractional_stop_of_two_is_refused_as_two_hundred_percent(self) -> None:
        """`fixed_pct` reads the field as a fraction, so 2 is a stop 200% below
        entry — a level price cannot reach, which is a stop that does not
        exist. This is the exact mistake one shared field makes possible."""
        with pytest.raises(ConfigError, match="200"):
            WorkerConfig(stop_type="fixed_pct", stop_multiplier=Decimal("2"))

    def test_the_same_number_is_ordinary_for_an_atr_stop(self) -> None:
        """2× ATR is docs/RISK.md's recommendation. A rule that refused it would
        be the bound applied to the wrong family."""
        assert WorkerConfig(stop_type="atr", stop_multiplier=Decimal("2")).stop_multiplier == 2

    def test_a_price_distance_is_not_bounded_below_one(self) -> None:
        assert WorkerConfig(stop_type="fixed_amount", stop_multiplier=Decimal("5")).stop_type == (
            "fixed_amount"
        )

    def test_a_non_positive_multiplier_is_refused(self) -> None:
        """A stop at or through the entry price is hit immediately."""
        with pytest.raises(ConfigError, match="positive"):
            WorkerConfig(stop_multiplier=Decimal("0"))

    def test_a_zero_period_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="at least 1 bar"):
            WorkerConfig(stop_period=0)


class TestTheWatchdog:
    def test_zero_silence_is_refused(self) -> None:
        """It would halt trading on the first quiet moment, which on a thin
        name is every session."""
        with pytest.raises(ConfigError, match="at least 1 second"):
            WorkerConfig(max_silence_seconds=0)


class TestStrategyParams:
    def test_empty_is_the_strategys_own_defaults(self) -> None:
        assert parse_strategy_params("  ") == {}

    def test_malformed_json_is_refused_rather_than_ignored(self) -> None:
        """Falling back to defaults would run a strategy on parameters the
        operator does not think it has."""
        with pytest.raises(ConfigError, match="not valid JSON"):
            parse_strategy_params("{fast: 20}")

    def test_a_list_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must be a JSON object"):
            parse_strategy_params("[1, 2]")

    def test_params_that_cannot_be_stored_are_refused_at_construction(self) -> None:
        """The column is JSON. A value that will not serialise fails on the way
        into the database otherwise — an IntegrityError-shaped 500 rather than a
        sentence about a field."""
        with pytest.raises(ConfigError, match="not storable as JSON"):
            WorkerConfig(strategy_params={"when": object()})

    def test_a_name_longer_than_the_column_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="36"):
            WorkerConfig(strategy="s" * 37)


class TestStrategyOptions:
    def test_every_registered_class_is_offered(self) -> None:
        options = strategy_options(registry.all_strategies())
        assert {o["value"] for o in options} == set(registry.all_strategies())

    def test_each_carries_the_defaults_it_would_actually_run_on(self) -> None:
        """`Strategy.__init__` stores `params or {}` and every accessor reads a
        default out of `params_schema`, so an empty box does not mean empty
        parameters — and the form has to be able to say what it does mean."""
        sma = next(
            o for o in strategy_options(registry.all_strategies()) if o["value"] == "sma_crossover"
        )
        assert sma["default_params"]
        assert sma["params_schema"]

    def test_no_empty_option_is_invented(self) -> None:
        """ "No strategy" is not a strategy. The screen renders it as its own
        choice with its own sentence."""
        assert all(o["value"] for o in strategy_options(registry.all_strategies()))


class TestTheRiskCeilingsTravelWithIt:
    """The eight ceilings are nested here, saved here, and published from here.

    They are a different *kind* of thing from the fields above — a limit the
    platform refuses to cross, rather than something it tries to do — and
    `atp_core.risk.limits` owns their rules. What this class holds is the
    consequence of putting them in the same object: one save, one revision, one
    restart comparison, and a worker that enforces the ceilings it booted with
    rather than whatever has been saved since.
    """

    def test_the_default_config_carries_the_default_ceilings(self) -> None:
        assert DEFAULT_WORKER_CONFIG.risk == DEFAULT_RISK_LIMITS

    def test_a_ceiling_can_be_set_without_touching_anything_else(self) -> None:
        config = WorkerConfig(risk=RiskLimits(max_position_pct=Decimal("0.05")))
        assert config.risk.max_position_pct == Decimal("0.05")
        assert config.sizing_value == DEFAULT_WORKER_CONFIG.sizing_value

    def test_a_bad_ceiling_is_refused_by_the_object_that_owns_it(self) -> None:
        """`RiskLimits` refuses at its own construction, so this never reaches
        `WorkerConfig` — which is the point: one set of rules, not two."""
        with pytest.raises(ConfigError, match="max_open_positions"):
            WorkerConfig(risk=RiskLimits(max_open_positions=0))

    def test_a_mapping_is_not_a_ceiling(self) -> None:
        """A `dict` decoded from a row or a JSON body would otherwise surface as
        an attribute error nine layers down, inside a rule, on the first order
        of the day."""
        with pytest.raises(ConfigError, match="must be a RiskLimits"):
            WorkerConfig(risk={"max_position_pct": "0.1"})  # type: ignore[arg-type]

    def test_two_configs_differing_only_in_a_ceiling_are_not_equal(self) -> None:
        """What the audit diff and the restart notice both rest on."""
        assert WorkerConfig() != WorkerConfig(risk=RiskLimits(max_open_positions=19))
