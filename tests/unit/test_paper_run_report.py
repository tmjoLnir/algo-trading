"""What a paper run demonstrated — and, more importantly, what it did not.

`docs/FIRST_PAPER_RUN.md` ends by asking whoever ran the week to say which
clauses of Phase 4's *Verifiable:* line held and how they know, and to "paste
the numbers rather than the conclusion". Nothing produced those numbers, so the
tick that follows a week is a recollection of a log tail.

Four properties, and the third is the one this module is really for:

1. **A refused week is attributed, not reported as an absence.** A run whose
   every entry was refused looks identical, from the book and the equity curve,
   to a run that never signalled — `recent_orders` is the only read in this
   platform where a refusal appears at all.
2. **Sessions are counted from evaluations, not from orders.** A week with one
   trade was still five sessions of ingestion, warmup and reconciliation.
3. **A clause with no evidence is `None`, never False and never quietly True.**
   Two of the four report to a log line and nowhere else. A report that rendered
   "no unprotected positions found" from a store that never held them would be
   believed, which is what makes it worse than no report.
4. **Vacuous is not shown.** Nothing filled means no position was ever held, so
   layer 5 was never asked to hold — that is not a pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from atp_core.analytics.paper_run import (
    RECONCILE_MARKERS,
    UNPROTECTED_MARKER,
    assess,
)
from atp_core.domain import Fill, Order, OrderStatus, OrderType, Side
from atp_core.execution.ports import EquityPoint

MONDAY = datetime(2026, 8, 17, 14, 30, tzinfo=UTC)
STRATEGY = "sma_crossover"


def an_order(
    *,
    day: int = 0,
    status: OrderStatus = OrderStatus.FILLED,
    symbol: str = "SPY",
    rejected_by: str | None = None,
    fills: int = 1,
    strategy_id: str = STRATEGY,
) -> Order:
    ts = MONDAY + timedelta(days=day)
    order = Order(
        symbol=symbol,
        side=Side.BUY,
        qty=Decimal(10),
        order_type=OrderType.MARKET,
        strategy_id=strategy_id,
        created_at=ts,
        submitted_at=ts,
        status=status,
        rejected_by=rejected_by,
    )
    if status is OrderStatus.FILLED:
        order.fills = [
            Fill(order_id=order.id, ts=ts, qty=Decimal(10) / fills, price=Decimal(100))
            for _ in range(fills)
        ]
    return order


def equity_over(days: int) -> list[EquityPoint]:
    return [
        EquityPoint(
            ts=MONDAY + timedelta(days=d),
            equity=Decimal(100_000) + Decimal(d * 10),
            cash=Decimal(50_000),
            gross_exposure=Decimal(50_000),
        )
        for d in range(days)
    ]


class TestTheFirstClause:
    def test_a_week_that_filled_says_how_much(self) -> None:
        report = assess(
            [an_order(day=0, fills=2), an_order(day=1, symbol="QQQ")],
            equity_over(5),
            strategy_id=STRATEGY,
        )
        traded = report.clauses[0]
        assert traded.held is True
        assert "2 orders filled across 3 fills" in traded.evidence
        assert "QQQ, SPY" in traded.evidence

    def test_a_week_of_refusals_names_the_rule_rather_than_reporting_nothing(self) -> None:
        """The failure this whole module exists for. From the book and the
        equity curve this is indistinguishable from a strategy that never
        signalled; `recent_orders` is the only read that keeps the refusals."""
        orders = [
            an_order(day=d, status=OrderStatus.REJECTED_RISK, rejected_by="max_position_size")
            for d in range(3)
        ] + [an_order(day=3, status=OrderStatus.REJECTED_RISK, rejected_by="daily_loss_limit")]
        report = assess(orders, equity_over(5), strategy_id=STRATEGY)

        traded = report.clauses[0]
        assert traded.held is False
        assert "mostly by max_position_size (3)" in traded.evidence
        assert report.refusals_by_rule == {"daily_loss_limit": 1, "max_position_size": 3}

    def test_no_orders_at_all_is_told_apart_from_refusals(self) -> None:
        report = assess([], equity_over(5), strategy_id=STRATEGY)
        traded = report.clauses[0]
        assert traded.held is False
        assert "no orders at all" in traded.evidence
        assert "preflight.py" in traded.how_to_check

    def test_another_strategys_orders_are_not_counted_as_this_ones(self) -> None:
        report = assess(
            [an_order(day=0), an_order(day=1, strategy_id="something_else")],
            equity_over(5),
            strategy_id=STRATEGY,
        )
        assert report.orders_submitted == 1

    def test_an_unattributed_refusal_is_still_counted(self) -> None:
        """`rejected_by` is null on rows written before it was a column. They
        are refusals and dropping them would understate the count an operator
        reads to judge whether risk is set too tight."""
        report = assess(
            [an_order(status=OrderStatus.REJECTED_RISK)], equity_over(5), strategy_id=STRATEGY
        )
        assert report.refusals_by_rule == {"unattributed": 1}


class TestTheSecondClause:
    def test_sessions_come_from_evaluations_not_from_orders(self) -> None:
        """One trade on Tuesday was still five sessions of ingestion, warmup and
        reconciliation. Counting orders would report the run as one session and
        understate exactly the part that worked."""
        report = assess([an_order(day=1)], equity_over(5), strategy_id=STRATEGY)
        assert report.sessions == 5
        assert report.clauses[1].held is True

    def test_a_week_is_five_sessions_not_seven_days(self) -> None:
        report = assess([an_order(day=0)], equity_over(4), strategy_id=STRATEGY)
        week = report.clauses[1]
        assert week.held is False
        assert "5 sessions, not 7 days" in week.how_to_check

    def test_orders_carry_the_count_when_the_equity_history_is_empty(self) -> None:
        """The shape of a run against a database that lost its snapshots — a
        weaker answer than the equity history, and better than none."""
        report = assess([an_order(day=d) for d in range(5)], [], strategy_id=STRATEGY)
        assert report.sessions == 5

    def test_nothing_at_all_is_not_a_week(self) -> None:
        report = assess([], [], strategy_id=STRATEGY)
        assert report.clauses[1].held is False


class TestTheClausesNoStoreHolds:
    def test_reconciliation_is_unanswered_rather_than_assumed(self) -> None:
        report = assess([an_order()], equity_over(5), strategy_id=STRATEGY)
        clause = report.clauses[2]
        assert clause.held is None
        assert "no durable record" in clause.evidence
        # And it hands over the exact grep rather than the observation that one
        # would be needed.
        for marker in RECONCILE_MARKERS:
            assert marker in clause.how_to_check

    def test_unprotected_positions_are_unanswered_rather_than_assumed(self) -> None:
        report = assess([an_order()], equity_over(5), strategy_id=STRATEGY)
        clause = report.clauses[3]
        assert clause.held is None
        assert UNPROTECTED_MARKER in clause.how_to_check

    def test_log_counts_answer_both(self) -> None:
        report = assess(
            [an_order()],
            equity_over(5),
            strategy_id=STRATEGY,
            reconcile_lines=340,
            mismatch_lines=0,
            unprotected_lines=0,
        )
        assert report.clauses[2].held is True
        assert "340 clean pass(es)" in report.clauses[2].evidence
        assert report.clauses[3].held is True

    def test_a_mismatch_fails_the_clause_and_points_at_the_runbook(self) -> None:
        report = assess(
            [an_order()],
            equity_over(5),
            strategy_id=STRATEGY,
            reconcile_lines=300,
            mismatch_lines=1,
        )
        clause = report.clauses[2]
        assert clause.held is False
        assert "RUNBOOK" in clause.how_to_check

    def test_a_reconciler_that_never_ran_is_a_failure_not_a_pass(self) -> None:
        """Zero clean and zero mismatch read from a real log means the
        reconciler did not run — which is SAFETY.md layer 7 absent, not layer 7
        holding."""
        report = assess(
            [an_order()], equity_over(5), strategy_id=STRATEGY, reconcile_lines=0, mismatch_lines=0
        )
        assert report.clauses[2].held is False

    def test_an_unprotected_position_fails_layer_5(self) -> None:
        report = assess([an_order()], equity_over(5), strategy_id=STRATEGY, unprotected_lines=2)
        clause = report.clauses[3]
        assert clause.held is False
        assert "layer 5" in clause.evidence

    def test_nothing_filled_makes_the_stop_clause_vacuous_not_true(self) -> None:
        """No position was ever held, so layer 5 was never asked to hold. A
        green tick here would be the emptiest possible kind."""
        report = assess(
            [an_order(status=OrderStatus.REJECTED_RISK, rejected_by="buying_power")],
            equity_over(5),
            strategy_id=STRATEGY,
            unprotected_lines=0,
        )
        clause = report.clauses[3]
        assert clause.held is None
        assert "never exercised" in clause.evidence


class TestTheVerdict:
    def test_an_unanswered_clause_exits_non_zero_just_like_a_failed_one(self) -> None:
        """ "We could not tell" must not share a shell exit with "it held", or a
        job wired to this goes green on a week that showed half of what it
        claims."""
        report = assess([an_order()], equity_over(5), strategy_id=STRATEGY)
        assert report.failed == []
        assert len(report.unanswerable) == 2
        assert report.exit_code() == 1

    def test_all_four_shown_is_the_only_zero(self) -> None:
        report = assess(
            [an_order(day=d) for d in range(5)],
            equity_over(5),
            strategy_id=STRATEGY,
            reconcile_lines=100,
            mismatch_lines=0,
            unprotected_lines=0,
        )
        assert [c.held for c in report.clauses] == [True, True, True, True]
        assert report.exit_code() == 0

    def test_the_numbers_a_roadmap_entry_would_quote(self) -> None:
        report = assess(
            [an_order(day=0, fills=2), an_order(day=1, status=OrderStatus.REJECTED_RISK)],
            equity_over(5),
            strategy_id=STRATEGY,
        )
        assert report.orders_submitted == 2
        assert report.orders_filled == 1
        assert report.orders_refused == 1
        assert report.fills == 2
        assert report.first_at == MONDAY
        assert report.symbols == ("SPY",)
        # Money as `Decimal` all the way out, because these are the figures a
        # roadmap entry quotes verbatim (CLAUDE.md §1.1).
        assert report.starting_equity == Decimal(100_000)
        assert isinstance(report.ending_equity, Decimal)
