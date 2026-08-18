"""The equity curve, thinned for a chart.

The runner records an equity point on every evaluation, which at the default
tick is one a minute: a month of them is around forty thousand points, for a
chart a few hundred pixels wide. Sending all of them would spend the bandwidth
and the browser's main thread to draw the same line.

The thinning rule is **last point in each bucket**, and it is the only one that
is honest here. Equity is a *level*, not a flow — averaging the minute points
inside an hour produces a number the account never held, and a chart of numbers
that never happened is worse than a coarse chart of numbers that did. The last
point is a real observation, at a real instant, and it is the one an operator
would compare against their memory of the close.

Pure, and separate from the endpoint that serves it, because "does this
downsample correctly at a bucket boundary" is a question worth answering
without HTTP in the way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from atp_core.execution.ports import EquityPoint

#: What a client may ask for, and how wide each bucket is. A closed set rather
#: than a parsed duration string: an open one invites `resolution=1s` over a
#: year, which is the request that returns half a million points and blames the
#: database.
RESOLUTIONS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}

#: Buckets are floored against a fixed instant rather than against the first
#: point in the series. Anchoring on the data would make the same day's chart
#: bucket differently depending on when the worker happened to start, so two
#: requests a minute apart would return points at different timestamps and the
#: line would appear to shift.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def resolve(resolution: str) -> timedelta:
    """Bucket width for a client-supplied resolution name.

    Raises `ValueError` naming what was allowed, which the API turns into a 422.
    Falling back to a default would answer a question nobody asked with a chart
    at a different scale than the axis label claims.
    """
    try:
        return RESOLUTIONS[resolution]
    except KeyError:
        raise ValueError(
            f"unknown resolution {resolution!r}; expected one of {', '.join(RESOLUTIONS)}"
        ) from None


def downsample(points: Iterable[EquityPoint], every: timedelta) -> list[EquityPoint]:
    """One point per `every`-wide bucket: the last observation in each.

    Input must be chronological — which is what `PortfolioRepository
    .equity_history` returns — and the output preserves that. The final point of
    the series always survives, because it is the last observation of its own
    bucket, so the chart's right-hand end is the newest equity rather than the
    end of the last *complete* bucket.
    """
    if every <= timedelta(0):
        raise ValueError(f"bucket width must be positive, got {every}")

    width = int(every.total_seconds())
    kept: dict[int, EquityPoint] = {}
    for point in points:
        bucket = int((point.ts - _EPOCH).total_seconds()) // width
        kept[bucket] = point  # later points overwrite earlier ones in the bucket
    return [kept[b] for b in sorted(kept)]


def default_resolution_for(days: int) -> str:
    """A bucket width that keeps a chart of `days` under a few hundred points.

    Offered so a caller that has not thought about it gets a sensible axis
    rather than a minute-resolution year. A caller that *has* thought about it
    passes its own and this is not consulted.
    """
    if days <= 2:
        return "5m"
    if days <= 14:
        return "1h"
    if days <= 90:
        return "4h"
    return "1d"


def last_before_or_at(points: Sequence[EquityPoint], ts: datetime) -> EquityPoint | None:
    """The newest point at or before `ts`, or None if the series starts later."""
    for point in reversed(points):
        if point.ts <= ts:
            return point
    return None
