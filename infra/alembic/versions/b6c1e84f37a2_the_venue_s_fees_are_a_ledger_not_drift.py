"""the venue's fees are a ledger, not drift

Alpaca charges regulatory fees — CAT, REG and TAF — and books them as account
activities, never on the fill. `AlpacaBroker` therefore built every `Fill` with
`fee=0`, and said so at both sites, calling it "a known gap the activities
endpoint closes". Nothing called that endpoint.

So our cash was a fills-only total and the venue's was not, and the two
ratcheted apart by every session's fee take. One session (2026-09-08) cost
$4.14 against the reconciler's $1.00 tolerance; the next morning the worker
halted at `warmup` and crash-looped, and `adopt_broker_state` would have bought
back less than a day.

This table is the ledger that makes applying a charge exactly once possible.
The venue's `activity_id` is the primary key, so a second attempt to apply the
same fee inserts nothing and returns nothing — the idempotency is the
database's, not a caller's discipline.

**Keyed on the id and not on a date.** A watermark was the obvious alternative
and it is wrong: Alpaca stamped 2026-09-08's fees with `created_at` just after
midnight UTC on the 9th, so a fee booked late slips behind a date cursor and is
never applied at all — the same silent under-count, arrived at more slowly.

`run_mode` because paper money and real money are different accounts with
their own fee streams, and an activity id is only unique inside one of them.
It is not part of the key: the id alone is unique enough to be safe, and
including the mode would let one account's charge be applied a second time
under another label.

No foreign key. A fee is charged against the account for a day's proceeds, so
it belongs to no order and no position, and an invented attribution would put a
number nobody chose into per-position P&L.

Revision ID: b6c1e84f37a2
Revises: a2f7d9c14e63
Create Date: 2026-09-09 09:45:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "b6c1e84f37a2"
down_revision: str | None = "a2f7d9c14e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_fees",
        sa.Column("activity_id", sa.String(128), primary_key=True),
        sa.Column("run_mode", sa.String(10), nullable=False),
        sa.Column("booked_on", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("sub_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_broker_fees_run_mode_booked_on", "broker_fees", ["run_mode", "booked_on"]
    )


def downgrade() -> None:
    op.drop_index("ix_broker_fees_run_mode_booked_on", table_name="broker_fees")
    op.drop_table("broker_fees")
