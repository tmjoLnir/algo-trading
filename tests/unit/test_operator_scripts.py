"""The operator scripts, as far as they can be tested without a live service.

`halt.py` is the one somebody runs while something is going wrong, and until
the dashboard exists it is the *only* path to the kill switch. So what is
tested here is everything that can go wrong before it reaches Redis: the
argument guards, and the rendering an operator reads under pressure.

What is deliberately not tested is the Redis round trip. `RedisKillSwitch` owns
that and has its own tests, including against a real Redis in
`tests/integration/test_kill_switch.py`; re-testing it through a fake here
would assert that argparse calls the method it obviously calls.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from atp_core.alerts import Severity
from atp_core.config import Settings
from atp_core.risk.killswitch import HaltReason, HaltRecord, HaltScope

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> Any:
    """Import a script by path. `scripts/` is not a package — it is a set of
    entry points run with `uv run python scripts/x.py`, and giving it an
    `__init__.py` to make this tidier would make them importable in ways they
    are not meant to be."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


halt = _load("halt")
status = _load("status")
check_alerts = _load("check_alerts")


class TestHaltArguments:
    def test_engage_demands_a_name(self) -> None:
        """ "Who stopped trading" is the first question anyone asks afterwards."""
        with pytest.raises(SystemExit):
            halt.parse_args(["engage"])

    def test_clear_demands_a_name(self) -> None:
        """Deliberately asymmetric with engage in spirit but not in this: the
        kill switch itself refuses an empty `cleared_by`, and the CLI should
        refuse before the round trip rather than after."""
        with pytest.raises(SystemExit):
            halt.parse_args(["clear"])

    def test_status_needs_nothing(self) -> None:
        """Asking whether trading is stopped must never be the hard part."""
        assert halt.parse_args(["status"]).command == "status"

    def test_the_default_scope_is_everything(self) -> None:
        """An operator reaching for this in an incident wants to stop the
        platform, not to remember which scope they meant."""
        assert halt.parse_args(["engage", "--by", "jo"]).scope == HaltScope.GLOBAL.value

    def test_the_default_reason_is_manual(self) -> None:
        """The automated reasons belong to the code that detects them."""
        assert halt.parse_args(["engage", "--by", "jo"]).reason == HaltReason.MANUAL.value

    def test_an_unknown_reason_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            halt.parse_args(["engage", "--by", "jo", "--reason", "vibes"])


class TestHaltScopeGuards:
    """These run before anything touches Redis, so a mistyped command fails in
    a shell rather than halfway through a halt."""

    def test_a_narrowed_scope_without_a_target_is_refused(self) -> None:
        with pytest.raises(SystemExit, match="needs --target"):
            halt.main(["engage", "--by", "jo", "--scope", "symbol"])

    def test_a_strategy_scope_without_a_target_is_refused(self) -> None:
        with pytest.raises(SystemExit, match="needs --target"):
            halt.main(["engage", "--by", "jo", "--scope", "strategy"])

    def test_a_global_scope_with_a_target_is_refused(self) -> None:
        """Silently ignoring it would let somebody believe they had halted one
        symbol when they had halted everything."""
        with pytest.raises(SystemExit, match="meaningless"):
            halt.main(["engage", "--by", "jo", "--scope", "global", "--target", "SPY"])

    def test_the_guards_apply_to_clear_too(self) -> None:
        with pytest.raises(SystemExit, match="needs --target"):
            halt.main(["clear", "--by", "jo", "--scope", "symbol"])


class TestHaltRendering:
    @staticmethod
    def record(**overrides: Any) -> HaltRecord:
        fields: dict[str, Any] = {
            "scope": HaltScope.GLOBAL,
            "reason": HaltReason.MANUAL,
            "engaged_at": datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
            "engaged_by": "jo",
            "detail": "",
            "target": None,
        }
        fields.update(overrides)
        return HaltRecord(**fields)

    def test_it_names_who_and_when(self) -> None:
        """The two things anyone asks about a halt they did not place."""
        rendered = halt._render(self.record())

        assert "jo" in rendered
        assert "2024-06-03T14:30:00+00:00" in rendered

    def test_a_narrowed_scope_shows_its_target(self) -> None:
        rendered = halt._render(self.record(scope=HaltScope.SYMBOL, target="SPY"))

        assert "symbol:SPY" in rendered

    def test_an_empty_detail_is_omitted_rather_than_blank(self) -> None:
        assert "detail" not in halt._render(self.record())
        assert "detail" in halt._render(self.record(detail="feed looked wrong"))

    def test_halted_status_exits_non_zero(self) -> None:
        """So it composes with a shell `if` and a health check. Named as a
        constant rather than a bare 2, because a reader who assumes 0-means-ok
        would otherwise read a halt as a failure to check."""
        assert halt.EXIT_HALTED != 0


class TestStatusArguments:
    def test_it_needs_nothing(self) -> None:
        """Read-only and safe during an incident — it should never be the
        command you get wrong."""
        assert status.parse_args([]).symbols is None

    def test_the_broker_can_be_skipped(self) -> None:
        """Local state is still worth seeing when there are no credentials, or
        when the venue is the thing that is down."""
        assert status.parse_args(["--no-broker"]).no_broker is True

    def test_an_unknown_timeframe_is_refused_by_name(self) -> None:
        with pytest.raises(SystemExit, match="--timeframe must be one of"):
            import asyncio

            asyncio.run(status.main(["--timeframe", "1fortnight"]))


