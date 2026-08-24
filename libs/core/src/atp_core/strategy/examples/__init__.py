"""Reference strategies.

Imported for the side effect: each module's `@register` decorator only runs
when the module is imported, so a fresh process that has not touched them has
an empty registry — and `registry.get("sma_crossover")` raises "unknown
strategy" for one that is very much present. Anything resolving a strategy by
name imports this package first.
"""

from atp_core.strategy.examples.buy_and_hold import BuyAndHold
from atp_core.strategy.examples.sma_crossover import SmaCrossover

__all__ = ["BuyAndHold", "SmaCrossover"]
