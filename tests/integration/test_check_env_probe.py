"""`make check-env` asks the database whether it accepts the password in `.env`.

This cannot be a unit test. The whole value of the check is that it agrees with
what Postgres *actually does* with a password, and every interesting part of
that lives outside Python: whether a wrong password is refused rather than
ignored, whether the refusal carries the SQLSTATE the classifier reads, and
whether a right one is accepted over a real scram-sha-256 handshake. A fake
raising what we believe asyncpg raises proves none of it — it is the same
reasoning `test_bar_repository.py` gives for not unit-testing `ON CONFLICT`.

The fault under test is the one docs/RUNBOOK.md calls the dominant cause in the
field: `POSTGRES_PASSWORD` is read by initdb and never again, so a volume that
already existed keeps whatever password it was created with. Rotate
`ATP_DB_PASSWORD` against one and every container sends a new password to a
database that still wants the old — with a `.env` that is correct and passes
every static check in `scripts/check_env.py`. The service container here stands
in for that volume: it was initialised with one password, and the wrong-password
case below is what a rotated `.env` looks like to it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> Any:
    """Import a script by path — `scripts/` is a set of entry points, not a
    package. Same approach as `tests/unit/test_env_doctor.py`."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_env = _load("check_env")


def _with_password(dsn: str, password: str) -> str:
    """The same database, reached with a different password.

    Built through SQLAlchemy's own url object rather than by string surgery, so
    a password needing escaping is rendered the way the thing under test will
    read it back.
    """
    return make_url(dsn).set(password=password).render_as_string(hide_password=False)


class TestAskingARealDatabase:
    def test_the_password_the_database_was_built_with_is_accepted(self, database_url: str) -> None:
        """The green path, and the only statement `make check-env` makes that is
        confirmed rather than inferred."""
        assert check_env.probe_stored_password(database_url) is check_env.Probe.ACCEPTED

    def test_a_rotated_password_is_refused(self, database_url: str) -> None:
        """The fault. This database was initialised with one password; `.env`
        now carries another. Nothing about the file is wrong and every static
        check passes — and this is the only thing in the repository that notices.
        """
        rotated = _with_password(database_url, "rotated-in-env-after-initdb")
        assert check_env.probe_stored_password(rotated) is check_env.Probe.REFUSED

    def test_a_database_that_is_not_there_is_not_a_finding(self, database_url: str) -> None:
        """Silence is not evidence about a stored password. Reporting it as one
        would send an operator to rotate a password that was never wrong — and
        a stack that is down is much the commonest way this command is run."""
        nowhere = make_url(database_url).set(port=5599).render_as_string(hide_password=False)
        assert check_env.probe_stored_password(nowhere) is check_env.Probe.UNREACHABLE

    def test_a_refusal_is_reported_without_the_password(self, database_url: str) -> None:
        """§1.6, checked against a refusal the driver really produced: asyncpg is
        entitled to quote the DSN it failed with, and the DSN carries the
        password."""
        secret = "sup3r-s3cret-db-password"
        rotated = _with_password(database_url, secret)

        assert check_env.probe_stored_password(rotated) is check_env.Probe.REFUSED
        report = "\n".join(check_env.describe_refusal(rotated, {"DATABASE_URL": 7}))
        assert secret not in report
        assert secret not in check_env.where_it_asked(rotated)

    def test_the_probe_leaves_no_connection_behind(self, database_url: str) -> None:
        """`max_connections` is finite and this runs on a stack already in
        trouble. Ten probes must cost nothing that outlives them."""
        for _ in range(10):
            assert check_env.probe_stored_password(database_url) is check_env.Probe.ACCEPTED
