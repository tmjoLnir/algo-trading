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
