"""Detecting a corporate action from two fetches of the same history.

The cases that matter are not "does a 4:1 split come out as 4". They are the
ones where something moved and it was *not* a corporate action — a half-restated
series, one bad bar, a symbol the nightly sweep has not adjusted yet — because
naming a factor there would have an operator adopting a share count from a
number nothing supports.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.data.corporate_actions import (
    MIN_BARS_COMPARED,
    Adjustment,
    detect_adjustment,
)
from atp_core.domain import Bar, Timeframe

if TYPE_CHECKING:
    from collections.abc import Sequence

SYMBOL = "SPY"
T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def bar(index: int, *, close: str, adj: str | None) -> Bar:
    """One daily bar. High and low bracket the closes so `Bar` accepts it."""
    value = Decimal(close)
    return Bar(
        symbol=SYMBOL,
        ts=T0 + timedelta(days=index),
        timeframe=Timeframe.D1,
        open=value,
        high=value * Decimal("1.01"),
        low=value * Decimal("0.99"),
        close=value,
        volume=Decimal(1_000),
        adj_close=Decimal(adj) if adj is not None else None,
    )


def series(adjusted: Sequence[str | None], *, close: str = "100") -> list[Bar]:
    return [bar(i, close=close, adj=a) for i, a in enumerate(adjusted)]


class TestNothingHappened:
    def test_an_unchanged_series_is_none(self) -> None:
        """The answer on almost every symbol on almost every day, and the one
        this is mostly written to produce cheaply."""
        same = ["100", "101", "102", "103"]

        assert detect_adjustment(SYMBOL, series(same), series(same)) is None

    def test_cent_rounding_is_not_a_corporate_action(self) -> None:
        """Vendors round adjusted closes, so two fetches of an untouched series
        differ in the last place. A tolerance narrower than that would report a
        split every morning."""
        stored = series(["100.00", "101.00", "102.00", "103.00"])
        fresh = series(["100.00", "101.00", "102.01", "103.00"])

        assert detect_adjustment(SYMBOL, stored, fresh) is None

    def test_bars_with_no_stored_adjusted_close_are_skipped(self) -> None:
        """A raw-only fetch stores no `adj_close`. Treating that as a change
        would report a corporate action every time the nightly sweep had not yet
        run over a symbol."""
        stored = series([None, None, None, None])
        fresh = series(["25", "25.25", "25.5", "25.75"])

        assert detect_adjustment(SYMBOL, stored, fresh) is None

    def test_bars_that_do_not_pair_cost_only_themselves(self) -> None:
        """A window that grew between the two fetches is ordinary."""
        stored = series(["100", "101", "102", "103"])
        fresh = series(["100", "101", "102", "103", "104"])

        assert detect_adjustment(SYMBOL, stored, fresh) is None


class TestASplit:
    def test_a_four_for_one_reads_as_four(self) -> None:
        """`factor` is stored ÷ fresh, so it reads the way the action does: the
        stored series is four times the fresh one, and a held 100 becomes 400."""
        stored = series(["400", "404", "408", "412"])
        fresh = series(["100", "101", "102", "103"])

        found = detect_adjustment(SYMBOL, stored, fresh)

        assert found is not None
        assert found.factor == Decimal(4)
        assert found.is_consistent
        assert found.is_split_like
        assert found.implied_position_factor == Decimal(4)

    def test_it_names_how_far_back_the_change_reaches(self) -> None:
        stored = series(["400", "404", "408", "412"])
        fresh = series(["100", "101", "102", "103"])

        found = detect_adjustment(SYMBOL, stored, fresh)

        assert found is not None and found.earliest_moved_at == T0

    def test_a_reverse_split_reads_below_one(self) -> None:
        """A 1:10 reverse split multiplies historical adjusted prices by ten, so
        the stored series is a tenth of the fresh one and a held 1000 becomes
        100. Getting this direction backwards is the expensive mistake."""
        stored = series(["10", "10.1", "10.2", "10.3"])
        fresh = series(["100", "101", "102", "103"])

        found = detect_adjustment(SYMBOL, stored, fresh)

        assert found is not None
        assert found.factor == Decimal("0.1")
        assert found.is_split_like


class TestADividend:
    def test_it_is_detected_but_not_split_like(self) -> None:
        """Real, worth storing, not worth an alert at 08:30. The line is about
        who needs telling — the refreshed prices are written back either way."""
        stored = series(["100", "101", "102", "103"])
        fresh = series(["99.5", "100.495", "101.49", "102.485"])

        found = detect_adjustment(SYMBOL, stored, fresh)

        assert found is not None
        assert found.is_consistent
        assert not found.is_split_like


class TestSomethingElseHappened:
    """The cases where naming a factor would be inventing a story."""

    def test_half_a_series_moving_is_not_one_corporate_action(self) -> None:
        """A split restates the *whole* history. A series where half the bars
        moved by four and half did not move at all is a data incident, and the
        unmoved bars correctly count as disagreeing with the factor."""
        stored = series(["400", "404", "102", "103"])
        fresh = series(["100", "101", "102", "103"])

        found = detect_adjustment(SYMBOL, stored, fresh)

        assert found is not None
        assert not found.is_consistent
        assert found.bars_agreeing < found.bars_compared

    def test_one_restated_bar_does_not_drag_the_factor(self) -> None:
        """The median rather than the mean. One bar restated on its own would
        pull an average away from the number every other bar agrees on."""
        stored = series(["400", "404", "408", "9999"])
        fresh = series(["100", "101", "102", "103"])

        found = detect_adjustment(SYMBOL, stored, fresh)

        assert found is not None
        assert found.factor == Decimal(4), "the outlier must not move the factor"
        assert not found.is_consistent, "and it must still be reported as disagreement"

    def test_too_few_bars_is_never_consistent(self) -> None:
        """Two bars agreeing with each other is not evidence, and a two-bar
        window would call a single mis-stored close a corporate action."""
        stored = series(["400", "404"])
        fresh = series(["100", "101"])

        found = detect_adjustment(SYMBOL, stored, fresh)

        assert found is not None
        assert found.bars_compared < MIN_BARS_COMPARED
        assert not found.is_consistent

    def test_a_zero_adjusted_close_is_skipped_not_divided_by(self) -> None:
        """It should not occur. A `DivisionByZero` inside a pre-open job nothing
        is watching is a worse way to find out than a bar count that does not
        add up."""
        stored = series(["400", "404", "408", "412"])
        fresh = [
            bar(0, close="100", adj="0"),
            bar(1, close="101", adj="101"),
            bar(2, close="102", adj="102"),
            bar(3, close="103", adj="103"),
        ]

        found = detect_adjustment(SYMBOL, stored, fresh)

        assert found is not None
        assert found.bars_compared == 3, "the zero bar was skipped, not counted"


class TestTheReport:
    def test_split_like_is_about_the_size_of_the_move(self) -> None:
        big = Adjustment(SYMBOL, Decimal(4), 10, 10, T0)
        small = Adjustment(SYMBOL, Decimal("1.005"), 10, 10, T0)

        assert big.is_split_like
        assert not small.is_split_like
