"""Reconciliation — does our book match the broker's?

Run at startup, on a schedule, and after any reconnect. Our position and order
state is a cache; the broker is the truth. They drift for ordinary reasons: a
fill arrived while we were restarting, a WebSocket dropped, a stop we did not
place fired, a corporate action changed a share count overnight.

**A mismatch halts trading.** Not "logs a warning" — halts. Continuing to size
orders against a position we believe is 100 shares when it is actually 1,000 is
how a small bug becomes a large loss, and it compounds with every subsequent
order. Stopping is cheap; being wrong about the book is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from atp_core.brokers.ports import BrokerPort
    from atp_core.domain import Portfolio
    from atp_core.risk.killswitch import KillSwitch


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: str  # "position_qty" | "missing_position" | "unknown_position" | "orphan_order"
    symbol: str
    ours: Decimal | None
    theirs: Decimal | None
    detail: str = ""


@dataclass(slots=True)
class ReconciliationReport:
    checked_at: datetime
    discrepancies: list[Discrepancy] = field(default_factory=list)
    orphan_order_ids: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies and not self.orphan_order_ids


class Reconciler:
    def __init__(self, broker: BrokerPort, kill_switch: KillSwitch) -> None:
        self.broker = broker
        self.kill_switch = kill_switch

    async def reconcile(
        self, portfolio: Portfolio, *, halt_on_mismatch: bool = True
    ) -> ReconciliationReport:
        """Compare our state with the broker's.

        Checks:
        1. Every broker position exists locally with the same signed quantity.
        2. No local position the broker does not have.
        3. Every open broker order is one we know about ("orphan" otherwise —
           usually a stop we placed before a restart, and cancelling it blindly
           would leave the position naked; report, do not auto-cancel).
        4. Cash and equity within tolerance (fees and interest cause small,
           legitimate drift — do not halt on a cent).

        On any discrepancy with `halt_on_mismatch`, engage the kill switch and
        page a human. See docs/RUNBOOK.md.
        """
        raise NotImplementedError

    async def adopt_broker_state(self, portfolio: Portfolio) -> None:
        """Overwrite local state with the broker's.

        The recovery action after a human has reviewed a mismatch. Deliberately
        NOT automatic: silently adopting hides the bug that caused the drift,
        and if the cause is a duplicate-submission bug, adopting is how you end
        up doing it again tomorrow.
        """
        raise NotImplementedError
