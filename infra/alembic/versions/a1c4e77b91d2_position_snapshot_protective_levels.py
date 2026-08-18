"""position snapshots carry every protective level

`position_snapshots` stored `stop_loss_price` and nothing else a `Position`
protects itself with. That was fine while nothing read the table — it is a
table with no reader, as the roadmap kept noting — and stops being fine the
moment a restart restores a book from it.

Three columns, and the first is the one that matters:

- `high_water_mark` — a trailing stop reloaded without it re-anchors on the
  current bar. `StopManager.update_trailing` guarantees monotonicity *relative
  to the mark it holds*, so a mark silently reset to a lower price makes the
  invariant hold around the wrong number and the stop ends up further from the
  entry than it was before the restart.
- `take_profit_price` — a position restored without it has no upside exit, and
  `StopManager` refuses to invent one.
- `opened_at` / `fees_paid` — a time stop measures from the open, and
  `Position.total_pnl` subtracts fees. Restoring a position without either
  produces numbers that look right and are not.

Revision ID: a1c4e77b91d2
Revises: 8140ae9c6209
Create Date: 2026-08-18 02:30:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "a1c4e77b91d2"
down_revision: str | None = "8140ae9c6209"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(20, 8)


def upgrade() -> None:
    # All nullable, or defaulted, so this applies to a table with rows in it.
    # Existing snapshots genuinely do not know these values, and inventing a
    # zero high-water mark would be worse than admitting the gap.
    op.add_column("position_snapshots", sa.Column("take_profit_price", MONEY, nullable=True))
    op.add_column("position_snapshots", sa.Column("high_water_mark", MONEY, nullable=True))
    op.add_column(
        "position_snapshots",
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "position_snapshots",
        sa.Column("fees_paid", MONEY, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("position_snapshots", "fees_paid")
    op.drop_column("position_snapshots", "opened_at")
    op.drop_column("position_snapshots", "high_water_mark")
    op.drop_column("position_snapshots", "take_profit_price")
