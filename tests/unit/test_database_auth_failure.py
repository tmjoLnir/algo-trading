"""A refused password is an outage that waiting does not fix, and the operator
tools have to say so.

The outage in `test_database_unavailable.py` is the same one, one layer out.
That file holds the API's answer to it — one `503` instead of eight `500`s. This
file holds what the two tools an operator runs *because* nothing is working did
with it, which was worse than the API's failure and quieter:

- `scripts/preflight.py` reported it as SKIP and **exited 0**. A skip is the
  honest state of a check whose source was not up yet, and it deliberately does
  not fail the command — but the stack here is up, and the disagreement it found
  resolves for nobody who waits. `make preflight` is the go/no-go before a paper
  week, so a green exit against a database that cannot store a bar buys a week of
  silence that is indistinguishable from a correct run of `sma_crossover`. The
  `fix` it printed was `make up && make migrate`; `make migrate` fails with this
  same error against this same password.
- `scripts/status.py` did not catch it at all. `_print_local` reads the one
  store, so the outage propagated out of it and killed the report **before**
  the broker section — losing the venue's positions and working orders at
  exactly the moment they are the only book anyone can still see.
  docs/RUNBOOK.md told operators this tool still worked during this fault.

The driver's real exception classes are used throughout rather than stand-ins,
for the reason the sibling file gives: a fake that raises what we *believe*
asyncpg raises would have passed against the code that shipped the outage.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from typing import Any

import asyncpg.exceptions as pg
import pytest

from atp_core.config import Settings
from atp_core.domain import Timeframe
from atp_core.errors import DatabaseUnavailableError
from atp_core.persistence.db import is_auth_failure, is_unavailable
from atp_worker.preflight import Preflight, Status

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> Any:
    """Import a script by path — `scripts/` is a set of entry points, not a
    package. Same approach as `test_env_doctor.py`, for the same reason."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight_script = _load("preflight")
status_script = _load("status")

#: Stands in for the password in the DSN. A connection error is free to quote
#: the string it failed to connect with, so it is carried through every case
#: below and looked for in the output (CLAUDE.md §1.6).
SECRET_IN_THE_DSN = "sup3r-s3cret-db-password"

#: The exception the incident actually produced, with the DSN in its message the
#: way asyncpg is entitled to put it there.
WRONG_PASSWORD = pg.InvalidPasswordError(
    f'password authentication failed for user "atp" '
    f"(postgresql+asyncpg://atp:{SECRET_IN_THE_DSN}@db:5432/atp)"
)


def unavailable(cause: BaseException) -> DatabaseUnavailableError:
    """As `session_scope` raises it: the driver's error as the `__cause__`.

    Every caller under test sees the wrapper rather than the driver exception,
    because `read_scope` translates before a repository method returns. A test
    that passed the bare driver error would be testing a path that no longer
    exists.
    """
    try:
        raise DatabaseUnavailableError(cause) from cause
    except DatabaseUnavailableError as wrapped:
        return wrapped


class TestTellingTheTwoOutagesApart:
    """`is_auth_failure` narrows `is_unavailable`, and the line is SQLSTATE 28.

    Everything here is unreachable — `is_unavailable` is `True` for all of it.
    The question is which of those a human has to go and fix a password for.
    """

    @pytest.mark.parametrize(
        "cause",
        [
            pytest.param(WRONG_PASSWORD, id="28P01-wrong-password"),
            pytest.param(
                pg.InvalidAuthorizationSpecificationError("no"), id="28000-bad-authorization"
            ),
        ],
    )
    def test_the_server_answered_and_refused_us(self, cause: BaseException) -> None:
        assert is_auth_failure(cause) is True
        assert is_unavailable(cause) is True

    @pytest.mark.parametrize(
        "cause",
        [
            pytest.param(ConnectionRefusedError(111, "refused"), id="stack-not-up-yet"),
            pytest.param(pg.CannotConnectNowError("starting"), id="57P03-still-starting"),
            pytest.param(pg.TooManyConnectionsError("full"), id="53300-out-of-connections"),
            pytest.param(pg.InvalidCatalogNameError("nodb"), id="3D000-no-such-database"),
        ],
    )
    def test_an_outage_that_is_not_about_credentials(self, cause: BaseException) -> None:
        """Each of these is a state that ends without anyone editing `.env` —
        which is exactly why they must keep their non-failing verdict below."""
        assert is_unavailable(cause) is True
        assert is_auth_failure(cause) is False

    @pytest.mark.parametrize(
        "cause",
        [
            pytest.param(pg.UniqueViolationError("dup"), id="23505-duplicate-name"),
            pytest.param(pg.QueryCanceledError("cancelled"), id="57014-cancelled-query"),
        ],
    )
    def test_a_failed_statement_is_neither(self, cause: BaseException) -> None:
        """A bug in this repository must not be reported as a credential fault.
        `23505` is the 409 a duplicate strategy name deserves."""
        assert is_auth_failure(cause) is False

    def test_it_sees_through_the_wrapper_callers_actually_hold(self) -> None:
        """The case that matters most: `session_scope` has already translated."""
        assert is_auth_failure(unavailable(WRONG_PASSWORD)) is True
        assert is_auth_failure(unavailable(ConnectionRefusedError(111, "refused"))) is False

    def test_a_wrapper_with_no_cause_is_not_guessed_at(self) -> None:
        """Constructed without `raise ... from`, so there is nothing to read.
        Answering `True` here would report a credential fault on no evidence."""
        assert is_auth_failure(DatabaseUnavailableError(RuntimeError("?"))) is False


