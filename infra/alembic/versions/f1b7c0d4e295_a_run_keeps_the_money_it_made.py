"""a run keeps the money it made

`BacktestResult.to_report()` produces nine figures that describe the money a run
made and the orders it took — ending equity, total return, the realised and
unrealised split, fees, and the signal/order/fill/open-position counts. The CLI's
`--out` JSON has all nine. Until this column, a *queued* run had none of them at
any layer: `result_to_storage` returned metrics, the curve, the trades and the
warnings, and `backtest_runs` had nowhere to put the rest.

The gap came from two decisions that are each correct. `metrics` is a bag of
floats by contract, because those are statistics over a return series rather
than balances; money must not be float (CLAUDE.md §1.1), so `to_report()`
deliberately keeps the money *out* of `metrics` and reports it as decimal
strings beside it. The queued path then stored only `metrics`. Together they
dropped every money figure a run produced.

What that cost: a `buy_and_hold` run over twenty symbols reported a total return
of 202.8% of which **none was realised** — twenty positions still open at the
end, `realized_pnl` zero, the whole 202.8% unrealised mark-to-market. The
dashboard read "202.8% return" with nothing to say otherwise. The nearest hint
was `num_trades: 0`, which says something different — that the trade statistics
rest on no closed round trips — and reads as "this strategy does not trade much"
rather than "you are still holding all of it".

One JSON column rather than nine typed ones. Five of the nine are money and four
are counts, so "a set of MONEY columns" would be nine columns of two types; and
this table already carries a run's whole result as JSON written in one
transaction, with `equity_curve` the standing precedent for money as decimal
strings inside it. Reasoning in ADR 0019.

Nullable with no backfill, for the same reason as `a9f37c14e6b2`: a run stored
before this column computed these figures and threw them away. NULL says that.
Anything else would be a number nobody can check.

Revision ID: f1b7c0d4e295
Revises: c5e9a03b1f47
Create Date: 2026-08-27 09:10:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "f1b7c0d4e295"
down_revision: str | None = "c5e9a03b1f47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("totals", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("backtest_runs", "totals")
