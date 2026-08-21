"""One vocabulary for the promotion ratchet, checked where it can be.

`strategies.state` was described in four places that did not agree.
`StrategyState` had six members; `StrategyRepository.ensure` wrote `"active"`,
which was none of them; the dashboard's filter offered `backtest` and `active`
and omitted `live` and `halted`; and the column was a bare `String(20)` that
accepted all of it. `ensure` is the only writer of a state value in the
platform, so the net effect was that every row held a state no filter could
match, and four of the filter's five options could not occur by construction.

Nothing caught it because nothing was looking. mypy cannot: `.values()` takes
`Any`, so a literal in the insert is invisible to it. `alembic check` cannot:
autogenerate does not compare CHECK constraints, which is also why the guard
below needs a test of its own rather than relying on the migration drift check
in `tests/integration/test_migrations.py`.

So this file holds the parts of the invariant that a unit test *can* reach —
the constraint's own predicate, the column default, and the standing debt that
a new rung owes a migration. The database half is
`tests/integration/test_decision_record.py`, and the browser half is `tsc`:
`useStrategies.ts` types its filter as a `Record` over the generated union, so a
missing or invented rung fails the build.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import CheckConstraint

from atp_core.domain import StrategyState
from atp_core.persistence.models import StrategyRow
from atp_core.persistence.strategies import first_boot_values
from atp_core.strategy.ports import StrategyRecord

if TYPE_CHECKING:
    from types import ModuleType

#: The migration that introduced the constraint. Named rather than discovered:
#: see `test_a_new_rung_owes_a_migration` for why this reference is deliberate.
CONSTRAINT_MIGRATION = "e2b6d1a70f93_strategy_state_is_a_member_of_its_enum"

REPO_ROOT = Path(__file__).resolve().parents[2]


def quoted(sql: str) -> set[str]:
    """Every single-quoted literal in a SQL fragment."""
    return set(re.findall(r"'([^']*)'", sql))


def state_check() -> CheckConstraint:
    """The `strategies.state` CHECK, from the model's own table definition."""
    checks = [c for c in StrategyRow.__table__.constraints if isinstance(c, CheckConstraint)]
    named = [c for c in checks if c.name == "ck_strategies_state"]
    assert named, "strategies has no state CHECK constraint"
    return named[0]


def migration() -> ModuleType:
    path = REPO_ROOT / "infra" / "alembic" / "versions" / f"{CONSTRAINT_MIGRATION}.py"
    spec = importlib.util.spec_from_file_location(CONSTRAINT_MIGRATION, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestWhatAFirstBootWrites:
    """The defect itself, now reachable without a database.

    `ensure` wrote `state="active"` inside a `pg_insert(...).values(...)` call,
    which no unit test could see and mypy cannot type — `.values()` takes `Any`.
    The only thing that would have failed was an integration test against a real
    Postgres, which is why it survived four phases and why the values are now
    built by a function that can simply be called.
    """

    @staticmethod
    def values() -> dict[str, object]:
        record = StrategyRecord(id="sma_crossover", name="sma_crossover", kind="coded")
        return first_boot_values(record, datetime(2026, 8, 21, tzinfo=UTC))

    def test_the_state_it_writes_is_a_rung(self) -> None:
        assert self.values()["state"] in set(StrategyState)

    def test_it_writes_the_first_rung(self) -> None:
        """`draft`, not something higher.

        A booting worker has been granted no promotion by anybody — it is
        running because somebody set `WORKER_STRATEGY`, which is a fact about
        the deployment. Writing itself onto a higher rung would be the ratchet
        with its pawl removed, and "is it running" is `updated_at`.
        """
        assert self.values()["state"] == StrategyState.DRAFT

    def test_it_does_not_write_the_value_that_caused_this(self) -> None:
        assert self.values()["state"] != "active"


class TestTheConstraintMatchesTheEnum:
    def test_it_lists_every_rung(self) -> None:
        listed = quoted(str(state_check().sqltext))

        assert listed == {state.value for state in StrategyState}

    def test_it_does_not_list_the_value_that_caused_this(self) -> None:
        """`active` is the specific word, and it is worth naming.

        It was in the table for four phases. A constraint that still admitted it
        would let the exact bug back in while looking like a fix.
        """
        assert "active" not in quoted(str(state_check().sqltext))
        assert "active" not in {state.value for state in StrategyState}

    def test_the_column_default_is_a_rung(self) -> None:
        """The default was already `draft` and correct — `ensure` overrode it
        with a literal. Pinned so a later edit cannot make the two disagree."""
        default = StrategyRow.__table__.c.state.default

        assert default is not None
        assert default.arg in set(StrategyState)


class TestTheMigrationMatchesTheEnum:
    def test_a_new_rung_owes_a_migration(self) -> None:
        """The one thing no automated check can see on its own.

        `alembic check` compares tables, columns and indexes — not CHECK
        constraints. So adding a member to `StrategyState` would widen the
        model's predicate, leave the database's alone, and the drift check would
        pass while every insert of the new rung failed.

        This test is the speed bump. When it fails because somebody added a
        rung, the fix is a new migration altering `ck_strategies_state`, and
        then pointing `CONSTRAINT_MIGRATION` above at it — not editing
        `e2b6d1a70f93`, which has already run in places this repository cannot
        reach.
        """
        in_migration = set(migration().STATES)

        assert in_migration == {state.value for state in StrategyState}, (
            "StrategyState and the applied CHECK constraint disagree. Adding a "
            "rung needs a migration that alters ck_strategies_state; see this "
            "test's docstring."
        )

    def test_the_migration_rewrites_the_value_it_replaces(self) -> None:
        """Existing rows are mapped, not left to fail the constraint.

        Every row in every deployed database holds `active`. A migration that
        added the CHECK without rewriting them first would abort on the rows it
        exists to protect.
        """
        source = (
            REPO_ROOT / "infra" / "alembic" / "versions" / f"{CONSTRAINT_MIGRATION}.py"
        ).read_text()
        upgrade = source.split("def upgrade()")[1].split("def downgrade()")[0]

        rewrite = upgrade.index("UPDATE strategies")
        constrain = upgrade.index("create_check_constraint")
        assert rewrite < constrain, "the rewrite has to precede the constraint"

    @pytest.mark.parametrize("rung", list(StrategyState))
    def test_every_rung_survives_a_round_trip_as_a_string(self, rung: StrategyState) -> None:
        """The column stores `str`, and `StrategyState` is a `StrEnum`.

        Which means a raw value read back from the database compares equal to
        its member and is `in set(StrategyState)` — the property the integration
        assertions and the API's filter both lean on.
        """
        assert rung.value in set(StrategyState)
        assert rung == rung.value
