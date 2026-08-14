"""In-process fill simulator.

Two uses:

1. **Backtests** — the only broker available there.
2. **Paper trading with our own simulator**, as an alternative to Alpaca's paper
   endpoint. Useful when you want fills modelled by *our* cost assumptions
   rather than the venue's, so paper results are directly comparable to the
   backtest that preceded them.

Be honest here. A simulator that fills every market order instantly at the last
trade price will make any strategy look good and teach you nothing. The realism
of this file bounds the trustworthiness of every backtest the platform produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atp_core.backtest.costs import CostModel
    from atp_core.brokers.ports import AccountSnapshot
    from atp_core.clock import Clock
    from atp_core.domain import Bar, Order, Position, Quote


@dataclass(slots=True)
class SimulatedBroker:
    """`BrokerPort` backed by a local matching engine."""

    clock: Clock
    cost_model: CostModel
    starting_cash: Decimal = Decimal("100000")

    _cash: Decimal = field(init=False, default=Decimal("100000"))
    _positions: dict[str, Position] = field(init=False, default_factory=dict)
    _open_orders: dict[str, Order] = field(init=False, default_factory=dict)
    _filled: list[Order] = field(init=False, default_factory=list)

    #: Simulated round-trip latency. Zero-latency fills are unrealistic in a
    #: way that flatters fast strategies specifically.
    latency_ms: int = 50

    @property
    def name(self) -> str:
        return "simulated"

    @property
    def supports_fractional(self) -> bool:
        return True

    async def get_account(self) -> AccountSnapshot:
        raise NotImplementedError

    async def submit_order(self, order: Order) -> Order:
        """Accept and rest the order. Fills happen in `on_bar` / `on_quote`."""
        raise NotImplementedError

    async def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError

    async def get_order(self, broker_order_id: str) -> Order | None:
        raise NotImplementedError

    async def get_open_orders(self) -> list[Order]:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        raise NotImplementedError

    async def close_position(self, symbol: str) -> Order:
        raise NotImplementedError

    async def close_all_positions(self) -> list[Order]:
        raise NotImplementedError

    async def is_market_open(self) -> bool:
        raise NotImplementedError

    # ── the matching engine ─────────────────────────────────────────────────

    def on_bar(self, bar: Bar) -> list[Order]:
        """Advance simulation by one bar; return orders that filled.

        Rules that keep this honest:
        - Market orders fill at the NEXT bar's open, not this close.
        - A limit order fills only if the bar's range actually reached it, and
          conservatively: a limit touched exactly at the extreme may not have
          filled in reality (you were last in the queue). Optionally require the
          bar to trade *through* the price.
        - Stops fill at trigger + slippage, not at the trigger.
        - Cap fill quantity at `max_volume_participation` of bar volume.
        - When a bar's range spans both the stop and the target, assume the
          stop filled — see `risk.stops.should_trigger`.
        """
        raise NotImplementedError

    def on_quote(self, quote: Quote) -> list[Order]:
        """Quote-driven fills, for tick-level simulation."""
        raise NotImplementedError
