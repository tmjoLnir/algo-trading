"""The end-of-day summary, and the distinction the whole module exists for.

Every assertion worth making here is about the difference between *zero* and
*not measured*. A report that renders "0 feed incidents" from a store that has
never held one is worse than no report, because somebody will believe it — and
the day this summarises is exactly the day a reader wants to know whether the
feed misbehaved.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from atp_core.analytics.daily import (
    Coverage,
    DailyReport,
    Section,
    count_outcomes,
    render,
    summarise,
)
from atp_core.audit.ports import Action, AuditEntry
from atp_core.domain import Order, OrderStatus, Side

DAY = date(2026, 3, 20)
T0 = datetime(2026, 3, 20, 14, 30, tzinfo=UTC)


def order(
    *,
    status: OrderStatus = OrderStatus.FILLED,
    symbol: str = "SPY",
    qty: str = "10",
    price: str = "100",
    rejected_by: str | None = None,
    at_hour: int = 0,
) -> Order:
    built = Order(
        symbol=symbol,
        side=Side.BUY,
        qty=Decimal(qty),
        strategy_id="sma",
        created_at=T0 + timedelta(hours=at_hour),
    )
    built.status = status
    built.rejected_by = rejected_by
    if status is OrderStatus.FILLED:
        built.filled_qty = Decimal(qty)
        built.avg_fill_price = Decimal(price)
    return built


def audit_entry(action: str) -> AuditEntry:
    return AuditEntry(at=T0, actor="operator", action=action, target="global")


class TestADayThatTraded:
    def test_it_counts_what_reached_the_venue(self) -> None:
        report = summarise(DAY, [order(), order(symbol="QQQ"), order(status=OrderStatus.SUBMITTED)])

        assert report.orders_submitted == 3
        assert report.orders_filled == 2
        assert report.traded
        assert report.symbols == ("QQQ", "SPY")

    def test_refusals_are_ranked_by_the_rule_that_made_them(self) -> None:
        """The number an operator reads to decide whether the risk config is too
        tight is useless without knowing which rule produced it."""
        report = summarise(
            DAY,
            [
                order(status=OrderStatus.REJECTED_RISK, rejected_by="max_position_size"),
                order(status=OrderStatus.REJECTED_RISK, rejected_by="max_position_size"),
                order(status=OrderStatus.REJECTED_RISK, rejected_by="kill_switch"),
            ],
        )

        assert report.orders_refused == 3
        assert report.refusals_by_rule == {"max_position_size": 2, "kill_switch": 1}

    def test_a_refusal_with_no_rule_recorded_is_named_unknown(self) -> None:
        """Rows written before `rejected_by` existed are null and stay null. A
        bucket called "unknown" is honest where dropping them is not."""
        report = summarise(DAY, [order(status=OrderStatus.REJECTED_RISK)])

        assert report.refusals_by_rule == {"unknown": 1}


class TestADayThatDidNot:
    def test_the_headline_leads_with_the_silence(self) -> None:
        """The outcome this platform has actually produced. Day 1 of the paper
        week ran ten hours, submitted zero orders and reported it nowhere."""
        report = summarise(DAY, [])

        assert report.headline() == "no orders submitted"
        assert not report.traded

    def test_the_trades_section_says_where_to_look_next(self) -> None:
        report = summarise(DAY, [])

        trades = next(s for s in report.sections if s.name == "trades")
        assert trades.value == 0
        assert "runner.evaluated" in trades.how_to_check


class TestZeroIsNotAbsent:
    """The distinction the module exists for, from both directions."""

    def test_a_countable_section_with_nothing_in_it_is_zero(self) -> None:
        """Refused orders are rows. A day with none is a *measured* zero, and
        folding it into "not measured" would waste a real answer."""
        report = summarise(DAY, [order()], audit=[])

        refusals = next(s for s in report.sections if s.name == "risk rejections")
        assert refusals.value == 0
        assert not refusals.is_absent

    def test_feed_incidents_are_always_absent(self) -> None:
        """Nothing counts them: reconnects, gaps and staleness are log lines
        with no table behind any of them."""
        report = summarise(DAY, [order()], audit=[])

        feed = next(s for s in report.sections if s.name == "feed incidents")
        assert feed.is_absent
        assert feed.how_to_check, "an absent section has to say how to get the answer"

    def test_the_absent_list_is_the_thing_to_read_first(self) -> None:
        """A report whose absent list is non-empty is a partial report, and
        saying so at the top is the difference between a summary and a claim."""
        # Equity and coverage supplied, so feed incidents is the one store that
        # does not exist. Without them those two are absent too, which
        # `TestTheDayFourReport` asserts.
        report = summarise(
            DAY,
            [order()],
            audit=[],
            starting_equity=Decimal(100_000),
            ending_equity=Decimal(100_000),
            coverage=Coverage(evaluated_minutes=390, session_minutes=390, open_at=T0),
        )

        assert [s.name for s in report.absent] == ["feed incidents"]


class TestHalts:
    def test_an_audit_table_that_was_read_gives_a_number(self) -> None:
        report = summarise(
            DAY,
            [],
            audit=[audit_entry(Action.HALT_ENGAGED), audit_entry(Action.LOGIN)],
        )

        halts = next(s for s in report.sections if s.name == "halts")
        assert halts.value == 1
        assert not halts.is_absent

    def test_an_audit_table_that_was_not_read_is_absent(self) -> None:
        """`None` says "I could not look"; `[]` says "I looked". A caller that
        could not reach the table and one that reached it and found nothing are
        different days, and only the caller knows which happened."""
        report = summarise(DAY, [], audit=None)

        halts = next(s for s in report.sections if s.name == "halts")
        assert halts.is_absent
        assert halts.how_to_check

    def test_a_counted_zero_still_says_what_it_excludes(self) -> None:
        """The risk layer's own triggers write no audit row, and on day 1 the
        halt that mattered was exactly one of those. "No halts" and "no halts
        anybody typed" are different days."""
        report = summarise(DAY, [], audit=[])

        halts = next(s for s in report.sections if s.name == "halts")
        assert halts.value == 0
        assert "no audit row" in halts.detail


