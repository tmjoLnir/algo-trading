"""The published snapshot: value objects, the builder, and the wire format.

Pure. Nothing here reaches Redis, a database or a broker — it takes a book and
returns a picture of it, which is what makes every number on the dashboard
testable without a process to publish it (CLAUDE.md §1.3).

Three rules govern the shapes below and each one is a bug that has happened
somewhere in this class of system:

1. **Every derived number is computed here, once.** `unrealized_pnl_pct`,
   `leverage`, `distance_to_stop_pct` — none of them are left for the browser.
   A dashboard doing arithmetic on money is a dashboard doing arithmetic in
   IEEE 754 doubles, and rule §1.1 does not stop being true because the value
   crossed a network.
2. **A number we do not know is `None`, never zero.** An unmarked position is
   not a position worth nothing, and an account with no day-open anchor has not
   made zero today. Zero is a value a reader acts on.
3. **Money crosses the wire as a string.** `encode_snapshot` renders every
   `Decimal` with `str`, and `decode_snapshot` parses it back. JSON has one
   numeric type and it is a float.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from atp_core.domain.enums import RunMode

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from atp_core.domain import Order, Portfolio, Position, Quote

#: How precisely a ratio is reported. Four decimal places is a hundredth of a
#: percent, which is finer than anything a human reads off a screen and coarse
#: enough that `Decimal` division does not put twenty-eight digits on the wire.
#: Ratios only — never applied to money, which is reported exactly.
RATIO_PLACES = Decimal("0.0001")

#: How many decisions the runner keeps for the feed. Bounded because this is a
#: fixed-size document in Redis and an unbounded list of a busy day's signals
#: would grow it without limit; fifty is what the endpoint's `signal_limit`
#: defaults to.
DEFAULT_SIGNAL_LIMIT = 50


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """A quantised ratio, or None when the denominator makes it meaningless.

    Returning None rather than zero is the whole point. A position whose entry
    price equals its stop has no entry-to-stop distance to be a fraction of,
    and reporting that as 0.0 would render a healthy position as one sitting
    exactly on its stop.
    """
    if denominator == 0:
        return None
    return (numerator / denominator).quantize(RATIO_PLACES)


@dataclass(frozen=True, slots=True)
class PositionSummary:
    """One holding, with everything the screen needs already computed."""

    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    #: None when nothing has marked this position. Every value below that
    #: depends on a mark is then None too, rather than silently zero — a
    #: position reported as worth nothing is a position a reader ignores.
    last_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pnl_pct: Decimal | None
    realized_pnl: Decimal
    fees_paid: Decimal
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    #: How much of the entry-to-stop distance is left, as a fraction: 1.0 means
    #: price is at the entry, 0.0 means it is at the stop, above 1.0 means the
    #: trade is in profit. **Negative means price is already through the stop**
    #: and the exit has not happened — which is a signed quantity on purpose,
    #: because an absolute one would render the most alarming state on the
    #: screen as an ordinary small number.
    distance_to_stop_pct: Decimal | None
    strategy_id: str | None
    opened_at: datetime | None


@dataclass(frozen=True, slots=True)
class SignalSummary:
    """A decision and its fate — requirement #7's "why is this trade on?".

    `acted_on` and `rejection_reason` are the pair that makes this worth
    keeping. A strategy refused by a risk rule on every bar looks, from any
    other vantage point, exactly like a strategy that had no ideas.
    """

    id: str
    ts: datetime
    strategy_id: str
    symbol: str
    action: str
    reason: str
    indicators: dict[str, str]
    acted_on: bool
    rejection_reason: str | None = None
    #: Which rule refused it, when one did. Separate from the prose reason so a
    #: client can group by it without parsing English.
    rejected_by: str | None = None


@dataclass(frozen=True, slots=True)
class OrderSummary:
    """An order we believe is working at the venue."""

    id: str
    client_order_id: str
    ts: datetime | None
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    filled_qty: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    avg_fill_price: Decimal | None
    status: str
    strategy_id: str | None


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """Account-level figures, all from one book at one instant.

    Deliberately no `buying_power`. That is the venue's number, and reading it
    costs a broker call — one per dashboard poll, on the process that is also
    placing orders against the same rate limit. `BuyingPowerRule` constrains
    against `cash`, so cash is the number that actually decides whether an
    order is approved here, and it is the one shown.
    """

    equity: Decimal
    cash: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    #: None when equity is zero: leverage against no capital is not infinity,
    #: it is undefined, and rendering it as 0.0 would read as "unlevered".
    leverage: Decimal | None
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    open_position_count: int
    #: Positions with no mark. Non-empty means the figures above understate
    #: exposure and equity, which is the direction that makes a breached limit
    #: look compliant — so it travels with them rather than being inferred.
    unmarked_symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LiveSnapshot:
    """Everything the worker knows about the book, at one instant.

    `as_of` is that instant. It is the worker's clock, not the API's, and it is
    what the dashboard displays an age against — a screen that cannot tell
    four-minute-old data from fresh data is trusted blindly rather than
    correctly (docs/DASHBOARD.md).
    """

    as_of: datetime
    run_mode: RunMode
    account: AccountSummary
    positions: tuple[PositionSummary, ...] = ()
    recent_signals: tuple[SignalSummary, ...] = ()
    working_orders: tuple[OrderSummary, ...] = ()
    #: Which strategy produced this. None when the worker is running without
    #: one, which is the default posture (`WORKER_STRATEGY` is empty).
    strategy: str | None = None
    #: The newest market-data timestamp the worker has seen. Not the time it
    #: was received: a feed that is connected and frozen keeps receiving
    #: nothing while its socket looks perfectly healthy, and the printed
    #: timestamp is the only thing that stops advancing.
    #:
    #: A *fact*, not a verdict. Whether that age means the feed is broken
    #: depends on whether the market is open, and the API is already answering
    #: that for the same response — judging it here as well would let one
    #: dashboard report a healthy feed beside a closed market and leave the
    #: reader to work out which half to believe.
    last_data_at: datetime | None = None
    #: Symbols the worker is subscribed to, so the socket can subscribe to the
    #: same set without a second request.
    symbols: tuple[str, ...] = ()


def build_snapshot(
    portfolio: Portfolio,
    *,
    at: datetime,
    run_mode: RunMode,
    working_orders: Iterable[Order] = (),
    recent_signals: Iterable[SignalSummary] = (),
    quotes: Mapping[str, Quote] | None = None,
    symbols: Iterable[str] = (),
    strategy: str | None = None,
) -> LiveSnapshot:
    """Render the book as the dashboard's one consistent picture.

    Positions come back **sorted by exposure, largest first**, and that ordering
    is part of the contract rather than a nicety. The alternative is a client
    that sorts, and a client that sorts a column of money is a client parsing
    money into a float to compare it. Flat positions are dropped: a `Portfolio`
    keeps a zeroed `Position` around after an exit, and rendering it as a row
    holding nothing is noise on the screen a person scans first.

    `at` is passed rather than read from a clock so that the whole picture
    shares one instant even if building it takes time (CLAUDE.md §1.2).
    """
    if at.tzinfo is None:
        raise ValueError(f"snapshot instant must be tz-aware UTC (rule §1.2), got {at!r}")

    positions = tuple(sorted((_position(p) for p in portfolio.open_positions), key=_by_exposure))
    return LiveSnapshot(
        as_of=at,
        run_mode=run_mode,
        account=_account(portfolio),
        positions=positions,
        recent_signals=tuple(recent_signals),
        working_orders=tuple(_order(o) for o in working_orders),
        strategy=strategy,
        last_data_at=_newest_quote_ts(quotes),
        symbols=tuple(symbols),
    )


def _by_exposure(position: PositionSummary) -> tuple[Decimal, str]:
    """Largest holding first, ties broken by symbol so the order is stable.

    An unmarked position sorts to the bottom rather than to the top: its
    exposure is unknown, and ranking "unknown" alongside "nothing" is the lesser
    of the two errors — the account summary already carries `unmarked_symbols`
    so the reader is told rather than left to notice a suspiciously quiet row.
    """
    exposure = position.market_value.copy_abs() if position.market_value is not None else Decimal(0)
    return (-exposure, position.symbol)


def _newest_quote_ts(quotes: Mapping[str, Quote] | None) -> datetime | None:
    """The freshest print across the watchlist.

    The newest rather than the oldest: this answers "is anything arriving?",
    which is the feed's pulse. Whether one *particular* symbol has gone quiet
    is `StaleDataRule`'s question, and it is asked per order against that
    symbol's own last tick — a watchlist where one name is halted and the rest
    are busy is a healthy feed with one dead symbol, and conflating the two
    would either halt on every halted stock or miss a dead feed entirely.
    """
    if not quotes:
        return None
    return max(quote.ts for quote in quotes.values())


def _account(portfolio: Portfolio) -> AccountSummary:
    open_positions = portfolio.open_positions
    return AccountSummary(
        equity=portfolio.equity,
        cash=portfolio.cash,
        gross_exposure=portfolio.gross_exposure,
        net_exposure=portfolio.net_exposure,
        leverage=_ratio(portfolio.gross_exposure, portfolio.equity),
        realized_pnl=sum((p.realized_pnl for p in portfolio.positions.values()), Decimal(0)),
        unrealized_pnl=sum((p.unrealized_pnl for p in open_positions), Decimal(0)),
        open_position_count=len(open_positions),
        unmarked_symbols=tuple(portfolio.unmarked_symbols),
    )


def _position(position: Position) -> PositionSummary:
    marked = position.last_price is not None
    cost_basis = (position.avg_entry_price * position.qty).copy_abs()
    return PositionSummary(
        symbol=position.symbol,
        qty=position.qty,
        avg_entry_price=position.avg_entry_price,
        last_price=position.last_price,
        market_value=position.market_value if marked else None,
        unrealized_pnl=position.unrealized_pnl if marked else None,
        unrealized_pnl_pct=_ratio(position.unrealized_pnl, cost_basis) if marked else None,
        realized_pnl=position.realized_pnl,
        fees_paid=position.fees_paid,
        stop_loss_price=position.stop_loss_price,
        take_profit_price=position.take_profit_price,
        distance_to_stop_pct=_distance_to_stop(position),
        strategy_id=None,
        opened_at=position.opened_at,
    )


def _distance_to_stop(position: Position) -> Decimal | None:
    """What fraction of the entry-to-stop distance is still standing.

    One expression covers long and short. For a long the entry is above the
    stop and price falling towards it shrinks the numerator; for a short both
    the numerator and the denominator invert, so the ratio still runs from 1.0
    at the entry to 0.0 at the stop. Writing it per side would be two chances
    to get a sign wrong on the number that says how close a position is to
    being closed.
    """
    stop = position.stop_loss_price
    if stop is None or position.last_price is None:
        return None
    return _ratio(position.last_price - stop, position.avg_entry_price - stop)


def _order(order: Order) -> OrderSummary:
    return OrderSummary(
        id=order.id,
        client_order_id=order.client_order_id,
        # The instant it reached the venue, falling back to when it was built.
        # Both can be None on an order that never got that far, which is honest
        # — stamping "now" would put a submission time on something that was
        # never submitted.
        ts=order.submitted_at or order.created_at,
        symbol=order.symbol,
        side=order.side.value,
        order_type=order.order_type.value,
        qty=order.qty,
        filled_qty=order.filled_qty,
        limit_price=order.limit_price,
        stop_price=order.stop_price,
        avg_fill_price=order.avg_fill_price,
        status=order.status.value,
        strategy_id=order.strategy_id,
    )


# ── wire format ─────────────────────────────────────────────────────────────
#
# Hand-written rather than reached for from a serialisation library, because
# the one thing that must not happen — a `Decimal` becoming a JSON number — is
# exactly what every library does by default.


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _require_decimal(value: object, field: str) -> Decimal:
    parsed = _decimal(value)
    if parsed is None:
        raise ValueError(f"{field} is required and was null")
    return parsed


def _ts(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_ts(value: object, field: str) -> datetime | None:
    """Stored ISO-8601 → tz-aware UTC (rule §1.2).

    A naive timestamp is refused rather than assumed to be UTC. Assuming would
    put a plausible time on the screen that is wrong by the reader's offset,
    which on a dashboard showing "updated 12s ago" is the difference between
    trusting the data and knowing not to.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be an ISO-8601 string, got {type(value).__name__}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{field} is naive: {value!r}")
    return parsed.astimezone(UTC)


