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

**Fees.** Alpaca charges regulatory fees — the SEC fee and FINRA's TAF, both
sell-side — and books them as *account activities*, never on the fill. Both
places this adapter builds a `Fill` therefore set `fee` to zero, and say so.
That left our cash a fills-only total against a venue cash that is not one, so
the two ratcheted apart by the fee take of every session until the reconciler
halted the worker (2026-09-09; $4.14 against a $1.00 tolerance). The gap those
two comments call "a known gap the activities endpoint closes" is closed by
`get_fee_activities` below, and `atp_core.execution.fees` applies what it
returns.

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
import random
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from atp_core import ws
from atp_core.brokers.ports import (
    AccountSnapshot,
    FeeActivity,
    TradeUpdate,
    TradeUpdatesReconnected,
)
from atp_core.clock import SystemClock
from atp_core.domain import Fill, Order, OrderStatus, OrderType, Position, Side, TimeInForce
from atp_core.domain.enums import RunMode
from atp_core.errors import (
    BrokerConnectionError,
    BrokerError,
    InsufficientFundsError,
    InventoryHeldError,
    MissingBrokerCredentialsError,
    OrderRejectedError,
)
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from atp_core.brokers.ports import TradeUpdateEvent
    from atp_core.clock import Clock
    from atp_core.config import Settings

log = get_logger(__name__)

_MAX_ATTEMPTS = 5

#: Alpaca's 403 code for "this order is not permitted". It is a **bucket, not a
#: diagnosis**: buying power, a wash trade against a working order on the
#: opposite side, and several other refusals all arrive carrying it.
#:
#: It is the only Alpaca error code this platform knows, and day 3 of the paper
#: week is what that cost. `_refusal` read it as proof of a buying-power problem
#: and returned `InsufficientFundsError` for a wash-trade rejection, which then
#: escaped the router and killed the worker
#: (docs/paper-week/day-3-review.md, B2). Treat a new code the same way when one
#: turns up: a bucket to narrow inside, never a conclusion.
_ORDER_NOT_PERMITTED = 40310000

#: What the body actually says when the refusal really is buying power. Matched
#: on the message because the code above cannot tell the cases apart, and
#: matched loosely because the wording belongs to the venue and it will change.
#:
#: A miss here is cheap by construction: both branches return an
#: `OrderRejectedError` subclass, so the worst outcome is an exception that says
#: less than it could. That is the property to preserve if this list grows.
_INSUFFICIENT_FUNDS_PHRASES = ("insufficient buying power", "insufficient funds")


def _reads_as_insufficient_funds(message: str) -> bool:
    """Whether a venue message names buying power as the reason."""
    lowered = message.lower()
    return any(phrase in lowered for phrase in _INSUFFICIENT_FUNDS_PHRASES)


#: What the body says when the shares exist and one of *our own* working orders
#: has them reserved. Shares the same 40310000 bucket as buying power and means
#: the opposite — the account holds the position, a resting order of ours holds
#: the quantity. Alpaca names the holder in the body it returns:
#:
#:     {"available":"0","code":40310000,"existing_qty":"6","held_for_orders":"6",
#:      "message":"insufficient qty available for order (requested: 6, available: 0)"}
#:
#: Narrowed because the answer is specific and nothing else's answer fits:
#: cancel the order doing the holding, then place the close again
#: (`OrderRouter._close`). Read as a generic rejection it looks like a market
#: condition to wait out, and day 4 of the paper week waited it out 38 times.
_INVENTORY_HELD_PHRASES = ("insufficient qty available", "held_for_orders")


def _reads_as_inventory_held(message: str, body: str) -> bool:
    """Whether a venue refusal says our own working order holds the shares.

    Checks the whole body as well as the parsed message: `held_for_orders` is a
    field name rather than prose, and it is the half of this that cannot be
    reworded without the API changing.
    """
    haystack = f"{message}\n{body}".lower()
    return any(phrase in haystack for phrase in _INVENTORY_HELD_PHRASES)