class TestEquity:
    def test_the_change_is_reported_when_both_ends_are_known(self) -> None:
        report = summarise(
            DAY, [order()], starting_equity=Decimal(100_000), ending_equity=Decimal(101_000)
        )

        assert report.pnl_change == Decimal(1_000)
        assert "equity +1000" in report.headline()

    def test_a_missing_snapshot_is_none_and_not_zero(self) -> None:
        """A report for a day before the platform stored snapshots has no equity
        to show, which is not an equity of zero."""
        report = summarise(DAY, [order()])

        assert report.pnl_change is None
        assert "equity" not in report.headline()


class TestRendering:
    def test_absent_sections_are_marked_and_never_omitted(self) -> None:
        """Dropping them would be the easy option and the wrong one: a reader
        who does not see feed incidents listed will assume there were none
        rather than that nothing counts them."""
        text = render(summarise(DAY, [order()], audit=[]))

        assert "NOT MEASURED" in text
        assert "feed incidents" in text

    def test_it_leads_with_the_day_and_the_headline(self) -> None:
        text = render(summarise(DAY, []))

        assert text.splitlines()[0] == "2026-03-20 — no orders submitted"

    def test_a_report_with_nothing_absent_says_nothing_about_absence(self) -> None:
        """Constructed directly rather than through `summarise`, which always
        includes the feed section — this pins the renderer's own behaviour."""
        report = DailyReport(
            day=DAY,
            sections=[Section("trades", 0, "none")],
            orders_submitted=0,
            orders_filled=0,
            orders_refused=0,
        )

        assert "NOT MEASURED" not in render(report)


def venue_rejected(reason: str) -> Order:
    built = order(status=OrderStatus.REJECTED)
    built.reject_reason = reason
    return built