def _require_ts(value: object, field: str) -> datetime:
    parsed = _parse_ts(value, field)
    if parsed is None:
        raise ValueError(f"{field} is required and was null")
    return parsed


def encode_snapshot(snapshot: LiveSnapshot) -> dict[str, Any]:
    """The stored document. Every number is a string (rule §1.1)."""
    account = snapshot.account
    return {
        "version": 1,
        "as_of": snapshot.as_of.isoformat(),
        "run_mode": snapshot.run_mode.value,
        "strategy": snapshot.strategy,
        "last_data_at": _ts(snapshot.last_data_at),
        "symbols": list(snapshot.symbols),
        "account": {
            "equity": str(account.equity),
            "cash": str(account.cash),
            "gross_exposure": str(account.gross_exposure),
            "net_exposure": str(account.net_exposure),
            "leverage": _money(account.leverage),
            "realized_pnl": str(account.realized_pnl),
            "unrealized_pnl": str(account.unrealized_pnl),
            "open_position_count": account.open_position_count,
            "unmarked_symbols": list(account.unmarked_symbols),
        },
        "positions": [
            {
                "symbol": p.symbol,
                "qty": str(p.qty),
                "avg_entry_price": str(p.avg_entry_price),
                "last_price": _money(p.last_price),
                "market_value": _money(p.market_value),
                "unrealized_pnl": _money(p.unrealized_pnl),
                "unrealized_pnl_pct": _money(p.unrealized_pnl_pct),
                "realized_pnl": str(p.realized_pnl),
                "fees_paid": str(p.fees_paid),
                "stop_loss_price": _money(p.stop_loss_price),
                "take_profit_price": _money(p.take_profit_price),
                "distance_to_stop_pct": _money(p.distance_to_stop_pct),
                "strategy_id": p.strategy_id,
                "opened_at": _ts(p.opened_at),
            }
            for p in snapshot.positions
        ],
        "recent_signals": [
            {
                "id": s.id,
                "ts": s.ts.isoformat(),
                "strategy_id": s.strategy_id,
                "symbol": s.symbol,
                "action": s.action,
                "reason": s.reason,
                "indicators": dict(s.indicators),
                "acted_on": s.acted_on,
                "rejection_reason": s.rejection_reason,
                "rejected_by": s.rejected_by,
            }
            for s in snapshot.recent_signals
        ],
        "working_orders": [
            {
                "id": o.id,
                "client_order_id": o.client_order_id,
                "ts": _ts(o.ts),
                "symbol": o.symbol,
                "side": o.side,
                "order_type": o.order_type,
                "qty": str(o.qty),
                "filled_qty": str(o.filled_qty),
                "limit_price": _money(o.limit_price),
                "stop_price": _money(o.stop_price),
                "avg_fill_price": _money(o.avg_fill_price),
                "status": o.status,
                "strategy_id": o.strategy_id,
            }
            for o in snapshot.working_orders
        ],
    }


