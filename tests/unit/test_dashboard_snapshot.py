"""The published snapshot: what it computes, and what it refuses to guess.

The dashboard is the one place a human forms a belief about the account without
running a query, so the failures worth pinning here are not "does it render" —
they are the ones that produce a *plausible* wrong number. Three kinds:

- a figure we cannot know reported as zero, which reads as a real value;
- a ratio computed the wrong way round, which is only visible if you know what
  the right answer was;
- a `Decimal` that becomes a float on the way to the browser.

Everything below is one of those.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from atp_core.dashboard.curve import (
    default_resolution_for,
    downsample,
    last_before_or_at,
    resolve,
)
from atp_core.dashboard.snapshot import (
    LiveSnapshot,
    SignalSummary,
    build_snapshot,
    decode_snapshot,
    encode_snapshot,
)
from atp_core.domain import Order, OrderStatus, OrderType, Portfolio, Position, RunMode, Side
from atp_core.execution.ports import EquityPoint

NOW = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


def portfolio_with(*positions: Position, cash: Decimal = Decimal("10000")) -> Portfolio:
    book = Portfolio(cash=cash, starting_equity=Decimal("10000"))
    for position in positions:
        book.positions[position.symbol] = position
    return book


def long_position(
    symbol: str = "AAPL",
    qty: str = "10",
    entry: str = "100",
    last: str | None = "110",
    stop: str | None = "90",
) -> Position:
    return Position(
        symbol=symbol,
        qty=Decimal(qty),
        avg_entry_price=Decimal(entry),
        last_price=None if last is None else Decimal(last),
        stop_loss_price=None if stop is None else Decimal(stop),
        opened_at=NOW - timedelta(days=1),
    )


def build(book: Portfolio, **kwargs: Any) -> LiveSnapshot:
    return build_snapshot(book, at=NOW, run_mode=RunMode.PAPER, **kwargs)


class TestWhatItRefusesToGuess:
    def test_an_unmarked_position_reports_none_not_zero(self) -> None:
        """A holding with no price is not a holding worth nothing.

        Zero is a value a reader acts on: it sorts to the bottom of the exposure
        column and makes a breached percentage limit look compliant, because
        every limit is denominated in a total this position is silently missing
        from.
        """
        snapshot = build(portfolio_with(long_position(last=None)))

        position = snapshot.positions[0]
        assert position.market_value is None
        assert position.unrealized_pnl is None
        assert position.unrealized_pnl_pct is None
        assert position.last_price is None

    def test_the_account_names_the_positions_it_could_not_value(self) -> None:
        """So the understatement travels with the number it understates."""
        book = portfolio_with(long_position("AAPL", last=None), long_position("MSFT"))

        snapshot = build(book)

        assert snapshot.account.unmarked_symbols == ("AAPL",)

    def test_leverage_against_no_equity_is_none(self) -> None:
        """Not 0.0, which reads as "unlevered" — the opposite of undefined."""
        snapshot = build(portfolio_with(cash=Decimal(0)))

        assert snapshot.account.leverage is None

    def test_a_position_with_no_stop_has_no_distance_to_one(self) -> None:
        snapshot = build(portfolio_with(long_position(stop=None)))

        assert snapshot.positions[0].distance_to_stop_pct is None

    def test_an_entry_equal_to_its_stop_has_no_distance_either(self) -> None:
        """There is no entry-to-stop distance to be a fraction of.

        Reporting 0.0 here would render a position sitting comfortably above its
        stop as one about to be closed.
        """
        snapshot = build(portfolio_with(long_position(entry="90", stop="90")))

        assert snapshot.positions[0].distance_to_stop_pct is None


class TestDistanceToStop:
    """1.0 at the entry, 0.0 at the stop, and signed past it."""

    @pytest.mark.parametrize(
        ("last", "expected"),
        [
            ("100", "1.0000"),  # at the entry: the whole distance is left
            ("90", "0.0000"),  # at the stop
            ("95", "0.5000"),  # halfway
            ("110", "2.0000"),  # in profit, past the entry
            ("85", "-0.5000"),  # THROUGH the stop and still open
        ],
    )
    def test_a_long_runs_from_one_at_entry_to_zero_at_the_stop(
        self, last: str, expected: str
    ) -> None:
        snapshot = build(portfolio_with(long_position(entry="100", stop="90", last=last)))

        assert snapshot.positions[0].distance_to_stop_pct == Decimal(expected)

    def test_a_short_reads_the_same_way_round(self) -> None:
        """The stop is *above* the entry, and the ratio still runs 1.0 → 0.0.

        One expression covers both sides. Written per side it would be two
        chances to get a sign wrong on the number that says how close a position
        is to being closed.
        """
        short = Position(
            symbol="AAPL",
            qty=Decimal("-10"),
            avg_entry_price=Decimal("100"),
            last_price=Decimal("105"),
            stop_loss_price=Decimal("110"),
        )

        snapshot = build(portfolio_with(short))

        assert snapshot.positions[0].distance_to_stop_pct == Decimal("0.5000")

    def test_price_through_a_short_stop_is_negative_too(self) -> None:
        short = Position(
            symbol="AAPL",
            qty=Decimal("-10"),
            avg_entry_price=Decimal("100"),
            last_price=Decimal("115"),
            stop_loss_price=Decimal("110"),
        )

        snapshot = build(portfolio_with(short))

        assert snapshot.positions[0].distance_to_stop_pct == Decimal("-0.5000")


class TestOrdering:
    def test_positions_come_back_largest_exposure_first(self) -> None:
        """Sorted here so the client never has to.

        A client that sorts a column of money is a client parsing money into a
        float to compare it, which is the one thing rule §1.1 exists to stop.
        """
        book = portfolio_with(
            long_position("AAA", qty="1", last="10"),
            long_position("BBB", qty="100", last="10"),
            long_position("CCC", qty="10", last="10"),
        )

        snapshot = build(book)

        assert [p.symbol for p in snapshot.positions] == ["BBB", "CCC", "AAA"]

    def test_an_unmarked_position_sorts_last_not_first(self) -> None:
        book = portfolio_with(long_position("AAA", last=None), long_position("BBB", last="10"))

        snapshot = build(book)

        assert [p.symbol for p in snapshot.positions] == ["BBB", "AAA"]

    def test_a_flat_position_is_not_a_row(self) -> None:
        """`Portfolio` keeps a zeroed `Position` after an exit. It is not a
        holding, and a row saying you own nothing of AAPL is noise on the screen
        a person scans first."""
        book = portfolio_with(long_position("AAA"), Position(symbol="ZZZ"))

        snapshot = build(book)

        assert [p.symbol for p in snapshot.positions] == ["AAA"]


class TestWireFormat:
    def test_every_number_leaves_as_a_string(self) -> None:
        """JSON has one numeric type and it is a binary float (rule §1.1)."""
        book = portfolio_with(long_position())

        payload = encode_snapshot(build(book))

        assert isinstance(payload["account"]["equity"], str)
        position = payload["positions"][0]
        for field in ("qty", "avg_entry_price", "last_price", "market_value", "unrealized_pnl"):
            assert isinstance(position[field], str), f"{field} left as {type(position[field])}"

    def test_a_snapshot_round_trips_exactly(self) -> None:
        """Exactly, not approximately: the assertion is on `Decimal` equality,
        which a float round trip would fail on the third of a dollar below."""
        book = portfolio_with(
            long_position(entry="100.333333333", last="110.1"), cash=Decimal("1234.56789")
        )
        order = Order(
            symbol="AAPL",
            side=Side.BUY,
            qty=Decimal("10"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("99.99"),
            status=OrderStatus.SUBMITTED,
            created_at=NOW,
            strategy_id="sma_crossover",
        )
        signal = SignalSummary(
            id="sig-1",
            ts=NOW,
            strategy_id="sma_crossover",
            symbol="AAPL",
            action="enter_long",
            reason="SMA(20) crossed above SMA(50)",
            indicators={"sma_fast": "100.5"},
            acted_on=False,
            rejection_reason="would exceed max gross exposure",
            rejected_by="max_gross_exposure",
        )

        original = build(book, working_orders=[order], recent_signals=[signal], symbols=["AAPL"])

        assert decode_snapshot(encode_snapshot(original)) == original

    def test_a_naive_timestamp_in_a_stored_snapshot_is_refused(self) -> None:
        """Rather than assumed to be UTC.

        Assuming would put a plausible time on a screen that also says "updated
        12s ago", which is the difference between trusting the data and knowing
        not to.
        """
        payload = encode_snapshot(build(portfolio_with()))
        payload["as_of"] = "2024-06-03T14:30:00"

        with pytest.raises(ValueError, match="naive"):
            decode_snapshot(payload)

    def test_the_builder_refuses_a_naive_instant(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            # Suppressed on this line rather than for the file: a naive
            # datetime is exactly what this test is about, and silencing DTZ
            # across the module would let a real one in somewhere else.
            naive = datetime(2024, 6, 3, 14, 30)  # noqa: DTZ001
            build_snapshot(portfolio_with(), at=naive, run_mode=RunMode.PAPER)


class TestSignalFeed:
    def test_a_refused_signal_is_kept_with_the_rule_that_refused_it(self) -> None:
        """A strategy blocked on every bar looks, from anywhere else in the
        system, exactly like a strategy with no ideas."""
        signal = SignalSummary(
            id="sig-1",
            ts=NOW,
            strategy_id="sma_crossover",
            symbol="AAPL",
            action="enter_long",
            reason="crossover",
            indicators={},
            acted_on=False,
            rejection_reason="trading is halted",
            rejected_by="kill_switch",
        )

        snapshot = build(portfolio_with(), recent_signals=[signal])

        kept = snapshot.recent_signals[0]
        assert kept.acted_on is False
        assert kept.rejected_by == "kill_switch"


class TestEquityCurve:
    """Thinning a level series. The rule is last-in-bucket, never an average."""

    def points(
        self, count: int, *, every: timedelta, start_equity: int = 1000
    ) -> list[EquityPoint]:
        return [
            EquityPoint(
                ts=NOW + every * i,
                equity=Decimal(start_equity + i),
                cash=Decimal(500),
                gross_exposure=Decimal(100),
            )
            for i in range(count)
        ]

    def test_it_keeps_the_last_observation_in_each_bucket(self) -> None:
        """Not the mean. An average of the minute points inside an hour is a
        number the account never held, and a chart of numbers that never
        happened is worse than a coarse chart of numbers that did.

        The series starts at 14:30, so the first bucket is the half-hour
        14:30–15:00 rather than a full one — which is the point of anchoring
        buckets to the clock instead of to the first sample.
        """
        minutes = self.points(120, every=timedelta(minutes=1))

        hourly = downsample(minutes, timedelta(hours=1))

        assert [p.ts.isoformat() for p in hourly] == [
            "2024-06-03T14:59:00+00:00",
            "2024-06-03T15:59:00+00:00",
            "2024-06-03T16:29:00+00:00",
        ]
        assert [p.equity for p in hourly] == [Decimal(1029), Decimal(1089), Decimal(1119)]

    def test_the_newest_point_always_survives(self) -> None:
        """So the right-hand end of the chart is current equity rather than the
        end of the last *complete* bucket."""
        minutes = self.points(90, every=timedelta(minutes=1))

        hourly = downsample(minutes, timedelta(hours=1))

        assert hourly[-1] is minutes[-1]

    def test_buckets_are_anchored_to_the_epoch_not_to_the_data(self) -> None:
        """Two requests a minute apart must return points at the same
        timestamps, or the line appears to shift under the reader."""
        every = timedelta(hours=1)
        series = self.points(120, every=timedelta(minutes=1))

        first = downsample(series, every)
        later = downsample(series[7:], every)

        assert [p.ts for p in first][1:] == [p.ts for p in later][1:]

    def test_output_is_chronological(self) -> None:
        series = self.points(200, every=timedelta(minutes=1))

        thinned = downsample(series, timedelta(minutes=15))

        assert [p.ts for p in thinned] == sorted(p.ts for p in thinned)

    def test_an_empty_series_thins_to_nothing(self) -> None:
        assert downsample([], timedelta(hours=1)) == []

    def test_an_unknown_resolution_names_what_was_allowed(self) -> None:
        with pytest.raises(ValueError, match="1m, 5m, 15m, 1h, 4h, 1d"):
            resolve("30s")

    def test_a_non_positive_bucket_is_refused(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            downsample([], timedelta(0))

    @pytest.mark.parametrize(
        ("days", "expected"), [(1, "5m"), (2, "5m"), (7, "1h"), (30, "4h"), (365, "1d")]
    )
    def test_the_default_resolution_scales_with_the_window(self, days: int, expected: str) -> None:
        assert default_resolution_for(days) == expected

    def test_last_before_or_at_finds_the_anchor(self) -> None:
        series = self.points(5, every=timedelta(minutes=1))

        assert last_before_or_at(series, NOW + timedelta(minutes=2, seconds=30)) is series[2]

    def test_last_before_or_at_is_none_when_the_series_starts_later(self) -> None:
        series = self.points(5, every=timedelta(minutes=1))

        assert last_before_or_at(series, NOW - timedelta(minutes=1)) is None