class TestTheDayFourReport:
    """docs/paper-week/day-4-review.md, F4: four of the five numbers were wrong.

    `209 submitted` was every row; 102 reached the venue and were accepted and
    107 were rejected by it. `0 refused` hid 38 exits the venue refused. There
    was no P&L. And the window was the last 24 hours, not the trading day (that
    one is the scheduler's, and is tested there).
    """

    def _day(self) -> list[Order]:
        return (
            [order() for _ in range(3)]
            + [order(status=OrderStatus.CANCELLED) for _ in range(2)]
            + [venue_rejected("insufficient qty available for order") for _ in range(4)]
            + [venue_rejected("potential wash trade detected. use complex orders")]
            + [order(status=OrderStatus.REJECTED_RISK, rejected_by="kill_switch")]
        )

    def test_submitted_means_reached_the_venue_not_has_a_row(self) -> None:
        report = summarise(DAY, self._day())

        assert report.orders_submitted == 10, "11 rows; one never left the risk chain"
        assert report.orders_accepted == 5
        assert report.orders_rejected_by_venue == 5
        assert report.orders_refused == 1
        assert report.orders_filled == 3

    def test_a_venue_rejection_is_counted_and_named(self) -> None:
        report = summarise(DAY, self._day())

        venue = next(s for s in report.sections if s.name == "venue rejections")
        assert venue.value == 5
        assert venue.detail.startswith("x4 insufficient qty available")
        assert report.rejections_by_reason == {
            "insufficient qty available for order": 4,
            "potential wash trade detected. use complex orders": 1,
        }

    def test_the_headline_says_what_the_venue_did(self) -> None:
        headline = summarise(DAY, self._day()).headline()

        assert headline == (
            "10 submitted, 5 accepted, 3 filled, 5 rejected by the venue, 1 refused by risk"
        )

    def test_a_day_of_nothing_but_risk_refusals_is_not_silence(self) -> None:
        """Every order refused before submission is not "no orders submitted"
        in the sense that sentence is read: the strategy spoke."""
        headline = summarise(DAY, [order(status=OrderStatus.REJECTED_RISK)]).headline()

        assert "1 refused by risk" in headline

    def test_missing_equity_is_an_absent_section_not_a_silent_one(self) -> None:
        report = summarise(DAY, [order()], audit=[])

        assert "equity" in [s.name for s in report.absent]
        assert "NOT MEASURED" in render(report)

    def test_there_is_no_realised_pnl_field_to_misread(self) -> None:
        """It summed the day's fill cash flows, which is not a P&L."""
        assert not hasattr(summarise(DAY, [order()]), "realised_pnl")


class TestCoverage:
    """F11: minutes of RTH with an evaluating runner, against minutes of RTH."""

    def test_it_is_absent_when_nobody_could_count_it(self) -> None:
        section = next(s for s in summarise(DAY, []).sections if s.name == "RTH coverage")

        assert section.is_absent
        assert section.how_to_check

    def test_it_reports_minutes_against_the_session(self) -> None:
        report = summarise(
            DAY, [], coverage=Coverage(evaluated_minutes=355, session_minutes=390, open_at=T0)
        )

        section = next(s for s in report.sections if s.name == "RTH coverage")
        assert section.value == 355
        assert section.detail.startswith("355 of 390 regular-hours minutes")

    def test_minutes_before_this_process_are_named_not_counted_as_gaps(self) -> None:
        """Day 4's worker restarted at 14:04. The minutes before that belong to
        a process this one cannot see, which is not the same as uncovered."""
        report = summarise(
            DAY,
            [],
            coverage=Coverage(
                evaluated_minutes=355,
                session_minutes=390,
                open_at=T0,
                visible_from=T0 + timedelta(minutes=34),
            ),
        )

        section = next(s for s in report.sections if s.name == "RTH coverage")
        assert "first 34 minute(s) belong to a process it cannot see" in section.detail


class TestCountOutcomes:
    def test_an_order_not_yet_sent_is_neither_accepted_nor_rejected(self) -> None:
        pending = order(status=OrderStatus.PENDING_SUBMIT)

        outcomes = count_outcomes([pending])

        assert (outcomes.recorded, outcomes.sent, outcomes.accepted) == (1, 0, 0)
