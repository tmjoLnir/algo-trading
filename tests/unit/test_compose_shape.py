"""`scripts/check_port_bindings.py`, on the two questions it answers about shape.

The port-binding half is exercised against the real compose files by
`make check-bindings`, in CI and as a pre-flight on `make up-prod`. What is worth
pinning here is the part that reads a *resolved* configuration and decides
whether it is deployable, because both assertions in it were written after a
production-shaped failure and both have a way of quietly passing.

`test_a_reload_inside_sh_c_is_still_a_reload` is the one to keep. The deployed
overlay strips `--reload` with `!reset`, and the check has to notice when it
did not; an element test against the command list stopped noticing the day the
base file started the API through `sh -c`, because the flag moved one level down
into the string and `"--reload" in [...]` went on being False.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> Any:
    """Import a script by path — `scripts/` is a set of entry points, not a
    package. Same approach as `test_backup_script.py`, for the same reason."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = _load("check_port_bindings")


class TestRestartPolicies:
    def test_a_service_without_a_policy_is_reported(self) -> None:
        problems = check.check_restart_policies(
            "development",
            {
                "api": {"restart": "unless-stopped"},
                "web": {},
            },
        )
        assert len(problems) == 1
        assert "web" in problems[0]

    def test_every_service_carrying_a_policy_passes(self) -> None:
        assert (
            check.check_restart_policies(
                "development",
                {"api": {"restart": "unless-stopped"}, "web": {"restart": "unless-stopped"}},
            )
            == []
        )

    def test_the_development_configuration_is_checked_too(self) -> None:
        """The regression that put this function here.

        This check ran against the deployed configuration alone, and the
        deployed overlay puts the dev server behind a profile — so `web`, the
        one service in the repository actually missing a policy, was absent from
        the only configuration anything looked at. A reboot brought back the
        API, the worker, the queue and both stores, and left the thing serving
        the dashboard stopped.
        """
        assert [label for label, _ in check.CONFIGS] == ["development", "deployed"]


class TestDeployedShape:
    def test_a_source_mount_is_reported(self) -> None:
        problems = check.check_deployed_shape(
            {
                "api": {"volumes": [{"source": "/repo/libs", "target": "/app/libs"}]},
                "worker": {},
            }
        )
        assert any("bind-mounts" in p for p in problems)

    def test_a_reload_inside_sh_c_is_still_a_reload(self) -> None:
        """The flag one level down inside the command string.

        `docker-compose.yml` starts the API as
        `sh -c "python -c 'import atp_api.main' && exec uvicorn ... --reload"`,
        so that a configuration it cannot import is an exit rather than a
        reloader idling forever with nothing bound to the port. The overlay
        resets that command; a compose that ignored `!reset` would leave it in
        place, and an element test against the resolved list would not see it.
        """
        problems = check.check_deployed_shape(
            {
                "api": {
                    "command": [
                        "sh",
                        "-c",
                        "python -c 'import atp_api.main' && exec uvicorn "
                        "atp_api.main:app --host 0.0.0.0 --port 8000 --reload",
                    ]
                },
                "worker": {},
            }
        )
        assert any("--reload" in p for p in problems)

    def test_the_deployed_command_and_mounts_are_accepted_when_reset(self) -> None:
        assert check.check_deployed_shape({"api": {}, "worker": {}}) == []

    def test_a_missing_code_service_is_reported(self) -> None:
        problems = check.check_deployed_shape({"worker": {}})
        assert any("api is missing" in p for p in problems)


#: Stands in for ATP_DB_PASSWORD. Carried through every case below and looked
#: for in the output, because the one thing this check must never print is the
#: thing it is comparing (CLAUDE.md §1.6).
DEPLOY_PASSWORD = "b8f1c0d4e7a2deadbeef"


def _db(password: str = DEPLOY_PASSWORD) -> dict[str, Any]:
    return {"environment": {"POSTGRES_PASSWORD": password}}


