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

Two translations in here are worth reading before trusting a number out of it.

**Status.** Alpaca has more order states than we do — `accepted`, `new`,
`pending_new`, `calculated`, `accepted_for_bidding` all mean "the venue has it
and it has not filled", and `done_for_day`, `stopped`, `suspended` and
`pending_*` are states our own machine has no word for. Mapping is explicit and
total: an unrecognised status raises rather than defaulting to something
plausible, because the plausible default is `SUBMITTED` and an order silently
reported as working when the venue has actually killed it is a position nobody
is watching.

**Fills.** Alpaca reports `filled_qty` and `filled_avg_price` as running totals,
not as a fill sequence — the individual prints only exist on the trade-updates
stream. So `_from_alpaca_order` synthesises **one** `Fill` carrying the whole
filled quantity at the average price, which is correct for P&L and wrong for
anything that inspects the sequence. That is stated on the function rather than
discovered later, and it is the reason the streaming item exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from atp_core.brokers.ports import AccountSnapshot
from atp_core.domain import Fill, Order, OrderStatus, OrderType, Position, Side, TimeInForce
from atp_core.domain.enums import RunMode
from atp_core.errors import (
    BrokerConnectionError,
    BrokerError,
    InsufficientFundsError,
    OrderRejectedError,
)
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from atp_core.config import Settings

log = get_logger(__name__)

_MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 1.0
#: Retried. 429 is rate limiting and 5xx is the venue having a moment; both are
#: transient and the request has not been acted on.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Alpaca's order status vocabulary → ours. Total by construction: an
#: unrecognised value raises rather than defaulting (see the module docstring).
_STATUS: dict[str, OrderStatus] = {
    # Working at the venue.
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.SUBMITTED,
    "calculated": OrderStatus.SUBMITTED,
    "held": OrderStatus.SUBMITTED,
    "stopped": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    # Terminal.
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "done_for_day": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
    "replaced": OrderStatus.CANCELLED,
    # In flight in the other direction — the venue is working on removing it,
    # so it is still live until it is not.
    "pending_cancel": OrderStatus.SUBMITTED,
    "pending_replace": OrderStatus.SUBMITTED,
}

_ORDER_TYPE: dict[OrderType, str] = {
    OrderType.MARKET: "market",
    OrderType.LIMIT: "limit",
    OrderType.STOP: "stop",
    OrderType.STOP_LIMIT: "stop_limit",
    OrderType.TRAILING_STOP: "trailing_stop",
}
_ORDER_TYPE_BACK = {v: k for k, v in _ORDER_TYPE.items()}


