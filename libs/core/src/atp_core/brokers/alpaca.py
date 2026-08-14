"""Alpaca adapter — live and paper.

Paper and live differ only in base URL and key pair. That is the whole
implementation of requirement #5 at this layer: identical code, different
endpoint, chosen by `Settings.broker_base_url`.

Alpaca specifics worth knowing before implementing:

- Paper and live use SEPARATE API key pairs. A live key against the paper
  endpoint fails auth — which is a useful accident, and why we do not try to
  share one key.
- `client_order_id` is Alpaca's idempotency mechanism too; max 128 chars.
  Reusing one returns the existing order rather than creating a second.
- Order updates arrive on the trade-updates WebSocket. Polling REST for fills
  is slow and rate-limited; stream them.
- Rate limit is 200 req/min on the free tier. Batch, and back off on 429.
- Fractional shares: market/DAY orders only. A fractional limit order is
  rejected — check `supports_fractional` before sizing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from atp_core.domain.enums import RunMode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from atp_core.brokers.ports import AccountSnapshot
    from atp_core.config import Settings
    from atp_core.domain import Order, Position


class AlpacaBroker:
    """`BrokerPort` over Alpaca's REST + WebSocket API."""

    def __init__(self, settings: Settings) -> None:
        if settings.run_mode is RunMode.BACKTEST:
            raise ValueError("AlpacaBroker cannot serve a backtest; use SimulatedBroker")
        self._settings = settings
        self._base_url = settings.broker_base_url
        self._is_live = settings.is_live

    @property
    def name(self) -> str:
        return "alpaca-live" if self._is_live else "alpaca-paper"

    @property
    def supports_fractional(self) -> bool:
        return True

    async def get_account(self) -> AccountSnapshot:
        raise NotImplementedError("GET /v2/account")

    async def submit_order(self, order: Order) -> Order:
        """POST /v2/orders.

        Send `client_order_id` on every request. On timeout, do NOT resubmit —
        GET /v2/orders?client_order_id=... first to find out whether it landed.
        """
        raise NotImplementedError

    async def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError("DELETE /v2/orders/{id}")

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
        raise NotImplementedError("GET /v2/clock")

    async def stream_trade_updates(self) -> AsyncIterator[dict[str, Any]]:
        """Fill and status events, pushed.

        Reconnect with backoff on drop, then reconcile open orders via REST —
        events during the gap are lost, and a missed fill means our position
        view is wrong (CLAUDE.md §5).
        """
        raise NotImplementedError
        yield {}  # pragma: no cover — makes the signature an async generator

    @staticmethod
    def _to_alpaca_order(order: Order) -> dict[str, Any]:
        """Domain order → Alpaca request body."""
        raise NotImplementedError

    @staticmethod
    def _from_alpaca_order(payload: dict[str, Any]) -> Order:
        """Alpaca response → domain order. The only place Alpaca's status
        vocabulary is translated to ours."""
        raise NotImplementedError
