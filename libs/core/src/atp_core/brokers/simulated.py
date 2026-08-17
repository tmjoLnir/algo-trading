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

Three things this file refuses to pretend about, each recorded because the
tempting version is the flattering one:

- **It does not invent a touch rule.** When a resting order fills, and at what
  price, is `execution.matching.intended_price` — the same function
  `BacktestEngine` calls. A simulator with its own copy could fill on a bar the
  engine would not, and then a paper run and the backtest that preceded it are
  no longer comparable, which is the entire reason to paper trade first.
- **It does not model latency at bar granularity.** `latency_ms` is 50ms and a
  bar is at minimum 60,000ms, so at bar resolution latency is below the
  resolution of the data and any attempt to model it there is theatre that
  moves fills by a whole bar. It is applied in `on_quote`, where it is
  meaningful, and `on_bar` instead reproduces the engine's rule exactly: an
  order rests for one bar and fills against the next.
- **It does not re-check buying power.** `RiskEngine.BuyingPowerRule` already
  ran — every order reaching a broker has passed the chain (rule §1.5). A
  second implementation here would be a second opinion, and the failure mode is
  that it refuses in paper what the platform approves in live, which is the one
  disagreement that makes paper trading actively misleading. Cash is tracked so
  `get_account` is real; it is not a veto.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.brokers.ports import AccountSnapshot
from atp_core.domain import Fill, Order, OrderStatus, OrderType, Position, Side, TimeInForce
from atp_core.errors import BrokerError, ExecutionError
from atp_core.execution.matching import intended_price
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.backtest.costs import CostModel
    from atp_core.clock import Clock
    from atp_core.domain import Bar, Quote

