"""a run keeps the warnings it produced

`backtest_runs` stored `metrics`, `equity_curve` and `trades`, and nothing else
the run had to say. `BacktestResult.warnings` — every per-order refusal the
engine booked, the coverage shortfalls `_validate` returns, and the cost and
sizing caveats `run_spec` appends — was assembled on every queued run and then
dropped by `result_to_storage`, which returned only the three columns above.

The API filled the gap by recomputing `warnings` at read time from the metric
set alone (`suspicious(run.metrics)`). That catches the two thresholds
docs/BACKTESTING.md names — too few trades, an implausible Sharpe — and it
cannot catch anything that is not a function of the metrics, which is most of
what actually goes wrong with a run.

The case that motivated this: a `buy_and_hold` run over twenty symbols whose
every entry was refused at sizing for want of a stop reported `total_return`
0.0, `max_drawdown` 0.0 and `num_trades` 0. The engine had booked twenty
refusals and `refusal_summary` had written the sentence that explains them; the
dashboard showed one line, "only 0 trades", and nothing at all about the
refusals. An all-zero metric set is identical whether a strategy was refused
everything or never signalled, and those two call for opposite responses.

Nullable with no backfill, deliberately. A run stored before this column
existed did not record its warnings and cannot have them reconstructed; NULL
says that, and `[]` would say the run finished clean, which is a claim about
rows nobody can check. `_to_view` keeps the distinction by concatenating rather
than substituting, so an old row still serves exactly what it serves today.

Revision ID: a9f37c14e6b2
Revises: b8e3f01c7d24
Create Date: 2026-08-26 13:40:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "a9f37c14e6b2"
down_revision: str | None = "b8e3f01c7d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("warnings", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("backtest_runs", "warnings")
