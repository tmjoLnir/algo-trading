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

#: Stops that ratchet with price rather than sitting where they were placed.
TRAILING_TYPES = frozenset({StopType.TRAILING_PCT, StopType.CHANDELIER})

#: Stops whose level is a distance from the entry price, computable without a
#: price history — the only ones a take-profit can be expressed as.
FROM_ENTRY_TYPES = frozenset({StopType.FIXED_PCT, StopType.FIXED_AMOUNT})


@dataclass(frozen=True, slots=True)
class StopConfig:
    stop_type: StopType
    value: Decimal | None = None
    multiplier: Decimal | None = None
    period: int = 14
    bars: int | None = None
    broker_side: bool = True


def _require(value: Decimal | None, field: str, config: StopConfig) -> Decimal:
    if value is None:
        raise ValueError(f"{config.stop_type.value} stops need `{field}`")
    if value <= 0:
        raise ValueError(f"{config.stop_type.value} `{field}` must be positive, got {value}")
    return value


def _guard_level(level: Decimal, what: str) -> Decimal:
    """A protective level has to be a real price.

    A `fixed_pct` of 1.5 puts a long's stop at minus half the entry price, and a
    multiplier wide enough to swamp the entry does the same to an ATR stop —
    both configuration errors that would otherwise present as a position which
    can never be stopped out.

    Only positivity is checked, not which side of entry the level sits on. The
    side is fixed by the sign this module applies, not by the caller, and every
    input it multiplies is already required to be positive — so a wrong-side
    level is unreachable rather than merely unlikely, and a branch for it would
    be a comforting line nothing could ever execute.
    """
    if level <= 0:
        raise ValueError(f"{what} of {level} is not a price")
    return level


