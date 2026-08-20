"""backtest runs are queued before they start

`backtest_runs` has existed since the initial schema with no reader and no
writer, so its shape was never tested against a caller. Wiring the queue found
three things wrong with it.

**`started_at` was NOT NULL, and a queued run has not started.** The row is
written when the request arrives and the job may sit on the queue for minutes;
the only value the API could have put there is the current time, which would
make every run's reported duration include however long the queue was backed up.
So `queued_at` is added — when somebody asked — and `started_at` becomes
nullable and means what its name says: when a worker picked it up.

Backfilled rather than defaulted, in the one direction that is correct: any
existing row's `started_at` is the best available answer for when it was asked
for, because nothing could have queued it separately. There are no such rows in
practice — nothing has ever written this table — but a migration that assumed
that would be wrong on the one deployment where it was not.

**There was nowhere to put the trades.** `GET /backtests/{id}/trades` is
specified to serve every simulated trade, because inspecting individual trades
is how a backtest that is "profitable" because of one impossible fill gets
caught. Only `metrics` and `equity_curve` existed, and neither can answer it.
Stored rather than recomputed on read — the opposite of the live trade
reconstruction (ADR 0015) — and the reason is not a preference: a backtest's
orders exist only inside the run that produced them, so there is nothing to
recompute from once the process exits.

**`queued_at` is indexed** because the list read orders by it. The table has no
other index beyond its primary key, and the screen's only query is "the newest
runs, optionally for one strategy".

Revision ID: d7a1c9f4b208
Revises: c3f8b2d5e714
Create Date: 2026-08-20 10:40:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "d7a1c9f4b208"
down_revision: str | None = "c3f8b2d5e714"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("trades", sa.JSON(), nullable=True))

    # Added nullable, backfilled, then constrained. Adding it NOT NULL in one
    # step fails on a table with rows, and a server-side default would leave
    # every existing run claiming it was queued at the moment of the migration.
    op.add_column(
        "backtest_runs", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE backtest_runs SET queued_at = started_at WHERE queued_at IS NULL")
    op.alter_column("backtest_runs", "queued_at", nullable=False)
    op.create_index("ix_backtest_runs_queued_at", "backtest_runs", ["queued_at"])

    # Widened last, so the backfill above still had a non-null column to read.
    op.alter_column(
        "backtest_runs", "started_at", existing_type=sa.DateTime(timezone=True), nullable=True
    )


def downgrade() -> None:
    # A run that never started has no `started_at` to restore, and the column is
    # about to be NOT NULL again. `queued_at` is the honest substitute: it is
    # when the run was asked for, which is what this column meant before this
    # migration split the two.
    op.execute("UPDATE backtest_runs SET started_at = queued_at WHERE started_at IS NULL")
    op.alter_column(
        "backtest_runs", "started_at", existing_type=sa.DateTime(timezone=True), nullable=False
    )
    op.drop_index("ix_backtest_runs_queued_at", table_name="backtest_runs")
    op.drop_column("backtest_runs", "queued_at")
    op.drop_column("backtest_runs", "trades")
