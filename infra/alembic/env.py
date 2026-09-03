"""Alembic environment.

The database URL comes from `Settings`, never from alembic.ini — one place for
credentials, and none of them in a file that gets committed.

**That sentence describes what this module does; it used to describe what it
intended.** It read `os.environ` directly and fell back to a hardcoded
`atp:atp@localhost` when the variable was unset — and the process environment is
not the configuration. `uv run alembic` does not load `.env`, so a host-side
`make migrate` never saw the url the operator had written there, while every
other host-side tool that opens the database — `seed`, `backfill`, `status`,
`preflight`, `check-env` — read it through `Settings` and did. On any stack whose
password is not the base compose file's development `atp`, that difference is:

    asyncpg.exceptions.InvalidPasswordError: password authentication failed
    for user "atp"

from the one command that has to work before anything else can, with a `.env`
that is correct, `docs/DEPLOYMENT.md` followed exactly, and a traceback that
names none of it. `.env.example` promised host-side tools read its
`DATABASE_URL`; this is the module that made that promise false.

Reading through `Settings` leaves the container path untouched. Pydantic
resolves a real environment variable ahead of `.env`, and the `migrate` service
sets `DATABASE_URL` in its compose `environment:` block — so in-container
migrations still name `db`, and only the host-side case changes, which is the
one that was wrong.

The url reaches the engine as an argument rather than through
`config.set_main_option`, which hands it to `ConfigParser` — where a `%` in a
password is an interpolation symbol and raises something about a config file
about a password. `.env.example` and `make check-env` already say a `%` cannot
survive the trip to Postgres; this way it is refused there, once, rather than
here in a second voice.
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig

from alembic import context
from pydantic import ValidationError
from sqlalchemy import pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from atp_core.config import Settings, config_problems
from atp_core.persistence.db import is_auth_failure
from atp_core.persistence.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """The url this migration will use, resolved exactly as the platform does.

    A plain `Settings()`. It used to be `Settings(risk=RiskLimits.model_construct())`,
    because `risk` was a nested `default_factory` and one bad `RISK_*` value
    raised out of it *during* `Settings()` — taking a schema migration down over
    a number that has nothing to do with a schema. The ceilings are a database
    row since ADR 0025, so the nesting that caused it is gone and the workaround
    with it. The property it protected is still tested
    (`tests/integration/test_alembic_env.py`), now with an unrelated bad value.

    Anything that will not load is refused rather than worked around. The
    fallback that used to live here is precisely the bug in this file's
    docstring: a default that is silently correct on a laptop and silently wrong
    everywhere else. A url that cannot be resolved is a question for the
    operator, not a value for this module to guess.
    """
    try:
        return Settings().database_url
    except ValidationError:
        raise SystemExit("\n".join(_unloadable_configuration())) from None


def _unloadable_configuration() -> list[str]:
    """`.env` has a value `Settings` will not accept, named without its value.

    Returned rather than printed so what it says can be tested without a
    terminal — the shape `scripts/check_env.py` uses, and for the same reason:
    the one thing that must never appear here is a credential (§1.6). Only the
    variable *names* cross this boundary, and `make check-env` is what renders
    the rest, so there is one voice describing a bad `.env` rather than two.
    """
    problems = config_problems()
    named = [p.env_var for p in problems if p.env_var]
    rules = [p.reason for p in problems if not p.env_var]
    lines = [
        "",
        "alembic: .env has a value that will not load, so there is no url to migrate.",
        "",
    ]
    if named:
        lines += [f"  {', '.join(dict.fromkeys(named))}", ""]
    lines += [f"  {reason}" for reason in rules]
    lines += [
        "  make check-env",
        "    names the line in .env and what is wrong with it. This is the same",
        "    file the api and worker read, so a value that stops a migration here",
        "    is a value that stops them starting.",
        "",
        "  values withheld — some of them are credentials (CLAUDE.md §1.6)",
        "",
    ]
    return lines


def refused_credentials(url: str, exc: BaseException) -> list[str]:
    """A refused password, as the lines to print for it.

    `is_auth_failure` is the platform's own answer to "did the server refuse
    *these credentials*, rather than merely not answer" (SQLSTATE class `28`),
    and this is the third tool to act on it after `preflight` and `status`. The
    advice it earns is the advice those two give: a `28` is a state that ends
    only when a human changes a password on one side, so "try again" and
    "`make up`" are both wrong answers.

    **The driver's message is deliberately not quoted.** asyncpg is entitled to
    put the DSN it failed with into it — `tests/unit/test_database_auth_failure.py`
    pins that it does — and the DSN carries the password. The class name and the
    SQLSTATE say everything the message did without carrying that.
    """
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None) or "28"
    return [
        "",
        "alembic: the database refused these credentials. The migration did not run.",
        "",
        f"  {make_url(url).render_as_string(hide_password=True)}",
        f"    answered, read this password, and refused it ({type(exc).__name__},",
        f"    SQLSTATE {sqlstate}). The server is up and it said no, so nothing here",
        "    is waiting on a container: retrying, `make up` and `make migrate` again",
        "    all fail identically until a password changes on one side or the other.",
        "",
        "  make check-env",
        "    says which side moved. Either .env carries a value that cannot survive",
        "    the trip to Postgres, or the password was rotated against a volume that",
        "    initdb read POSTGRES_PASSWORD from once and never again.",
        '    docs/RUNBOOK.md, "password authentication failed", has both fixes.',
        "",
        "  password withheld — this is a credential (CLAUDE.md §1.6)",
        "",
    ]


def include_object(obj, name, type_, reflected, compare_to):
    """Keep TimescaleDB's internal chunk tables out of autogenerate.

    Without this, every `make revision` proposes dropping the hypertable chunks —
    and one day someone will accept it.
    """
    return not (type_ == "table" and name.startswith(("_hyper_", "_timescaledb")))


def run_migrations_offline(url: str) -> None:
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, include_object=include_object
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online(url: str) -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        url=url,
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def main() -> None:
    url = database_url()
    if context.is_offline_mode():
        run_migrations_offline(url)
        return
    try:
        asyncio.run(run_migrations_online(url))
    except BaseException as exc:
        if not is_auth_failure(exc):
            raise
        # Printed and exited rather than re-raised: the traceback that used to
        # end here is sixty frames of SQLAlchemy's pool machinery above one
        # meaningful line, and it is the shape docs/RUNBOOK.md tells an operator
        # to read backwards. A non-zero exit still fails `make migrate`.
        print("\n".join(refused_credentials(url, exc)), file=sys.stderr)
        raise SystemExit(1) from None


main()
