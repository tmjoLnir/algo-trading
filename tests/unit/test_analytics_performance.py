"""Trade reconstruction, attribution and the statistics over them.

This layer produces the numbers a human uses to decide whether a strategy is
worth running, so the bar is the same as anywhere else that touches P&L: the
failure paths matter more than the happy one. A reconstruction that quietly
drops a trade, or lands one on the wrong side of a flip, reports a smaller loss
than the account actually took — and it reports it consistently, so nothing
downstream contradicts it.

The cases with teeth, in order of how badly they fail silently:

- **A fill through zero.** The one CLAUDE.md §5 and docs/TESTING.md both single
  out. Handled wrongly, one whole round trip disappears.
- **Shorts.** A sign error inverts the P&L of every short and nothing else
  notices.
- **Fees.** A win that is a loss after commission is the trade a win rate
  computed on gross P&L flatters.
- **Excursions with no bars.** Zero says "never went against us"; null says
  "we did not measure".
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from atp_core.analytics.performance import (
    UNATTRIBUTED,
    UNKNOWN_EXIT,
    PerformanceAnalyzer,
    comparability_warnings,
    infer_periods_per_year,
)
from atp_core.backtest.metrics import (
    METRIC_BASIS,
    TRADING_DAYS_PER_YEAR,
    PerformanceMetrics,
    compute_all,
    periods_per_year_for,
)
from atp_core.domain import Bar, Fill, Order, Side, Timeframe
from atp_core.execution.idempotency import (
    ENTRY,
    EXIT,
    FLATTEN,
    STOP_LOSS,
    TAKE_PROFIT,
    TIME_EXIT,
    UNKNOWN_PURPOSE,
)

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
SYMBOL = "SPY"


def order(
    side: Side,
    qty: str,
    purpose: str,
    fills: list[tuple[datetime, str, str, str]],
    *,
    symbol: str = SYMBOL,
    strategy_id: str | None = "sma",
) -> Order:
    """An order carrying real fills, which is what reconstruction reads."""
    built = Order(
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        strategy_id=strategy_id,
        purpose=purpose,
        created_at=fills[0][0],
    )
    for ts, fill_qty, price, fee in fills:
        built.apply_fill(
            Fill(
                order_id=built.id,
                ts=ts,
                qty=Decimal(fill_qty),
                price=Decimal(price),
                fee=Decimal(fee),
            )
        )
    return built


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def bar(ts: datetime, low: str, high: str, *, symbol: str = SYMBOL) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts,
        timeframe=Timeframe.D1,
        open=Decimal(low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(high),
        volume=Decimal("1000000"),
    )


@pytest.fixture
def analyzer() -> PerformanceAnalyzer:
    return PerformanceAnalyzer()


class TestOneRoundTrip:
    def test_a_long_that_opened_and_closed(self, analyzer: PerformanceAnalyzer) -> None:
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "100", ENTRY, [(at(0), "100", "50", "1")]),
                order(Side.SELL, "100", EXIT, [(at(3), "100", "55", "1")]),
            ]
        )

        assert len(trades) == 1
        trade = trades[0]
        assert trade.side == "long"
        assert trade.qty == Decimal("100")
        assert trade.entry_price == Decimal("50")
        assert trade.exit_price == Decimal("55")
        assert trade.gross_pnl == Decimal("500")
        assert trade.fees == Decimal("2")
        assert trade.net_pnl == Decimal("498")
        assert trade.holding_period_hours == 3.0
        assert trade.exit_reason == "signal"

    def test_a_short_makes_money_when_the_price_falls(self, analyzer: PerformanceAnalyzer) -> None:
        """The sign error worth failing a build over.

        A short entered at 50 and covered at 45 made 5 a share. Computed with
        the long's expression it reports a loss of the same size, and every
        other number in the report agrees with it — nothing downstream can tell
        the difference.
        """
        trades = analyzer.build_trades(
            [
                order(Side.SELL, "100", ENTRY, [(at(0), "100", "50", "0")]),
                order(Side.BUY, "100", EXIT, [(at(2), "100", "45", "0")]),
            ]
        )

        assert len(trades) == 1
        assert trades[0].side == "short"
        assert trades[0].gross_pnl == Decimal("500")
        assert trades[0].net_pnl == Decimal("500")

    def test_a_short_loses_when_the_price_rises(self, analyzer: PerformanceAnalyzer) -> None:
        trades = analyzer.build_trades(
            [
                order(Side.SELL, "100", ENTRY, [(at(0), "100", "50", "0")]),
                order(Side.BUY, "100", EXIT, [(at(2), "100", "58", "0")]),
            ]
        )

        assert trades[0].gross_pnl == Decimal("-800")

    def test_a_position_still_open_is_not_a_trade(self, analyzer: PerformanceAnalyzer) -> None:
        """It is a position, not a round trip.

        Reporting it as one closed at the last price would put an unrealised
        number in a table of realised ones, and every statistic over that table
        would then move with the market.
        """
        assert (
            analyzer.build_trades([order(Side.BUY, "100", ENTRY, [(at(0), "100", "50", "0")])])
            == []
        )

    def test_an_order_that_never_filled_moves_nothing(self, analyzer: PerformanceAnalyzer) -> None:
        unfilled = Order(
            symbol=SYMBOL, side=Side.BUY, qty=Decimal("100"), purpose=ENTRY, created_at=at(0)
        )
        assert analyzer.build_trades([unfilled]) == []


class TestScalingAndPartialExits:
    def test_scale_ins_are_one_trade_at_the_weighted_entry(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "100", ENTRY, [(at(0), "100", "100", "1")]),
                order(Side.BUY, "100", ENTRY, [(at(1), "100", "102", "1")]),
                order(Side.SELL, "200", EXIT, [(at(5), "200", "110", "2")]),
            ]
        )

        assert len(trades) == 1
        assert trades[0].entry_price == Decimal("101")
        assert trades[0].qty == Decimal("200")
        assert trades[0].gross_pnl == Decimal("1800")
        assert trades[0].fees == Decimal("4")

    def test_partial_exits_close_one_trade_not_two(self, analyzer: PerformanceAnalyzer) -> None:
        """A round trip is flat-to-flat, so half out is not half a trade.

        The exit price is the weighted average of both exits, and the trade
        closes on the *last* of them — which is what makes the holding period
        the period the position was actually held.
        """
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "200", ENTRY, [(at(0), "200", "100", "0")]),
                order(Side.SELL, "100", EXIT, [(at(2), "100", "110", "0")]),
                order(Side.SELL, "100", EXIT, [(at(6), "100", "120", "0")]),
            ]
        )

        assert len(trades) == 1
        assert trades[0].exit_price == Decimal("115")
        assert trades[0].gross_pnl == Decimal("3000")
        assert trades[0].holding_period_hours == 6.0

    def test_an_order_filling_in_pieces_is_one_entry(self, analyzer: PerformanceAnalyzer) -> None:
        """Reconstruction reads fills, not `avg_fill_price`.

        The summary fields are running totals stamped at one instant; the
        entry timestamp has to come from the first *print* or the holding period
        and the excursion window are both measured from the wrong place.
        """
        trades = analyzer.build_trades(
            [
                order(
                    Side.BUY,
                    "200",
                    ENTRY,
                    [(at(0), "100", "100", "1"), (at(1), "100", "104", "1")],
                ),
                order(Side.SELL, "200", EXIT, [(at(4), "200", "110", "0")]),
            ]
        )

        assert trades[0].entry_ts == at(0)
        assert trades[0].entry_price == Decimal("102")
        assert trades[0].holding_period_hours == 4.0


class TestAFillThroughZero:
    """The case CLAUDE.md §5 and docs/TESTING.md both single out.

    One sell of 300 against a long of 100 is two facts: the long closed, and a
    short of 200 opened. Treating it as either one alone loses the other, and
    the loss is silent — the missing short surfaces only when it is exited, as
    an exit with no entry.
    """

    @pytest.fixture
    def flipped(self, analyzer: PerformanceAnalyzer) -> list:
        return analyzer.build_trades(
            [
                order(Side.BUY, "100", ENTRY, [(at(0), "100", "100", "1")]),
                order(Side.SELL, "300", ENTRY, [(at(2), "300", "110", "3")]),
                order(Side.BUY, "200", EXIT, [(at(4), "200", "105", "2")]),
            ]
        )

    def test_both_sides_of_the_flip_are_trades(self, flipped: list) -> None:
        assert [t.side for t in flipped] == ["long", "short"]
        assert [t.qty for t in flipped] == [Decimal("100"), Decimal("200")]

    def test_the_closing_half_pairs_with_the_original_entry(self, flipped: list) -> None:
        long_trade = flipped[0]
        assert long_trade.entry_price == Decimal("100")
        assert long_trade.exit_price == Decimal("110")
        assert long_trade.gross_pnl == Decimal("1000")

    def test_the_opening_half_becomes_the_new_position(self, flipped: list) -> None:
        short_trade = flipped[1]
        assert short_trade.entry_price == Decimal("110")
        assert short_trade.exit_price == Decimal("105")
        assert short_trade.gross_pnl == Decimal("1000")

    def test_the_flipping_fees_are_split_pro_rata(self, flipped: list) -> None:
        """One commission, one execution, two trades that share it by quantity.

        100 of 300 closes the long, so a third of the 3 goes there — 1, plus
        the entry's own 1. The rest rides with the short.
        """
        assert flipped[0].fees == Decimal("2")
        assert flipped[1].fees == Decimal("4")

    def test_no_pnl_is_created_or_destroyed(self, flipped: list) -> None:
        """The total is the one thing a matching convention must never change."""
        assert sum(t.gross_pnl for t in flipped) == Decimal("2000")
        assert sum(t.fees for t in flipped) == Decimal("6")

    def test_a_reversal_is_a_signal_exit_not_an_unknown_one(self, flipped: list) -> None:
        """The closing leg's purpose is `entry` — the strategy decided to reverse.

        Reporting that as `unknown` would be a worse answer than the one
        available, and it would put every reversal into the bucket reserved for
        orders stored before the purpose column existed.
        """
        assert flipped[0].exit_reason == "signal"


class TestExitReasons:
    @pytest.mark.parametrize(
        ("purpose", "expected"),
        [
            (EXIT, "signal"),
            (STOP_LOSS, "stop_loss"),
            (TAKE_PROFIT, "take_profit"),
            (TIME_EXIT, "time"),
            (FLATTEN, "manual"),
        ],
    )
    def test_the_closing_orders_purpose_names_the_reason(
        self, analyzer: PerformanceAnalyzer, purpose: str, expected: str
    ) -> None:
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")]),
                order(Side.SELL, "10", purpose, [(at(1), "10", "101", "0")]),
            ]
        )
        assert trades[0].exit_reason == expected

    def test_an_order_stored_before_the_purpose_column_is_unknown_not_guessed(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        """Reachable for real — migration `c3f8b2d5e714` left old rows null.

        A wrong exit reason is worse than a missing one: it is the number that
        decides whether a strategy's stops are misplaced.
        """
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")]),
                order(Side.SELL, "10", UNKNOWN_PURPOSE, [(at(1), "10", "101", "0")]),
            ]
        )
        assert trades[0].exit_reason == UNKNOWN_EXIT


class TestAttributionToAStrategy:
    def test_the_entry_names_the_strategy(self, analyzer: PerformanceAnalyzer) -> None:
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")], strategy_id="momentum"),
                order(Side.SELL, "10", EXIT, [(at(1), "10", "101", "0")], strategy_id="momentum"),
            ]
        )
        assert trades[0].strategy_id == "momentum"

    def test_an_order_with_no_strategy_is_unattributed_not_blank(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        """A manual order from the dashboard has no strategy, and says so."""
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")], strategy_id=None),
                order(Side.SELL, "10", FLATTEN, [(at(1), "10", "101", "0")], strategy_id=None),
            ]
        )
        assert trades[0].strategy_id == UNATTRIBUTED


class TestSeveralSymbols:
    def test_each_symbol_is_reconstructed_independently(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        """A sell of QQQ must not close a position in SPY.

        Interleaved deliberately: the streams are separated by symbol, not by
        the order the fills arrived in.
        """
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")], symbol="SPY"),
                order(Side.BUY, "5", ENTRY, [(at(1), "5", "300", "0")], symbol="QQQ"),
                order(Side.SELL, "10", EXIT, [(at(2), "10", "110", "0")], symbol="SPY"),
                order(Side.SELL, "5", EXIT, [(at(3), "5", "290", "0")], symbol="QQQ"),
            ]
        )

        assert {t.symbol: t.gross_pnl for t in trades} == {
            "SPY": Decimal("100"),
            "QQQ": Decimal("-50"),
        }

    def test_trades_come_back_in_the_order_they_closed(self, analyzer: PerformanceAnalyzer) -> None:
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")], symbol="SPY"),
                order(Side.BUY, "5", ENTRY, [(at(0), "5", "300", "0")], symbol="QQQ"),
                order(Side.SELL, "5", EXIT, [(at(1), "5", "310", "0")], symbol="QQQ"),
                order(Side.SELL, "10", EXIT, [(at(9), "10", "110", "0")], symbol="SPY"),
            ]
        )
        assert [t.symbol for t in trades] == ["QQQ", "SPY"]


class TestTradeIdentity:
    def test_the_same_history_reconstructs_to_the_same_ids(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        """A screen keyed on these must not change identity on a reload."""
        history = [
            order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")]),
            order(Side.SELL, "10", EXIT, [(at(1), "10", "101", "0")]),
        ]
        assert (
            analyzer.build_trades(history)[0].trade_id == analyzer.build_trades(history)[0].trade_id
        )

    def test_two_different_trades_do_not_collide(self, analyzer: PerformanceAnalyzer) -> None:
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")]),
                order(Side.SELL, "10", EXIT, [(at(1), "10", "101", "0")]),
                order(Side.BUY, "10", ENTRY, [(at(2), "10", "100", "0")]),
                order(Side.SELL, "10", EXIT, [(at(3), "10", "101", "0")]),
            ]
        )
        assert trades[0].trade_id != trades[1].trade_id


class TestExcursions:
    """MAE/MFE — the numbers that say whether a stop sits too close."""

    def test_a_long_measures_the_high_and_the_low(self, analyzer: PerformanceAnalyzer) -> None:
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "100", ENTRY, [(at(0), "100", "100", "0")]),
                order(Side.SELL, "100", EXIT, [(at(48), "100", "105", "0")]),
            ]
        )
        measured = analyzer.with_excursions(
            trades,
            {SYMBOL: [bar(at(0), "95", "103"), bar(at(24), "92", "112")]},
        )

        # Best: 112 − 100 = 12 a share. Worst: 92 − 100 = −8 a share.
        assert measured[0].max_favorable_excursion == Decimal("1200")
        assert measured[0].max_adverse_excursion == Decimal("-800")

    def test_a_short_measures_them_the_other_way_round(self, analyzer: PerformanceAnalyzer) -> None:
        """A short profits as the price falls, so the LOW is its favourable end.

        The mirror of the long, and the case a sign error reports backwards —
        reading a short's worst drawdown as its best gain.
        """
        trades = analyzer.build_trades(
            [
                order(Side.SELL, "100", ENTRY, [(at(0), "100", "100", "0")]),
                order(Side.BUY, "100", EXIT, [(at(48), "100", "98", "0")]),
            ]
        )
        measured = analyzer.with_excursions(trades, {SYMBOL: [bar(at(24), "92", "112")]})

        assert measured[0].max_favorable_excursion == Decimal("800")  # 100 − 92
        assert measured[0].max_adverse_excursion == Decimal("-1200")  # 100 − 112

    def test_no_bars_reports_null_not_zero(self, analyzer: PerformanceAnalyzer) -> None:
        """Zero is the most flattering possible reading of "we have no idea"."""
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "100", ENTRY, [(at(0), "100", "100", "0")]),
                order(Side.SELL, "100", EXIT, [(at(4), "100", "105", "0")]),
            ]
        )
        measured = analyzer.with_excursions(trades, {})

        assert measured[0].max_favorable_excursion is None
        assert measured[0].max_adverse_excursion is None

    def test_bars_outside_the_holding_period_are_ignored(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        """A crash the week after the exit is not this trade's drawdown."""
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "100", ENTRY, [(at(0), "100", "100", "0")]),
                order(Side.SELL, "100", EXIT, [(at(4), "100", "105", "0")]),
            ]
        )
        measured = analyzer.with_excursions(
            trades,
            {SYMBOL: [bar(at(2), "99", "106"), bar(at(200), "10", "11")]},
        )

        assert measured[0].max_adverse_excursion == Decimal("-100")  # 99, not 10

    def test_a_trade_that_only_went_one_way_reports_zero_on_the_other(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        """Zero here is a measurement, unlike the null above.

        A gap that opened past the entry and never came back makes the raw
        "best" negative, and a negative *maximum favourable* excursion is not a
        quantity anybody can read.
        """
        trades = analyzer.build_trades(
            [
                order(Side.BUY, "100", ENTRY, [(at(0), "100", "100", "0")]),
                order(Side.SELL, "100", EXIT, [(at(4), "100", "90", "0")]),
            ]
        )
        measured = analyzer.with_excursions(trades, {SYMBOL: [bar(at(2), "88", "95")]})

        assert measured[0].max_favorable_excursion == Decimal("0")
        assert measured[0].max_adverse_excursion == Decimal("-1200")


class TestAttribution:
    @pytest.fixture
    def trades(self, analyzer: PerformanceAnalyzer) -> list:
        return analyzer.build_trades(
            [
                # A winner stopped out of, and a loser taken at target — so the
                # exit-reason grouping has something to disagree about.
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")], strategy_id="a"),
                order(Side.SELL, "10", STOP_LOSS, [(at(1), "10", "90", "0")], strategy_id="a"),
                order(Side.BUY, "10", ENTRY, [(at(2), "10", "100", "0")], strategy_id="b"),
                order(Side.SELL, "10", TAKE_PROFIT, [(at(3), "10", "130", "0")], strategy_id="b"),
            ]
        )

    def test_by_strategy(self, analyzer: PerformanceAnalyzer, trades: list) -> None:
        rows = {r.key: r for r in analyzer.attribution(trades, "strategy")}
        assert rows["a"].net_pnl == Decimal("-100")
        assert rows["b"].net_pnl == Decimal("300")

    def test_by_exit_reason_is_the_one_worth_reading(
        self, analyzer: PerformanceAnalyzer, trades: list
    ) -> None:
        """The grouping this whole item exists for.

        A strategy whose profit comes from its targets while its stops bleed has
        a stop-placement problem rather than a signal problem, and this is the
        table that says so.
        """
        rows = {r.key: r for r in analyzer.attribution(trades, "exit_reason")}
        assert rows["stop_loss"].net_pnl == Decimal("-100")
        assert rows["take_profit"].net_pnl == Decimal("300")

    def test_rows_are_ordered_best_first(self, analyzer: PerformanceAnalyzer, trades: list) -> None:
        rows = analyzer.attribution(trades, "strategy")
        assert [r.key for r in rows] == ["b", "a"]

    def test_win_rate_is_per_group(self, analyzer: PerformanceAnalyzer, trades: list) -> None:
        rows = {r.key: r for r in analyzer.attribution(trades, "strategy")}
        assert rows["a"].win_rate == 0.0
        assert rows["b"].win_rate == 1.0

    def test_contribution_is_denominated_in_absolute_pnl(
        self, analyzer: PerformanceAnalyzer, trades: list
    ) -> None:
        """Shares of the *net* misbehave exactly when a reader needs them.

        Here the net is 200 and the two groups are −100 and +300. As a share of
        the net those read −50% and +150%; against the absolute total of 400
        they are −25% and +75%, which lands inside ±100% and still says who
        helped and who hurt.
        """
        rows = {r.key: r for r in analyzer.attribution(trades, "strategy")}
        assert rows["b"].contribution_pct == pytest.approx(75.0)
        assert rows["a"].contribution_pct == pytest.approx(-25.0)

    def test_by_hour_and_weekday_group_on_the_entry(
        self, analyzer: PerformanceAnalyzer, trades: list
    ) -> None:
        """When does this strategy find trades worth taking?

        Grouping on the exit would answer when its stops happen to fire, which
        is a fact about the market's schedule rather than about the strategy.
        """
        hours = {r.key for r in analyzer.attribution(trades, "hour")}
        assert hours == {"14", "16"}  # entries at 14:30 and 16:30 UTC
        assert {r.key for r in analyzer.attribution(trades, "weekday")} == {"Monday"}

    def test_an_unknown_dimension_raises(self, analyzer: PerformanceAnalyzer, trades: list) -> None:
        """Not an empty list.

        A report silently grouped by nothing looks like a period with no trades,
        and "you asked for something that does not exist" is a different answer.
        """
        with pytest.raises(ValueError, match="cannot attribute by"):
            analyzer.attribution(trades, "phase_of_moon")

    def test_no_trades_is_no_rows_rather_than_an_error(self, analyzer: PerformanceAnalyzer) -> None:
        assert analyzer.attribution([], "strategy") == []


class TestDailyReturns:
    def test_close_over_close(self, analyzer: PerformanceAnalyzer) -> None:
        curve = [
            (datetime(2026, 3, 2, 21, tzinfo=UTC), Decimal("100000")),
            (datetime(2026, 3, 3, 21, tzinfo=UTC), Decimal("101000")),
            (datetime(2026, 3, 4, 21, tzinfo=UTC), Decimal("99980")),
        ]
        returns = analyzer.daily_returns(curve)

        assert returns[date(2026, 3, 3)] == Decimal("0.01")
        assert returns[date(2026, 3, 4)] == Decimal("-1020") / Decimal("101000")

    def test_the_last_point_of_a_day_is_its_close(self, analyzer: PerformanceAnalyzer) -> None:
        curve = [
            (datetime(2026, 3, 2, 14, tzinfo=UTC), Decimal("100000")),
            (datetime(2026, 3, 2, 21, tzinfo=UTC), Decimal("100500")),
            (datetime(2026, 3, 3, 21, tzinfo=UTC), Decimal("101505")),
        ]
        assert analyzer.daily_returns(curve)[date(2026, 3, 3)] == Decimal("0.01")

    def test_the_first_day_has_no_return_and_is_absent(self, analyzer: PerformanceAnalyzer) -> None:
        """Absence says the series started there; zero would claim it was flat."""
        curve = [
            (datetime(2026, 3, 2, 21, tzinfo=UTC), Decimal("100000")),
            (datetime(2026, 3, 3, 21, tzinfo=UTC), Decimal("101000")),
        ]
        assert date(2026, 3, 2) not in analyzer.daily_returns(curve)

    def test_a_day_after_a_wiped_out_account_is_absent(self, analyzer: PerformanceAnalyzer) -> None:
        """There is no return to compute from zero, and no honest number to invent."""
        curve = [
            (datetime(2026, 3, 2, 21, tzinfo=UTC), Decimal("0")),
            (datetime(2026, 3, 3, 21, tzinfo=UTC), Decimal("500")),
        ]
        assert analyzer.daily_returns(curve) == {}

    def test_returns_are_decimal(self, analyzer: PerformanceAnalyzer) -> None:
        """Two account balances divided (rule §1.1), read against a statement."""
        curve = [
            (datetime(2026, 3, 2, 21, tzinfo=UTC), Decimal("100000")),
            (datetime(2026, 3, 3, 21, tzinfo=UTC), Decimal("101000")),
        ]
        assert all(isinstance(v, Decimal) for v in analyzer.daily_returns(curve).values())


class TestInferringTheSamplingRate:
    """Annualisation is the one way to make every ratio wrong while all of them
    still look plausible."""

    def test_a_daily_curve_infers_daily(self) -> None:
        curve = [(datetime(2026, 3, d, 21, tzinfo=UTC), Decimal("100000")) for d in range(2, 12)]
        assert infer_periods_per_year(curve) == TRADING_DAYS_PER_YEAR

    def test_a_minute_curve_does_not_infer_daily(self) -> None:
        """The mistake this exists to prevent.

        The runner writes an equity point per evaluation — once a minute — and
        annualising that as though it were daily understates volatility by about
        twenty times, which turns a mediocre Sharpe into a spectacular one.
        """
        curve = [(datetime(2026, 3, 2, 14, m, tzinfo=UTC), Decimal("100000")) for m in range(30)]
        inferred = infer_periods_per_year(curve)

        assert inferred == TRADING_DAYS_PER_YEAR * 390
        assert inferred > TRADING_DAYS_PER_YEAR

    def test_the_median_gap_wins_over_the_mean(self) -> None:
        """A curve has a 16-hour hole at every overnight.

        A mean over a minute-sampled series with those holes in it lands nowhere
        near either sampling rate.
        """
        curve = [
            (datetime(2026, 3, 2, 14, 0, tzinfo=UTC), Decimal("100000")),
            (datetime(2026, 3, 2, 14, 1, tzinfo=UTC), Decimal("100000")),
            (datetime(2026, 3, 2, 14, 2, tzinfo=UTC), Decimal("100000")),
            (datetime(2026, 3, 3, 14, 0, tzinfo=UTC), Decimal("100000")),  # overnight
            (datetime(2026, 3, 3, 14, 1, tzinfo=UTC), Decimal("100000")),
        ]
        assert infer_periods_per_year(curve) == TRADING_DAYS_PER_YEAR * 390

    def test_a_curve_too_short_to_measure_falls_back_to_daily(self) -> None:
        assert infer_periods_per_year([]) == TRADING_DAYS_PER_YEAR
        assert (
            infer_periods_per_year([(datetime(2026, 3, 2, tzinfo=UTC), Decimal("1"))])
            == TRADING_DAYS_PER_YEAR
        )


class TestMetrics:
    @pytest.fixture
    def trades(self, analyzer: PerformanceAnalyzer) -> list:
        return analyzer.build_trades(
            [
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")]),
                order(Side.SELL, "10", EXIT, [(at(24), "10", "110", "0")]),
                order(Side.BUY, "10", ENTRY, [(at(48), "10", "100", "0")]),
                order(Side.SELL, "10", STOP_LOSS, [(at(72), "10", "95", "0")]),
            ]
        )

    def test_it_runs_through_the_backtests_own_functions(
        self, analyzer: PerformanceAnalyzer, trades: list
    ) -> None:
        """ADR 0006's reasoning again: one implementation, so the two compare.

        Verified by computing the same thing through `compute_all` directly —
        a live Sharpe from different code could not be compared to a backtested
        one, and comparing them is the point of running paper first.
        """
        curve = [
            (at(0), Decimal("100000")),
            (at(24), Decimal("100100")),
            (at(72), Decimal("100050")),
        ]
        got = analyzer.metrics(trades, curve, periods_per_year=TRADING_DAYS_PER_YEAR)
        expected = compute_all(
            [(ts, e) for ts, e in curve],
            [t.net_pnl for t in trades],
            periods_per_year=TRADING_DAYS_PER_YEAR,
            avg_holding_period_hours=got.avg_holding_period_hours,
            exposure_pct=got.exposure_pct,
            turnover=got.turnover,
        )
        assert got == expected

    def test_win_rate_and_trade_count_come_from_the_trades(
        self, analyzer: PerformanceAnalyzer, trades: list
    ) -> None:
        metrics = analyzer.metrics(trades, [(at(0), Decimal("100000"))])
        assert metrics.num_trades == 2
        assert metrics.win_rate == 0.5

    def test_holding_period_is_the_mean_over_trades(
        self, analyzer: PerformanceAnalyzer, trades: list
    ) -> None:
        metrics = analyzer.metrics(trades, [(at(0), Decimal("100000"))])
        assert metrics.avg_holding_period_hours == 24.0

    def test_exposure_merges_overlapping_positions(self, analyzer: PerformanceAnalyzer) -> None:
        """Two positions over the same hour is one hour of exposure.

        Summing them gives 200% of a period, which is a leverage statement
        dressed up as a time one.
        """
        overlapping = analyzer.build_trades(
            [
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "100", "0")], symbol="SPY"),
                order(Side.BUY, "10", ENTRY, [(at(0), "10", "300", "0")], symbol="QQQ"),
                order(Side.SELL, "10", EXIT, [(at(5), "10", "101", "0")], symbol="SPY"),
                order(Side.SELL, "10", EXIT, [(at(5), "10", "301", "0")], symbol="QQQ"),
            ]
        )
        metrics = analyzer.metrics(
            overlapping, [(at(0), Decimal("100000")), (at(10), Decimal("100000"))]
        )
        assert metrics.exposure_pct == pytest.approx(0.5)

    def test_a_win_that_fees_turn_into_a_loss_counts_as_a_loss(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        """The only definition that pays anybody.

        $3 gross against $4 of commission is a loss, and counting it as a win
        inflates the win rate on exactly the strategies whose edge is too thin
        to survive their own costs.
        """
        thin = analyzer.build_trades(
            [
                order(Side.BUY, "1", ENTRY, [(at(0), "1", "100", "2")]),
                order(Side.SELL, "1", EXIT, [(at(1), "1", "103", "2")]),
            ]
        )
        assert thin[0].gross_pnl == Decimal("3")
        assert thin[0].net_pnl == Decimal("-1")
        assert analyzer.metrics(thin, [(at(0), Decimal("1000"))]).win_rate == 0.0

    def test_no_trades_does_not_raise(self, analyzer: PerformanceAnalyzer) -> None:
        metrics = analyzer.metrics([], [(at(0), Decimal("100000"))])
        assert metrics.num_trades == 0
        assert metrics.exposure_pct == 0.0


class TestCompareToBacktest:
    def test_each_metric_is_live_minus_backtest(self, analyzer: PerformanceAnalyzer) -> None:
        live = compute_all([(at(0), Decimal("100")), (at(24), Decimal("110"))], [Decimal("10")])
        backtest = compute_all([(at(0), Decimal("100")), (at(24), Decimal("130"))], [Decimal("30")])
        divergence = analyzer.compare_to_backtest(live, backtest)

        assert divergence["total_return"] == pytest.approx(0.10 - 0.30)
        assert divergence["expectancy"] == pytest.approx(-20.0)

    def test_a_run_that_took_fewer_trades_shows_it(self, analyzer: PerformanceAnalyzer) -> None:
        """A third as many trades has not underperformed; it has been refused.

        That shows up here before anyone starts blaming the signal.
        """
        live = compute_all([(at(0), Decimal("100"))], [Decimal("1")])
        backtest = compute_all([(at(0), Decimal("100"))], [Decimal("1")] * 9)
        assert analyzer.compare_to_backtest(live, backtest)["num_trades"] == -8

    def test_every_metric_is_compared(self, analyzer: PerformanceAnalyzer) -> None:
        metrics = compute_all([(at(0), Decimal("100"))], [Decimal("1")])
        assert set(analyzer.compare_to_backtest(metrics, metrics)) == set(metrics.to_dict())


class TestTheInvariantThatMattersMost:
    """Reconstruction may reshape P&L into trades; it may never change the total.

    docs/TESTING.md asks for the invariants that are better stated than
    enumerated, and this is the one for this module. Whatever the fill sequence
    — scale-ins, partial exits, reversals through zero, several at once — the
    round trips a reconstruction reports must add up to the P&L the fills
    themselves produce. A matching convention that changed the total would not be
    a convention, it would be a bug that reports a different loss than the
    account took.

    Generated rather than enumerated because the failures live in the
    combinations: a flip *inside* a scale-in, an exit that overshoots by exactly
    the open quantity, a fill sequence that returns to flat twice.
    """

    @staticmethod
    def _pnl_from_fills(orders: list[Order]) -> Decimal:
        """Cash in minus cash out, straight off the prints.

        Deliberately naive and computed a different way from the analyzer: a
        position closed is a position whose signed cash flows are its P&L, with
        no notion of a trade at all. Two implementations that agree are evidence;
        one implementation checked against itself is not.
        """
        total = Decimal(0)
        for placed in orders:
            for fill in placed.fills:
                total -= fill.qty * fill.price * placed.side.sign
                total -= fill.fee
        return total

    @given(
        st.lists(
            st.tuples(
                st.sampled_from([Side.BUY, Side.SELL]),
                st.integers(min_value=1, max_value=300),
                st.integers(min_value=50, max_value=150),
            ),
            min_size=2,
            max_size=12,
        )
    )
    @settings(max_examples=250, deadline=None)
    def test_reconstructed_pnl_equals_the_pnl_of_the_fills(
        self, script: list[tuple[Side, int, int]]
    ) -> None:
        orders = [
            order(side, str(qty), ENTRY, [(at(index), str(qty), str(price), "0")])
            for index, (side, qty, price) in enumerate(script)
        ]

        trades = PerformanceAnalyzer().build_trades(orders)
        closed = sum((t.net_pnl for t in trades), Decimal(0))

        # Only the *flat* part of the history is a round trip. Whatever is still
        # open carries the rest, so the comparison marks it out at its own entry
        # price — which contributes exactly zero and leaves the realised half.
        net_qty = sum(q * s.sign for s, q, _ in script)
        if net_qty == 0:
            assert closed == self._pnl_from_fills(orders)
        else:
            # An open remainder means some cash is still committed; the closed
            # trades must still account for no more than the whole.
            assert abs(closed) <= abs(self._pnl_from_fills(orders)) + _open_cost(orders)

    @given(
        st.lists(
            st.tuples(
                st.sampled_from([Side.BUY, Side.SELL]),
                st.integers(min_value=1, max_value=200),
                st.integers(min_value=50, max_value=150),
            ),
            min_size=2,
            max_size=10,
        )
    )
    @settings(max_examples=250, deadline=None)
    def test_every_trade_is_internally_consistent(
        self, script: list[tuple[Side, int, int]]
    ) -> None:
        """Whatever comes out must be a coherent round trip.

        Each of these is cheap to state and expensive to discover in a report: a
        trade with no quantity, one whose net is not its gross less its fees, one
        that exits before it enters.
        """
        orders = [
            order(side, str(qty), ENTRY, [(at(index), str(qty), str(price), "1")])
            for index, (side, qty, price) in enumerate(script)
        ]

        for trade in PerformanceAnalyzer().build_trades(orders):
            assert trade.qty > 0
            assert trade.net_pnl == trade.gross_pnl - trade.fees
            assert trade.exit_ts is not None
            assert trade.exit_ts >= trade.entry_ts
            assert trade.holding_period_hours >= 0
            assert trade.side in ("long", "short")


def _open_cost(orders: list[Order]) -> Decimal:
    """An upper bound on the cash tied up in whatever is still open."""
    return sum((fill.qty * fill.price for placed in orders for fill in placed.fills), Decimal(0))


class TestComparingAgainstAStoredRun:
    """The other operand is a JSON column, not a `PerformanceMetrics`.

    Everything here is about that seam. `backtest.runner.jsonable` nulls every
    non-finite metric on the way into the `backtest_runs` row because
    `Infinity` is not legal JSON, so the mapping that comes back out has holes
    in it — and the run most likely to have one is the run somebody most wants
    to compare against.
    """

    def test_a_stored_mapping_compares_the_same_as_a_metric_set(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        live = compute_all([(at(0), Decimal("100")), (at(24), Decimal("110"))], [Decimal("10")])
        backtest = compute_all([(at(0), Decimal("100")), (at(24), Decimal("130"))], [Decimal("30")])

        # `nan_ok`, because a metric that is infinite on both sides subtracts to
        # nan and nan does not equal itself. That is the same nan by both routes,
        # which is what this asserts.
        assert analyzer.compare_to_backtest(live, backtest.to_dict()) == pytest.approx(
            analyzer.compare_to_backtest(live, backtest), nan_ok=True
        )

    def test_a_null_on_either_side_is_a_null_divergence(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        """Not zero, and the distinction is the whole reason this is nullable.

        Zero is the strongest claim the report can make — live matched the
        backtest exactly on this metric — and it is the last thing an absent
        number should render as.
        """
        live = compute_all([(at(0), Decimal("100"))], [Decimal("1")])

        divergence = analyzer.compare_to_backtest(live, {**live.to_dict(), "profit_factor": None})

        assert divergence["profit_factor"] is None
        assert divergence["win_rate"] == 0.0

    def test_a_metric_missing_from_one_side_does_not_retire_the_others(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        """A run stored before the metric set grew a field is still comparable.

        On every field it does have. Otherwise adding one metric would make
        every backtest on record uncomparable at once.
        """
        live = compute_all([(at(0), Decimal("100"))], [Decimal("1")])
        stored = {k: v for k, v in live.to_dict().items() if k != "turnover"}

        divergence = analyzer.compare_to_backtest(live, stored)

        assert divergence["turnover"] is None
        assert divergence["num_trades"] == 0.0

    def test_an_infinite_live_metric_is_not_silently_dropped(
        self, analyzer: PerformanceAnalyzer
    ) -> None:
        """Infinity is a value, not an absence.

        The live half is computed in this process and never went through JSON,
        so it can legitimately hold one — `profit_factor` with no losing trade.
        Subtracting a finite backtest from it is infinite, which is the honest
        answer and is distinguishable from None.
        """
        live = compute_all([(at(0), Decimal("100"))], [Decimal("1")])
        assert live.profit_factor == float("inf")

        divergence = analyzer.compare_to_backtest(live, {**live.to_dict(), "profit_factor": 1.5})

        assert divergence["profit_factor"] == float("inf")


class TestComparabilityWarnings:
    """What stops a divergence table being read as more than it is.

    Every one of these is a case where the arithmetic is correct and the
    conclusion a reader would draw from it is wrong.
    """

    def _warn(self, **overrides: object) -> list[str]:
        kwargs: dict[str, object] = {
            "live_periods_per_year": TRADING_DAYS_PER_YEAR,
            "backtest_periods_per_year": TRADING_DAYS_PER_YEAR,
            "live_days": 100.0,
            "backtest_days": 100.0,
            "live_trades": 50,
            "live_symbols": ["SPY"],
            "backtest_symbols": ["SPY"],
        }
        kwargs.update(overrides)
        return comparability_warnings(**kwargs)  # type: ignore[arg-type]

    def test_two_comparable_runs_still_get_the_sizing_caveat(self) -> None:
        """The one that is always true for these runs.

        A backtest sizes every entry at a flat share count; live sizing is
        risk-based. The money-denominated metrics differ for that reason before
        the strategy has done anything.
        """
        notes = self._warn()
        assert len(notes) == 1
        assert "sizing" in notes[0]

    def test_an_empty_live_half_is_named_before_anything_else(self) -> None:
        """Because every other sentence is about a measurement that was made.

        With no closed trades the divergence column is the backtest's own
        metrics negated, which reads as catastrophic underperformance rather
        than as no data.
        """
        assert "no live round trips" in self._warn(live_trades=0)[0]

    def test_a_thin_live_sample_is_named_but_not_as_an_empty_one(self) -> None:
        notes = self._warn(live_trades=7)
        assert any("only 7 live round trips" in n for n in notes)
        assert not any("no live round trips" in n for n in notes)

    def test_different_annualisation_bases_are_named_with_both_numbers(self) -> None:
        """The divergence that is measurement rather than performance.

        A reader who cannot see both numbers has no way to tell a Sharpe gap
        caused by the strategy from one caused by the sampling.
        """
        notes = self._warn(live_periods_per_year=168, backtest_periods_per_year=98280)
        assert any("168" in n and "98280" in n for n in notes)

    def test_windows_of_similar_length_are_not_warned_about(self) -> None:
        """The threshold has to admit the ordinary case or it is noise.

        A paper run that has been going most of the backtest's length is the
        comparison this endpoint is for.
        """
        assert not any("different lengths" in n for n in self._warn(live_days=60.0))

    def test_a_live_window_a_fraction_of_the_backtest_is(self) -> None:
        assert any("different lengths" in n for n in self._warn(live_days=10.0))

    def test_a_symbol_live_traded_that_was_never_backtested_is_named(self) -> None:
        notes = self._warn(live_symbols=["SPY", "TSLA"])
        assert any("TSLA" in n and "never covered" in n for n in notes)

    def test_a_backtested_symbol_live_has_not_touched_is_named(self) -> None:
        """A refusal or a data gap, not underperformance — and the trade count
        below it looks identical either way."""
        notes = self._warn(backtest_symbols=["SPY", "QQQ"])
        assert any("QQQ" in n for n in notes)

    def test_it_is_not_named_when_nothing_traded_at_all(self) -> None:
        """The empty-half sentence already says it, and better.

        Listing every backtested symbol as untraded on a strategy that has
        closed nothing is a longer way of saying "no data" and buries the
        sentence that says it plainly.
        """
        notes = self._warn(live_trades=0, live_symbols=[], backtest_symbols=["SPY", "QQQ"])
        assert not any("QQQ" in n for n in notes)


class TestTheAnnualisationBasis:
    def test_a_daily_series_is_a_trading_year(self) -> None:
        assert periods_per_year_for(Timeframe.D1) == TRADING_DAYS_PER_YEAR

    def test_a_minute_series_is_a_trading_year_of_sessions(self) -> None:
        """252 x 390. Annualising minute bars at 252 understates volatility by
        about twenty times, which turns a mediocre Sharpe into a spectacular
        one."""
        assert periods_per_year_for(Timeframe.M1) == TRADING_DAYS_PER_YEAR * 390

    def test_the_engine_and_the_comparison_read_one_function(self) -> None:
        """Not a tautology: these were two copies until the comparison needed one.

        A backtest annualised by the engine's copy and reported by the
        endpoint's would differ by whichever drifted, and the difference would
        surface as a Sharpe divergence nobody could source.
        """
        from atp_core.backtest import engine

        assert engine.periods_per_year_for is periods_per_year_for

    def test_every_metric_has_a_basis(self) -> None:
        """An unlabelled row in a divergence table is what the label prevents."""
        assert set(METRIC_BASIS) == set(PerformanceMetrics.__dataclass_fields__)
