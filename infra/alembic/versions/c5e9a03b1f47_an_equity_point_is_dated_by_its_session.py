"""an equity point is dated by its session

`BacktestEngine.run` stamped every equity point with the clock, and the clock
stands at `ts + step` — the instant the bar's decision could first be taken.
That is the right instant for an *order* and the wrong label for a *point on a
curve*. For a daily bar the two differ by a calendar day: a daily bar is stamped
at exchange-local midnight, so `ts + 24h` is the next midnight rather than the
21:00 UTC close. A weekday histogram of a real 1,525-point curve had 304
Saturdays and zero Mondays.

The engine stamps a point at its bar's `ts` now (ADR 0018). This moves the rows
already on record to the same convention, because the alternative is one table
holding two of them with nothing in a row to say which — and a chart comparing
an old run against a new one silently comparing conventions rather than runs.

Unlike the `warnings` backfill (`a9f37c14e6b2`), which had nothing to backfill
*from* and correctly left NULLs, this correction is a deterministic function of
data every row already carries: the shift is the run's own timeframe, recorded
in `config`. It can be checked, which is what makes it worth applying.

**No figure changes, only labels.** Each point keeps its equity; only the
timestamp beside it moves. Every metric is computed either from the equity
series alone or, in `_duration_days`' case, from a *difference* between two
timestamps — which a uniform shift leaves alone. No run's reported performance
moves, which is the property that makes this migration safe to run against
results a human has already read.

A row whose `config` names no timeframe this migration knows is left exactly as
it is: its convention cannot be identified, so correcting it would be a guess.

Revision ID: c5e9a03b1f47
Revises: a9f37c14e6b2
Create Date: 2026-08-26 16:20:00.000000

"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "c5e9a03b1f47"
down_revision: str | None = "a9f37c14e6b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out rather than imported from `Timeframe`, for the reason
#: `e2b6d1a70f93` gives: a migration records what happened at one moment, and one
#: that read a live enum would change meaning the next time somebody edited it.
TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

runs = sa.table(
    "backtest_runs",
    sa.column("id", sa.String),
    sa.column("config", sa.JSON),
    sa.column("equity_curve", sa.JSON),
)


def _shifted(curve: list[Any], delta: timedelta) -> list[Any] | None:
    """`curve` with every timestamp moved by `delta`, or None if it is not one.

    All-or-nothing per row: a curve this cannot parse in full is one whose shape
    this migration does not understand, and half-shifting it would leave a
    single row holding both conventions — worse than the table did.
    """
    moved = []
    for point in curve:
        if not isinstance(point, list) or len(point) != 2:
            return None
        stamp, equity = point
        if not isinstance(stamp, str):
            return None
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        moved.append([(when + delta).isoformat(), equity])
    return moved


def _retime(delta_sign: int) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(runs.c.id, runs.c.config, runs.c.equity_curve).where(
            runs.c.equity_curve.isnot(None)
        )
    ).all()

    for run_id, config, curve in rows:
        if not isinstance(curve, list) or not curve:
            continue
        timeframe = (config or {}).get("timeframe")
        if not isinstance(timeframe, str) or timeframe not in TIMEFRAME_SECONDS:
            continue  # an unidentifiable convention is not one to correct
        seconds = TIMEFRAME_SECONDS[timeframe]
        moved = _shifted(curve, timedelta(seconds=delta_sign * seconds))
        if moved is None:
            continue
        connection.execute(sa.update(runs).where(runs.c.id == run_id).values(equity_curve=moved))


def upgrade() -> None:
    _retime(-1)


def downgrade() -> None:
    _retime(+1)
