"""The operator scripts, as far as they can be tested without a live service.

`halt.py` is the one somebody runs while something is going wrong. It is no
longer the only path to the kill switch — the dashboard's HALT button reaches
it through `/risk/halt` (#70) — but it is the one that still works when the API
is the thing that is down, which is when you most want it. So what is
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
import re
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from atp_api.auth import hash_password, looks_like_bcrypt_hash, verify_password
from atp_core.alerts import Severity
from atp_core.audit.ports import Action
from atp_core.config import Settings
from atp_core.domain import Timeframe
from atp_core.risk.killswitch import HaltReason, HaltRecord, HaltScope
from atp_core.risk.limits import DEFAULT_RISK_LIMITS, RiskLimits
from atp_core.worker import StoredWorkerConfig, WorkerConfig
from atp_worker import preflight
from atp_worker.preflight import Status
from tests.fakes import FakeWorkerConfigRepository

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
preflight_cli = _load("preflight")
paper_report = _load("paper_report")


def _settings() -> Settings:
    """Enough of `Settings` for the reads under test. The url is never dialled:
    `_stub_config_repo` replaces the repository before anything opens a socket."""
    return Settings(database_url="postgresql+asyncpg://nobody@127.0.0.1:1/none")


def _stored(risk: RiskLimits) -> StoredWorkerConfig:
    return StoredWorkerConfig(
        config=WorkerConfig(risk=risk),
        revision=4,
        updated_at=datetime(2026, 9, 3, tzinfo=UTC),
        updated_by="josh",
    )


def _stub_config_repo(monkeypatch: pytest.MonkeyPatch, repo: FakeWorkerConfigRepository) -> None:
    """Swap the repository and neutralise the engine the script builds around it.

    `status._saved_limits` opens an engine, constructs a repository over it and
    disposes it. Only the middle step is under test, so the other two are made
    inert rather than pointed at a database this suite must not need (§1.7).
    """
    monkeypatch.setattr(status, "PostgresWorkerConfigRepository", lambda _factory: repo)
    monkeypatch.setattr(status, "create_engine", lambda _url: _InertEngine())
    monkeypatch.setattr(status, "create_session_factory", lambda _engine: None)


class _InertEngine:
    async def dispose(self) -> None:
        return None


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
        settings = Settings(
            ALERT_NTFY_TOPIC="atp-abc123",
            ALERT_TELEGRAM_TOKEN="123:AAF",
            ALERT_TELEGRAM_CHAT_ID="9876",
        )
        assert check_alerts._configured_transports(settings) == ["ntfy", "telegram"]

    def test_half_a_telegram_configuration_is_not_a_transport(self) -> None:
        """It is not one for `build_alert_sink` either, and a checker that
        disagreed with the factory would report a transport that cannot
        deliver — the exact belief this script exists to correct."""
        settings = Settings(ALERT_TELEGRAM_TOKEN="123:AAF")
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


class TestHashPasswordOutput:
    """The line the operator pastes, and the two things that read it.

    `.env` has two readers that disagree about `$`. Docker Compose interpolates
    `$NAME` in it; `Settings`, through pydantic-settings, does not interpolate
    at all. A bcrypt hash is `$2b$12$<salt><digest>`, so the naive line

        API_PASSWORD_HASH=$2b$12$hnn.KpQ8...

    makes compose warn `The "hnn" variable is not set` and hand the API
    container `$2b$12.KpQ8...` — a hash with its salt bitten off. Non-empty, so
    the startup check for an unset hash stays quiet; not a hash, so every login
    is refused. Roughly four salts in five start with a letter and trigger it.

    The usual `$$` escaping is not the fix: it satisfies compose and breaks the
    other reader. Single quotes satisfy both, and that is what these pin — the
    quotes being present, and the quoted line still meaning the hash when read
    back by `Settings`.
    """

    @staticmethod
    def _emitted_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
        """Run the script's `main` with the prompts answered, return its line."""
        script = _load("hash_password")
        monkeypatch.setattr(script.getpass, "getpass", lambda _prompt: "operator-password")

        assert script.main() == 0

        lines = [
            line for line in capsys.readouterr().out.splitlines() if "API_PASSWORD_HASH=" in line
        ]
        assert len(lines) == 1, "exactly one pasteable line, or an operator has to choose"
        return lines[0]

    def test_the_line_is_single_quoted(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        line = self._emitted_line(monkeypatch, capsys)

        value = line.partition("=")[2]
        assert value.startswith("'") and value.endswith("'"), (
            f"unquoted, so Docker Compose will eat the salt: {line[:24]}..."
        )
        # Single, not double: compose interpolates inside double quotes too, so
        # they look like protection and are not.
        assert not value.startswith('"')

    def test_the_hash_is_emitted_whole(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Quoting must not have become escaping. `$$` would satisfy compose and
        leave `Settings` with a hash that never verifies."""
        value = self._emitted_line(monkeypatch, capsys).partition("=")[2].strip("'")

        assert "$$" not in value
        assert looks_like_bcrypt_hash(value)
        assert verify_password("operator-password", value)

    def test_the_pasted_line_reads_back_as_the_hash(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """End to end through the reader we can drive in a unit test.

        Paste the line into a `.env` exactly as printed and load it the way the
        API does. The quotes must be stripped as syntax rather than carried into
        the value — a hash wrapped in literal quote characters is as dead as a
        truncated one.

        Compose is the other reader and needs a daemon, so it is not driven
        here; its behaviour is captured in `test_auth.py` as the mangled value
        `docker compose config` actually produced.
        """
        env = tmp_path / ".env"
        env.write_text(self._emitted_line(monkeypatch, capsys) + "\n", encoding="utf-8")

        settings = Settings(_env_file=env)

        assert verify_password("operator-password", settings.api_password_hash.get_secret_value())


class TestPreflightArguments:
    """The gathering half. The decisions are `atp_worker.preflight`'s and are
    tested in `test_preflight.py`; what can go wrong here is the arguments and
    the rendering an operator reads at 09:29."""

    def test_an_unknown_timeframe_lists_the_valid_ones(self) -> None:
        with pytest.raises(SystemExit, match="1m, 5m, 15m, 30m, 1h, 4h, 1d"):
            preflight_cli._timeframe("3d")

    def test_the_flag_no_longer_answers_for_the_saved_row(self) -> None:
        """The defect, at the argument layer. `--timeframe` defaulted to `1d`
        and every priced check took it, while the worker boots on
        `WorkerConfig.timeframe` — `1m` — so a green preflight described a
        platform nobody was running (the day-1 fix audit, §3.3)."""
        assert preflight_cli.parse_args([]).timeframe is None


class TestPreflightPricesTheSavedSeries:
    """§3.3. Which bar series the checks are measured on is the saved row's
    answer, not an argv default's.

    Driven through the gathering functions rather than through
    `check_sizing_is_reachable` directly, because the defect was never in the
    decision — F11's check was correct and was handed the wrong series. A test
    that calls the check with a hand-picked timeframe cannot express that, which
    is why the suite was green while `make preflight` was lying.
    """

    def _bars(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        """Capture every `(symbol, timeframe, n)` the script asks storage for."""
        asked: list[Any] = []

        class RecordingBars:
            def __init__(self, _factory: Any) -> None:
                pass

            async def get_last_n_bars(self, symbol: str, timeframe: Any, n: int) -> list[Any]:
                asked.append((symbol, timeframe, n))
                return []

        monkeypatch.setattr(preflight_cli, "PostgresBarRepository", RecordingBars)
        monkeypatch.setattr(preflight_cli, "create_engine", lambda _url: _InertEngine())
        monkeypatch.setattr(preflight_cli, "create_session_factory", lambda _engine: None)
        return asked

    async def test_the_history_check_reads_the_configured_series(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = self._bars(monkeypatch)
        config = WorkerConfig(symbols=("SPY",), strategy="sma_crossover", timeframe="1m")

        await preflight_cli._local_checks(_settings(), config)

        assert [tf for _symbol, tf, _n in asked] == [Timeframe.M1]

    async def test_a_different_saved_series_is_the_one_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pinned against a constant: `1d` used to be the answer whatever the
        row said, so a test that only ever asserts `1m` would pass on a
        hard-coded `M1` too."""
        asked = self._bars(monkeypatch)
        config = WorkerConfig(symbols=("SPY",), strategy="sma_crossover", timeframe="15m")

        await preflight_cli._local_checks(_settings(), config)

        assert [tf for _symbol, tf, _n in asked] == [Timeframe.M15]

    async def test_the_sizing_check_reads_the_configured_series(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The half that is F11's whole fix: the ATR that sets the stop
        distance, and therefore the share count, comes off this series."""
        asked = self._bars(monkeypatch)
        config = WorkerConfig(symbols=("SPY",), strategy="sma_crossover", timeframe="1m")

        await preflight_cli._sizing_check(_settings(), config, equity=Decimal(100_000))

        assert [tf for _symbol, tf, _n in asked] == [Timeframe.M1]

    def test_an_override_says_it_is_answering_a_what_if(self) -> None:
        """The escape hatch stays — asking "would 1d fit?" before rewriting the
        row is one of the two ways out of a sizing a minute series cannot carry
        — but it may not answer that question in the words that describe the
        saved configuration."""
        same = preflight.check_timeframe(Timeframe.M1, saved=Timeframe.M1)
        overridden = preflight.check_timeframe(Timeframe.D1, saved=Timeframe.M1)

        assert same.status is Status.PASS
        assert "saved worker_config row" in same.detail
        assert overridden.status is Status.WARN
        assert "the worker will boot on 1m" in overridden.detail

    def test_no_broker_needs_no_credentials(self) -> None:
        assert preflight_cli.parse_args(["--no-broker"]).no_broker is True

    def test_an_exception_is_rendered_as_its_type_and_never_its_message(self) -> None:
        """Rule §1.6, and the reason it is a rule rather than a habit. A driver
        that is handed `Settings` puts a `repr` of it in the message — SQLAlchemy
        does exactly this — and every credential the platform holds is in that
        repr. The `fix` line carries what an operator acts on regardless."""
        leaked = RuntimeError("connect failed: password=hunter2 token=sk-live-abc")

        assert preflight_cli._why(leaked) == "RuntimeError"
        assert "hunter2" not in preflight_cli._why(leaked)

    def test_a_run_with_unchecked_items_never_reads_as_ready(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The headline is the only part most people read. "READY" over five
        checks that did not run is precisely the confusion SKIP exists to
        prevent, so it says NO FAILURES and names them instead."""
        from atp_worker.preflight import Check, Preflight, Status

        preflight_cli._render(
            Preflight([Check("run mode", Status.PASS, ""), Check("history", Status.SKIP, "")])
        )
        out = capsys.readouterr().out

        assert "READY" not in out
        assert "did not run: history" in out

    def test_a_fully_checked_pass_does_say_ready(self, capsys: pytest.CaptureFixture[str]) -> None:
        from atp_worker.preflight import Check, Preflight, Status

        preflight_cli._render(Preflight([Check("run mode", Status.PASS, "")]))
        out = capsys.readouterr().out

        assert "READY — every check ran" in out
        # And the one limit nothing in this repo can check is still stated.
        assert "Layer 8" in out

    def test_every_status_has_a_mark(self) -> None:
        """A status added without a mark would raise inside the renderer, which
        is a crash in the tool an operator reaches for when something is already
        wrong."""
        from atp_worker.preflight import Status

        assert set(preflight_cli.MARK) == set(Status)


class TestPaperReportArguments:
    def test_a_malformed_since_is_refused_before_the_query(self) -> None:
        with pytest.raises(SystemExit, match="--since must be YYYY-MM-DD"):
            paper_report._since("last tuesday")

    def test_a_missing_log_file_is_refused_rather_than_counted_as_zero(self) -> None:
        """Zero clean reconciliations and zero mismatches is a real and
        meaningful answer — it says the reconciler never ran. Reading it out of
        a path that does not exist would put that finding in front of someone
        who simply mistyped a filename."""
        with pytest.raises(SystemExit, match="no such file"):
            paper_report._log_counts("/nowhere/worker.log")

    def test_no_log_file_leaves_the_two_clauses_unanswered(self) -> None:
        assert paper_report._log_counts(None) == {
            "reconcile_lines": None,
            "mismatch_lines": None,
            "unprotected_lines": None,
        }

    def test_the_markers_are_counted_from_the_log(self, tmp_path: Path) -> None:
        log = tmp_path / "worker.log"
        log.write_text(
            "execution.reconcile.clean x\n"
            "execution.reconcile.clean y\n"
            "execution.reconcile.mismatch z\n"
            "runner.position_unprotected SPY\n",
            encoding="utf-8",
        )

        assert paper_report._log_counts(str(log)) == {
            "reconcile_lines": 2,
            "mismatch_lines": 1,
            "unprotected_lines": 1,
        }

    def test_an_unanswered_clause_survives_into_the_markdown(self, tmp_path: Path) -> None:
        """A roadmap block that listed three clauses would read as a line with
        three clauses, and the fourth would stop existing."""
        from atp_core.analytics.paper_run import assess

        block = paper_report._markdown(assess([], [], strategy_id="x"), "x", "paper")

        assert block.count("- [") == 4
        assert "[?]" in block
        assert "were not shown" in block


class TestTheRiskBudgetStatusPrints:
    """`status.py` reports quote freshness against a ceiling that is now a row.

    Three states, and the *label* is the whole point of the test: 30 seconds
    means something different if an operator chose it, if nobody has chosen
    anything, and if the database could not be asked. This is the script somebody
    runs when the stack is half up, so the third happens — and printing a
    fallback under "Config tab → risk limits" would tell a reader a number was
    chosen when it was invented.
    """

    async def test_a_saved_row_is_labelled_as_the_operators(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = RiskLimits(max_quote_age_seconds=12)
        _stub_config_repo(monkeypatch, FakeWorkerConfigRepository(_stored(saved)))

        limits, source = await status._saved_limits(_settings())

        assert limits.max_quote_age_seconds == 12
        assert source == "Config tab → risk limits"

    async def test_nothing_saved_says_so_rather_than_claiming_a_choice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defaults genuinely *are* what an unconfigured platform enforces,
        so this is not a fallback — but nobody typed them, and the header must
        not imply somebody did."""
        _stub_config_repo(monkeypatch, FakeWorkerConfigRepository())

        limits, source = await status._saved_limits(_settings())

        assert limits == DEFAULT_RISK_LIMITS
        assert "nothing saved" in source

    async def test_an_unreachable_database_is_labelled_a_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Swallowed rather than raised, for the reason `_saved_symbols` returns
        an empty list — there is plenty else on this page to report — but never
        silently: a stale-quote verdict must not read as measured against a
        ceiling the operator set."""
        broken = FakeWorkerConfigRepository()
        broken.load_error = RuntimeError("postgres is unreachable")
        _stub_config_repo(monkeypatch, broken)

        limits, source = await status._saved_limits(_settings())

        assert limits == DEFAULT_RISK_LIMITS
        assert "not answering" in source


class _RecordingSwitch:
    """Enough kill switch to see what `halt.py` did, and no Redis.

    The round trip is `RedisKillSwitch`'s to prove and it has its own tests
    (`tests/integration/test_kill_switch.py`). What is worth asserting here is
    the thing that lives in the script: whether it demanded a password before
    it called `clear`, and what it wrote down afterwards.
    """

    def __init__(self, engaged: HaltRecord | None = None) -> None:
        self.engaged_calls: list[dict[str, Any]] = []
        self.cleared_calls: list[dict[str, Any]] = []
        self._engaged = engaged

    def engage(self, scope: HaltScope, reason: HaltReason, **kwargs: Any) -> HaltRecord:
        self.engaged_calls.append({"scope": scope, "reason": reason, **kwargs})
        return HaltRecord(
            scope=scope,
            reason=reason,
            engaged_at=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
            engaged_by=kwargs.get("engaged_by", ""),
            detail=kwargs.get("detail", ""),
            target=kwargs.get("target"),
        )

    def clear(self, scope: HaltScope, cleared_by: str, target: str | None = None) -> Any:
        self.cleared_calls.append({"scope": scope, "cleared_by": cleared_by, "target": target})
        return self._engaged

    def active_halts(self) -> list[HaltRecord]:
        return []


#: Hashed once. bcrypt is deliberately slow, and these tests want the real
#: credential check rather than a stub of it — the point of F9 is that the shell
#: and the dashboard verify the same password against the same hash.
_PASSWORD = "the-operator-password"
_HASH = hash_password(_PASSWORD)


def _gate(monkeypatch: pytest.MonkeyPatch, *, engaged: HaltRecord | None = None) -> Any:
    """Wire `halt.py` to a fake switch and a recording audit sink."""
    switch = _RecordingSwitch(engaged)
    written: list[Any] = []
    monkeypatch.setattr(
        halt,
        "get_settings",
        lambda: Settings(
            API_PASSWORD_HASH=_HASH,
            API_USER="operator",
            database_url="postgresql+asyncpg://nobody@127.0.0.1:1/none",
        ),
    )
    monkeypatch.setattr(halt, "create_sync_redis", lambda _url: object())
    monkeypatch.setattr(halt, "build_alert_sink", lambda _s: None)
    monkeypatch.setattr(halt, "RedisKillSwitch", lambda *_a, **_k: switch)
    monkeypatch.setattr(halt, "_record", lambda _settings, entry: written.append(entry))
    switch.written = written  # type: ignore[attr-defined]
    return switch


class TestClearingIsGated:
    """F9: the shell cleared a halt for anyone who could run it, while
    docs/RUNBOOK.md said clearing asks for the password "wherever you do it"
    (docs/paper-week/day-1-review.md)."""

    def test_a_correct_password_clears_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        switch = _gate(monkeypatch)
        monkeypatch.setattr(halt.getpass, "getpass", lambda _prompt: _PASSWORD)

        assert halt.main(["clear", "--by", "jo"]) == 0
        assert switch.cleared_calls == [
            {"scope": HaltScope.GLOBAL, "cleared_by": "jo", "target": None}
        ]

    def test_a_wrong_password_leaves_trading_stopped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The refusal has to happen *before* the clear. A password checked
        afterwards would be a password that did not stop anything."""
        switch = _gate(monkeypatch)
        monkeypatch.setattr(halt.getpass, "getpass", lambda _prompt: "not-it")

        with pytest.raises(SystemExit, match="NOT resumed"):
            halt.main(["clear", "--by", "jo"])

        assert switch.cleared_calls == []

    def test_an_empty_password_leaves_trading_stopped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing return at the prompt is not a password."""
        switch = _gate(monkeypatch)
        monkeypatch.setattr(halt.getpass, "getpass", lambda _prompt: "")

        with pytest.raises(SystemExit, match="NOT resumed"):
            halt.main(["clear", "--by", "jo"])

        assert switch.cleared_calls == []

    def test_an_unconfigured_hash_is_no_way_in_rather_than_a_free_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs/SAFETY.md's posture for the login form, applied here: with no
        `API_PASSWORD_HASH` set, nothing is accepted rather than everything.
        The message has to carry the next command, since an operator meets this
        mid-incident."""
        switch = _gate(monkeypatch)
        monkeypatch.setattr(
            halt,
            "get_settings",
            lambda: Settings(database_url="postgresql+asyncpg://nobody@127.0.0.1:1/none"),
        )

        with pytest.raises(SystemExit, match=re.escape("hash_password.py")):
            halt.main(["clear", "--by", "jo"])

        assert switch.cleared_calls == []

    def test_engaging_still_asks_for_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The asymmetry is the design (docs/RISK.md): stopping is reflexive,
        restarting is a decision. A prompt in front of a halt is the failure
        this must not introduce, so it is asserted rather than assumed — the
        stub raises if anything asks."""
        switch = _gate(monkeypatch)

        def refuse(_prompt: str) -> str:
            raise AssertionError("engaging must never prompt for a password")

        monkeypatch.setattr(halt.getpass, "getpass", refuse)

        assert halt.main(["engage", "--by", "jo"]) == 0
        assert switch.engaged_calls


class TestTheAuditTrail:
    """F9's second half: the record held resumes done through the API and
    nothing else, so an incident stopped and resumed from the shell left no
    trace on the audit tab at all."""

    def test_engaging_writes_a_row_attributed_to_the_script(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR 0008: an actor the caller fills in is not an audit trail. Nothing
        authenticated `--by`, so it travels in `detail` and the `actor` column
        names the tool, which is the one attribution here that is true."""
        switch = _gate(monkeypatch)

        halt.main(["engage", "--by", "jo", "--detail", "feed looked wrong"])

        entry = switch.written[-1]
        assert entry.action == Action.HALT_ENGAGED
        assert entry.actor == halt.SCRIPT_ACTOR
        assert entry.detail["by"] == "jo"
        assert entry.detail["detail"] == "feed looked wrong"

    def test_clearing_writes_a_row_attributed_to_the_operator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The password proved a person, so this row may name the account."""
        switch = _gate(monkeypatch)
        monkeypatch.setattr(halt.getpass, "getpass", lambda _prompt: _PASSWORD)

        halt.main(["clear", "--by", "jo"])

        entry = switch.written[-1]
        assert entry.action == Action.HALT_CLEARED
        assert entry.actor == "operator"
        assert entry.detail["by"] == "jo"

    def test_a_refused_password_is_recorded_before_it_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wrong password against a resume is either a typo or somebody
        working through guesses with access to the box, the two look identical
        at the moment of refusal, and the login rate limiter only ever counted
        attempts at the API's form."""
        switch = _gate(monkeypatch)
        monkeypatch.setattr(halt.getpass, "getpass", lambda _prompt: "not-it")

        with pytest.raises(SystemExit):
            halt.main(["clear", "--by", "jo"])

        entry = switch.written[-1]
        assert entry.action == Action.FORBIDDEN
        assert entry.detail["reason"] == "step_up_failed"

    def test_a_resume_says_which_halt_it_ended(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A resume and the engagement it answers are rows written hours apart,
        often by different processes and sometimes by a trigger that wrote no
        row at all. `engaged_at` is the only thing common to both, so without it
        the pair cannot be joined even when both are present — which is what F9
        means by an incident that cannot be read end to end."""
        original = HaltRecord(
            scope=HaltScope.GLOBAL,
            reason=HaltReason.DATA_FEED_LOST,
            engaged_at=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
            engaged_by="worker",
        )
        switch = _gate(monkeypatch, engaged=original)
        monkeypatch.setattr(halt.getpass, "getpass", lambda _prompt: _PASSWORD)

        halt.main(["clear", "--by", "jo"])

        detail = switch.written[-1].detail
        assert detail["was_halted"] is True
        assert detail["original_reason"] == "data_feed_lost"
        assert detail["originally_engaged_by"] == "worker"
        assert detail["originally_engaged_at"] == "2024-06-03T14:30:00+00:00"

    def test_every_row_carries_the_correlation_id_of_its_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The key from a row on the audit tab to the log lines the same command
        emitted. It needed no new field — `atp_core.logging` has bound one since
        ADR 0013 and the audit row simply never carried it."""
        switch = _gate(monkeypatch)

        halt.main(["engage", "--by", "jo"])

        assert switch.written[-1].detail["correlation_id"]
