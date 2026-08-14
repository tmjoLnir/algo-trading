"""Dashboard endpoints — requirement #7.

The dashboard polls `GET /api/v1/dashboard/live` every 5 minutes. That endpoint
returns everything the main view needs in ONE response: account, open positions,
recent signals, working orders, and any active halt.

One aggregate endpoint rather than six parallel requests, for a reason worth
stating: six independent fetches produce a screen assembled from six different
instants. On a fast-moving position, a P&L figure computed from one snapshot and
a price from another simply disagree, and the human reading it cannot tell which
number to trust. One query, one `as_of` timestamp, one consistent picture.

Live prices still arrive over the WebSocket between polls — the 5-minute refresh
is the floor, not the ceiling.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class PositionView(BaseModel):
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    last_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_pct: Decimal
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    #: How far price sits from the stop, as a fraction of the entry-to-stop
    #: distance. The single most useful number on the screen: it says how close
    #: this position is to being closed, without arithmetic in the reader's head.
    distance_to_stop_pct: Decimal | None = None
    strategy_id: str | None = None
    strategy_name: str | None = None
    opened_at: datetime


class SignalView(BaseModel):
    """A recent decision, with its reasoning — requirement #7's "trades selected
    based on preset rules"."""

    id: str
    ts: datetime
    strategy_name: str
    symbol: str
    action: str
    reason: str
    indicators: dict[str, float] = Field(default_factory=dict)
    acted_on: bool
    rejection_reason: str | None = None


class OrderView(BaseModel):
    id: str
    ts: datetime
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    filled_qty: Decimal
    limit_price: Decimal | None
    avg_fill_price: Decimal | None
    status: str
    strategy_name: str | None


class AccountView(BaseModel):
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    leverage: Decimal
    day_pnl: Decimal
    day_pnl_pct: Decimal
    open_position_count: int


class HaltView(BaseModel):
    scope: str
    reason: str
    engaged_at: datetime
    engaged_by: str
    detail: str
    target: str | None


class LiveDashboard(BaseModel):
    """One consistent snapshot."""

    as_of: datetime
    run_mode: str  # drives the "PAPER"/"LIVE" banner — never let this be ambiguous
    market_open: bool
    account: AccountView
    positions: list[PositionView]
    recent_signals: list[SignalView]
    working_orders: list[OrderView]
    active_halts: list[HaltView]
    #: Echoed so the client's poll interval follows the server's config rather
    #: than a hardcoded constant that drifts out of sync.
    refresh_seconds: int
    data_feed_healthy: bool
    last_data_at: datetime | None


@router.get("/live", response_model=LiveDashboard)
async def get_live_dashboard(signal_limit: int = 50) -> LiveDashboard:
    """Everything the dashboard needs, from one point in time.

    Must be fast — it is polled by every open browser tab. Serve positions and
    account from the Redis-cached snapshot the worker maintains rather than
    recomputing from fills, and cap `recent_signals`.
    """
    raise NotImplementedError


@router.get("/equity-curve")
async def get_equity_curve(days: int = 30, resolution: str = "1h") -> dict[str, object]:
    """Equity over time, for the dashboard's headline chart."""
    raise NotImplementedError


@router.get("/health")
async def get_system_health() -> dict[str, object]:
    """Operational status: feed connected, broker reachable, worker heartbeat,
    DB lag, last reconciliation result."""
    raise NotImplementedError