#: The non-trade activity types that move cash as a *charge* against trading.
#:
#: **One type, not three.** The account feed for 2026-09-08 answered:
#:
#:     FEE / CAT  -0.01  "CAT fee for proceed of 174 trades"
#:     FEE / REG  -3.82  "REG fee for proceed of $185271.37"
#:     FEE / TAF  -0.31  "TAF fee for proceed of 1572 shares (89 trades)"
#:
#: — so `REG` and `TAF` are `activity_sub_type` values *under* `FEE`, and
#: asking for them as activity types would have quietly returned nothing while
#: looking like it worked. Those three sum to exactly the $4.14 that halted the
#: worker the next morning, which is the whole of this path's evidence.
#:
#: Narrow on purpose. The rest of the feed is journals, dividends and
#: transfers: money that moves for reasons the platform has no opinion about
#: and must not silently fold into a trading ledger.
#:
#: **A type missing from this list fails safely.** It is not applied, so our
#: cash stays above the venue's, so the reconciler reports the drift and halts
#: — the same visible failure this list exists to remove, rather than a book
#: quietly corrected by a number nobody chose.
_FEE_ACTIVITY_TYPES = ("FEE",)

#: The only `status` whose money has actually moved. A row in any other state
#: is a fee the venue has not charged yet, and applying it would put our cash
#: *below* the venue's — drift in the other direction, reported identically and
#: harder to read.
_EXECUTED = "executed"

#: Alpaca caps `page_size` on the activities endpoint; ask for the cap so a
#: quiet account is one request.
_ACTIVITY_PAGE_SIZE = 100
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

#: The one account stream this adapter listens to.
_TRADE_UPDATES_STREAM = "trade_updates"

#: Alpaca's trade-update **event** vocabulary → ours. A separate map from
#: `_STATUS` above on purpose: these are event names, not statuses, and they
#: disagree with the REST spellings in exactly the places that matter
#: (`fill` vs `filled`, `partial_fill` vs `partially_filled`). Folding them
#: into one map would mean one of the two vocabularies quietly accepting
#: strings the venue never sends on that channel.
#:
#: A fill's entry is None because `Order.apply_fill` owns the resulting status:
#: only the arithmetic knows whether this print completed the order.
_STREAM_EVENTS: dict[str, OrderStatus | None] = {
    # Working at the venue.
    "new": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "stopped": OrderStatus.SUBMITTED,
    "calculated": OrderStatus.SUBMITTED,
    "held": OrderStatus.SUBMITTED,
    "pending_cancel": OrderStatus.SUBMITTED,
    "pending_replace": OrderStatus.SUBMITTED,
    # Fills — status decided by the arithmetic, not by the event name.
    "fill": None,
    "partial_fill": None,
    # Terminal.
    "canceled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "done_for_day": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
    "replaced": OrderStatus.CANCELLED,
    # A cancel or replace the venue refused. The order is untouched by it, so
    # there is no status change to make — but it is a real event and silently
    # dropping it would leave a caller believing its cancel took effect.
    "order_cancel_rejected": None,
    "order_replace_rejected": None,
}

#: The two that carry an execution.
_FILL_EVENTS = frozenset({"fill", "partial_fill"})


class _PermanentStreamError(BrokerError):
    """The stream refused in a way another connection would not fix.

    Internal: callers see `BrokerError`. It exists so the reconnect loop can
    tell "try again" from "stop and tell somebody" without inspecting messages.
    """


