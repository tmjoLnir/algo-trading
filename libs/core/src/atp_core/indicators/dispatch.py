"""Indicator lookup by name, for callers that hold bars rather than arrays.

`ta.py` is arrays in, floats out — the right shape for maths and the wrong one
for a rule spec, which names an indicator in a string and hands over a list of
`Bar`. This is the thin layer between them.

It exists in one place for the reason ADR 0006 gives: the backtest engine and
the live runner both resolve `"sma"` for a strategy, and a divergence between
them would mean a strategy trading on a different number live than the one it
was approved on. That is the single failure this platform's premise cannot
survive, and two copies of a dispatch table is how it happens — one gains
`vwap` and the other does not, or one seeds EMA differently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from atp_core.errors import StrategyError
from atp_core.indicators import ta

if TYPE_CHECKING:
    from atp_core.domain import Bar

#: Every name a rule spec may use. Listed rather than inferred so an
#: unrecognised one is an error at the call site instead of a silent None.
KNOWN_INDICATORS = frozenset({"sma", "ema", "rsi", "stddev", "atr"})

#: Indicators whose first value needs one bar more than their period.
#:
#: Wilder's smoothing seeds off *differences* between consecutive bars, so an
#: n-period average of them spans n+1 bars — `ta.rsi` and `ta.atr` both demand
#: `period + 1` where `sma`, `ema` and `stddev` demand `period`. Same fact,
#: stated where a caller sizing a window can read it.
_NEEDS_A_PRIOR_BAR = frozenset({"rsi", "atr"})


def min_bars(name: str, period: int) -> int:
    """The shortest series `compute` can answer for.

    A caller that sizes its own history window has to know this, and a caller
    that guesses `period` gets None back forever from `rsi` and `atr` — not
    during warmup, but for the whole run, because a window cut to exactly
    `period` never grows. That failure is silent: the rule simply never fires,
    and an empty result looks like a strategy that never signalled.

    Lives here rather than in the caller for the reason at the top of this
    module: `ta`'s minimum lengths are one fact, and a second copy of it is how
    the backtest and the live runner come to disagree.
    """
    if name not in KNOWN_INDICATORS:
        raise StrategyError(f"unknown indicator {name!r}")
    return period + 1 if name in _NEEDS_A_PRIOR_BAR else period


def compute(name: str, bars: list[Bar], period: int) -> float | None:
    """Dispatch a name from a rule spec onto `indicators.ta`.

    Returns None when the series is too short, which is the ordinary state
    during warmup — the alternative is every rule raising for the first fifty
    bars of every run. An unknown *name*, by contrast, raises: it is a
    malformed spec rather than a series that has not filled up yet, and
    returning None for it would let a typo read as "no signal yet" forever.
    """
    closes = np.array([float(b.close) for b in bars], dtype=float)
    try:
        if name == "sma":
            return ta.sma(closes, period)
        if name == "ema":
            return ta.ema(closes, period)
        if name == "rsi":
            return ta.rsi(closes, period)
        if name == "stddev":
            return ta.stddev(closes, period)
        if name == "atr":
            highs = np.array([float(b.high) for b in bars], dtype=float)
            lows = np.array([float(b.low) for b in bars], dtype=float)
            return ta.atr(highs, lows, closes, period)
    except ValueError:
        return None  # not enough history yet
    raise StrategyError(f"unknown indicator {name!r}")