log = get_logger(__name__)


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

    #: Reject fills where our order exceeds this share of the bar's volume.
    #: Same default and same reasoning as `BacktestConfig`: without it a run
    #: happily "buys" 10× a small-cap's turnover.
    max_volume_participation: Decimal = Decimal("0.10")

    #: Require a limit or stop to trade *through* its price rather than merely
    #: touch it. Off by default, matching the engine. A touch at the bar's
    #: extreme means you were last in the queue and may not have traded at all,
    #: so turning this on is the pessimistic reading of a thin book.
    require_through: bool = False

    #: client_order_id → broker_order_id, so idempotency and lookup can be
    #: keyed by the two different ids the port uses for them.
    _by_client_id: dict[str, str] = field(init=False, default_factory=dict)

    _next_id: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._cash = self.starting_cash

    @property
    def name(self) -> str:
        return "simulated"

    @property
    def supports_fractional(self) -> bool:
        return True

    # ── BrokerPort ──────────────────────────────────────────────────────────

    async def get_account(self) -> AccountSnapshot:
        """Cash, and cash plus marks. There is no margin here.

        `buying_power` equals cash rather than some multiple of it: this
        simulator models a cash account, and reporting margin it does not
        extend would let a strategy size against leverage that will not be
        there when the same code meets a real venue.
        """
        equity = self._cash + sum((p.market_value for p in self._positions.values()), Decimal(0))
        return AccountSnapshot(
            account_id="simulated",
            equity=equity,
            cash=self._cash,
            buying_power=self._cash,
            maintenance_margin=Decimal(0),
            is_pattern_day_trader=False,
            trading_blocked=False,
            as_of=self.clock.now(),
        )

    async def submit_order(self, order: Order) -> Order:
        """Accept and rest the order. Fills happen in `on_bar` / `on_quote`.

        Idempotent on `client_order_id`, as the port requires: a resubmit of a
        key this broker already holds returns what it holds rather than opening
        a second order. That is not a formality — it is the behaviour the
        router's timeout path depends on to avoid a duplicate position.

        Returns a *copy*. A venue holds its own record of an order, and an
        adapter that handed back the caller's object would make reconciliation
        compare our book against itself and always agree.
        """
        known = self._by_client_id.get(order.client_order_id)
        if known is not None:
            held = self._open_orders.get(known)
            if held is None:
                held = next(o for o in self._filled if o.broker_order_id == known)
            log.info(
                "simulated.duplicate_submit",
                client_order_id=order.client_order_id,
                broker_order_id=known,
            )
            return replace(held, fills=list(held.fills))

        self._next_id += 1
        broker_order_id = f"sim-{self._next_id}"
        accepted = replace(
            order,
            broker_order_id=broker_order_id,
            status=OrderStatus.SUBMITTED,
            submitted_at=self.clock.now(),
            fills=[],
        )
        self._open_orders[broker_order_id] = accepted
        self._by_client_id[order.client_order_id] = broker_order_id
        log.debug(
            "simulated.accepted",
            symbol=order.symbol,
            side=order.side.value,
            qty=str(order.qty),
            broker_order_id=broker_order_id,
        )
        return replace(accepted, fills=[])

    async def cancel_order(self, broker_order_id: str) -> None:
        """Cancel. Cancelling an already-filled order is not an error — it is a
        race we lost, and the fill stands (see `BrokerPort`)."""
        order = self._open_orders.pop(broker_order_id, None)
        if order is None:
            return
        order.status = OrderStatus.CANCELLED
        self._filled.append(order)

    async def get_order(self, broker_order_id: str) -> Order | None:
        order = self._open_orders.get(broker_order_id)
        if order is None:
            order = next((o for o in self._filled if o.broker_order_id == broker_order_id), None)
        return None if order is None else replace(order, fills=list(order.fills))

    async def get_open_orders(self) -> list[Order]:
        return [replace(o, fills=list(o.fills)) for o in self._open_orders.values()]

    async def get_positions(self) -> list[Position]:
        return [replace(p) for p in self._positions.values() if not p.is_flat]

    async def close_position(self, symbol: str) -> Order:
        """Flatten one symbol at market.

        The order rests like any other and fills on the next bar. A venue that
        closed the position instantly at a price of its own choosing would be
        the flattering lie this whole file exists to avoid.
        """
        position = self._positions.get(symbol)
        if position is None or position.is_flat:
            raise BrokerError(f"no open position in {symbol} to close")
        return await self.submit_order(
            Order(
                symbol=symbol,
                side=Side.SELL if position.is_long else Side.BUY,
                qty=abs(position.qty),
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                created_at=self.clock.now(),
            )
        )

    async def close_all_positions(self) -> list[Order]:
        """Emergency flatten. See docs/RUNBOOK.md."""
        return [
            await self.close_position(p.symbol)
            for p in list(self._positions.values())
            if not p.is_flat
        ]

    async def is_market_open(self) -> bool:
        """Always true.

        This broker has no calendar and should not grow one: it fills whatever
        bars it is handed, and the caller feeding it bars is the thing that
        knows whether those bars are inside a session. A calendar here would
        also disagree with `SimulatedClock` in a backtest, where "now" is
        wherever the engine has moved it to.
        """
        return True

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

        The touch rule itself is `execution.matching.intended_price`, shared
        with `BacktestEngine`; what this method adds is the one-bar rest, the
        volume cap, the stop-before-target ordering and the book-keeping.
        """
        candidates = [
            order
            for order in self._open_orders.values()
            if order.symbol == bar.symbol and self._may_fill_on(order, bar)
        ]

        filled: list[Order] = []
        for order in self._stop_first(candidates):
            price = intended_price(order, bar, require_through=self.require_through)
            if price is None:
                # Never touched. A DAY order dies at the session's end rather
                # than resting forever and filling on some unrelated later bar.
                if order.time_in_force is TimeInForce.DAY:
                    self._retire(order, OrderStatus.EXPIRED)
                continue

            slippage = self.cost_model.slippage(order, bar, price)
            fill_price = price + slippage
            if fill_price <= 0:
                raise BrokerError(f"slippage produced a non-positive fill price {fill_price}")

            qty = min(order.remaining_qty, self.max_volume_participation * bar.volume)
            if qty <= 0:
                # Nothing traded, or our order dwarfs what did. Refused rather
                # than pretended: a fill against a bar with no volume describes
                # a market that was not there.
                continue

            self._book(order, qty, fill_price, bar.ts)
            filled.append(replace(order, fills=list(order.fills)))

            if order.remaining_qty > 0 and order.time_in_force is TimeInForce.DAY:
                self._retire(order, OrderStatus.EXPIRED)
            elif order.remaining_qty == 0:
                self._retire(order, OrderStatus.FILLED)

        return filled

    def on_quote(self, quote: Quote) -> list[Order]:
        """Quote-driven fills, for tick-level simulation.

        This is where `latency_ms` becomes meaningful: an order is eligible
        only once `quote.ts` is at least that far past its acceptance, so a
        strategy reacting to a quote cannot also trade on it. At bar
        granularity that check is below the resolution of the data; here it is
        the difference between a plausible tick simulation and a time machine.

        Crossing the spread **is** the slippage, so the cost model's slippage
        term is deliberately not applied on top — it estimates a spread from
        bar data, and here the actual spread is known. Commission still
        applies. Size is capped at the resting size on the far side of the
        book, because that is what was actually offered.
        """
        eligible_before = quote.ts.timestamp() - self.latency_ms / 1000

        filled: list[Order] = []
        candidates = [o for o in self._open_orders.values() if o.symbol == quote.symbol]
        for order in self._stop_first(candidates):
            if order.submitted_at is None or order.submitted_at.timestamp() > eligible_before:
                continue

            price = self._quote_price(order, quote)
            if price is None:
                continue

            available = quote.ask_size if order.side is Side.BUY else quote.bid_size
            qty = min(order.remaining_qty, available) if available > 0 else order.remaining_qty
            if qty <= 0:
                continue

            self._book(order, qty, price, quote.ts)
            filled.append(replace(order, fills=list(order.fills)))
            if order.remaining_qty == 0:
                self._retire(order, OrderStatus.FILLED)

        return filled

    # ── internals ───────────────────────────────────────────────────────────

    @staticmethod
    def _may_fill_on(order: Order, bar: Bar) -> bool:
        """Is this bar's price one the order could actually have got?

        Only if the bar **opened at or after the order was submitted**. That
        single comparison is the one-bar rest, and it is a statement about
        reality rather than a bookkeeping convention: you cannot be filled at
        the open of a bar that had already opened when you placed the order.

        It reproduces the engine exactly on a contiguous series, because the
        engine parks its clock at each bar's *close* before the strategy
        decides — so an order decided on bar N carries N's close as its
        `submitted_at`, which is precisely bar N+1's open. Bar N+1 therefore
        fills it and bar N, already in the past, does not. Filling at the
        signal bar's own price is what docs/BACKTESTING.md names as the single
        most common way a backtest overstates returns.

        An order with no `submitted_at` never fills. That is unreachable
        through `submit_order`, which always stamps one, and refusing is the
        right way to fail if some future path forgets: the alternative default
        is "eligible on every bar including ones from before it existed".
        """
        return order.submitted_at is not None and bar.ts >= order.submitted_at

    @staticmethod
    def _stop_first(orders: list[Order]) -> list[Order]:
        """Stops before anything else.

        When a bar's range spans both a stop and a take-profit the bar cannot
        say which came first, and `risk.stops.should_trigger` resolves that the
        same way: assume the stop. Filling the target first would report the
        winning exit on every ambiguous bar, which flatters every strategy that
        uses brackets.
        """
        return sorted(orders, key=lambda o: o.order_type is not OrderType.STOP)

    def _book(self, order: Order, qty: Decimal, price: Decimal, ts: datetime) -> None:
        """Apply one fill to the order, the position and cash."""
        fee = self.cost_model.commission(order, price, qty)
        fill = Fill(order_id=order.id, ts=ts, qty=qty, price=price, fee=fee)
        order.apply_fill(fill)

        position = self._positions.setdefault(order.symbol, Position(symbol=order.symbol))
        position.apply_fill(fill, qty * order.side.sign)
        self._cash -= qty * price * order.side.sign + fee

        log.debug(
            "simulated.fill",
            symbol=order.symbol,
            side=order.side.value,
            qty=str(qty),
            price=str(price),
            fee=str(fee),
        )

    def _retire(self, order: Order, status: OrderStatus) -> None:
        """Move an order out of the book.

        `Order.apply_fill` has already set FILLED where it applies, so this only
        assigns a status the fill accounting does not own.
        """
        if order.broker_order_id is not None:
            self._open_orders.pop(order.broker_order_id, None)
        if status is not OrderStatus.FILLED:
            order.status = status
        self._filled.append(order)

    @staticmethod
    def _quote_price(order: Order, quote: Quote) -> Decimal | None:
        """The price this order would take off the book, or None if it would not.

        A buy lifts the ask and a sell hits the bid — never the mid, which is a
        price nobody is offering. A stop triggers off the mid, because the
        trigger is about where the market *is* rather than about which side of
        the book we would then have to cross.
        """
        if order.order_type is OrderType.MARKET:
            return quote.ask if order.side is Side.BUY else quote.bid

        if order.order_type is OrderType.LIMIT:
            limit = order.limit_price
            if limit is None:  # pragma: no cover — Order.__post_init__ rejects this
                return None
            if order.side is Side.BUY:
                return quote.ask if quote.ask <= limit else None
            return quote.bid if quote.bid >= limit else None

        if order.order_type is OrderType.STOP:
            stop = order.stop_price
            if stop is None:  # pragma: no cover — Order.__post_init__ rejects this
                return None
            mid = quote.mid
            if order.side is Side.BUY:
                return quote.ask if mid >= stop else None
            return quote.bid if mid <= stop else None

        # `ExecutionError`, matching `execution.matching` rather than the
        # broker errors around it: this is the same "we cannot price this order
        # type" failure, and a caller should not have to catch two exception
        # types depending on whether it fed the simulator a bar or a quote.
        raise ExecutionError(f"{order.order_type} is not modelled by the quote simulator")
