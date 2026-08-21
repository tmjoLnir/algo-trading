"""an order records who refused it

`orders.reject_reason` said *why* an order was refused and nothing said *who*.
Both halves were computed together — `RiskDecision` carries `rule` beside
`reason` — and `OrderRouter._route` logged both while storing only the reason,
so a stored refusal could read "no price available for SPY" without naming
which of the three rules that check a price had said it.

**There is nothing to backfill, and that is the difference from
`f4d2e8b1a075`.** That migration could reconstruct `signals.rejected_by`
because the rule had been *packed* into the reason as `"[rule] reason"` and
only needed splitting back out. An order's reason never carried it: the rule
was dropped at the call to `transition()` and survives only in the
`order.risk_denied` log line, which rotates. So rows written before this column
are null, permanently, and the read path has to distinguish that from "nothing
refused this order" rather than rendering both as an empty cell.

The column holds a rule name (`max_gross_exposure`, `kill_switch`) or the
pre-rule stage `routing` where `status` is `rejected_risk`, and the broker's
name where it is `rejected`. One column for both because the question an
operator asks of a refused order is "who refused this", and `status` already
says which of the two vocabularies the answer is drawn from — which is what
keeps "refusals by rule" countable despite the sharing.

**No index, deliberately.** `f4d2e8b1a075` added one because `/risk/rejections`
filters on `signals.rejected_by` in SQL. Nothing filters on this one: `/orders`
scopes by run mode and reads the column per row. An index whose only
justification is that the neighbouring table has one is the shape that
migration's own note argues against — it can be added by the query that needs
it.

Revision ID: b8e3f01c7d24
Revises: f4d2e8b1a075
Create Date: 2026-08-21 12:20:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "b8e3f01c7d24"
down_revision: str | None = "f4d2e8b1a075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("rejected_by", sa.String(50), nullable=True))


def downgrade() -> None:
    # Dropped rather than re-packed into `reject_reason`. The downgraded reader
    # has no unpacking step to meet it — `orders` never had the `"[rule] reason"`
    # grammar `signals` did, so writing one here would invent a format that only
    # this migration has ever produced, and leave it in the reason text a screen
    # renders verbatim.
    op.drop_column("orders", "rejected_by")
