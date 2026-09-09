"""a snapshot records venue-side protection, not only the level we armed

`position_snapshots.stop_loss_price` is *intent*. The router arms it before it
submits the protective child order — deliberately, so that a refused stop still
leaves the engine-side fallback a level to watch — which means the column is
populated whether or not any order exists at the venue.

On day 2 of the paper week all 85 protective orders were rejected off-tick and
all 38 positions ran naked for 24 position-hours. Every artifact built from this
table, and the live dashboard beside it, read the armed level and reported those
positions as protected. The operator refreshed `/api/v1/positions` six times
while sixteen CRITICAL lines were already in the log, and the screen reassured
them (docs/paper-week/day-2-review.md, F2a).

So this column is the other half of the sentence: how much of the position has a
stop actually resting at the venue. A quantity and not a boolean, for the reason
`OrderRouter.broker_side_protected_qty` gives — a boolean reports a partly
covered position as protected and hides the naked remainder.

**Backfilled to 0, and 0 is the honest value for history.** Every row written
before this migration was written by a platform that did not ask the question,
so "no venue-side cover recorded" is exactly what those rows mean. Backfilling
from `stop_loss_price` would retroactively assert the very thing the paper week
disproved. The `server_default` exists only so the `ALTER TABLE` can add a NOT
NULL column to a populated table, and is dropped immediately after for the
reason `c7e2a9f43b18` gives: the writer sets the column on every insert, so a
default left in the schema would be a second source of truth nothing reads.

With this column, docs/SAFETY.md's go-live gate — *"there are no unprotected
positions"* — is answerable in SQL. Until now the only way to evaluate it was to
grep the worker's container logs, which is how the day-2 review found the
failure, a day late.

Revision ID: a2f7d9c14e63
Revises: d3a7f16c8e40
Create Date: 2026-09-09 04:45:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "a2f7d9c14e63"
down_revision: str | None = "d3a7f16c8e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position_snapshots",
        sa.Column(
            "broker_protected_qty",
            sa.Numeric(20, 8),
            nullable=False,
            server_default="0",
        ),
    )
    op.alter_column("position_snapshots", "broker_protected_qty", server_default=None)


def downgrade() -> None:
    op.drop_column("position_snapshots", "broker_protected_qty")
