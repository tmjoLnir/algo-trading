"""Reference rule set: RSI mean reversion, with the trend filter that makes it one.

The declarative counterpart of `sma_crossover.py`, and the worked example in
`docs/STRATEGY_AUTHORING.md` — shipped verbatim, so that the page and the file
cannot drift into disagreeing about what a rule set looks like.

Buy when RSI(14) drops under 30 *while price is above its 200-day average*; sell
when RSI recovers past 55. The second half of that entry is the whole strategy:

    close > SMA(200)

Without it this is "buy whatever has fallen hardest", which in a downtrend is
buying things on their way to zero — the mistake `docs/STRATEGY_AUTHORING.md`
lists under *Common mistakes*, and the one a backtest on a survivorship-biased
universe is least likely to show you, since the companies it would have bought
into oblivion are the ones missing from the index today.

Kept as **YAML rather than a dict literal**, and parsed on the way out. A rule
set is a document a person edits — in the UI, or by pasting one of these — so
the thing under review here is the document, not a Python transcription of it
that could be valid where the document is not. Parsing it is also the only test
of the YAML path that the UI's own save actually takes.
"""

from __future__ import annotations

import yaml

from atp_core.strategy.rules import RuleSet

#: The spec, exactly as `docs/STRATEGY_AUTHORING.md` prints it.
RSI_MEAN_REVERSION_YAML = """
name: rsi_mean_reversion
description: Buy oversold in an uptrend; sell on the bounce
universe: [SPY, QQQ, IWM]
timeframe: 1d

entry_long:
  all:
    - {left: {indicator: rsi, period: 14}, op: "<", right: {value: 30}}
    - {left: {price: close}, op: ">", right: {indicator: sma, period: 200}}

exit:
  any:
    - {left: {indicator: rsi, period: 14}, op: ">", right: {value: 55}}

risk:
  stop_loss:   {type: atr, multiplier: 2.0, period: 14}
  take_profit: {type: fixed_pct, value: 0.06}
  position_size: {type: risk_pct, value: 0.01}

max_concurrent_positions: 3
cooldown_bars: 5
"""


def rsi_mean_reversion() -> RuleSet:
    """A freshly parsed copy of the shipped spec.

    A function rather than a module-level constant, and the reason is ownership:
    `compile_ruleset` hands the spec to the strategy it builds and the strategy
    keeps it, so a single shared `RuleSet` would be held by every compiled copy
    at once. `RuleSet` is not frozen — one caller adjusting a universe before a
    run would silently adjust every other run in the process. A parse costs
    microseconds and the shared-mutable class of bug does not announce itself.
    """
    return RuleSet.model_validate(yaml.safe_load(RSI_MEAN_REVERSION_YAML))
