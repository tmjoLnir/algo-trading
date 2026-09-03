"""`RiskLimits` — the eight account-wide ceilings, and every refusal they carry.

These were `RISK_*` environment variables until they became a row the dashboard
writes, and the move is what makes this module necessary. Pydantic used to
check the *types*; nothing checked the *values*, because a value that only
reached the platform through a file somebody edited over SSH was as validated as
the person editing it. Now a browser posts them, so the rules have to be written
down — and written down **once**, in the value object, for the reason
`test_worker_config.py` states: the API refuses a bad edit and the worker refuses
to boot on a bad row, and a ceiling the first accepts and the second rejects
saves cleanly and then kills the process at its next start.

Two properties are load-bearing here and neither is obvious:

- **Zero is refused rather than read as "off".** Every instinct about a numeric
  setting says zero disables it. For every one of these it does the opposite: a
  zero position limit refuses every order, a zero daily-loss limit halts on the
  first cent. An operator who wants trading stopped has a kill switch that says
  so on the screen.
- **The defaults are exactly what `.env.example` shipped.** That is what makes
  the migration a no-op for an existing deployment, and what makes "nothing has
  been saved" a state rather than a fault.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from atp_core.errors import ConfigError
from atp_core.risk.limits import (
    DEFAULT_RISK_LIMITS,
    MAX_GROSS_CEILING,
    MAX_STOP_LOSS,
    RISK_LIMIT_FIELDS,
    RiskLimits,
    parse_limit_decimal,
)

#: The value each field carried in `.env.example` before the move. Restated
#: here rather than read off `DEFAULT_RISK_LIMITS`, which is the thing under
#: test — a test that asked the defaults what the defaults are would pass for
#: any of them.
SHIPPED = {
    "max_position_pct": Decimal("0.10"),
    "max_gross_exposure_pct": Decimal("1.00"),
    "max_daily_loss_pct": Decimal("0.03"),
    "max_orders_per_minute": 30,
    "max_open_positions": 20,
    "max_quote_age_seconds": 30,
    "default_stop_loss_pct": Decimal("0.02"),
    "default_take_profit_pct": Decimal("0.06"),
}

FRACTIONS = (
    "max_position_pct",
    "max_gross_exposure_pct",
    "max_daily_loss_pct",
    "default_stop_loss_pct",
    "default_take_profit_pct",
)
COUNTS = ("max_orders_per_minute", "max_open_positions", "max_quote_age_seconds")


def limits_with(field: str, value: object) -> RiskLimits:
    """A `RiskLimits` with one field overridden, named at run time.

    `**{field: value}` is exactly what a parametrised case needs and exactly
    what a static checker cannot narrow: the key is a variable, so mypy sees
    every keyword as possibly any of the eight and objects that a `Decimal` was
    passed where an `int` belongs. The `Any` is confined to this one function
    rather than repeated as an ignore at each call site.
    """
    return RiskLimits(**{field: value})  # type: ignore[arg-type]


class TestTheDefaultsAreWhatTheFileShipped:
    """An upgrade that saves nothing must trade exactly as it did.

    The migration backfills these into the existing row, and `.env.example` no
    longer carries them — so if these drifted, an operator who upgraded would
    silently get ceilings they never chose, and the file that used to say what
    they were is gone.
    """

    @pytest.mark.parametrize(("field", "expected"), SHIPPED.items())
    def test_each_default_is_the_value_env_carried(self, field: str, expected: object) -> None:
        assert getattr(DEFAULT_RISK_LIMITS, field) == expected

    def test_the_defaults_are_loadable(self) -> None:
        """`DEFAULT_RISK_LIMITS` passes its own validation.

        Not circular: the defaults are class attributes, and `__post_init__`
        could perfectly well refuse one — a tightened bound that nobody checked
        against the default it was tightening past would make every unsaved
        deployment fail to start.
        """
        assert RiskLimits() == DEFAULT_RISK_LIMITS


class TestZeroIsNotOff:
    """The refusal an operator is most likely to be surprised by, so it says why."""

    @pytest.mark.parametrize("field", FRACTIONS)
    def test_a_zero_fraction_is_refused(self, field: str) -> None:
        with pytest.raises(ConfigError, match="not 'no limit'"):
            limits_with(field, Decimal(0))

    @pytest.mark.parametrize("field", COUNTS)
    def test_a_zero_count_is_refused(self, field: str) -> None:
        with pytest.raises(ConfigError, match="at least 1"):
            limits_with(field, 0)

    @pytest.mark.parametrize("field", FRACTIONS)
    def test_a_negative_fraction_is_refused(self, field: str) -> None:
        with pytest.raises(ConfigError, match="greater than zero"):
            limits_with(field, Decimal("-0.1"))

    @pytest.mark.parametrize("field", FRACTIONS)
    def test_the_refusal_names_the_field(self, field: str) -> None:
        """Both callers put this sentence in front of a person."""
        with pytest.raises(ConfigError, match=field):
            limits_with(field, Decimal(0))


class TestTheUpperBounds:
    """Typo guards, and one of them is not.

    Four of these refuse a misplaced decimal point. `default_stop_loss_pct` is
    the exception: 1 is not an implausible number there, it is an *impossible
    stop* — a whole entry price below entry is the level zero, which price
    cannot reach.
    """

    def test_a_position_may_be_the_whole_account(self) -> None:
        """100% in one symbol is aggressive, not incoherent. Only past it is."""
        assert RiskLimits(max_position_pct=Decimal(1)).max_position_pct == Decimal(1)

    def test_a_position_larger_than_the_account_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="150%"):
            RiskLimits(max_position_pct=Decimal("1.5"), max_gross_exposure_pct=Decimal(4))

    def test_reg_t_leverage_is_allowed(self) -> None:
        assert RiskLimits(max_gross_exposure_pct=MAX_GROSS_CEILING).max_gross_exposure_pct == (
            MAX_GROSS_CEILING
        )

    def test_more_than_reg_t_leverage_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="max_gross_exposure_pct"):
            RiskLimits(max_gross_exposure_pct=Decimal(5))

    def test_a_stop_at_the_whole_entry_price_is_refused(self) -> None:
        """The exclusive bound. A stop 100% below entry sits at zero."""
        with pytest.raises(ConfigError, match="at or above"):
            RiskLimits(default_stop_loss_pct=MAX_STOP_LOSS)

    def test_a_stop_just_under_it_is_allowed(self) -> None:
        assert RiskLimits(default_stop_loss_pct=Decimal("0.99")) is not None

    def test_a_take_profit_may_exceed_the_entry_price(self) -> None:
        """Unlike a stop. A target of 200% above entry is a long hold, not a typo."""
        assert RiskLimits(default_take_profit_pct=Decimal(2)) is not None

    def test_a_nan_is_refused_before_it_is_compared(self) -> None:
        """`Decimal('NaN') > x` is False for every x, so an unguarded bound check
        lets it through — and every later comparison against it is False too,
        which is a ceiling that approves everything."""
        with pytest.raises(ConfigError, match="finite"):
            RiskLimits(max_position_pct=Decimal("NaN"))

    def test_an_infinite_ceiling_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="finite"):
            RiskLimits(max_position_pct=Decimal("Infinity"))


class TestTheTwoCeilingsMustAgree:
    """One symbol may not be allowed to exceed the whole book.

    Not a safety hole — the tighter rule wins, so the gross limit would refuse
    what the position limit let through — but the operator who typed it believes
    they have a position limit they do not have.
    """

    def test_a_position_limit_above_the_gross_limit_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="not the one in force"):
            RiskLimits(max_position_pct=Decimal("0.5"), max_gross_exposure_pct=Decimal("0.3"))

    def test_equal_is_allowed(self) -> None:
        """A single-position book. Coherent, and someone will want it."""
        assert (
            RiskLimits(max_position_pct=Decimal("0.3"), max_gross_exposure_pct=Decimal("0.3"))
            is not None
        )


class TestTheFormCatalogue:
    """`RISK_LIMIT_FIELDS` is what the dashboard renders, so it must be complete.

    A ceiling missing from it is a ceiling with no box on the screen — and,
    because the API's audit diff is derived from this same tuple, one whose
    change would go unrecorded.
    """

    def test_every_field_has_an_entry(self) -> None:
        assert {f.name for f in RISK_LIMIT_FIELDS} == set(SHIPPED)

    def test_no_entry_names_a_field_that_does_not_exist(self) -> None:
        for field in RISK_LIMIT_FIELDS:
            assert hasattr(DEFAULT_RISK_LIMITS, field.name)

    def test_every_entry_carries_its_own_prose(self) -> None:
        """The argument for a number belongs beside the box it is typed into."""
        for field in RISK_LIMIT_FIELDS:
            assert field.label and field.unit
            assert len(field.help) > 40, f"{field.name} has no real help text"

    def test_a_fraction_declares_its_ceiling_and_a_count_does_not(self) -> None:
        """The browser's `max` comes from here. A count is bounded below only,
        and inventing an upper bound would refuse what the server accepts."""
        for field in RISK_LIMIT_FIELDS:
            if field.unit == "fraction":
                assert field.maximum is not None, field.name
            else:
                assert field.maximum is None, field.name

    def test_the_declared_ceiling_is_the_one_enforced(self) -> None:
        """A `maximum` looser than `__post_init__`'s would let the browser
        accept a value the server then refuses, which is the round trip this
        field exists to save."""
        for field in RISK_LIMIT_FIELDS:
            if field.maximum is None:
                continue
            with pytest.raises(ConfigError):
                limits_with(field.name, field.maximum + Decimal("0.01"))


class TestParsingWhatTheWireCarried:
    def test_a_string_becomes_a_decimal(self) -> None:
        assert parse_limit_decimal("0.10", field_name="x") == Decimal("0.10")

    def test_a_float_goes_via_str(self) -> None:
        """`Decimal(0.1)` is not `Decimal('0.1')` (rule §1.1), and this is
        multiplied by equity to produce a ceiling."""
        assert parse_limit_decimal(0.1, field_name="x") == Decimal("0.1")

    def test_junk_is_refused_by_name(self) -> None:
        with pytest.raises(ConfigError, match="max_position_pct is not a number"):
            parse_limit_decimal("ten percent", field_name="max_position_pct")


class TestItIsFrozen:
    def test_a_ceiling_cannot_change_under_a_running_evaluation(self) -> None:
        limits = RiskLimits()
        with pytest.raises(FrozenInstanceError, match="cannot assign"):
            limits.max_position_pct = Decimal("0.5")  # type: ignore[misc]
