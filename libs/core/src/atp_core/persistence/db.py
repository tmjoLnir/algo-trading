"""Async engine and session management.

Also the one place that decides what a database failure *means*. A driver
exception is not self-describing: `InvalidPasswordError` and `UniqueViolation`
arrive by the same route and call for opposite responses, and every caller that
has to work that out for itself gets it wrong in the same direction — reporting
an outage as a bug in this repository. `is_unavailable` makes the call once and
`session_scope` acts on it, so a repository method raises either
`DatabaseUnavailableError` (Postgres is not there) or the driver's own error
(the statement was wrong) and a caller can simply believe it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from atp_core.errors import DatabaseUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

#: SQLSTATE *classes* where the whole class means "not now", never "not like
#: that". `08` connection exception, `28` invalid authorization specification —
#: the wrong password is `28P01`, which is the one that started this — and `53`
#: insufficient resources, which is Postgres out of connections, memory or disk.
#: Every code in all three is the server declining to serve this process; none
#: of them is a statement being wrong.
UNAVAILABLE_SQLSTATE_CLASSES: Final = frozenset({"08", "28", "53"})

#: Individual codes from classes that are a mix. Class `57` is "operator
#: intervention", which holds both a database shutting down under us and a query
#: somebody cancelled (`57014`) — and a cancelled query is not an outage, so the
#: class is spelled out code by code rather than taken whole.
UNAVAILABLE_SQLSTATES: Final = frozenset(
    {
        "3D000",  # invalid_catalog_name — the database in the DSN does not exist
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now — Postgres is still starting up
        "57P04",  # database_dropped
    }
)


def is_unavailable(exc: BaseException) -> bool:
    """Is this the database being out of reach, rather than a bad statement?

    Three signals, in the order they can be trusted:

    1. **SQLAlchemy invalidated the connection.** It had one, it broke, and
       SQLAlchemy has already decided the pool cannot reuse it. Nothing beats
       the library's own verdict on its own connection.
    2. **An `OSError`.** Refused, unresolvable, timed out — the socket never
       carried a query, so nothing about the request can be at fault. Broad on
       purpose, and safe because of *where* this is asked: inside a session
       scope, where the only thing performing I/O is the database driver.
    3. **A SQLSTATE Postgres assigned.** The authoritative answer when a server
       was reached and refused — `28P01` for the wrong password, `3D000` for a
       database that is not there, `57P03` for one still starting up.

    Anything else is a statement that failed, which is a bug here and must keep
    surfacing as one. In particular an `IntegrityError` (`23505`) is not in any
    set above, because `PostgresStrategyRepository.create` reads it as a
    duplicate name and answers 409 — reclassifying it would turn a user's
    mistake into an outage report.

    Read off the exception by attribute rather than by importing asyncpg: this
    module already names one driver in a DSN and does not need to name it in an
    `isinstance` too, and psycopg spells the same value `pgcode`.
    """
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True

    # SQLAlchemy wraps a driver error as `.orig`; at connect time it does not
    # wrap it at all — the traceback the outage produced ended in asyncpg's own
    # `connect_utils.py` — so the original may be either the wrapper's payload
    # or the exception itself.
    original = getattr(exc, "orig", None) or exc
    if isinstance(original, OSError):
        return True

    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if not isinstance(sqlstate, str):
        return False
    return sqlstate[:2] in UNAVAILABLE_SQLSTATE_CLASSES or sqlstate in UNAVAILABLE_SQLSTATES


@contextmanager
def _as_unavailable() -> Iterator[None]:
    """Re-raise an unreachable database as `DatabaseUnavailableError`.

    Synchronous, and wrapping `async with` blocks rather than living inside
    them: an exception propagates out of an async context manager through an
    enclosing `with` exactly as it would through any other frame, and the
    failure being caught here can come from opening the session, running a
    statement, or closing it.
    """
    try:
        yield
    except DatabaseUnavailableError:
        # Already classified — one scope nested inside another must not rewrap
        # it and bury the driver's exception a second `__cause__` deep.
        raise
    except Exception as exc:
        if is_unavailable(exc):
            raise DatabaseUnavailableError(exc) from exc
        raise


def create_engine(database_url: str, echo: bool = False) -> Any:
    """Build the async engine.

    `pool_pre_ping` matters here: the worker holds connections idle across quiet
    market periods, and a stale connection surfaces as a failure at exactly the
    moment a signal fires.
    """
    return create_async_engine(
        database_url, echo=echo, pool_pre_ping=True, pool_size=10, max_overflow=20
    )


def create_session_factory(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on any exception.

    An unreachable database leaves here as `DatabaseUnavailableError` rather
    than as whatever the driver raised. See `is_unavailable` for the line, and
    `read_scope` for the same translation without the commit.
    """
    with _as_unavailable():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


@asynccontextmanager
async def read_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """A session for reads: no commit, and the same unavailability translation.

    Separate from `session_scope` because committing a read is a round trip to
    say nothing happened, and the readers that use this are the hot ones — the
    runner asks `get_last_n_bars` for every symbol on every bar.

    It exists at all, rather than callers opening the factory themselves, for
    the translation: a bare `factory()` puts asyncpg's connection failure
    straight into the caller's lap, and then every caller has to know what
    asyncpg raises. That is exactly the gap the analytics endpoints fell
    through — they reach the bar store for MAE/MFE, so a reader left outside
    this would have gone on answering 500 while its neighbours answered 503.
    """
    with _as_unavailable():
        async with factory() as session:
            yield session
