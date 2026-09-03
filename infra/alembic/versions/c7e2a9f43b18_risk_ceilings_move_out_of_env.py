"""the risk ceilings move out of .env and onto the worker configuration row

The eight `RISK_*` environment variables — the position, gross-exposure and
daily-loss fractions, the order-rate, open-position and quote-age budgets, and
the two stop/target fallbacks — were the last half of this platform's trading
configuration still living in a file on the host. That made them unreachable
from the dashboard, unreadable by the API that returns the refusals they cause,
and unattributable: `.env` records no author and no time, so "who lifted the
position limit to 25% before that week" had no answer beyond asking whoever last
had shell access.

They join `worker_config` rather than getting a table of their own, because an
operator who widens a stop and lifts a ceiling in one sitting made **one**
decision. One row means one revision, one audit entry, and one "your worker is
running something older than this" comparison covering all of it — where a
second table would have needed a second revision counter and a screen able to
explain two of them.

NUMERIC, not DOUBLE PRECISION, for the five fractions (rule §1.1). None of them
is a balance; every one is multiplied by equity to produce the ceiling an order
is measured against, and a `0.1` that had been through a binary float would move
that ceiling.

**The defaults are backfilled and then dropped.** A `server_default` is needed
to add a NOT NULL column to a table that may already hold the single
configuration row — without one, an existing deployment's `ALTER TABLE` fails.
It is dropped again immediately because the application is the authority on
these values: `_values()` writes all eight on every save, so a default left in
the schema would be a second set of numbers that nothing reads and that would
quietly diverge from `DEFAULT_RISK_LIMITS` the first time one changed.

The backfilled values are exactly what `.env.example` shipped, so an existing
row keeps the ceilings its deployment was already running — the migration cannot
read an operator's own `.env`, so anyone who had tuned those numbers types them
into the dashboard once, and the removed lines in `.env.example` say so.

Revision ID: c7e2a9f43b18
Revises: b4d9e17c3a68
Create Date: 2026-09-03 02:10:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "c7e2a9f43b18"
down_revision: str | None = "b4d9e17c3a68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Column name → (type, the value `.env.example` shipped). Ordered as
#: `RiskLimits` declares them, which is the order the dashboard renders.
#:
#: `Any` for the type slot rather than `TypeEngine[object]`: SQLAlchemy's type
#: objects are generic in the Python type they carry, so `Numeric[Decimal]` and
#: `Integer` share no annotatable supertype that is also assignable from both.
_COLUMNS: tuple[tuple[str, Any, str], ...] = (
    ("risk_max_position_pct", sa.Numeric(20, 8), "0.10"),
    ("risk_max_gross_exposure_pct", sa.Numeric(20, 8), "1.00"),
    ("risk_max_daily_loss_pct", sa.Numeric(20, 8), "0.03"),
    ("risk_max_orders_per_minute", sa.Integer(), "30"),
    ("risk_max_open_positions", sa.Integer(), "20"),
    ("risk_max_quote_age_seconds", sa.Integer(), "30"),
    ("risk_default_stop_loss_pct", sa.Numeric(20, 8), "0.02"),
    ("risk_default_take_profit_pct", sa.Numeric(20, 8), "0.06"),
)


def upgrade() -> None:
    for name, column_type, default in _COLUMNS:
        op.add_column(
            "worker_config",
            sa.Column(name, column_type, nullable=False, server_default=default),
        )
        op.alter_column("worker_config", name, server_default=None)


def downgrade() -> None:
    for name, _type, _default in reversed(_COLUMNS):
        op.drop_column("worker_config", name)
