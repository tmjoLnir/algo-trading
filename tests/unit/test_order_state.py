"""Order state machine and fill accumulation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from atp_core.domain.enums import OrderStatus, Side
from atp_core.domain.order import Fill, Order
from atp_core.errors import InvalidStateTransitionError
from atp_core.execution.state import (
    assert_transition,
    can_transition,
    is_stale_event,
    transition,
)

TS = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


def _order(qty: str) -> Order:
    return Order(symbol="SPY", side=Side.BUY, qty=Decimal(qty), status=OrderStatus.SUBMITTED)


def _fill(order: Order, qty: str, price: str, fee: str = "0") -> Fill:
    return Fill(order_id=order.id, ts=TS, qty=Decimal(qty), price=Decimal(price), fee=Decimal(fee))


def test_legal_transition() -> None:
    assert can_transition(OrderStatus.SUBMITTED, OrderStatus.FILLED)


def test_terminal_states_go_nowhere() -> None:
    assert not can_transition(OrderStatus.FILLED, OrderStatus.CANCELLED)
    with pytest.raises(InvalidStateTransitionError):
        assert_transition(OrderStatus.FILLED, OrderStatus.SUBMITTED)


def test_partial_to_partial_is_legal() -> None:
    """An order fills in many pieces; each is a real event."""
    assert can_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED)


def test_stale_event_after_terminal_is_detected() -> None:
    """A replayed 'submitted' after a reconnect must not resurrect a filled
    order — discard, do not raise."""
    assert is_stale_event(OrderStatus.FILLED, OrderStatus.SUBMITTED)


class TestTransition:
    """`transition` is the only way a status should be assigned.

    The table is a guarantee only where something consults it, and a plain
    `order.status = ...` consults nothing.
    """

    def test_a_legal_move_lands_and_reports_that_it_did(self) -> None:
        order = _order(qty="100")
        order.status = OrderStatus.PENDING_RISK

        assert transition(order, OrderStatus.PENDING_SUBMIT)
        assert order.status is OrderStatus.PENDING_SUBMIT

    def test_an_illegal_move_raises_rather_than_silently_landing(self) -> None:
        order = _order(qty="100")
        order.status = OrderStatus.PENDING_RISK

        with pytest.raises(InvalidStateTransitionError):
            transition(order, OrderStatus.SUBMITTED)
        assert order.status is OrderStatus.PENDING_RISK

    def test_a_replayed_event_on_a_terminal_order_is_discarded(self) -> None:
        """The bug this whole module exists for: a late "submitted" arriving
        after the fill would make a position appear to vanish. Discarded, not
        raised — a reconnect replaying history is ordinary."""
        order = _order(qty="100")
        order.apply_fill(_fill(order, "100", "10"))

        assert not transition(order, OrderStatus.SUBMITTED)
        assert order.status is OrderStatus.FILLED

    def test_repeating_the_current_status_is_a_no_op(self) -> None:
        """Brokers re-send. `PARTIALLY_FILLED → PARTIALLY_FILLED` is meaningful
        only when a *fill* comes with it, which is `apply_fill`'s job."""
        order = _order(qty="100")
        assert not transition(order, OrderStatus.SUBMITTED)
        assert order.status is OrderStatus.SUBMITTED

    def test_it_stamps_the_fields_that_belong_to_the_move(self) -> None:
        order = _order(qty="100")
        order.status = OrderStatus.PENDING_SUBMIT

        transition(order, OrderStatus.SUBMITTED, at=TS)
        assert order.submitted_at == TS

        transition(order, OrderStatus.REJECTED, reason="halted symbol", rejected_by="alpaca-paper")
        assert order.reject_reason == "halted symbol"
        assert order.rejected_by == "alpaca-paper"

    def test_a_refusal_records_who_as_well_as_why(self) -> None:
        """The two were computed together and only one was kept.

        `RiskDecision` carries `rule` beside `reason`, and the router passed the
        reason alone — so a stored refusal could say "no price available for
        SPY" without naming which of the three rules that check a price had
        said it. Taken together here so they cannot drift apart again.
        """
        order = _order(qty="100")
        order.status = OrderStatus.PENDING_RISK

        transition(
            order,
            OrderStatus.REJECTED_RISK,
            reason="SPY would take gross exposure to 112% of equity",
            rejected_by="max_gross_exposure",
        )

        assert order.rejected_by == "max_gross_exposure"
        assert order.reject_reason == "SPY would take gross exposure to 112% of equity"

    def test_it_records_a_refuser_on_no_other_status(self) -> None:
        """A cancel is not a refusal. Stamping one would put a rule name on an
        order nothing refused, which the screen reads as "this was refused"."""
        order = _order(qty="100")
        order.status = OrderStatus.SUBMITTED

        transition(order, OrderStatus.CANCELLED, reason="pulled", rejected_by="kill_switch")

        assert order.rejected_by is None
        assert order.reject_reason is None

    def test_a_redelivered_refusal_does_not_blank_the_refuser(self) -> None:
        """Brokers re-send, and a repeat of a status the order already holds is
        discarded. It must leave the refusal it already recorded standing —
        a redelivery that arrived carrying neither half would otherwise erase
        the record of who refused and why."""
        order = _order(qty="100")
        order.status = OrderStatus.PENDING_SUBMIT
        transition(order, OrderStatus.REJECTED, reason="halted symbol", rejected_by="alpaca-live")

        assert not transition(order, OrderStatus.REJECTED)

        assert order.rejected_by == "alpaca-live"
        assert order.reject_reason == "halted symbol"

    def test_it_never_stamps_filled_at(self) -> None:
        """A status carries no execution time. `filled_at` comes from the
        fill's own timestamp, inside `apply_fill`."""
        order = _order(qty="100")
        transition(order, OrderStatus.CANCELLED, at=TS)

        assert order.filled_at is None

    def test_an_acknowledgement_cannot_be_skipped(self) -> None:
        """`PENDING_SUBMIT` has no edge to `FILLED`, and it should not: an order
        acquiring a fill without ever having been acknowledged is an order the
        venue may never have had."""
        assert not can_transition(OrderStatus.PENDING_SUBMIT, OrderStatus.FILLED)


