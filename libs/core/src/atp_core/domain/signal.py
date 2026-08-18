"""Signals — a strategy's expressed intent.

A signal is not an order. Strategies emit intent; the position sizer turns it
into a quantity and the risk engine decides whether it may proceed. Keeping
these separate is what lets one risk policy govern every strategy uniformly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from atp_core.domain.enums import SignalAction

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class Signal:
    """One decision by one strategy about one symbol at one instant.

    `reason` and `indicators` are not decoration: they are what the dashboard
    shows a human when asked "why is this trade on?", and what makes a losing
    run diagnosable after the fact. Populate them.
    """

    strategy_id: str
    symbol: str
    action: SignalAction
    ts: datetime

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    strength: Decimal = Decimal(1)  # 0..1 — scales position size when sizing allows
    limit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None

    reason: str = ""  # human-readable: "SMA(20) crossed above SMA(50)"
    indicators: dict[str, Any] = field(default_factory=dict)  # values at decision time
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject a naive timestamp at the domain boundary (rule §1.2).

        Every other dated value object here already does — `Bar`, `Quote`,
        `Trade` and `OrderRequest` all refuse one — and a `Signal` is the last
        one that did not, which made it the one way a naive instant could get
        into the system. It travels a long way: into `OrderRequest.decided_at`,
        where it derives the `client_order_id` that rule §1.4 depends on, and
        into the dashboard's signal feed, where one unparseable timestamp makes
        the whole published snapshot undecodable. Both failures surface far from
        the strategy that emitted it; this one surfaces at the line that built
        it.
        """
        if self.ts.tzinfo is None:
            raise ValueError(f"Signal.ts must be tz-aware UTC (rule §1.2), got naive {self.ts!r}")

    @property
    def is_entry(self) -> bool:
        return self.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT)

    @property
    def is_exit(self) -> bool:
        return self.action in (SignalAction.EXIT, SignalAction.SCALE_OUT)
