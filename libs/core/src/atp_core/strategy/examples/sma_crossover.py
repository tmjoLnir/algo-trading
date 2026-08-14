"""Reference strategy: SMA crossover.

Deliberately simple and deliberately complete — it is the template for every
hand-written strategy, and the fixture the backtest engine's own tests run
against. Note what it does NOT do: no sizing, no broker calls, no clock reads.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from atp_core.domain import Bar, Signal, SignalAction, Timeframe
from atp_core.errors import StrategyError
from atp_core.indicators import ta
from atp_core.strategy.base import Strategy
from atp_core.strategy.registry import register

if TYPE_CHECKING:
    from atp_core.strategy.context import StrategyContext


@register
class SmaCrossover(Strategy):
    """Go long when the fast SMA crosses above the slow SMA; exit on the reverse.

    A crossover is a *transition*, not a state: comparing only the current bar
    would re-fire an entry on every bar while fast stays above slow. We check
    the previous bar's relationship too.
    """

    name: ClassVar[str] = "sma_crossover"
    description: ClassVar[str] = "Long when fast SMA crosses above slow SMA"
    params_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "fast_period": {"type": "integer", "minimum": 2, "default": 20},
            "slow_period": {"type": "integer", "minimum": 3, "default": 50},
            "timeframe": {"type": "string", "default": "1d"},
        },
        "required": ["fast_period", "slow_period"],
    }

    def validate_params(self) -> None:
        fast = int(self.params.get("fast_period", 20))
        slow = int(self.params.get("slow_period", 50))
        if fast >= slow:
            raise StrategyError(f"fast_period ({fast}) must be < slow_period ({slow})")

    @property
    def warmup_bars(self) -> int:
        # +1 because we compare against the previous bar to detect the crossing.
        return int(self.params.get("slow_period", 50)) + 1

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> list[Signal]:
        fast_n = int(self.params.get("fast_period", 20))
        slow_n = int(self.params.get("slow_period", 50))
        tf = Timeframe(self.params.get("timeframe", "1d"))

        closes = ctx.closes(bar.symbol, tf, slow_n + 1)
        if len(closes) < slow_n + 1:
            return []

        fast_now, fast_prev = ta.sma(closes, fast_n), ta.sma(closes[:-1], fast_n)
        slow_now, slow_prev = ta.sma(closes, slow_n), ta.sma(closes[:-1], slow_n)

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now
        position = ctx.position(bar.symbol)

        indicators = {"sma_fast": fast_now, "sma_slow": slow_now, "close": float(bar.close)}

        if crossed_up and position.is_flat:
            return [
                Signal(
                    strategy_id=self.name,
                    symbol=bar.symbol,
                    action=SignalAction.ENTER_LONG,
                    ts=ctx.now,
                    strength=Decimal(1),
                    reason=f"SMA({fast_n})={fast_now:.2f} crossed above SMA({slow_n})={slow_now:.2f}",
                    indicators=indicators,
                )
            ]

        if crossed_down and position.is_long:
            return [
                Signal(
                    strategy_id=self.name,
                    symbol=bar.symbol,
                    action=SignalAction.EXIT,
                    ts=ctx.now,
                    reason=f"SMA({fast_n})={fast_now:.2f} crossed below SMA({slow_n})={slow_now:.2f}",
                    indicators=indicators,
                )
            ]

        return []
