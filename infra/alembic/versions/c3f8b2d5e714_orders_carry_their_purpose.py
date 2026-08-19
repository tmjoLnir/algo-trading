"""orders carry their purpose

`OrderRequest.purpose` — entry, exit, stop_loss, take_profit, time_exit,
flatten, manual — already existed and was already load-bearing: it is part of
the `client_order_id` derivation, which is what stops a reversal's two SELLs
collapsing into one key (`execution/idempotency.py`). But it was consumed by
that derivation and then dropped. `Order` did not carry it and this table did
not store it, and a SHA-256 digest cannot be read backwards.

That made every closed position unattributable. Trade reconstruction can see
*that* a position closed and cannot see *why*, and "why" is the most actionable
thing the analytics layer reports — a strategy whose profit comes entirely from
its take-profits while its stops bleed has a stop-placement problem, not a
signal problem. Without this column all three engine-side exits (a triggered
stop, a hit target, a time exit) stored identically, because all three reach the
venue as `router.flatten`.

Nullable, and deliberately not defaulted to `entry`. Orders written before this
column existed genuinely do not know their purpose, and labelling a historical
exit as an entry would put it on the wrong side of every round trip the
reconstruction folds — a wrong number rather than a missing one.

The index is for the read that consumes this: reconstruction walks every filled
order for one run mode in creation order. The two existing indexes lead on
`status` and on `symbol`, and neither serves it — reconstruction is not scoped
to a symbol, and it wants both filled statuses rather than one.

Revision ID: c3f8b2d5e714
Revises: a1c4e77b91d2
Create Date: 2026-08-19 09:15:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "c3f8b2d5e714"
down_revision: str | None = "a1c4e77b91d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("purpose", sa.String(20), nullable=True))
    op.create_index("ix_orders_runmode_created", "orders", ["run_mode", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_orders_runmode_created", table_name="orders")
    op.drop_column("orders", "purpose")