def _iter_messages(raw: str | bytes) -> list[dict[str, Any]]:
    """A frame's messages. Alpaca sends a bare object here; tolerate an array."""
    payload = json.loads(raw, parse_float=Decimal)
    if isinstance(payload, dict):
        return [payload]
    return [m for m in payload if isinstance(m, dict)]


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
        connect: Callable[[str], Awaitable[ws.WebSocketConnection]] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
        stream_backoff_base_seconds: float = ws.BACKOFF_BASE_SECONDS,
        stream_backoff_max_seconds: float = ws.BACKOFF_MAX_SECONDS,
        reconnect_budget_seconds: float = ws.RECONNECT_BUDGET_SECONDS,
        clock: Clock | None = None,
        handshake_timeout_seconds: float = ws.HANDSHAKE_TIMEOUT_SECONDS,
    ) -> None:
        if settings.run_mode is RunMode.BACKTEST:
            raise ValueError("AlpacaBroker cannot serve a backtest; use SimulatedBroker")
        if not settings.alpaca_api_key.get_secret_value():
            # Here rather than in `Settings`, which is where this lived and
            # could not stay: refusing during settings validation made an
            # unbuildable *broker* into an unimportable *process* (see
            # `Settings._guard_live_trading`). This is the one place that
            # genuinely cannot proceed without the key, and both callers —
            # `atp_api.deps` and the worker — come through it.
            #
            # The key is never quoted, here or in any other message: naming the
            # variable is what an operator needs, and the value is the one thing
            # that must not reach a log (CLAUDE.md §1.6).
            raise MissingBrokerCredentialsError(
                f"ALPACA_API_KEY is not set, and run_mode={settings.run_mode.value} trades "
                "against Alpaca. Set ALPACA_API_KEY and ALPACA_API_SECRET in .env, or use "
                "ATP_RUN_MODE=backtest, which needs no credentials."
            )
        self._settings = settings
        self._base_url = settings.broker_base_url
        self._is_live = settings.is_live
        self._client = client
        #: Only close what we opened — an injected client belongs to its owner.
        self._owns_client = client is None
        self._backoff_base_seconds = backoff_base_seconds

        # ── trade-updates stream seams ──────────────────────────────────────
        # Injected so the reconnect and handshake state machine is drivable off
        # a scripted fake, with no network anywhere (CLAUDE.md §1.7).
        self._connect: Callable[[str], Awaitable[ws.WebSocketConnection]] = (
            connect if connect is not None else ws.connect_websocket
        )
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else ws.sleep_seconds
        )
        #: Jitter, not cryptography — a seeded one is what makes the backoff
        #: schedule assertable in a test.
        self._rng = rng if rng is not None else random.Random()
        self._backoff_base_seconds_stream = stream_backoff_base_seconds
        self._backoff_max_seconds = stream_backoff_max_seconds
        self._reconnect_budget_seconds = reconnect_budget_seconds
        #: Injected for the same reason `sleep` is: the reconnect budget below
        #: is measured in elapsed time, and a test cannot drive a fifteen-minute
        #: outage off a wall clock. Used by the trade-updates loop only — the
        #: REST half still stamps `datetime.now(UTC)` in three places, which is
        #: a separate (and smaller) §1.2 debt this change does not widen into.
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._handshake_timeout_seconds = handshake_timeout_seconds

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

        **Every refusal returned here is an `OrderRejectedError` or a subclass
        of one**, and that is what makes a wrong guess cheap. This method used
        to read `code == 40310000` as proof of a buying-power refusal and return
        `InsufficientFundsError` unconditionally, before even looking at the
        status. On day 3 a wash-trade rejection carried that code, got that
        class, and — the class being a sibling of `OrderRejectedError` at the
        time — escaped the router's catch and killed the worker
        (docs/paper-week/day-3-review.md, B2).

        So the narrowing below decides how *specific* the exception is, never
        whether a caller can catch it. A phrase this misses costs a less precise
        error; it cannot cost a process.
        """
        detail = response.text[:400]
        code, message = None, ""
        with contextlib.suppress(ValueError):
            payload = json.loads(response.text)
            if isinstance(payload, dict):
                code = payload.get("code")
                message = str(payload.get("message") or "")

        if response.status_code in (httpx.codes.FORBIDDEN, httpx.codes.UNPROCESSABLE_ENTITY):
            refused = f"Alpaca refused {method} {path}: {detail}"
            if code == _ORDER_NOT_PERMITTED and _reads_as_insufficient_funds(message):
                return InsufficientFundsError(refused)
            if _reads_as_inventory_held(message, detail):
                return InventoryHeldError(refused)
            return OrderRejectedError(refused)
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

    async def get_fee_activities(self, since: date) -> list[FeeActivity]:
        """GET /v2/account/activities — the fees, oldest first.

        Alpaca books a regulatory fee as its own non-trade activity with a
        `net_amount` and a `date`, and never attaches it to the fill it came
        from. So this cannot populate `Fill.fee`, and does not pretend to: it
        reports the charges, and `atp_core.execution.fees` settles them against
        cash. Attributing an account-level debit back to one fill would be a
        guess, and a guessed fee is a wrong number in the P&L ledger — the
        reason both `Fill` sites here hold zero rather than an estimate.

        **Sign is normalised on the way out.** Alpaca reports money leaving the
        account as a negative `net_amount`; `FeeActivity.amount` is positive for
        the same event, so a caller settles with `cash -= amount` for every
        venue. A rebate keeps the arithmetic honest by arriving negative.

        **Paginated to exhaustion.** `page_token` is the last id seen. A partial
        read here would look exactly like a session that paid fewer fees, which
        is the failure this whole path exists to stop.

        A malformed entry is skipped and logged rather than raising. This runs
        inside reconciliation, on the unattended path, and one unparseable row
        must not be able to stop the fees around it from being applied — the
        same argument `sweepable_series` makes for one dead ticker. What is
        skipped stays unapplied and therefore stays visible as drift.
        """
        activities: list[FeeActivity] = []
        page_token: str | None = None

        while True:
            params: dict[str, Any] = {
                "activity_types": ",".join(_FEE_ACTIVITY_TYPES),
                "after": since.isoformat(),
                "page_size": _ACTIVITY_PAGE_SIZE,
            }
            if page_token:
                params["page_token"] = page_token

            payload = await self._request("GET", "/v2/account/activities", params=params)
            if not payload:
                break

            for entry in payload:
                activity = self._to_fee_activity(entry)
                if activity is not None:
                    activities.append(activity)

            if len(payload) < _ACTIVITY_PAGE_SIZE:
                break
            page_token = str(payload[-1]["id"])

        # By date only, and `sort` is stable — so the venue's own order within
        # a day survives. The id's suffix is a UUID: ordering on it would put
        # a day's CAT, REG and TAF fees in an order nothing chose, and a fee
        # list an operator reads should look like the feed it came from.
        activities.sort(key=lambda item: item.booked_on)
        return activities

    @staticmethod
    def _to_fee_activity(entry: Any) -> FeeActivity | None:
        """One activity row, or None if it is not one we can bank on.

        `id` and `net_amount` are both required: an activity with no identifier
        cannot be applied exactly once, and one with no amount has nothing to
        apply. Neither is a case worth guessing through.
        """
        status = str(entry.get("status", _EXECUTED)) if isinstance(entry, dict) else ""
        if status != _EXECUTED:
            log.info(
                "broker.alpaca.fee_activity_skipped",
                status=status,
                msg="fee is not executed; nothing has left the account yet",
            )
            return None
        try:
            activity_id = str(entry["id"])
            net_amount = _as_decimal(entry["net_amount"])
            booked_on = date.fromisoformat(str(entry["date"])[:10])
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "broker.alpaca.fee_activity_unparseable",
                error=str(exc),
                msg="skipping one activity row; it stays unapplied and therefore "
                "stays visible to reconciliation as cash drift",
            )
            return None

        return FeeActivity(
            activity_id=activity_id,
            booked_on=booked_on,
            # Negated: Alpaca signs money leaving the account negative, and
            # `FeeActivity.amount` is positive for a charge at every venue.
            amount=-net_amount,
            # `activity_sub_type` is the fee's kind — CAT, REG, TAF — and it is
            # what an operator reconciling a session actually reads.
            sub_type=str(entry.get("activity_sub_type", "")),
            description=str(entry.get("description", "")),
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

    async def stream_trade_updates(self) -> AsyncIterator[TradeUpdateEvent]:
        """Fill and status events, pushed.

        Reconnect with backoff on drop, then reconcile open orders via REST —
        events during the gap are lost, and a missed fill means our position
        view is wrong (CLAUDE.md §5).

        That reconciliation is the *consumer's* job and this yields
        `TradeUpdatesReconnected` to demand it. The adapter deliberately does
        not re-read open orders itself: it holds no book to correct, and a
        catch-up it performed silently would be one the consumer could not
        order against its own state. Carrying the signal in the stream is what
        makes "close the gap before handling the next event" a guarantee rather
        than a hope — the same reasoning as `data.ports.FeedReconnected`.

        Unlike the market-data feed, there is nothing to re-subscribe on
        reconnect beyond the single `trade_updates` stream, and nothing to
        replay: Alpaca does not re-send events for the gap. REST is the only
        way back.
        """
        attempts = 0
        #: The last instant the order state is known good. Seeded with "now"
        #: rather than left empty: before the first event there is nothing to
        #: be missing, and a first connection that takes four attempts to come
        #: up has genuinely missed whatever filled while it was struggling.
        gap_since = self._clock.now()
        reconnecting = False
        #: When the current run of failures began, or None while connected. The
        #: budget is elapsed time rather than a count of attempts, so a venue
        #: away for seven minutes is waited out instead of killing the worker
        #: four minutes in (docs/paper-week/day-1-review.md, F6). This stream
        #: and the market-data feed exhausted together on day 1, which is why
        #: both ladders changed in one commit.
        first_failure_at: datetime | None = None

        while True:
            try:
                connection = await self._open_stream()
            except _PermanentStreamError:
                raise
            except Exception as exc:  # every transport failure retries alike
                attempts += 1
                now = self._clock.now()
                if first_failure_at is None:
                    first_failure_at = now
                if ws.budget_exhausted(first_failure_at, now, self._reconnect_budget_seconds):
                    raise BrokerConnectionError(
                        f"Alpaca trade updates did not come back within "
                        f"{self._reconnect_budget_seconds:.0f}s "
                        f"({attempts} attempts): {exc}"
                    ) from exc
                log.warning(
                    "broker.alpaca.trade_updates_reconnecting",
                    attempt=attempts,
                    trying_for_seconds=round((now - first_failure_at).total_seconds(), 1),
                    budget_seconds=self._reconnect_budget_seconds,
                    error=str(exc),
                )
                await self._sleep(
                    ws.backoff_delay(
                        attempts,
                        base_seconds=self._backoff_base_seconds_stream,
                        max_seconds=self._backoff_max_seconds,
                        rng=self._rng,
                    )
                )
                reconnecting = True
                continue

            if reconnecting:
                reconnecting = False
                yield TradeUpdatesReconnected(
                    gap_since=gap_since,
                    reconnected_at=self._clock.now(),
                    attempts=attempts + 1,
                )

            #: When this connection was established. The ladder is cleared, or
            #: not, on how long it goes on to live — see the end of this loop.
            connected_at = self._clock.now()

            while True:
                try:
                    raw = await connection.recv()
                except Exception as exc:  # a closed socket arrives in many shapes
                    log.warning("broker.alpaca.trade_updates_disconnected", error=str(exc))
                    await ws.close_quietly(connection)
                    break

                for message in _iter_messages(raw):
                    update = self._to_trade_update(message)
                    if update is not None:
                        gap_since = update.at
                        yield update

            reconnecting = True
            now = self._clock.now()
            if not ws.is_flap(connected_at, now):
                # A connection that lived is an ordinary disconnect. **Not
                # whether it carried anything**: this stream is silent whenever
                # nobody is trading, so a guard that read silence as failure
                # would exhaust the budget on a healthy socket and halt the
                # platform on a quiet afternoon — worse than the loop it
                # replaced.
                attempts = 0
                first_failure_at = None
            else:
                # A connection that ended before it could have been real is a
                # failed attempt. Without this the outer loop spun with no sleep
                # and no budget check, so the give-up path was unreachable — and
                # each turn calls `reconciler.reconcile()` through
                # `consume_trade_updates`, three unpaced REST calls, until the
                # venue rate-limits and the five-attempt REST ladder engages a
                # global halt. Day 1's crash loop through a different door.
                attempts += 1
                if first_failure_at is None:
                    first_failure_at = now
                if ws.budget_exhausted(first_failure_at, now, self._reconnect_budget_seconds):
                    raise BrokerConnectionError(
                        f"Alpaca trade updates kept dropping within "
                        f"{ws.MIN_HEALTHY_UPTIME_SECONDS:.0f}s of connecting for "
                        f"{self._reconnect_budget_seconds:.0f}s ({attempts} attempts)"
                    )
                log.warning(
                    "broker.alpaca.trade_updates_flapping",
                    attempt=attempts,
                    trying_for_seconds=round((now - first_failure_at).total_seconds(), 1),
                    budget_seconds=self._reconnect_budget_seconds,
                    msg="the connection was dropped before it could have been established",
                )
                await self._sleep(
                    ws.backoff_delay(
                        attempts,
                        base_seconds=self._backoff_base_seconds_stream,
                        max_seconds=self._backoff_max_seconds,
                        rng=self._rng,
                    )
                )

    # ── trade-updates transport ─────────────────────────────────────────────

    @property
    def stream_url(self) -> str:
        """The account stream, derived from the REST host.

        Deliberately derived rather than configured: paper and live trade
        updates must come from the same account the orders went to, and a
        separately-configured URL is one edit away from watching the paper
        account while trading the live one.
        """
        return f"{self._base_url.replace('https://', 'wss://').rstrip('/')}/stream"

    async def _open_stream(self) -> ws.WebSocketConnection:
        """Connect, authenticate and listen, or clean up trying."""
        connection = await self._connect(self.stream_url)
        try:
            async with asyncio.timeout(self._handshake_timeout_seconds):
                await self._authenticate_stream(connection)
                await self._listen(connection)
        except BaseException:
            # Includes the timeout and a cancellation. A half-authenticated
            # socket left open still holds a connection slot, so the retry
            # would be refused by our own leak.
            await ws.close_quietly(connection)
            raise

        log.info("broker.alpaca.trade_updates_connected", account=self.name)
        return connection

    async def _authenticate_stream(self, connection: ws.WebSocketConnection) -> None:
        """Send credentials and wait for the server to accept them.

        The account stream's handshake is **not** the market-data one: the
        action is `authenticate`, the credentials are nested under `data`, and
        they are named `key_id`/`secret_key`. Sending the market-data frame
        here authenticates nothing and the server simply never answers, which
        is why the two are not shared despite looking alike.

        No credential reaches a log line or an exception message (rule §1.6) —
        the frame is built inline and the errors quote only the server's own
        words.
        """
        await connection.send(
            json.dumps(
                {
                    "action": "authenticate",
                    "data": {
                        "key_id": self._settings.alpaca_api_key.get_secret_value(),
                        "secret_key": self._settings.alpaca_api_secret.get_secret_value(),
                    },
                }
            )
        )

        for _ in range(ws.MAX_HANDSHAKE_FRAMES):
            for message in _iter_messages(await connection.recv()):
                if message.get("stream") != "authorization":
                    continue  # listening confirmations and stray frames
                status = str((message.get("data") or {}).get("status", ""))
                if status == "authorized":
                    return
                raise _PermanentStreamError(
                    f"Alpaca refused the trade-updates handshake: {status or 'unauthorized'}. "
                    "Check that these are the credentials for this account — paper and live "
                    "use separate key pairs."
                )

        raise BrokerConnectionError(
            f"Alpaca sent {ws.MAX_HANDSHAKE_FRAMES} frames without authorizing the stream"
        )

    @staticmethod
    async def _listen(connection: ws.WebSocketConnection) -> None:
        """Subscribe to the one stream this adapter wants."""
        await connection.send(
            json.dumps({"action": "listen", "data": {"streams": [_TRADE_UPDATES_STREAM]}})
        )

    def _to_trade_update(self, message: dict[str, Any]) -> TradeUpdate | None:
        """One frame's message → a `TradeUpdate`, or None if it is not one.

        Handshake confirmations and anything on another stream are not events
        and are skipped. An `event` we have no mapping for is **refused**: the
        plausible default is "ignore it", and an ignored `rejected` is an order
        our book believes is still working.
        """
        if message.get("stream") != _TRADE_UPDATES_STREAM:
            return None

        data = message.get("data") or {}
        event = str(data.get("event", "")).lower()
        if event not in _STREAM_EVENTS:
            raise BrokerError(
                f"unrecognised Alpaca trade-update event {event!r} — refusing to guess "
                "whether it leaves the order working"
            )

        payload = data.get("order") or {}
        order = self._from_alpaca_order(payload)
        at = _parse_ts(data.get("timestamp")) or datetime.now(UTC)

        fill = None
        if event in _FILL_EVENTS:
            fill = Fill(
                order_id=order.id,
                ts=at,
                qty=_as_decimal(data["qty"]),
                price=_as_decimal(data["price"]),
                # Fees are not on this event; they arrive on the account
                # activities feed. Zero rather than estimated — a guessed fee
                # is a wrong number in the P&L ledger.
                fee=Decimal(0),
                venue_fill_id=str(data["execution_id"]) if data.get("execution_id") else None,
            )

        position_qty = data.get("position_qty")
        return TradeUpdate(
            event=event,
            client_order_id=order.client_order_id,
            broker_order_id=str(payload.get("id", "")),
            symbol=order.symbol,
            at=at,
            status=_STREAM_EVENTS[event],
            fill=fill,
            position_qty=None if position_qty is None else _as_decimal(position_qty),
            reason=str(payload.get("reject_reason")) if payload.get("reject_reason") else None,
            broker=self.name,
        )

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
