"""Naming the value in `.env` that stops the platform starting.

The behaviour under test is a diagnosis, so the thing worth pinning is that it
points at the *right* line. Two of these exist because the first implementation
got them wrong, and both mistakes were the kind that leave a green check over a
misleading answer rather than a failure:

- `test_every_broken_value_is_reported_in_one_run` — `Settings.risk` is a
  `default_factory`, so a bad `RISK_*` value raises out of it *during*
  `Settings()` and takes the rest of the validation with it. The first version
  reported one bad risk limit and stayed silent about every other broken value
  in the file.
- `test_an_exported_value_is_not_blamed_on_the_env_file` — the environment wins
  over `.env`, so a key that is both exported and written in the file is being
  read from the export. Pointing at the `.env` line sends an operator to edit a
  line that cannot change the value.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from atp_core.config import (
    ConfigProblem,
    RiskLimits,
    Settings,
    _resolve,
    config_problem_summary,
    config_problems,
)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

#: Set in some environments (a CI runner, a remote container) and inherited by
#: the test process, where they would make "a clean environment" mean different
#: things on different machines.
AMBIENT = ("ATP_RUN_MODE", "ATP_ALLOW_LIVE_TRADING", "RISK_MAX_POSITION_PCT", "WORKER_METRICS_PORT")


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


check_env = _load("check_env")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No `.env` and no inherited overrides, so a case sets exactly its own."""
    monkeypatch.chdir(tmp_path)
    for name in AMBIENT:
        monkeypatch.delenv(name, raising=False)


class TestWhatIsReported:
    def test_a_loadable_environment_has_no_problems(self, clean_env: None) -> None:
        assert config_problems() == []
        assert config_problem_summary() is None

    def test_a_risk_limit_is_named_as_it_was_written(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`RISK_MAX_POSITION_PCT`, not `max_position_pct`.

        The traceback gives the field name, which is the half that is not in the
        file — the whole point of this is to print the other half.
        """
        monkeypatch.setenv("RISK_MAX_POSITION_PCT", "not-a-number")
        problems = config_problems()
        assert [p.env_var for p in problems] == ["RISK_MAX_POSITION_PCT"]
        assert problems[0].value == "not-a-number"

    def test_an_aliased_setting_is_named_by_its_alias(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATP_RUN_MODE", "papertrading")
        assert [p.env_var for p in config_problems()] == ["ATP_RUN_MODE"]

    def test_every_broken_value_is_reported_in_one_run(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad risk limit must not hide the rest of the file.

        One problem per edit-and-retry cycle is the wrong shape for a file whose
        mistakes arrive in batches, after a hand-merge or a first setup.
        """
        monkeypatch.setenv("RISK_MAX_POSITION_PCT", "not-a-number")
        monkeypatch.setenv("WORKER_METRICS_PORT", "not-a-port")
        assert sorted(p.env_var for p in config_problems()) == [
            "RISK_MAX_POSITION_PCT",
            "WORKER_METRICS_PORT",
        ]

    def test_a_problem_is_reported_once_not_per_model(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`RiskLimits` is validated on its own *and* reached through `Settings`."""
        monkeypatch.setenv("RISK_MAX_POSITION_PCT", "not-a-number")
        assert len(config_problems()) == 1

    def test_a_rule_between_values_names_no_single_variable(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§1.8's two locks. Blaming whichever half was read last would be a
        guess, and the message already says what the rule is."""
        monkeypatch.setenv("ATP_RUN_MODE", "live")
        monkeypatch.setenv("ATP_ALLOW_LIVE_TRADING", "false")
        problems = config_problems()
        assert [p.is_whole_configuration for p in problems] == [True]
        assert "ATP_ALLOW_LIVE_TRADING=true" in problems[0].reason
        assert problems[0].value is None

    def test_the_summary_names_the_variables(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RISK_MAX_POSITION_PCT", "not-a-number")
        summary = config_problem_summary()
        assert summary is not None and "RISK_MAX_POSITION_PCT" in summary


class TestSecretsAreNotPrinted:
    """CLAUDE.md §1.6. A value that failed to load is still a credential, and
    most of one is still an offline grinding target."""

    @pytest.mark.parametrize(
        "field", ["api_password_hash", "api_secret_key", "alpaca_api_secret", "metrics_token"]
    )
    def test_a_credential_field_is_classified_secret(self, field: str) -> None:
        _, secret = _resolve(Settings, field)
        assert secret is True

    @pytest.mark.parametrize("field", ["run_mode", "worker_metrics_port", "api_cors_origins"])
    def test_an_ordinary_field_is_not(self, field: str) -> None:
        _, secret = _resolve(Settings, field)
        assert secret is False

    def test_an_unrecognised_name_is_treated_as_a_secret(self) -> None:
        """Fail safe. Redacting something harmless costs a less helpful line;
        the other mistake puts a credential in a terminal."""
        env_var, secret = _resolve(Settings, "something_added_later")
        assert env_var == "SOMETHING_ADDED_LATER"
        assert secret is True

    def test_a_secret_problem_withholds_its_value(self) -> None:
        problem = ConfigProblem(env_var="API_SECRET_KEY", reason="bad", value=None)
        assert "withheld" in "\n".join(check_env.describe(problem, {}))

    def test_the_risk_prefix_is_applied(self) -> None:
        env_var, _ = _resolve(RiskLimits, "max_position_pct")
        assert env_var == "RISK_MAX_POSITION_PCT"


class TestWhereTheValueCameFrom:
    def test_a_value_only_in_the_file_points_at_its_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WORKER_METRICS_PORT", raising=False)
        assert check_env.source_of("WORKER_METRICS_PORT", {"WORKER_METRICS_PORT": 117}) == (
            ".env line 117"
        )

    def test_an_exported_value_is_not_blamed_on_the_env_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trap this function exists for.

        Compose sets DATABASE_URL and REDIS_URL in `environment:`, and an export
        in a shell does the same. Both beat the file.
        """
        monkeypatch.setenv("WORKER_METRICS_PORT", "not-a-port")
        described = check_env.source_of("WORKER_METRICS_PORT", {"WORKER_METRICS_PORT": 117})
        assert "OVERRIDDEN" in described and "117" in described

    def test_an_exported_value_absent_from_the_file_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOME_KEY", "x")
        assert "environment" in check_env.source_of("SOME_KEY", {})

    def test_a_value_from_neither_is_the_built_in_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SOME_KEY", raising=False)
        assert "default" in check_env.source_of("SOME_KEY", {})


class TestReadingTheEnvFile:
    def test_line_numbers_skip_comments_and_blanks(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("# a comment\n\nATP_RUN_MODE=backtest\n\n# another\nAPI_USER=operator\n")
        assert check_env.env_file_lines(path) == {"ATP_RUN_MODE": 3, "API_USER": 6}

    def test_the_last_assignment_wins(self, tmp_path: Path) -> None:
        """Which is what the reader does, so it is the line to correct."""
        path = tmp_path / ".env"
        path.write_text("API_USER=one\nAPI_USER=two\n")
        assert check_env.env_file_lines(path) == {"API_USER": 2}

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert check_env.env_file_lines(tmp_path / "nope") == {}
