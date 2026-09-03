"""Where `make migrate` gets its database url, against a real Postgres.

This file exists because of a bug it now makes impossible. `infra/alembic/env.py`
read `os.environ` and fell back to a hardcoded `atp:atp@localhost` — and
`uv run alembic` does not load `.env`, so a host-side `make migrate` never saw
the url the operator had written there. Every other host-side tool did, through
`Settings`. On a stack whose password is not the development `atp`, the first
command in every runbook failed with

    asyncpg.exceptions.InvalidPasswordError: password authentication failed
    for user "atp"

against a `.env` that was correct.

**CI could not have caught it and neither could a unit test.** CI's service
container uses `atp:atp`, which is the fallback — the bug is invisible whenever
the wrong url happens to be the right one. So the tests here put a password in
`.env` that the database will *refuse*: under the old module the refusal never
happened, because the file was never read. A green run is the file being read.

Nothing here writes to the repository. Alembic is invoked from a temporary
directory with a copy of the ini whose two relative paths are made absolute,
because `.env` is resolved against the working directory and a test that wrote
one into the repo root would destroy a developer's credentials to make a point.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "infra" / "alembic" / "alembic.ini"

#: Never a real credential, and never one that could be right by accident: the
#: point of every assertion below is that the database *refuses* it.
WRONG_PASSWORD = "rotated-against-the-volume-and-never-valid"


def _ini_with_absolute_paths(tmp_path: Path) -> Path:
    """The repo's alembic.ini, runnable from somewhere that is not the repo.

    `script_location` and `prepend_sys_path` are relative to the working
    directory, and the working directory is the whole point here — it is what
    `Settings` resolves `.env` against. Rewritten rather than hand-written so
    the logging configuration and every other option stay exactly as shipped.
    """
    text = ALEMBIC_INI.read_text()
    text = text.replace(
        "script_location = infra/alembic", f"script_location = {REPO_ROOT}/infra/alembic"
    )
    text = text.replace(
        "prepend_sys_path = libs/core/src", f"prepend_sys_path = {REPO_ROOT}/libs/core/src"
    )
    ini = tmp_path / "alembic.ini"
    ini.write_text(text)
    return ini


def _run_alembic(
    cwd: Path, *args: str, database_url: str | None = None
) -> subprocess.CompletedProcess[str]:
    """`alembic` as the Makefile runs it, from `cwd`, with `DATABASE_URL` controlled.

    `database_url=None` *removes* the variable rather than leaving it unset in
    the parent — CI exports one, and a test that inherited it would be asking
    about the environment when the question is about `.env`.
    """
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    if database_url is not None:
        env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(_ini_with_absolute_paths(cwd)), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )


def _write_env(cwd: Path, **values: str) -> None:
    cwd.joinpath(".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def _with_password(url: str, password: str) -> str:
    return make_url(url).set(password=password).render_as_string(hide_password=False)


class TestTheUrlComesFromSettings:
    """`.env` is configuration; `os.environ` is only one of the places it lives."""

    def test_a_url_in_dotenv_is_used(self, database_url: str, tmp_path: Path) -> None:
        _write_env(tmp_path, DATABASE_URL=database_url)

        result = _run_alembic(tmp_path, "current")

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    def test_a_password_in_dotenv_is_the_one_actually_sent(
        self, database_url: str, tmp_path: Path
    ) -> None:
        """The regression test, and the only one whose failure is silent.

        A wrong password in `.env` must reach Postgres and be refused. The old
        module ignored the file and sent the fallback — which on this database
        is *correct* — so this passed as a green `alembic current` and the fault
        only ever appeared on a machine nobody was testing on.
        """
        _write_env(tmp_path, DATABASE_URL=_with_password(database_url, WRONG_PASSWORD))

        result = _run_alembic(tmp_path, "current")

        assert result.returncode == 1
        assert "the database refused these credentials" in result.stderr

    def test_the_environment_still_wins_over_dotenv(
        self, database_url: str, tmp_path: Path
    ) -> None:
        """The container path, which must not change.

        Compose sets `DATABASE_URL` on the `migrate` service, and an image that
        happened to carry a `.env` must not outrank it.
        """
        _write_env(tmp_path, DATABASE_URL=_with_password(database_url, WRONG_PASSWORD))

        result = _run_alembic(tmp_path, "current", database_url=database_url)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


class TestARefusedPasswordIsDiagnosed:
    """SQLSTATE `28` is a state that ends only when a human changes a password.

    So it is reported the way `preflight` and `status` report it, rather than as
    the sixty-frame traceback docs/RUNBOOK.md has to tell an operator to read
    backwards.
    """

    @pytest.fixture
    def refusal(self, database_url: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        return _run_alembic(
            tmp_path, "current", database_url=_with_password(database_url, WRONG_PASSWORD)
        )

    def test_it_fails_the_command(self, refusal: subprocess.CompletedProcess[str]) -> None:
        assert refusal.returncode == 1

    def test_it_names_the_fault_and_the_command_that_explains_it(
        self, refusal: subprocess.CompletedProcess[str]
    ) -> None:
        assert "refused these credentials" in refusal.stderr
        assert "make check-env" in refusal.stderr
        assert (
            "password authentication failed" in refusal.stderr.lower() or "28P01" in refusal.stderr
        )

    def test_it_says_waiting_will_not_help(self, refusal: subprocess.CompletedProcess[str]) -> None:
        """The distinction `is_auth_failure` exists to draw. An operator who
        reads this must not go and run `make up` again."""
        assert "the server is up and it said no" in refusal.stderr.lower()

    def test_it_does_not_print_the_password(
        self, refusal: subprocess.CompletedProcess[str]
    ) -> None:
        """CLAUDE.md §1.6. asyncpg puts the DSN it failed with into its own
        message — `tests/unit/test_database_auth_failure.py` pins that it does —
        so quoting the driver would have leaked the credential."""
        assert WRONG_PASSWORD not in refusal.stderr
        assert WRONG_PASSWORD not in refusal.stdout

    def test_it_replaces_the_traceback_rather_than_decorating_it(
        self, refusal: subprocess.CompletedProcess[str]
    ) -> None:
        assert "Traceback (most recent call last)" not in refusal.stderr

    def test_it_names_the_database_it_asked(
        self, refusal: subprocess.CompletedProcess[str], database_url: str
    ) -> None:
        """A developer with an unrelated Postgres on 5432 gets a true statement
        about the wrong server; this is the line that shows it. Same reason
        `scripts/check_env.py` prints `where_it_asked`."""
        url = make_url(database_url)
        assert str(url.host) in refusal.stderr
        assert str(url.database) in refusal.stderr


class TestAnUnloadableEnvFileIsNamed:
    """A `.env` `Settings` will not accept stops the migration and says which key.

    The fallback that used to stand in for this is the bug the module docstring
    describes: a default that is silently correct on a laptop and silently wrong
    everywhere else.
    """

    @pytest.fixture
    def unloadable(self, database_url: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        _write_env(tmp_path, DATABASE_URL=database_url, API_LOGIN_ATTEMPTS="twelve")
        return _run_alembic(tmp_path, "current")

    def test_it_fails_the_command(self, unloadable: subprocess.CompletedProcess[str]) -> None:
        assert unloadable.returncode == 1

    def test_it_names_the_variable_as_it_was_written(
        self, unloadable: subprocess.CompletedProcess[str]
    ) -> None:
        """`API_LOGIN_ATTEMPTS` is what is in the file; `api_login_attempts` is
        what a pydantic traceback gives you."""
        assert "API_LOGIN_ATTEMPTS" in unloadable.stderr

    def test_it_points_at_the_one_tool_that_renders_the_rest(
        self, unloadable: subprocess.CompletedProcess[str]
    ) -> None:
        assert "make check-env" in unloadable.stderr

    def test_it_withholds_the_values(self, unloadable: subprocess.CompletedProcess[str]) -> None:
        """Some of them are credentials, and this code cannot tell which."""
        assert "twelve" not in unloadable.stderr

    def test_an_unrelated_bad_value_does_not_stop_a_schema_migration(
        self, database_url: str, tmp_path: Path
    ) -> None:
        """A value that has nothing to do with a schema must not block one.

        This was written when the risk ceilings were `RISK_*` variables and
        `Settings.risk` was a nested `default_factory`, which made one bad
        ceiling raise during `Settings()` and take the migration with it. The
        ceilings are a database row now, so the specific trap is gone — but the
        property is about `env.py` reading only `DATABASE_URL` out of the file,
        which is what the runbook's first command depends on, and any other
        unparseable value still exercises it.
        """
        _write_env(tmp_path, DATABASE_URL=database_url, ENGINE_TICK_INTERVAL_SECONDS="not-a-number")

        result = _run_alembic(tmp_path, "current")

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
