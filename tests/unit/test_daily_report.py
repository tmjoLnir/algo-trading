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

from atp_core.analytics.daily import DailyReport, Section, render, summarise
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
        report = summarise(DAY, [order()], audit=[])

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
