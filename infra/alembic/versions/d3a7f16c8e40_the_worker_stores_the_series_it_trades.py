"""the worker stores the bar series it trades

The strategy runner was built with a hard-coded `Timeframe.D1` while the
ingestor wrote `1m` bars, and `bars` is filtered strictly on the column — so
every pass asked the repository for the newest daily bar, got back the one
loaded at warmup, and handed the strategy nothing. Nothing raised. `on_bar` was
called zero times across a full session while the worker logged itself ready
and trading (docs/paper-week/day-1-review.md).

A column rather than a constant because the two call sites have to be
configured from one value. Reading and writing are the same decision, and the
only way to make the disagreement unrepresentable is to give it one place to
live. `WorkerConfig.bar_timeframe` is that place; this row is where it persists.

`String(8)` rather than an enum type: the stored vocabulary is
`WorkerConfig.TimeframeName`, deliberately restated there rather than imported
from `Timeframe`, so that a value already written stays loadable if the enum is
later reordered. A database enum would have to be migrated in lockstep with a
Python one to no benefit — `_check_timeframe` refuses anything outside the list
on the way in and on the way out.

**Backfilled to `1m`, then the default is dropped.** `1m` is what the realtime
feed subscribes to and therefore the series every existing deployment actually
has bars for; backfilling `1d` would preserve the bug this migration exists to
make unrepresentable. The `server_default` is needed only so the `ALTER TABLE`
can add a NOT NULL column to a table that already holds the single
configuration row, and is dropped immediately afterwards for the reason
`c7e2a9f43b18` gives: `_values()` writes the column on every save, so a default
left in the schema would be a second source of truth that nothing reads.

Revision ID: d3a7f16c8e40
Revises: c7e2a9f43b18
Create Date: 2026-09-04 15:40:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "d3a7f16c8e40"
down_revision: str | None = "c7e2a9f43b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worker_config",
        sa.Column("timeframe", sa.String(8), nullable=False, server_default="1m"),
    )
    op.alter_column("worker_config", "timeframe", server_default=None)


def downgrade() -> None:
    op.drop_column("worker_config", "timeframe")
