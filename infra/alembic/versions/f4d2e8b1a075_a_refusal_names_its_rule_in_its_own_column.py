"""a refusal names its rule in its own column

`signals.rejected_by` did not exist. `PostgresSignalRepository` flattened the
rule name into `rejection_reason` as `"[max_position_size] SPY would be …"` and
split it back out on read, and its own docstring said a column "would be the
tidier answer and is not worth a migration for one string".

That was true while nothing read the field. `/risk/rejections` is the reader
that changes it, and the reason is not tidiness. The endpoint has to *exclude*
one value — `no_action`, which `SubmitResult.no_action` sets for a HOLD or an
exit against an already-flat position, and which the router marks `approved`
precisely so it does not inflate the rejection count an operator reads to judge
whether the risk config is too tight. Against the packed column that exclusion
is `rejection_reason NOT LIKE '[no_action]%'`, which also matches any refusal
whose reason text happens to begin with a bracket, and silently stops matching
if the packing format is ever adjusted.

The backfill parses the existing packed values with the same grammar
`_split_reason` used: a leading `[`, the rule up to the first `]`, the rest as
the reason. Rows that do not match that shape keep their `rejection_reason`
whole and get a null `rejected_by`, which is what they already meant.

The index serves the endpoint's own query — newest refusals, optionally for one
rule. `ix_signals_strategy_ts` leads on `strategy_id` and does not, because the
first question this endpoint answers is not scoped to a strategy: it is "is
anything being refused at all".

Revision ID: f4d2e8b1a075
Revises: e2b6d1a70f93
Create Date: 2026-08-21 00:50:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "f4d2e8b1a075"
down_revision: str | None = "e2b6d1a70f93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("rejected_by", sa.String(50), nullable=True))

    # `[rule] rest` → (rule, rest). Anchored on a leading bracket with a closing
    # one present, which is exactly what `_split_reason` tested for; anything
    # else is a reason that was stored without a rule and stays whole.
    op.execute(
        """
        UPDATE signals
           SET rejected_by = substring(rejection_reason from '^\\[([^\\]]+)\\]'),
               rejection_reason = nullif(
                   btrim(regexp_replace(rejection_reason, '^\\[[^\\]]+\\]', '')), ''
               )
         WHERE rejection_reason LIKE '[%]%'
        """
    )
    op.create_index("ix_signals_rejected_by_ts", "signals", ["rejected_by", "ts"])


def downgrade() -> None:
    op.drop_index("ix_signals_rejected_by_ts", table_name="signals")
    # Re-pack, so a downgraded reader's `_split_reason` still finds the rule.
    op.execute(
        """
        UPDATE signals
           SET rejection_reason = btrim('[' || rejected_by || '] ' || coalesce(rejection_reason, ''))
         WHERE rejected_by IS NOT NULL
        """
    )
    op.drop_column("signals", "rejected_by")