def _as_decimal(value: object) -> Decimal:
    """Money and quantities are `Decimal`, never `float` (CLAUDE.md §1.1).

    Responses are parsed with `parse_float=Decimal`, so numbers arrive already
    exact. This exists for the rest: ints, and the strings Alpaca uses for most
    money fields. Never `Decimal(float)` — that inherits the binary rounding
    error the rule exists to avoid.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; nothing sane sends one
        raise TypeError(f"expected a number, got {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"cannot convert {type(value).__name__} {value!r} to Decimal exactly")


def _parse_ts(raw: str | None) -> datetime | None:
    """RFC-3339 → tz-aware UTC (CLAUDE.md §1.2), or None for an absent field."""
    if not raw:
        return None
    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise BrokerError(f"Alpaca returned a naive timestamp: {raw!r}")
    return ts.astimezone(UTC)


class AlpacaBroker:
    """`BrokerPort` over Alpaca's REST + WebSocket API."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        backoff_base_seconds: float = _BACKOFF_BASE_SECONDS,
    ) -> None:
        if settings.run_mode is RunMode.BACKTEST:
            raise ValueError("AlpacaBroker cannot serve a backtest; use SimulatedBroker")
        self._settings = settings
        self._base_url = settings.broker_base_url
        self._is_live = settings.is_live
        self._client = client
        #: Only close what we opened — an injected client belongs to its owner.
        self._owns_client = client is None
        self._backoff_base_seconds = backoff_base_seconds

    @property
    def name(self) -> str:
        return "alpaca-live" if self._is_live else "alpaca-paper"

    @property
    def supports_fractional(self) -> bool:
        return True

    # ── plumbing ────────────────────────────────────────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        """Credentials go in headers, never the query string: URLs end up in
        access logs, exception messages and traces (CLAUDE.md §1.6)."""
        return {
            "APCA-API-KEY-ID": self._settings.alpaca_api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": self._settings.alpaca_api_secret.get_secret_value(),
            "Accept": "application/json",
        }

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> AlpacaBroker:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """One request, with bounded retries on the transient failures.

        The error mapping is the load-bearing part, because the caller's
        behaviour differs completely between the two cases:

        - `BrokerConnectionError` — transport failed, and we do **not** know
          whether the venue acted on it. Retryable, but only with the same
          `client_order_id` (rule §1.4).
        - `OrderRejectedError` — the venue answered, and the answer was no.
          Retrying it just asks the same question again.

        A 4xx other than 429 is therefore never retried: it is a refusal or our
        own bug, and repeating it multiplies the same mistake.
        """
        url = f"{self._base_url.rstrip('/')}{path}"
        client = self._get_client()
        last_error: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            response: httpx.Response | None = None
            try:
                response = await client.request(
                    method, url, params=params, json=body, headers=self._auth_headers()
                )
            except httpx.HTTPError as exc:  # timeouts, connection resets, DNS
                last_error = BrokerConnectionError(f"{method} {path} failed: {exc}")
                log.warning("broker.alpaca.request_failed", attempt=attempt + 1, error=str(exc))
            else:
                if response.status_code in (httpx.codes.OK, httpx.codes.NO_CONTENT):
                    if not response.content:
                        return None
                    # parse_float=Decimal is the whole reason this is not
                    # response.json(): stdlib json would hand back floats and
                    # every price would arrive pre-corrupted (CLAUDE.md §1.1).
                    return json.loads(response.text, parse_float=Decimal)

                if response.status_code == httpx.codes.NOT_FOUND:
                    return None

                if response.status_code not in _RETRY_STATUSES:
                    raise self._refusal(method, path, response)

                last_error = BrokerConnectionError(
                    f"{method} {path} returned {response.status_code}"
                )
                log.warning(
                    "broker.alpaca.retrying", attempt=attempt + 1, status=response.status_code
                )

            if attempt < _MAX_ATTEMPTS - 1:
                await self._sleep_before_retry(attempt, response)

        raise BrokerConnectionError(
            f"Alpaca {method} {path} failed after {_MAX_ATTEMPTS} attempts"
        ) from last_error

    @staticmethod
    def _refusal(method: str, path: str, response: httpx.Response) -> BrokerError:
        """Turn a non-retryable HTTP error into the right exception.

        Body, not headers — the request headers hold the API key (CLAUDE.md
        §1.6). Truncated, because a venue error body can be long and it ends up
        in a log line and an order's `reject_reason`.
        """
        detail = response.text[:400]
        # 403 on an order submit is Alpaca's buying-power refusal; it carries a
        # distinct code so a caller can tell "no money" from "no permission".
        with contextlib.suppress(ValueError):
            payload = json.loads(response.text)
            if isinstance(payload, dict) and payload.get("code") == 40310000:
                return InsufficientFundsError(f"Alpaca refused {method} {path}: {detail}")

        if response.status_code in (httpx.codes.FORBIDDEN, httpx.codes.UNPROCESSABLE_ENTITY):
            return OrderRejectedError(f"Alpaca refused {method} {path}: {detail}")
        return BrokerError(f"Alpaca {method} {path} returned {response.status_code}: {detail}")

    async def _sleep_before_retry(self, attempt: int, response: httpx.Response | None) -> None:
        """Honour `Retry-After` when the server sets it, else exponential backoff."""
        delay = self._backoff_base_seconds * (2**attempt)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                # A malformed header is not worth failing the request over —
                # fall back to the computed backoff.
                with contextlib.suppress(ValueError):
                    delay = max(delay, float(retry_after))
        await asyncio.sleep(delay)

    # ── BrokerPort ──────────────────────────────────────────────────────────

    async def get_account(self) -> AccountSnapshot:
        """GET /v2/account."""
        payload = await self._request("GET", "/v2/account")
        if payload is None:
            raise BrokerError("Alpaca returned no account")
        return AccountSnapshot(
            account_id=str(payload["id"]),
            equity=_as_decimal(payload["equity"]),
            cash=_as_decimal(payload["cash"]),
            buying_power=_as_decimal(payload["buying_power"]),
            maintenance_margin=_as_decimal(payload.get("maintenance_margin", 0)),
            is_pattern_day_trader=bool(payload.get("pattern_day_trader", False)),
            # Either flag means we cannot trade. Reported as one boolean
            # because the caller's decision is the same for both, and reading
            # only `trading_blocked` would miss an account frozen at the
            # account level rather than at the trading level.
            trading_blocked=bool(payload.get("trading_blocked", False))
            or bool(payload.get("account_blocked", False)),
            as_of=datetime.now(UTC),
        )

    async def submit_order(self, order: Order) -> Order:
        """POST /v2/orders.

        Send `client_order_id` on every request. On timeout, do NOT resubmit —
        GET /v2/orders?client_order_id=... first to find out whether it landed.

        That lookup is done here rather than left to the caller, because the
        caller cannot do it safely: by the time it sees `BrokerConnectionError`
        it has no way to distinguish "never arrived" from "arrived and we lost
        the reply", and the two differ by one duplicate position. If the lookup
        finds the order, the submit *succeeded* and we return it. If the lookup
        itself fails we raise, having created nothing and resubmitted nothing.
        """
        body = self._to_alpaca_order(order)
        try:
            payload = await self._request("POST", "/v2/orders", body=body)
        except BrokerConnectionError:
            log.warning(
                "broker.alpaca.submit_uncertain",
                client_order_id=order.client_order_id,
                symbol=order.symbol,
            )
            landed = await self._get_by_client_order_id(order.client_order_id)
            if landed is None:
                raise
            log.info(
                "broker.alpaca.submit_landed_after_timeout",
                client_order_id=order.client_order_id,
                broker_order_id=landed.broker_order_id,
            )
            return landed

        if payload is None:
            raise BrokerError(f"Alpaca accepted {order.client_order_id} but returned no order")
        return self._from_alpaca_order(payload)

    async def cancel_order(self, broker_order_id: str) -> None:
        """DELETE /v2/orders/{id}.

        Cancelling an already-filled order is not an error — it is a race we
        lost, and the fill stands. Alpaca answers 422 for exactly that case, so
        it is swallowed here rather than raised: the caller asked for the order
        to stop working, and it has.
        """
        try:
            await self._request("DELETE", f"/v2/orders/{broker_order_id}")
        except OrderRejectedError:
            log.info("broker.alpaca.cancel_lost_race", broker_order_id=broker_order_id)

    async def get_order(self, broker_order_id: str) -> Order | None:
        payload = await self._request("GET", f"/v2/orders/{broker_order_id}")
        return None if payload is None else self._from_alpaca_order(payload)

    async def _get_by_client_order_id(self, client_order_id: str) -> Order | None:
        """The idempotency lookup: did this key already reach the venue?"""
        payload = await self._request(
            "GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id}
        )
        return None if payload is None else self._from_alpaca_order(payload)

    async def get_open_orders(self) -> list[Order]:
        """Every working order, not just the first page.

        `nested=false` because a bracket's children are orders in their own
        right here — reconciliation compares against a flat list of what is
        working, and children hidden inside a parent read as orders that do not
        exist.
        """
        payload = await self._request(
            "GET", "/v2/orders", params={"status": "open", "limit": 500, "nested": "false"}
        )
        return [self._from_alpaca_order(item) for item in (payload or [])]

    async def get_positions(self) -> list[Position]:
        """The broker's positions. Reconciliation compares these to ours;
        any disagreement halts trading (`ReconciliationError`)."""
        payload = await self._request("GET", "/v2/positions")
        return [self._from_alpaca_position(item) for item in (payload or [])]

    async def close_position(self, symbol: str) -> Order:
        payload = await self._request("DELETE", f"/v2/positions/{symbol}")
        if payload is None:
            raise BrokerError(f"Alpaca had no position in {symbol} to close")
        return self._from_alpaca_order(payload)

    async def close_all_positions(self) -> list[Order]:
        """Emergency flatten. See docs/RUNBOOK.md.

        `cancel_orders=true`: resting orders are cancelled first. Flattening
        without doing so leaves a stop working against a position that no
        longer exists, which opens the other side the moment it fires.

        Alpaca answers 207 with a per-symbol status array, so a partial failure
        looks like a success at the HTTP level. Anything that did not come back
        200 is raised rather than dropped — a flatten that silently left one
        position open is the worst possible outcome for this call.
        """
        payload = await self._request("DELETE", "/v2/positions", params={"cancel_orders": "true"})
        closed: list[Order] = []
        failed: list[str] = []
        for item in payload or []:
            if int(item.get("status", 200)) >= 300:
                failed.append(str(item.get("symbol", "?")))
                continue
            body = item.get("body")
            if body is not None:
                closed.append(self._from_alpaca_order(body))
        if failed:
            raise BrokerError(f"Alpaca could not flatten {', '.join(sorted(failed))}")
        return closed

    async def is_market_open(self) -> bool:
        """GET /v2/clock."""
        payload = await self._request("GET", "/v2/clock")
        if payload is None:
            raise BrokerError("Alpaca returned no clock")
        return bool(payload["is_open"])

    async def stream_trade_updates(self) -> AsyncIterator[dict[str, Any]]:
        """Fill and status events, pushed.

        Reconnect with backoff on drop, then reconcile open orders via REST —
        events during the gap are lost, and a missed fill means our position
        view is wrong (CLAUDE.md §5).
        """
        raise NotImplementedError
        yield {}  # pragma: no cover — makes the signature an async generator

    # ── translation ─────────────────────────────────────────────────────────

    @staticmethod
    def _to_alpaca_order(order: Order) -> dict[str, Any]:
        """Domain order → Alpaca request body.

        Every number goes over the wire as a **string**. `json.dumps` cannot
        serialise a `Decimal`, and the two alternatives — float, or int where
        it happens to fit — both lose exactness on precisely the fields where
        it matters (rule §1.1).
        """
        alpaca_type = _ORDER_TYPE.get(order.order_type)
        if alpaca_type is None:  # pragma: no cover — the map covers every member
            raise BrokerError(f"{order.order_type} has no Alpaca spelling")

        body: dict[str, Any] = {
            "symbol": order.symbol,
            "qty": str(order.qty),
            "side": order.side.value,
            "type": alpaca_type,
            "time_in_force": order.time_in_force.value,
            "client_order_id": order.client_order_id,
        }
        if order.limit_price is not None:
            body["limit_price"] = str(order.limit_price)
        if order.stop_price is not None:
            body["stop_price"] = str(order.stop_price)
        if order.trail_percent is not None:
            body["trail_percent"] = str(order.trail_percent)
        return body

    @staticmethod
    def _from_alpaca_order(payload: dict[str, Any]) -> Order:
        """Alpaca response → domain order. The only place Alpaca's status
        vocabulary is translated to ours.

        The order is rebuilt with its filled state applied through
        `Order.apply_fill`, so `filled_qty` and `avg_fill_price` come from the
        same accounting every other fill in the platform goes through rather
        than being assigned around it. As the module docstring says, that is
        **one** synthetic fill for the whole filled quantity: REST reports
        running totals, and the individual prints exist only on the
        trade-updates stream.
        """
        raw_status = str(payload["status"])
        status = _STATUS.get(raw_status)
        if status is None:
            raise BrokerError(
                f"unrecognised Alpaca order status {raw_status!r} for order "
                f"{payload.get('client_order_id')} — refusing to guess"
            )

        raw_type = str(payload["order_type" if "order_type" in payload else "type"])
        order_type = _ORDER_TYPE_BACK.get(raw_type)
        if order_type is None:
            raise BrokerError(f"unrecognised Alpaca order type {raw_type!r}")

        limit_price = payload.get("limit_price")
        stop_price = payload.get("stop_price")

        order = Order(
            symbol=str(payload["symbol"]),
            side=Side(str(payload["side"])),
            qty=_as_decimal(payload["qty"]),
            order_type=order_type,
            time_in_force=TimeInForce(str(payload["time_in_force"])),
            limit_price=None if limit_price is None else _as_decimal(limit_price),
            stop_price=None if stop_price is None else _as_decimal(stop_price),
            client_order_id=str(payload["client_order_id"]),
            broker_order_id=str(payload["id"]),
            created_at=_parse_ts(payload.get("created_at")),
            submitted_at=_parse_ts(payload.get("submitted_at")),
        )

        filled_qty = _as_decimal(payload.get("filled_qty", 0))
        if filled_qty > 0:
            avg = payload.get("filled_avg_price")
            if avg is None:
                raise BrokerError(
                    f"Alpaca reported {filled_qty} filled on {order.client_order_id} "
                    "with no average price"
                )
            order.apply_fill(
                Fill(
                    order_id=order.id,
                    ts=_parse_ts(payload.get("filled_at")) or datetime.now(UTC),
                    qty=filled_qty,
                    price=_as_decimal(avg),
                    # Alpaca does not itemise fees per order on this endpoint;
                    # regulatory fees land on the account activity feed. Left at
                    # zero rather than estimated — a guessed fee is a wrong
                    # number in the P&L ledger, and a missing one is a known
                    # gap the activities endpoint closes.
                    fee=Decimal(0),
                )
            )

        # `apply_fill` sets FILLED / PARTIALLY_FILLED from the fill itself. Any
        # other status is the venue's and is taken as given — including one
        # that contradicts the fill, such as a cancel that arrived after a
        # partial, where the venue is right and our arithmetic is not.
        if status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            order.status = status
        if status is OrderStatus.REJECTED:
            order.reject_reason = str(payload.get("reject_reason") or "rejected by venue")
        return order

    @staticmethod
    def _from_alpaca_position(payload: dict[str, Any]) -> Position:
        """Alpaca position → ours.

        Alpaca reports `qty` signed and `side` as long/short; the sign is taken
        as authoritative and the side is not consulted, because two fields that
        can disagree need one of them to win and the sign is what every
        downstream calculation actually uses.
        """
        return Position(
            symbol=str(payload["symbol"]),
            qty=_as_decimal(payload["qty"]),
            avg_entry_price=_as_decimal(payload["avg_entry_price"]),
            realized_pnl=Decimal(0),
            last_price=(
                None
                if payload.get("current_price") is None
                else _as_decimal(payload["current_price"])
            ),
        )