def _client(password: str) -> dict[str, Any]:
    return {"environment": {"DATABASE_URL": f"postgresql+asyncpg://atp:{password}@db:5432/atp"}}


class TestDatabaseCredentials:
    """One value has to reach Postgres and every client of it by two routes.

    `POSTGRES_PASSWORD`, which initdb stores verbatim, and a `DATABASE_URL` per
    service — and the overlay writes that interpolation out once per service, so
    four correct copies and one stale literal is a stack that starts perfectly
    and refuses every query.
    """

    def test_a_service_using_a_different_password_is_reported(self) -> None:
        problems = check.check_database_credentials(
            "deployed",
            {"db": _db(), "api": _client(DEPLOY_PASSWORD), "migrate": _client("atp")},
        )
        assert len(problems) == 1
        assert "migrate" in problems[0]

    def test_every_client_agreeing_passes(self) -> None:
        assert (
            check.check_database_credentials(
                "deployed",
                {
                    "db": _db(),
                    "api": _client(DEPLOY_PASSWORD),
                    "worker": _client(DEPLOY_PASSWORD),
                    "queue": _client(DEPLOY_PASSWORD),
                    "migrate": _client(DEPLOY_PASSWORD),
                },
            )
            == []
        )

    def test_a_service_that_does_not_use_the_database_is_not_a_finding(self) -> None:
        """`redis` and `web-prod` have nothing to disagree about."""
        assert (
            check.check_database_credentials(
                "deployed", {"db": _db(), "redis": {}, "web-prod": {"environment": {}}}
            )
            == []
        )

    def test_a_url_with_no_readable_password_is_reported(self) -> None:
        """Refused rather than guessed, as the host-address check above does."""
        problems = check.check_database_credentials(
            "deployed",
            {
                "db": _db(),
                "api": {"environment": {"DATABASE_URL": "postgresql+asyncpg://atp@db/atp"}},
            },
        )
        assert any("no readable password" in p for p in problems)

    def test_a_configuration_with_no_database_is_reported(self) -> None:
        problems = check.check_database_credentials("deployed", {"api": _client(DEPLOY_PASSWORD)})
        assert any("db is missing" in p for p in problems)

    def test_a_database_with_no_password_is_reported(self) -> None:
        problems = check.check_database_credentials(
            "deployed", {"db": {"environment": {}}, "api": _client(DEPLOY_PASSWORD)}
        )
        assert any("POSTGRES_PASSWORD" in p for p in problems)

    def test_the_password_is_never_printed(self, capsys: Any) -> None:
        """§1.6, in both directions: the failing report and the passing one.

        The passing case matters as much — `make check-bindings` runs on an
        operator's terminal with their real ATP_DB_PASSWORD interpolated in, and
        a check that listed what it compared would put it on screen every time
        it succeeded.
        """
        check.check_database_credentials(
            "deployed", {"db": _db(), "api": _client(DEPLOY_PASSWORD), "migrate": _client("atp")}
        )
        check.check_database_credentials("deployed", {"db": _db(), "api": _client(DEPLOY_PASSWORD)})
        assert DEPLOY_PASSWORD not in capsys.readouterr().out

    def test_the_comparison_is_raw_rather_than_url_decoded(self) -> None:
        """A `%` that does not survive a url is `make check-env`'s finding, not
        this one's. Decoding here would report one fault twice, and the vaguer
        half would land second — the gate `check_env.should_ask_the_database`
        draws for the same reason."""
        problems = check.check_database_credentials(
            "deployed", {"db": _db("p%40ss"), "api": _client("p%40ss")}
        )
        assert problems == []

    def test_both_configurations_resolve_the_migrate_profile(self) -> None:
        """The regression that put this function here.

        `docker compose config` omits a profiled service entirely, so the schema
        step was in neither configuration under test — and it was the service
        with the wrong password in one of them. The same blind spot the
        restart-policy check had over `web`.
        """
        for _, command in check.CONFIGS:
            assert "migrate" in command, command
