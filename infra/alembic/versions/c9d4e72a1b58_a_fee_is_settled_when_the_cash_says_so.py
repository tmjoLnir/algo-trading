"""a fee is settled when the cash says so

ADR 0030 made the fee ledger the record of what had been applied, keyed on the
venue's activity id, and argued that `INSERT ... ON CONFLICT DO NOTHING
RETURNING` made applying a charge twice impossible. It did. What it did not
make impossible was applying a charge *zero* times while the ledger said
otherwise.

`record_unseen` committed the rows; `portfolio.cash -= total` was in memory;
cash became durable only when the next snapshot was written. On 2026-09-09 a
worker did the first two, ended before the third, and every run after it read
an un-corrected book, was told by the ledger that all three charges were
applied, and halted on the $4.14 they explained. The rows carried
`applied_at = 10:25:29` and had never been applied to any durable balance. The
correction was not recoverable: nothing in the schema could tell "applied" from
"recorded and then lost".

So the claim moves to where the cash is. `equity_snapshots.fees_settled` is how
much of the venue's fee take the `cash` on that same row already reflects, and
it is written by the same statement — the two cannot disagree across a crash.
`broker_fees` goes back to being what it can actually vouch for: the charges the
venue has told us about, so `applied_at` is renamed `seen_at`.

The correction owed becomes `sum(broker_fees.amount) - fees_settled`, computed
fresh on every reconcile. A crash anywhere leaves both operands durable and
unchanged, so the next run derives the same number and applies it. That is
stronger than exactly-once: it self-heals, where the previous design could only
fail safely if it never failed at the wrong instant.

**Backfilled to zero, deliberately.** Every existing book has had no fee
correction applied to it — that is precisely the bug — so zero is the true
value, not a convenient default. On the first reconcile after this migration
the whole of `broker_fees` reads as owed and is applied in one go, which is the
$4.14 that has been outstanding since 10:25:29 and is why no operator has to
delete a row by hand to recover.

Revision ID: c9d4e72a1b58
Revises: b6c1e84f37a2
Create Date: 2026-09-09 10:52:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "c9d4e72a1b58"
down_revision: str | None = "b6c1e84f37a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "equity_snapshots",
        sa.Column("fees_settled", sa.Numeric(20, 8), nullable=False, server_default="0"),
    )
    # Dropped immediately, for the reason c7e2a9f43b18 gives: the writer sets
    # this column on every snapshot, so a default left in the schema would be a
    # second source of truth that nothing reads. It exists only so the ALTER can
    # add a NOT NULL column to a table that already holds rows.
    op.alter_column("equity_snapshots", "fees_settled", server_default=None)
    op.alter_column("broker_fees", "applied_at", new_column_name="seen_at")


def downgrade() -> None:
    op.alter_column("broker_fees", "seen_at", new_column_name="applied_at")
    op.drop_column("equity_snapshots", "fees_settled")
