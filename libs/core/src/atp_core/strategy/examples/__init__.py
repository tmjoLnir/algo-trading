"""Reference strategies.

Imported for the side effect: each module's `@register` decorator only runs
when the module is imported, so a fresh process that has not touched them has
an empty registry — and `registry.get("sma_crossover")` raises "unknown
strategy" for one that is very much present. Anything resolving a strategy by
name imports this package first.
"""

from atp_core.strategy.examples.rsi_mean_reversion import (
    RSI_MEAN_REVERSION_YAML,
    rsi_mean_reversion,
)
from atp_core.strategy.examples.sma_crossover import SmaCrossover

#: `rsi_mean_reversion` is exported for reach, not for registration: a rule set
#: is a document rather than a class, and `compile_ruleset` deliberately does
#: not register what it builds — see its docstring for why.
__all__ = ["RSI_MEAN_REVERSION_YAML", "SmaCrossover", "rsi_mean_reversion"]