class TestOrderFills:
    def test_partial_fill_updates_vwap(self) -> None:
        """50 @ $10 then 50 @ $12 → filled 100 @ $11."""
        order = _order(qty="100")

        order.apply_fill(_fill(order, "50", "10"))
        assert order.status is OrderStatus.PARTIALLY_FILLED
        assert order.filled_qty == Decimal(50)
        assert order.avg_fill_price == Decimal(10)
        assert order.remaining_qty == Decimal(50)
        assert not order.is_complete

        order.apply_fill(_fill(order, "50", "12"))
        # mypy narrowed `status` at the assertion above and cannot see that
        # `apply_fill` moved it on; the mutation is the thing under test.
        assert order.status is OrderStatus.FILLED  # type: ignore[comparison-overlap]
        assert order.filled_qty == Decimal(100)
        assert order.avg_fill_price == Decimal(11)
        assert order.remaining_qty == Decimal(0)
        assert order.filled_at == TS
        assert order.is_complete

    def test_uneven_partials_weight_by_quantity(self) -> None:
        """VWAP is volume-weighted, not an average of prices: 90 @ $10 and
        10 @ $20 is $11, not $15."""
        order = _order(qty="100")

        order.apply_fill(_fill(order, "90", "10"))
        order.apply_fill(_fill(order, "10", "20"))

        assert order.avg_fill_price == Decimal(11)

    def test_overfill_rejected(self) -> None:
        order = _order(qty="100")
        order.apply_fill(_fill(order, "60", "10"))

        with pytest.raises(ValueError, match="overfill"):
            order.apply_fill(_fill(order, "50", "10"))

        # The rejected fill must leave no trace behind.
        assert order.filled_qty == Decimal(60)
        assert order.avg_fill_price == Decimal(10)
        assert len(order.fills) == 1
        assert order.status is OrderStatus.PARTIALLY_FILLED

    def test_non_positive_fill_qty_rejected(self) -> None:
        order = _order(qty="100")
        with pytest.raises(ValueError, match="fill qty must be positive"):
            order.apply_fill(_fill(order, "0", "10"))

    def test_total_fees_accumulate_across_fills(self) -> None:
        order = _order(qty="100")
        order.apply_fill(_fill(order, "50", "10", fee="0.75"))
        order.apply_fill(_fill(order, "50", "12", fee="0.30"))

        assert order.total_fees == Decimal("1.05")

    @settings(max_examples=200)
    @given(
        lots=st.lists(
            st.tuples(
                st.integers(min_value=1, max_value=100),  # qty
                st.integers(min_value=1, max_value=1000),  # price
            ),
            min_size=1,
            max_size=20,
        )
    )
    def test_vwap_between_min_and_max_fill(self, lots: list[tuple[int, int]]) -> None:
        """A weighted average cannot escape the range it averages over.

        If it does, the weighting is wrong — which is exactly the bug that
        misprices a position built out of many prints.
        """
        order = _order(qty=str(sum(qty for qty, _ in lots)))

        for qty, price in lots:
            order.apply_fill(_fill(order, str(qty), str(price)))

        prices = [Decimal(price) for _, price in lots]
        assert order.avg_fill_price is not None
        assert min(prices) <= order.avg_fill_price <= max(prices)
