"""When a resting order touches a bar, and at what price.

This is one rule with two callers — `backtest.engine.BacktestEngine` and
`brokers.simulated.SimulatedBroker` — and it lives here rather than in either of
them because two implementations of it are two chances to get it subtly wrong.
That is the same reasoning the indicators module states for making the series
form the primitive and the scalar its last element, and it matters more here: a
simulator that fills on a bar the engine would not have filled makes a paper run
and the backtest that preceded it incomparable, which is the one thing a paper
run is for.

The rule itself is conservative in a specific direction — it never invents a
price the bar did not trade at:

- **Market** fills at the open. Not the close: an order decided on a bar's close
  fills on the *next* bar, and the open is that bar's first observable price.
- **Limit** fills only if the bar's range actually reached the limit, and takes
  the better of the open and the limit. A bar that opened through our limit
  filled at the open; we would not pay the limit for something offered cheaper.
- **Stop** triggers on the extreme and fills at the worse of the open and the
  stop. A stop becomes a market order in a moving market, so the fill is
  routinely worse than the trigger, and a gap through it fills at the open.

What this deliberately does *not* model is queue position. A limit touched
exactly at the bar's extreme is treated as filled, which is optimistic: in
reality you were last in the queue and may not have traded. `require_through`
takes the other side of that — it demands the bar trade strictly through the
price — and exists because the honest answer for a thin book is that we do not
know, so both readings should be available to whoever is calibrating against
real fills.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atp_core.domain.enums import OrderType, Side
from atp_core.errors import ExecutionError

if TYPE_CHECKING:
    from decimal import Decimal

    from atp_core.domain import Bar, Order


def intended_price(order: Order, bar: Bar, *, require_through: bool = False) -> Decimal | None:
    """The price `order` would touch on `bar`, or None if it would not.

    "Intended" because slippage has not been applied yet: this is the price the
    order reached, and what it actually pays is that price plus whatever the
    cost model charges for crossing. Keeping the two apart is what lets a
    simulator and a backtest share the touch rule while pricing the crossing
    differently.

    `require_through` makes a limit or stop that the bar only *touched* — high
    or low exactly equal to the price — count as untouched. Off by default so
    the engine's long-standing behaviour is unchanged; a caller modelling a
    thin book should turn it on and expect fewer fills.

    Raises `ExecutionError` for an order type nothing here models, rather than
    returning None. None means "the market did not reach it", and a caller that
    could not tell that apart from "we cannot price this" would silently expire
    every trailing stop it was handed.
    """
    if order.order_type is OrderType.MARKET:
        return bar.open

    if order.order_type is OrderType.LIMIT:
        limit = order.limit_price
        if limit is None:  # pragma: no cover — Order.__post_init__ rejects this
            raise ExecutionError(f"limit order {order.id} has no limit_price")
        if order.side is Side.BUY:
            return (
                min(bar.open, limit)
                if _reached(bar.low, limit, require_through, up=False)
                else None
            )
        return max(bar.open, limit) if _reached(bar.high, limit, require_through, up=True) else None

    if order.order_type is OrderType.STOP:
        stop = order.stop_price
        if stop is None:  # pragma: no cover — Order.__post_init__ rejects this
            raise ExecutionError(f"stop order {order.id} has no stop_price")
        if order.side is Side.BUY:
            return (
                max(bar.open, stop) if _reached(bar.high, stop, require_through, up=True) else None
            )
        return min(bar.open, stop) if _reached(bar.low, stop, require_through, up=False) else None

    raise ExecutionError(f"{order.order_type} is not modelled by the fill simulator")


def _reached(extreme: Decimal, price: Decimal, require_through: bool, *, up: bool) -> bool:
    """Did the bar's `extreme` reach `price`?

    `up` says which way the extreme has to travel: a buy stop and a sell limit
    are reached from below by the bar's high, a sell stop and a buy limit from
    above by its low.
    """
    if up:
        return extreme > price if require_through else extreme >= price
    return extreme < price if require_through else extreme <= price
