"""Reference strategies, and the reference rule set.

Imported for the side effect: each module's `@register` decorator only runs
when the module is imported, so a fresh process that has not touched them has
an empty registry — and `registry.get("sma_crossover")` raises "unknown
strategy" for one that is very much present. Anything resolving a strategy by
name imports this package first.

`rsi_mean_reversion` is the exception that proves the rule. It is a rule set
rather than a class, so there is no decorator and nothing to register — it is
exported for reach alone, and `compile_ruleset`'s docstring says why what it
builds stays out of the registry.
"""

from atp_core.strategy.examples.buy_and_hold import BuyAndHold
from atp_core.strategy.examples.rsi_mean_reversion import (
    RSI_MEAN_REVERSION_YAML,
    rsi_mean_reversion,
)
from atp_core.strategy.examples.sma_crossover import SmaCrossover

__all__ = ["RSI_MEAN_REVERSION_YAML", "BuyAndHold", "SmaCrossover", "rsi_mean_reversion"]
