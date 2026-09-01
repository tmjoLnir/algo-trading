"""worker configuration is a row, not a file

The ten settings that decide what a worker trades — the watchlist, the strategy
and its parameters, how orders are sized, the protective stop, the feed
watchdog, and whether an unattended loop may place live orders — were
environment variables. That made every one of them unreachable from the
dashboard, unreadable by the API, and unattributable: `.env` records no author
and no time, so "why is it risking 2% a trade" had no answer beyond asking
whoever last had shell access on the host.

One row, and single-row by construction rather than by convention. The CHECK on
the primary key means a second configuration cannot be inserted even by hand,
where a `SELECT ... LIMIT 1` over an unconstrained table reads identically right
up until the day two rows exist and the worker and the dashboard start
disagreeing about which is in force.

`revision` is what makes "saved" and "running" comparable. A worker reads this
row once, at start, and publishes the number it read; the dashboard compares the
two and says so when they differ. A counter rather than a timestamp because two
saves in the same second must stay distinct, and because a clock that steps
backwards would make the comparison lie.

`sizing_value` and `stop_multiplier` are NUMERIC, not DOUBLE PRECISION (rule
§1.1). Neither is a balance, but both scale one: the first decides order size
and the second decides where the protective stop sits.

**No row is inserted here.** An empty table means the defaults — no watchlist,
no strategy, no orders — which is exactly what an unset `WORKER_STRATEGY` meant
before, and the same posture for the same reason: a worker that starts trading
because it was deployed, rather than because somebody chose to, is the accident
that default exists to prevent. An operator who had these set in `.env` types
them into the dashboard once; the migration cannot read their file.

Revision ID: b4d9e17c3a68
Revises: f1b7c0d4e295
Create Date: 2026-09-01 21:40:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "b4d9e17c3a68"
down_revision: str | None = "f1b7c0d4e295"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_config",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("max_silence_seconds", sa.Integer(), nullable=False),
        sa.Column("strategy", sa.String(36), nullable=False),
        sa.Column("strategy_params", sa.JSON(), nullable=False),
        sa.Column("sizing_method", sa.String(20), nullable=False),
        sa.Column("sizing_value", sa.Numeric(20, 8), nullable=False),
        sa.Column("stop_type", sa.String(20), nullable=False),
        sa.Column("stop_multiplier", sa.Numeric(20, 8), nullable=False),
        sa.Column("stop_period", sa.Integer(), nullable=False),
        sa.Column("allow_live_orders", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(100), nullable=False),
        sa.CheckConstraint("id = 'default'", name="ck_worker_config_single_row"),
    )


def downgrade() -> None:
    op.drop_table("worker_config")
