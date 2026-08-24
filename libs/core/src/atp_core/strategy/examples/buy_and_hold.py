"""Reference strategy: buy and hold — the benchmark every other result is read against.

Not a strategy anybody trades. It exists because a return with nothing beside it
is not evidence: 18% over a year is skill against a flat market and a bad year
against one that returned 30%, and the number alone cannot tell you which
(docs/BACKTESTING.md, "Before believing a result"). Run this over the same bars,
the same costs and the same sizing as the strategy under test, and the
difference is the part the strategy is responsible for.

The rules are the whole of it: buy once, hold to the end, never sell. What takes
explaining is why those three sentences are not one line of code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from atp_core.domain import Bar, Signal, SignalAction
from atp_core.strategy.base import Strategy
from atp_core.strategy.registry import register

if TYPE_CHECKING:
    from atp_core.strategy.context import StrategyContext


@register
class BuyAndHold(Strategy):
    """Enter long once per symbol, at the first opportunity, and never exit.

    **It buys at the second bar's open, not the first bar's close, and that is
    correct.** The decision is taken on a bar that has closed and fills at the
    next bar's open, exactly as every other strategy's does — the engine
    enforces it and `docs/BACKTESTING.md` explains why. A benchmark exempted
    from that rule would be measured from a price nobody could have paid, and
    since it is the thing every strategy is compared *against*, flattering it by
    one bar's move would understate every strategy in the platform by the same
    amount. The benchmark has to be reachable or the comparison is not one.

    **Once per symbol, not "whenever flat".** The tempting version enters any
    time it holds nothing, which is one line shorter and is a different
    strategy: with a stop configured it becomes buy, get stopped out, buy again
    — a re-entry system whose results depend on the stop, which is the opposite
    of a fixed baseline. This one gets a single attempt per symbol and then
    stops having opinions.

    **Read off the position, not off its own signals.** A signal is a request:
    it fills a bar later, at a price that may never come, and risk or sizing can
    refuse it. Counting emitted signals would mean a restart re-entering a
    position it already holds and doubling it; observing the book means the
    first bar after a restart sees the open position and stands down. The
    residual case is a restart while flat *after* a stop closed the position,
    which would buy again — unreachable in the configuration a benchmark should
    actually run in, which is one with no stop at all.
    """

    name: ClassVar[str] = "buy_and_hold"
    description: ClassVar[str] = "Buy at the start of the run and hold to the end"
    #: No parameters, deliberately. Every knob is one more thing that could be
    #: tuned, and a benchmark that was tuned to the comparison is not a baseline
    #: — it is a second strategy with the first one's marking scheme.
    params_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        # `BacktestEngine.run` and `StrategyRunner.warmup` both call `on_start`
        # before the first bar, so this is belt and braces — but the failure it
        # removes is an `AttributeError` on bar 1 of whatever calls `on_bar`
        # without it, which is a poor way for a new driver to find out.
        self.on_start()

    @property
    def warmup_bars(self) -> int:
        """Nothing to warm up: it computes no indicator and reads no history.

        Zero rather than one. The engine discards signals while `seen <= warmup`,
        so zero means the first closed bar is decidable — which for a benchmark
        is the point, since every bar spent waiting is a bar of the market's
        return it did not capture and the strategy under test did.
        """
        return 0

    def on_start(self) -> None:
        """Forget which symbols have had their attempt (authoring rule 2).

        A runner may restart mid-session, and this set does not survive it. That
        is safe because `on_bar` re-derives the fact from the book rather than
        trusting the set: an open position re-marks the symbol before anything
        can decide to buy it again.
        """
        #: Symbols that have had their one entry attempt, or are already held.
        self._settled: set[str] = set()

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> list[Signal]:
        if not ctx.position(bar.symbol).is_flat:
            # Holding is the strategy. Marked here rather than at the signal, so
            # that a restart mid-run finds the position and does not buy it
            # twice.
            self._settled.add(bar.symbol)
            return []

        if bar.symbol in self._settled:
            return []

        self._settled.add(bar.symbol)
        return [
            Signal(
                strategy_id=self.name,
                symbol=bar.symbol,
                action=SignalAction.ENTER_LONG,
                ts=ctx.now,
                reason=f"buy and hold: first decidable bar for {bar.symbol}",
                # No stop and no target: a benchmark that could be stopped out
                # is not measuring the market's return any more. This is also
                # why `risk_pct` cannot size it — there is no distance to risk
                # against — and why a benchmark run wants `equity_pct` or
                # `fixed_qty`. Across a universe of n symbols, remember that
                # each gets a full-sized position.
                indicators={"close": float(bar.close)},
            )
        ]