class TestCheckAlertsArguments:
    """The alert checker, minus the sending.

    What it does over the wire is the one thing no test here can cover — the
    whole reason the script exists is that a live send is the only proof, and
    CLAUDE.md §1.7 keeps this suite off live endpoints. So what is tested is
    what it decides before it gets there.
    """

    def test_it_demands_a_name(self) -> None:
        """The name goes into the message. Whoever gets a `critical` at 03:00
        should be able to see from the notification that it was a drill and who
        to ask about it, without opening anything."""
        with pytest.raises(SystemExit):
            check_alerts.parse_args([])

    def test_by_itself_is_enough(self) -> None:
        assert check_alerts.parse_args(["--by", "jo"]).severity is None

    def test_an_unknown_severity_is_refused(self) -> None:
        """Rather than sending nothing and exiting 0, which would read exactly
        like a working alert path."""
        with pytest.raises(SystemExit):
            check_alerts.parse_args(["--by", "jo", "--severity", "panic"])

    def test_no_severity_means_all_of_them(self) -> None:
        """The levels differ in whether they make a phone ring, and that is the
        part of the configuration most likely to be wrong."""
        assert check_alerts._severities(None) == list(Severity)

    def test_severities_are_deduplicated_and_ordered(self) -> None:
        """Typed in any order, sent in declaration order — so the messages land
        in the chat in the order their levels escalate."""
        chosen = check_alerts._severities(["critical", "info", "critical"])
        assert chosen == [Severity.INFO, Severity.CRITICAL]

    def test_it_reports_the_transports_the_factory_would_build(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            ALERT_NTFY_TOPIC="atp-abc123",
            ALERT_TELEGRAM_TOKEN="123:AAF",
            ALERT_TELEGRAM_CHAT_ID="9876",
        )
        assert check_alerts._configured_transports(settings) == ["ntfy", "telegram"]

    def test_half_a_telegram_configuration_is_not_a_transport(self) -> None:
        """It is not one for `build_alert_sink` either, and a checker that
        disagreed with the factory would report a transport that cannot
        deliver — the exact belief this script exists to correct."""
        settings = Settings(ALERT_TELEGRAM_TOKEN="123:AAF")  # type: ignore[call-arg]
        assert check_alerts._configured_transports(settings) == []

    def test_nothing_configured_is_not_a_success(self) -> None:
        """An unconfigured platform is the failure this script is for. Exiting
        0 on it would confirm precisely the thing that is not true."""
        assert check_alerts.EXIT_UNCONFIGURED != 0
        assert check_alerts.EXIT_UNDELIVERED != 0
        assert check_alerts.EXIT_UNCONFIGURED != check_alerts.EXIT_UNDELIVERED


class TestHashPasswordDependencies:
    """The first command a new operator runs, and the one that used to fail worst.

    `scripts/hash_password.py` puts `apps/api/src` on `sys.path`, so the
    first-party import resolves from a bare checkout and then dies one layer
    down on `bcrypt` — a third-party package that has to be genuinely
    installed. It is declared by `atp-api`, a workspace member, while the
    workspace ROOT declares no runtime dependencies at all, so a plain
    `uv sync` leaves the script importable and unusable.

    What that produced was `ModuleNotFoundError: No module named 'bcrypt'` and
    a traceback through `auth.py`, at the moment a person has the least context
    to act on it — before there is any password to sign in with at all. These
    pin the message that replaced it, in a real subprocess, because the failure
    is an import failure and cannot be reached by importing the module.
    """

    @staticmethod
    def _run_without_bcrypt(tmp_path: Path) -> subprocess.CompletedProcess[str]:
        """Run the script for real, with `bcrypt` made unimportable.

        A `sitecustomize` on `PYTHONPATH` rather than an uninstall: the point is
        to reproduce the *user's* environment without wrecking the one the rest
        of the suite runs in. `find_spec` raising with `name=` set is what makes
        `ImportError.name` carry `bcrypt`, which is the same thing a genuinely
        absent package produces and is what the message quotes back.
        """
        (tmp_path / "sitecustomize.py").write_text(
            "import sys\n"
            "class _Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'bcrypt':\n"
            "            raise ImportError(\"No module named 'bcrypt'\", name='bcrypt')\n"
            "        return None\n"
            "sys.meta_path.insert(0, _Block())\n"
        )
        env = {**os.environ, "PYTHONPATH": str(tmp_path)}
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "hash_password.py")],
            capture_output=True,
            text=True,
            input="",
            env=env,
            timeout=120,
        )

    def test_a_missing_dependency_names_itself_and_the_remedy(self, tmp_path: Path) -> None:
        result = self._run_without_bcrypt(tmp_path)

        assert result.returncode != 0, "a script that cannot hash must not exit 0"
        assert "bcrypt is not installed" in result.stderr
        # Both ways out, because they are not equivalent: one fixes the
        # environment for everything else too, the other works without
        # re-syncing anything.
        assert "uv sync --all-packages" in result.stderr
        assert "uv run --package atp-api" in result.stderr

    def test_it_does_not_traceback(self, tmp_path: Path) -> None:
        """A traceback is what this replaced. It named `bcrypt` in a frame of
        `auth.py` and said nothing about how to proceed."""
        result = self._run_without_bcrypt(tmp_path)

        assert "Traceback (most recent call last)" not in result.stderr
        assert "ModuleNotFoundError" not in result.stderr

    def test_the_message_survives_an_unnamed_import_error(self) -> None:
        """`ImportError.name` is not always set — a re-raise inside a package
        loses it. The remedy must not depend on knowing which module was
        missing, so the fallback still reads as a sentence."""
        hash_password_script = _load("hash_password")
        message = hash_password_script._dependency_help("A dependency")

        assert message.startswith("A dependency is not installed")
        assert "uv sync --all-packages" in message
