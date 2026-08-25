"""Strategy storage — the `StrategyRepository` port over PostgreSQL.

The `strategies` table's job here is narrow: be the row that
`signals.strategy_id` and `orders.strategy_id` can point at. Nothing wrote it,
so both those columns were always null and no order could be traced to the
strategy that placed it.

`ensure` is an upsert that deliberately updates almost nothing, and the
asymmetry between insert and update is the point. On a **first** boot the worker
is the only thing that knows this strategy exists, so it writes what it has. On
every **later** boot the row may have been edited by a strategy-management API
that knows more about it than a booting worker does, so only `updated_at` moves.
That keeps "when did a worker last run this?" answerable without letting a
restart quietly reset a strategy someone had configured.

**The state a first boot writes is `draft`, and this used to be `"active"`** —
a string that is not a member of `StrategyState` at all, which nothing rejected
because the column is a plain `String(20)`. The consequence was not cosmetic: it
was the only value any row could ever hold, so filtering by any real state
matched nothing, and the dashboard's filter offered four options that could not
occur.

The word was wrong, and so was the reasoning behind it — "a strategy a worker is
running is not a draft" conflates *running* with *promoted*. `state` is the
rung a strategy has been promoted to on the ratchet (draft → backtesting →
paper → live), and every rung above the first is a human decision the API is
supposed to gate. A booting worker has been granted nothing by anybody; it is
running because somebody set `WORKER_STRATEGY`, which is a fact about the
deployment rather than an authorisation. A worker that wrote itself onto a
higher rung would be the ratchet with its pawl removed.

Nothing is lost by saying `draft`, because the question that word seems to
answer is answered elsewhere and better: *is it running* is `updated_at`, which
this method bumps on every boot and which the API serves as `last_started_at`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from atp_core.domain import StrategyState
from atp_core.errors import StrategyExistsError
from atp_core.logging import get_logger
from atp_core.persistence.db import session_scope
from atp_core.persistence.models import StrategyRow
from atp_core.strategy.ports import NewStrategy, StoredStrategy, StrategyRecord

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from atp_core.clock import Clock
    from atp_core.strategy.ports import StrategyRepository

log = get_logger(__name__)


def first_boot_values(record: StrategyRecord, now: datetime) -> dict[str, object]:
    """The columns a first boot writes into `strategies`.

    Pulled out of `ensure` so they can be asserted **without a database**, and
    that is the whole reason it is a function. The defect this module's
    docstring describes lived in this dict: a `state` of `"active"`, which
    `StrategyState` has never contained. It survived four phases because the
    only place it was observable was a real Postgres — mypy cannot see into
    `.values()`, which takes `Any`, and every other layer read the column back
    as the `str` it is.

    `state` is the enum member rather than its value, which SQLAlchemy stores as
    the string either way. Referring to the member is what makes a typo a
    `mypy` error instead of a row nothing can match.
    """
    return {
        "id": record.id,
        "name": record.name,
        "description": "",
        "kind": record.kind,
        "class_name": record.class_name,
        "params": dict(record.params or {}),
        "ruleset": None,
        "state": StrategyState.DRAFT,
        "universe": list(record.universe),
        "timeframe": record.timeframe,
        "created_at": now,
        "updated_at": now,
    }


def authored_values(new: NewStrategy, now: datetime) -> dict[str, object]:
    """The columns a create writes into `strategies`.

    Pulled out of `create` for the reason `first_boot_values` is pulled out of
    `ensure`: it can then be asserted without a database, and the defect that
    function's docstring describes — a `state` no `StrategyState` contained,
    invisible to mypy because `.values()` takes `Any` — is the same one this
    dict is one typo away from.

    **`state` is not a parameter.** `NewStrategy` has no such field, so the
    ratchet's first rung is not a default a request can override; it is the only
    value this function can produce. Promotion is a separate act with separate
    preconditions (docs/SAFETY.md).

    `created_at` and `updated_at` are the same instant, which is what a row that
    exists and has never been started looks like. See `StoredStrategy` for what
    `updated_at` means once a worker has booted it.
    """
    return {
        "id": new.id,
        "name": new.name,
        "description": new.description,
        "kind": new.kind,
        "class_name": new.class_name,
        "params": dict(new.params),
        "ruleset": None if new.ruleset is None else dict(new.ruleset),
        "state": StrategyState.DRAFT,
        "universe": list(new.universe),
        "timeframe": new.timeframe,
        "risk_config": dict(new.risk_config),
        "created_at": now,
        "updated_at": now,
    }


class PostgresStrategyRepository:
    """`StrategyRepository` over the `strategies` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        # Injected rather than `datetime.now()` (rule §1.2), and it matters more
        # here than it looks: a backtest or a replay running on a `SimulatedClock`
        # must stamp these rows with market time, not with the wall clock of the
        # machine doing the replaying.
        self._clock = clock

    async def create(self, new: NewStrategy) -> StoredStrategy:
        """Insert a row, refusing a name that is already stored.

        A plain INSERT rather than the upsert `ensure` uses, and the contrast is
        the point: `ensure` exists to be safe to call on every boot, so it
        resolves a conflict by leaving the row alone. Here a conflict is the
        answer — somebody is authoring a strategy under a name that already
        names one, and quietly writing over it would replace a strategy that may
        be running, under the identity every one of its signals carries.

        The uniqueness is the database's rather than a `SELECT` first, because a
        check before an insert is a race: two requests can both find the name
        free. Translating every `IntegrityError` into that one refusal is safe
        because of what this table constrains: a primary key, a unique name —
        the same value, since a strategy's id is its name — and a CHECK on
        `state` that this method cannot violate, because `authored_values` is
        the only thing that decides it and it decides `draft`.
        """
        now = self._clock.now()
        row = StrategyRow(**authored_values(new, now))
        async with session_scope(self._session_factory) as session:
            session.add(row)
            try:
                # Flushed explicitly so the conflict surfaces here, where it can
                # be named, rather than out of `session_scope`'s commit as an
                # `IntegrityError` the caller would have to interpret.
                await session.flush()
            except IntegrityError as exc:
                log.info("strategy.duplicate", strategy=new.id)
                raise StrategyExistsError(
                    f"a strategy named {new.name!r} is already stored"
                ) from exc
        return _to_stored(row)

    async def ensure(self, record: StrategyRecord) -> None:
        """Create the row if it is absent; otherwise only bump `updated_at`.

        The new row starts at `draft` — the ratchet's first rung, and the
        column's own default. See the module docstring for why a running worker
        does not put itself on a higher one.
        """
        now = self._clock.now()
        async with session_scope(self._session_factory) as session:
            await session.execute(
                pg_insert(StrategyRow)
                .values(**first_boot_values(record, now))
                # Only the timestamp. See the module docstring: everything else
                # in this row may have been edited by someone who knows more
                # about it than a booting worker does, and an upsert that reset
                # `state` would stop a strategy by restarting it.
                .on_conflict_do_update(
                    index_elements=[StrategyRow.id],
                    set_={"updated_at": now},
                )
            )

    async def get(self, strategy_id: str) -> StrategyRecord | None:
        """The stored identity, or None."""
        async with session_scope(self._session_factory) as session:
            row = (
                await session.execute(select(StrategyRow).where(StrategyRow.id == strategy_id))
            ).scalar_one_or_none()
        return None if row is None else _to_record(row)

    async def get_stored(self, strategy_id: str) -> StoredStrategy | None:
        """One row in full, or None. `get`'s query with `list_all`'s mapping."""
        async with session_scope(self._session_factory) as session:
            row = (
                await session.execute(select(StrategyRow).where(StrategyRow.id == strategy_id))
            ).scalar_one_or_none()
        return None if row is None else _to_stored(row)

    async def list_all(self, *, state: str | None = None) -> list[StoredStrategy]:
        """Every stored strategy, newest first.

        Ordered by `created_at` descending with the id as a tie-break, so the
        list is stable: two strategies a worker registered in the same
        microsecond on first boot would otherwise swap places between reads.

        `created_at` rather than `updated_at`, even though the latter is the
        livelier column. `updated_at` moves on every worker boot, so ordering by
        it would reshuffle the whole screen each morning — the reader's mental
        map of a short list is worth more than putting the most recently booted
        strategy on top, and the timestamp is a column they can read.
        """
        query = select(StrategyRow)
        if state is not None:
            query = query.where(StrategyRow.state == state)
        query = query.order_by(StrategyRow.created_at.desc(), StrategyRow.id.desc())

        async with session_scope(self._session_factory) as session:
            rows = (await session.execute(query)).scalars().all()
        return [_to_stored(row) for row in rows]


def _to_stored(row: StrategyRow) -> StoredStrategy:
    """The whole row. See `StoredStrategy` for what `state` and `updated_at`
    actually mean, both of which are narrower than their names."""
    return StoredStrategy(
        id=row.id,
        name=row.name,
        description=row.description or "",
        kind=row.kind,
        class_name=row.class_name,
        params=dict(row.params or {}),
        ruleset=dict(row.ruleset) if row.ruleset else None,
        state=row.state,
        universe=tuple(row.universe or ()),
        timeframe=row.timeframe,
        risk_config=dict(row.risk_config or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_record(row: StrategyRow) -> StrategyRecord:
    return StrategyRecord(
        id=row.id,
        name=row.name,
        kind=row.kind,
        class_name=row.class_name,
        params=dict(row.params or {}),
        universe=tuple(row.universe or ()),
        timeframe=row.timeframe,
    )


def _typecheck(repo: PostgresStrategyRepository) -> StrategyRepository:
    """Structural conformance, checked by mypy rather than asserted at runtime."""
    return repo
