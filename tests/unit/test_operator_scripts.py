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
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

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
