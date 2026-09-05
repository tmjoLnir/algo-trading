"""Corporate actions, as the adjusted price series reveals them.

A split or a dividend does not announce itself in this platform. What it does is
change every historical *adjusted* close for the symbol, at the vendor, all at
once — so a series we fetched last week and a series we fetch this morning
disagree by one constant factor across every bar they share. That disagreement
is the evidence, and it is available from the one provider method this codebase
already has, already tests, and already trusts.

**Deliberately not an Alpaca corporate-actions client.** That endpoint exists and
would name the action outright, which is strictly better information. It is not
here because its response shape cannot be verified from inside this repository —
`tests/` reaches no network (CLAUDE.md §1.7) — and an adapter written against a
remembered API shape is a thing that passes every test and fails on the first
real split. `get_bars(adjusted=True)` is exercised against the real endpoint by
every backfill this platform has ever run. Detection by comparison is less
informative and more certain, and the trade is worth making until somebody can
run the other one against a live account and see the payload.

What this module is *for* is bounded, and the boundary matters: it reports what
moved. It does not touch a position. `Reconciler.adopt_broker_state` is
"deliberately NOT automatic" because silently adopting the venue's numbers hides
whatever caused the drift, and a split is exactly the case that looks harmless
and shares a shape with a duplicate-submission bug. So the reconciler still
halts on the quantity mismatch a split produces — and the point of running this
before the open is that the halt is then *expected and explained* rather than a
mystery an operator meets at 09:45.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from atp_core.domain import Bar

#: How far a ratio may sit from 1 and still be called "unchanged". Vendors round
#: adjusted closes to the cent, so two fetches of an unadjusted series differ in
#: the last place on low-priced names; this is wide enough to absorb that and far
#: narrower than any real dividend.
UNCHANGED_TOLERANCE = Decimal("0.0005")

#: How far two bars' ratios may differ and still be called the same factor. One
#: corporate action moves every historical bar by one number, so a series where
#: the ratios disagree is not one action — it is a vendor restatement, a symbol
#: that changed meaning, or bars that were wrong before.
AGREEMENT_TOLERANCE = Decimal("0.005")

#: Above this, a factor is split-shaped and worth waking somebody for: it will
#: move a position's apparent value by more than a fifth. Below it, the move is
#: dividend-shaped — real, worth storing, not worth an alert at 08:30. The line
#: is about *who needs telling*, not about what gets corrected: every detected
#: change is written back either way.
SPLIT_LIKE_MOVE = Decimal("0.20")

#: Pairs of bars that must agree before a factor is believed at all. One bar
#: agreeing with itself is not evidence, and a two-bar window would call a single
#: mis-stored close a corporate action.
MIN_BARS_COMPARED = 3


@dataclass(frozen=True, slots=True)
class Adjustment:
    """What the two fetches of one symbol's history disagree about.

    `factor` is *stored ÷ fresh*, so it reads the way the action does: a 4:1
    split divides every historical adjusted close by four, so the stored series
    is four times the fresh one and the factor is 4. A position of 100 shares
    becomes 400.
    """

    symbol: str
    factor: Decimal
    bars_compared: int
    bars_agreeing: int
    #: The oldest bar whose adjusted close moved. A corporate action restates
    #: the whole history, so this is normally the start of the window rather
    #: than the action's own date — it says how far back the change reaches,
    #: not when it happened.
    earliest_moved_at: datetime | None

    @property
    def is_consistent(self) -> bool:
        """Whether one factor explains every bar that moved.

        False is the interesting case and must not be treated as a smaller
        version of True: it means the history changed in a way a single
        corporate action cannot account for, which is a data-quality incident
        rather than a split. The caller still refreshes the series — the fresh
        figures are the vendor's current truth either way — and says so loudly
        instead of naming a factor nothing supports.
        """
        return self.bars_compared >= MIN_BARS_COMPARED and self.bars_agreeing == self.bars_compared

    @property
    def is_split_like(self) -> bool:
        """Whether this is large enough that a human should hear about it now."""
        return abs(self.factor - Decimal(1)) >= SPLIT_LIKE_MOVE

    @property
    def implied_position_factor(self) -> Decimal:
        """What a held quantity would be multiplied by at the venue.

        The same number, named separately because it is the one an operator
        checks against the broker's position — and because reading a share count
        off a price ratio is the step where getting the direction backwards
        costs money. Prices go *down* by the factor, quantities go *up*.
        """
        return self.factor


def detect_adjustment(
    symbol: str, stored: Sequence[Bar], fresh: Sequence[Bar]
) -> Adjustment | None:
    """Compare two fetches of one symbol's adjusted history.

    Returns None when nothing moved — which is every symbol on almost every day,
    and is the answer this is mostly written to produce cheaply.

    Bars are paired on `ts`, so a window that grew or lost bars between the two
    fetches costs only the pairs it cannot make. Only pairs where **both** sides
    carry an `adj_close` are compared: a stored bar written by a raw-only fetch
    has none, and treating a missing adjusted close as a change would report a
    corporate action every time the nightly sweep had not yet run.

    A zero or negative adjusted close is skipped rather than divided by. It
    should not occur, and a `DivisionByZero` inside a pre-open job that nothing
    is watching is a worse way to find out than a bar count that does not add up.
    """
    fresh_by_ts = {bar.ts: bar for bar in fresh}

    ratios: list[tuple[datetime, Decimal]] = []
    for old in stored:
        new = fresh_by_ts.get(old.ts)
        if new is None or old.adj_close is None or new.adj_close is None:
            continue
        if old.adj_close <= 0 or new.adj_close <= 0:
            continue
        ratios.append((old.ts, old.adj_close / new.adj_close))

    moved = [(ts, ratio) for ts, ratio in ratios if abs(ratio - Decimal(1)) > UNCHANGED_TOLERANCE]
    if not moved:
        return None

    # The median of the bars that *moved*, rather than the mean of everything:
    # one bar restated on its own would drag an average away from the factor
    # every other bar agrees on, and whether the bars agree is the whole
    # question here.
    ordered = sorted(ratio for _, ratio in moved)
    factor = ordered[len(ordered) // 2]

    # Counted over **every** compared bar and not only the ones that moved. A
    # corporate action restates the whole history, so a series where half the
    # bars moved by 4 and half did not move at all is precisely the
    # inconsistency worth refusing to name: the unmoved bars sit at a ratio of
    # 1, nowhere near a factor of 4, and correctly count as disagreeing.
    agreeing = sum(1 for _, ratio in ratios if abs(ratio - factor) <= AGREEMENT_TOLERANCE)

    return Adjustment(
        symbol=symbol,
        factor=factor,
        bars_compared=len(ratios),
        bars_agreeing=agreeing,
        earliest_moved_at=min(ts for ts, _ in moved),
    )