def decode_snapshot(payload: Mapping[str, Any]) -> LiveSnapshot:
    """The stored document → a `LiveSnapshot`.

    Raises on anything it cannot read. The caller — the API — turns that into a
    refusal to serve rather than into an empty dashboard, because "the worker
    has published nothing" and "the worker published something this version
    cannot parse" are different problems and only one of them is normal.
    """
    account = payload["account"]
    return LiveSnapshot(
        as_of=_require_ts(payload["as_of"], "as_of"),
        run_mode=RunMode(payload["run_mode"]),
        account=AccountSummary(
            equity=_require_decimal(account["equity"], "account.equity"),
            cash=_require_decimal(account["cash"], "account.cash"),
            gross_exposure=_require_decimal(account["gross_exposure"], "account.gross_exposure"),
            net_exposure=_require_decimal(account["net_exposure"], "account.net_exposure"),
            leverage=_decimal(account["leverage"]),
            realized_pnl=_require_decimal(account["realized_pnl"], "account.realized_pnl"),
            unrealized_pnl=_require_decimal(account["unrealized_pnl"], "account.unrealized_pnl"),
            open_position_count=int(account["open_position_count"]),
            unmarked_symbols=tuple(account.get("unmarked_symbols", ())),
        ),
        positions=tuple(
            PositionSummary(
                symbol=p["symbol"],
                qty=_require_decimal(p["qty"], "position.qty"),
                avg_entry_price=_require_decimal(p["avg_entry_price"], "position.avg_entry_price"),
                last_price=_decimal(p["last_price"]),
                market_value=_decimal(p["market_value"]),
                unrealized_pnl=_decimal(p["unrealized_pnl"]),
                unrealized_pnl_pct=_decimal(p["unrealized_pnl_pct"]),
                realized_pnl=_require_decimal(p["realized_pnl"], "position.realized_pnl"),
                fees_paid=_require_decimal(p["fees_paid"], "position.fees_paid"),
                stop_loss_price=_decimal(p["stop_loss_price"]),
                take_profit_price=_decimal(p["take_profit_price"]),
                distance_to_stop_pct=_decimal(p["distance_to_stop_pct"]),
                strategy_id=p["strategy_id"],
                opened_at=_parse_ts(p["opened_at"], "position.opened_at"),
            )
            for p in payload["positions"]
        ),
        recent_signals=tuple(
            SignalSummary(
                id=s["id"],
                ts=_require_ts(s["ts"], "signal.ts"),
                strategy_id=s["strategy_id"],
                symbol=s["symbol"],
                action=s["action"],
                reason=s["reason"],
                indicators=dict(s["indicators"]),
                acted_on=bool(s["acted_on"]),
                rejection_reason=s["rejection_reason"],
                rejected_by=s.get("rejected_by"),
            )
            for s in payload["recent_signals"]
        ),
        working_orders=tuple(
            OrderSummary(
                id=o["id"],
                client_order_id=o["client_order_id"],
                ts=_parse_ts(o["ts"], "order.ts"),
                symbol=o["symbol"],
                side=o["side"],
                order_type=o["order_type"],
                qty=_require_decimal(o["qty"], "order.qty"),
                filled_qty=_require_decimal(o["filled_qty"], "order.filled_qty"),
                limit_price=_decimal(o["limit_price"]),
                stop_price=_decimal(o["stop_price"]),
                avg_fill_price=_decimal(o["avg_fill_price"]),
                status=o["status"],
                strategy_id=o["strategy_id"],
            )
            for o in payload["working_orders"]
        ),
        strategy=payload.get("strategy"),
        last_data_at=_parse_ts(payload.get("last_data_at"), "last_data_at"),
        symbols=tuple(payload.get("symbols", ())),
    )
