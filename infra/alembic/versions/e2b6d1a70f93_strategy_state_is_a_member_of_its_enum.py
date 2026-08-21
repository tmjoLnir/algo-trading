"""strategy state is a member of its own enum

`StrategyRepository.ensure` wrote `state="active"` into `strategies.state` on
every first boot. `StrategyState` has never contained `active` — its members are
draft, backtesting, paper, live, paused and halted — and the column is a plain
`String(20)`, so nothing rejected it at any layer.

`ensure` is the only writer of a state value in the platform: the strategy
write endpoints are all stubs, and the column default is never reached because
the insert names every column. So `active` was not one possible value among
several, it was the **only** value any row could ever hold. Filtering the
strategies screen by any real state therefore matched nothing, and the four
options the filter offered besides `active` could not occur by construction.

Two steps, in this order. The rewrite maps every existing row onto the rung the
repository should have written — `draft`, the ratchet's first, because a booting
worker has been granted no promotion by anybody and `updated_at` is what answers
"is it running". Then the CHECK, which would have caught the original bug on the
first insert and which cannot be added before the rewrite without rejecting the
rows it exists to protect.

`active` is mapped rather than dropped: these rows are real strategies a worker
registered, and the only thing wrong with them is the word.

The downgrade restores `active` for rows it finds at `draft`, which is lossy in
the one case where somebody has since set a row to `draft` deliberately. There
is no way to tell those apart — the column records a state and not a history —
and re-breaking the constraint to preserve them would be the worse trade.

Revision ID: e2b6d1a70f93
Revises: d7a1c9f4b208
Create Date: 2026-08-21 00:05:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "e2b6d1a70f93"
down_revision: str | None = "d7a1c9f4b208"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out rather than imported from `StrategyState`. A migration is a
#: record of what the schema became at one moment, and one that read a live enum
#: would silently change meaning the next time somebody edited that enum — a
#: migration already applied would then describe a constraint the database does
#: not have.
STATES = ("draft", "backtesting", "paper", "live", "paused", "halted")

_IN_LIST = ", ".join(f"'{state}'" for state in STATES)


def upgrade() -> None:
    op.execute("UPDATE strategies SET state = 'draft' WHERE state = 'active'")
    # Anything else outside the enum is data this migration did not anticipate;
    # send it to the same rung rather than let the constraint fail the deploy.
    # Losing a state nobody can name is better than a migration that cannot run.
    op.execute(f"UPDATE strategies SET state = 'draft' WHERE state NOT IN ({_IN_LIST})")
    op.create_check_constraint("ck_strategies_state", "strategies", f"state IN ({_IN_LIST})")


def downgrade() -> None:
    op.drop_constraint("ck_strategies_state", "strategies", type_="check")
    op.execute("UPDATE strategies SET state = 'active' WHERE state = 'draft'")