class StopManager:
    """Computes and maintains protective levels for open positions."""

    def initial_stop(
        self, entry_price: Decimal, side: Side, config: StopConfig, atr_value: Decimal | None = None
    ) -> Decimal:
        """The stop level at entry.

        Long stops sit below entry, short stops above — sign errors here are
        catastrophic and silent, so this is heavily unit-tested for both sides.

        A `chandelier` stop starts life identical to an `atr` one: its anchor is
        the highest high *since entry*, and at entry that is the entry bar. It
        diverges on the first `update_trailing`.

        A `time` stop has no price, so it is refused here rather than given a
        made-up level — see `time_exit_due`.
        """
        direction = Decimal(-1) if side is Side.BUY else Decimal(1)

        if config.stop_type is StopType.TIME:
            raise ValueError(
                "a time stop has no price level — it exits after a number of "
                "bars regardless of price. Use time_exit_due()"
            )

        if config.stop_type in (StopType.FIXED_PCT, StopType.TRAILING_PCT):
            # A trailing stop's opening level is a fixed percentage too; what
            # makes it trailing is that the anchor moves afterwards.
            pct = _require(config.value, "value", config)
            level = entry_price * (Decimal(1) + direction * pct)
        elif config.stop_type is StopType.FIXED_AMOUNT:
            amount = _require(config.value, "value", config)
            level = entry_price + direction * amount
        elif config.stop_type in (StopType.ATR, StopType.CHANDELIER):
            multiplier = _require(config.multiplier, "multiplier", config)
            if atr_value is None or atr_value <= 0:
                raise ValueError(
                    f"{config.stop_type.value} stops need a positive ATR, got {atr_value}"
                )
            level = entry_price + direction * multiplier * atr_value
        else:  # pragma: no cover - StopType is exhaustive above
            raise ValueError(f"unsupported stop type {config.stop_type}")

        return _guard_level(level, f"{config.stop_type.value} stop")

    def update_trailing(
        self, position: Position, bar: Bar, config: StopConfig, atr_value: Decimal | None = None
    ) -> Decimal | None:
        """Ratchet a trailing stop; return the new level or None if unchanged.

        MUST be monotonic: for a long, the stop only ever rises. Track
        `position.high_water_mark` off bar highs, not closes — otherwise an
        intraday spike that should have locked in gains is invisible.

        Mutates the position: the high-water mark and, when it ratchets, the
        stop. That is what "maintains protective levels" means, and returning a
        level the caller then had to remember to assign is how a stop ends up
        computed but never armed.

        Non-trailing stops return None. They are not broken, they are just not
        the kind of stop that moves — and moving one would be widening or
        tightening a level the strategy chose deliberately.
        """
        if config.stop_type not in TRAILING_TYPES:
            return None
        if position.is_flat:
            return None

        long = position.is_long
        extreme = bar.high if long else bar.low
        # Off the bar's extreme, not its close. A spike that should have
        # ratcheted the stop is invisible in closes, and the whole point of a
        # trailing stop is to keep what the move handed you.
        anchor = position.high_water_mark
        if anchor is None:
            anchor = (
                max(position.avg_entry_price, extreme)
                if long
                else min(position.avg_entry_price, extreme)
            )
        else:
            anchor = max(anchor, extreme) if long else min(anchor, extreme)
        position.high_water_mark = anchor

        direction = Decimal(-1) if long else Decimal(1)
        if config.stop_type is StopType.TRAILING_PCT:
            pct = _require(config.value, "value", config)
            candidate = anchor * (Decimal(1) + direction * pct)
        else:  # CHANDELIER
            multiplier = _require(config.multiplier, "multiplier", config)
            if atr_value is None or atr_value <= 0:
                raise ValueError(f"chandelier stops need a positive ATR, got {atr_value}")
            candidate = anchor + direction * multiplier * atr_value

        if candidate <= 0:
            return None

        current = position.stop_loss_price
        # The invariant the whole class exists for. Moving a stop away from
        # price to avoid being hit converts a planned small loss into an
        # unplanned large one, and it always feels justified at the time.
        if current is not None and (candidate <= current if long else candidate >= current):
            return None

        position.stop_loss_price = candidate
        return candidate

    def time_exit_due(self, bars_held: int, config: StopConfig) -> bool:
        """Whether a time stop has run out.

        Separate from the price stops because a time stop is not a level: it
        exits after `bars` regardless of where price is, which is the point for
        a mean-reversion strategy that has not reverted.
        """
        if config.stop_type is not StopType.TIME:
            return False
        if config.bars is None or config.bars <= 0:
            raise ValueError(f"time stops need a positive `bars`, got {config.bars}")
        return bars_held >= config.bars

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
        stop = position.stop_loss_price
        if stop is None or position.is_flat:
            return False
        return bar.low <= stop if position.is_long else bar.high >= stop

    def take_profit_level(
        self, entry_price: Decimal, side: Side, config: StopConfig
    ) -> Decimal | None:
        """The profit target, mirrored from entry.

        Only the fixed distances are expressible: a target has to be a level the
        position is aiming *at*, and a trailing or time rule describes when to
        leave rather than where. An ATR-derived target would need the ATR, which
        this signature has nowhere to take — refused rather than silently
        returning None, because a take-profit that quietly does not exist is a
        position with no upside exit.
        """
        if config.stop_type not in FROM_ENTRY_TYPES:
            raise ValueError(
                f"a take-profit must be a fixed distance from entry; "
                f"{config.stop_type.value} describes when to exit, not where. "
                f"Use {', '.join(sorted(t.value for t in FROM_ENTRY_TYPES))}"
            )

        # Mirrored: a target sits above entry for a long, below for a short —
        # the opposite side from the stop.
        direction = Decimal(1) if side is Side.BUY else Decimal(-1)
        value = _require(config.value, "value", config)
        level = (
            entry_price * (Decimal(1) + direction * value)
            if config.stop_type is StopType.FIXED_PCT
            else entry_price + direction * value
        )
        return _guard_level(level, "take-profit")
