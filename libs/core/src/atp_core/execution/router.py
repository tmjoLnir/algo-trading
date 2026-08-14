"""The order router — the single path from signal to market (rule §1.5).

Every order in this system goes through `submit()`. Not "most orders". Not
"orders from strategies". Every one, including protective stops, manual
dashboard orders and emergency exits. That is what makes the risk engine a
guarantee instead of a suggestion — one path to audit, one place a limit can be
enforced, one place a bug can hide.

    Signal → size → build Order → RiskEngine.validate → broker.submit_order
                                        │
                                        └── denied → record, emit, stop
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atp_core.brokers.ports import BrokerPort
    from atp_core.domain import Order, OrderRequest, Portfolio, Signal
    from atp_core.risk.engine import RiskDecision, RiskEngine
    from atp_core.risk.stops import StopManager


@dataclass(frozen=True, slots=True)
class SubmitResult:
    order: Order | None
    decision: RiskDecision
    submitted: bool


class OrderRouter:
    """Turns intent into executed orders, subject to risk."""

    def __init__(
        self,
        broker: BrokerPort,
        risk_engine: RiskEngine,
        stop_manager: StopManager,
    ) -> None:
        self.broker = broker
        self.risk_engine = risk_engine
        self.stop_manager = stop_manager

    async def submit_signal(
        self, signal: Signal, portfolio: Portfolio, sizing_config: object
    ) -> SubmitResult:
        """Size a signal, validate it, and send it.

        A risk denial is a normal outcome, not an exception: return a
        `SubmitResult` with `submitted=False` and a reason the dashboard can
        show. Raising here would make one blocked strategy look like a crash.
        """
        raise NotImplementedError

    async def submit(self, request: OrderRequest, portfolio: Portfolio) -> SubmitResult:
        """Submit a concrete request. THE submission path — do not add another."""
        raise NotImplementedError

    async def submit_protective_orders(self, entry_order: Order, portfolio: Portfolio) -> list[Order]:
        """Attach stop-loss and take-profit children after an entry fills.

        Submit these immediately on fill, before anything else. The window
        between "we own it" and "we have a stop on it" is unprotected exposure,
        and it is exactly when a fat-finger or a gap will find you. Prefer a
        broker-side bracket so the stop survives our process dying
        (`risk/stops.py`).
        """
        raise NotImplementedError

    async def cancel_all(self, symbol: str | None = None) -> int:
        """Cancel open orders; return how many. Symbol-scoped or everything."""
        raise NotImplementedError

    async def flatten(self, symbol: str, portfolio: Portfolio) -> SubmitResult:
        """Close a position at market.

        Exits bypass entry-blocking risk rules (e.g. the daily loss limit) but
        still pass through `validate()` — a rule that blocks an exit is a rule
        that traps you in a losing position (see `DailyLossLimitRule`).
        """
        raise NotImplementedError

    @staticmethod
    def _size(signal: Signal, portfolio: Portfolio, sizing_config: object) -> Decimal:
        raise NotImplementedError("delegates to risk.rules.position_size")
