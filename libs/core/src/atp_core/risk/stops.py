"""Stop-loss management — the core of requirement #3.

Two placement strategies, and the choice matters more than it looks:

**Broker-side (preferred for live).** Submit a real stop order to the venue when
the position opens. It survives our process dying, a network partition, and a
deploy. This is the only kind of stop that protects you at 3am when the worker
has crashed.

**Engine-side.** We watch prices and submit a market order when the level trades.
Necessary for logic the venue cannot express (ATR trailing, time stops), but it
protects nothing if the platform is down. Never rely on it alone in live.

Default: broker-side stop for the initial protective level, engine-side logic
layered on top to *tighten* it. Never widen a stop — moving a stop away from
price to avoid being hit is how a small loss becomes an account.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.domain.enums import Side, StopType

if TYPE_CHECKING:
    from atp_core.domain import Bar, Position


@dataclass(frozen=True, slots=True)
class StopConfig:
    stop_type: StopType
    value: Decimal | None = None
    multiplier: Decimal | None = None
    period: int = 14
    bars: int | None = None
    broker_side: bool = True


class StopManager:
    """Computes and maintains protective levels for open positions."""

    def initial_stop(
        self, entry_price: Decimal, side: Side, config: StopConfig, atr_value: Decimal | None = None
    ) -> Decimal:
        """The stop level at entry.

        Long stops sit below entry, short stops above — sign errors here are
        catastrophic and silent, so this is heavily unit-tested for both sides.
        """
        raise NotImplementedError

    def update_trailing(
        self, position: Position, bar: Bar, config: StopConfig, atr_value: Decimal | None = None
    ) -> Decimal | None:
        """Ratchet a trailing stop; return the new level or None if unchanged.

        MUST be monotonic: for a long, the stop only ever rises. Track
        `position.high_water_mark` off bar highs, not closes — otherwise an
        intraday spike that should have locked in gains is invisible.
        """
        raise NotImplementedError

    def should_trigger(self, position: Position, bar: Bar) -> bool:
        """Did this bar trade through the stop?

        Compare against the bar's LOW for a long and HIGH for a short, never the
        close. A bar that dipped to the stop and recovered did hit it in
        reality; using the close pretends you were never stopped out and
        inflates every backtest that uses stops.

        When both the stop and the take-profit are inside one bar's range, the
        bar alone cannot say which came first. Assume the stop filled — the
        pessimistic reading is the only honest one at bar resolution. Use
        intrabar data if you need the truth.
        """
        raise NotImplementedError

    def take_profit_level(
        self, entry_price: Decimal, side: Side, config: StopConfig
    ) -> Decimal | None:
        raise NotImplementedError
