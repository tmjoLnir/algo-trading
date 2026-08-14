"""Order state machine and fill accumulation."""

from __future__ import annotations

import pytest

from atp_core.domain.enums import OrderStatus
from atp_core.errors import InvalidStateTransition
from atp_core.execution.state import assert_transition, can_transition, is_stale_event


def test_legal_transition() -> None:
    assert can_transition(OrderStatus.SUBMITTED, OrderStatus.FILLED)


def test_terminal_states_go_nowhere() -> None:
    assert not can_transition(OrderStatus.FILLED, OrderStatus.CANCELLED)
    with pytest.raises(InvalidStateTransition):
        assert_transition(OrderStatus.FILLED, OrderStatus.SUBMITTED)


def test_partial_to_partial_is_legal() -> None:
    """An order fills in many pieces; each is a real event."""
    assert can_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED)


def test_stale_event_after_terminal_is_detected() -> None:
    """A replayed 'submitted' after a reconnect must not resurrect a filled
    order — discard, do not raise."""
    assert is_stale_event(OrderStatus.FILLED, OrderStatus.SUBMITTED)


class TestOrderFills:
    def test_partial_fill_updates_vwap(self) -> None:
        """50 @ $10 then 50 @ $12 → filled 100 @ $11."""
        pytest.skip("TODO")

    def test_overfill_rejected(self) -> None:
        pytest.skip("TODO")

    def test_vwap_between_min_and_max_fill(self) -> None:
        pytest.skip("TODO: hypothesis")