class TestPreflightRefusesToGiveAGoSignal:
    """The verdict `make preflight` returns, per kind of unreachable database."""

    def test_a_refused_password_fails_the_command(self) -> None:
        """The regression. This exited 0 — a go-live signal for a paper week
        against a platform that could not persist a bar, an order or a fill."""
        check = preflight_script._database_check(
            "history", unavailable(WRONG_PASSWORD), fix="make up && make migrate"
        )
        report = Preflight([check])

        assert check.status is Status.FAIL
        assert report.ready is False
        assert report.exit_code() == 1

    def test_it_does_not_send_the_operator_to_a_command_that_cannot_work(self) -> None:
        """`make migrate` fails with this same error against this same password,
        and `make up` starts a stack that is already up."""
        check = preflight_script._database_check(
            "history", unavailable(WRONG_PASSWORD), fix="make up && make migrate"
        )
        assert "make migrate" not in check.fix
        assert "check-env" in check.fix
        assert "RUNBOOK" in check.source

    @pytest.mark.parametrize(
        "cause",
        [
            pytest.param(ConnectionRefusedError(111, "refused"), id="stack-not-up-yet"),
            pytest.param(pg.CannotConnectNowError("starting"), id="57P03-still-starting"),
        ],
    )
    def test_a_database_that_is_merely_not_up_still_skips(self, cause: BaseException) -> None:
        """Not narrowed by accident. An operator bringing the stack up a piece
        at a time runs this against a half-started machine on purpose, and a
        SKIP that failed the command would make the local-only run useless."""
        check = preflight_script._database_check("history", unavailable(cause), fix="make up")
        report = Preflight([check])

        assert check.status is Status.SKIP
        assert report.exit_code() == 0

    def test_the_detail_names_the_driver_verdict_not_the_wrapper(self) -> None:
        """`database unreachable (DatabaseUnavailableError)` says the same word
        twice and drops `InvalidPasswordError`, which is most of the diagnosis."""
        check = preflight_script._database_check(
            "history", unavailable(WRONG_PASSWORD), fix="make up"
        )
        assert "InvalidPasswordError" in check.detail
        assert "DatabaseUnavailableError" not in check.detail

    def test_no_check_ever_carries_the_password(self) -> None:
        check = preflight_script._database_check(
            "history", unavailable(WRONG_PASSWORD), fix="make up"
        )
        assert SECRET_IN_THE_DSN not in f"{check.detail} {check.fix} {check.source}"


class _DisposableEngine:
    """Enough of an engine for `_print_local`'s `finally` to dispose of it."""

    async def dispose(self) -> None:
        return None


class RefusingBars:
    """A bar repository against a database that refuses the credentials."""

    def __init__(self, cause: BaseException) -> None:
        self._cause = cause

    async def get_last_n_bars(self, *args: object, **kwargs: object) -> list[object]:
        raise unavailable(self._cause)


class TestStatusSurvivesTheOutage:
    """`scripts/status.py` is read during the incident, so it has to finish."""

    @pytest.fixture
    def local_report(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """`_print_local` with Redis answering and Postgres refusing — the shape
        the runbook describes, where `/readyz` says `redis: ok`."""

        async def _no_quotes(self: object, symbols: list[str]) -> dict[str, object]:
            return {}

        async def _close(_: object) -> None:
            return None

        monkeypatch.setattr(status_script.RedisQuoteCache, "get_quotes", _no_quotes)
        monkeypatch.setattr(status_script, "create_redis", lambda url: None)
        monkeypatch.setattr(status_script, "close_redis", _close)
        monkeypatch.setattr(status_script, "create_engine", lambda url: _DisposableEngine())
        monkeypatch.setattr(status_script, "create_session_factory", lambda engine: engine)

        def run(cause: BaseException) -> str:
            monkeypatch.setattr(
                status_script, "PostgresBarRepository", lambda factory: RefusingBars(cause)
            )
            settings = Settings(database_url="postgresql+asyncpg://atp:x@127.0.0.1:5599/atp")
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                asyncio.run(status_script._print_local(settings, ["SPY"], Timeframe.D1))
            return buffer.getvalue()

        return run

    def test_it_returns_instead_of_dying(self, local_report: Any) -> None:
        """The regression. This raised, and `_print_broker` — the venue's own
        book, the only one still readable — never ran."""
        output = local_report(WRONG_PASSWORD)
        assert "UNREACHABLE" in output

    def test_it_says_the_refusal_will_not_resolve_on_its_own(self, local_report: Any) -> None:
        output = local_report(WRONG_PASSWORD)
        assert "check-env" in output
        assert "password authentication failed" in output

    def test_an_ordinary_outage_gets_no_credential_advice(self, local_report: Any) -> None:
        """Telling someone to check a password when the stack is simply not up
        is the same misdirection pointing the other way."""
        output = local_report(ConnectionRefusedError(111, "refused"))
        assert "UNREACHABLE" in output
        assert "check-env" not in output

    def test_the_report_never_prints_the_password(self, local_report: Any) -> None:
        assert SECRET_IN_THE_DSN not in local_report(WRONG_PASSWORD)
