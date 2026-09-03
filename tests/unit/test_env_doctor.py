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

import asyncpg.exceptions as pg
import pytest
from dotenv import dotenv_values

from atp_core.config import (
    ConfigProblem,
    RiskLimits,
    Settings,
    _resolve,
    config_problem_summary,
    config_problems,
    known_env_vars,
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


class TestKeysNothingReads:
    """The silent half of a broken `.env`.

    `Settings` is `extra="ignore"`, which is right — the file is shared with
    compose and Vite — and the cost is that a misspelled key is dropped without
    a word. `RISK_MAX_POSITION_PC=0.02` loads cleanly and leaves the cap at
    0.10, five times looser than the operator believes they set.
    """

    def test_known_names_cover_aliases_and_the_risk_prefix(self) -> None:
        known = known_env_vars()
        assert "ATP_RUN_MODE" in known  # aliased
        assert "ALPACA_API_KEY" in known  # unaliased, field name upper
        assert "RISK_MAX_POSITION_PCT" in known  # nested, env_prefix applied
        assert "max_position_pct" not in known  # never the field name

    def test_a_misspelled_key_is_reported_with_the_name_meant(self) -> None:
        found = check_env.unread_keys({"RISK_MAX_POSITION_PC": 51})
        assert len(found) == 1
        key, line, note = found[0]
        assert (key, line) == ("RISK_MAX_POSITION_PC", 51)
        assert "RISK_MAX_POSITION_PCT" in note

    def test_a_key_nothing_resembles_is_still_reported(self) -> None:
        found = check_env.unread_keys({"MY_OWN_TOOL_VAR": 9})
        assert [k for k, _, _ in found] == ["MY_OWN_TOOL_VAR"]
        assert "nothing in this platform reads it" in found[0][2]

    def test_a_recognised_key_is_not_reported(self) -> None:
        assert check_env.unread_keys({"ATP_RUN_MODE": 18}) == []

    def test_keys_read_by_something_other_than_settings_are_not_reported(self) -> None:
        """Vite, the dev server and compose all read from this file.

        Without the allowlist a stock `.env` reports four typos, which is the
        fastest way to teach someone to ignore this check.
        """
        lines = dict.fromkeys(check_env.READ_ELSEWHERE, 1)
        assert check_env.unread_keys(lines) == []

    def test_a_key_that_moved_says_where_it_went(self) -> None:
        """Ten keys left `.env` for the database in ADR 0023. Reported as
        unread — because they are, and the consequence is the dangerous one —
        but not as a *typo*: an operator upgrading would otherwise get ten
        lines saying "nothing reads it" and go looking for a misspelling that
        is not there."""
        found = check_env.unread_keys({"WORKER_STRATEGY": 12})

        assert [k for k, _, _ in found] == ["WORKER_STRATEGY"]
        assert "Worker tab" in found[0][2]
        assert "did you mean" not in found[0][2]

    def test_every_moved_key_is_one_settings_really_stopped_reading(self) -> None:
        """The list fails *open* if it goes stale: a key added back to
        `Settings` and left here would be reported as moved while being read,
        which is the same lie in the opposite direction."""
        known = known_env_vars()
        assert not (set(check_env.MOVED) & known)

    def test_reported_in_file_order(self) -> None:
        found = check_env.unread_keys({"ZZZ_LATE": 90, "AAA_EARLY": 3})
        assert [k for k, _, _ in found] == ["AAA_EARLY", "ZZZ_LATE"]


class TestTheAllowlistDoesNotDrift:
    """Both directions, because both are silent.

    `READ_ELSEWHERE` is the one hand-maintained list here, and a stale entry
    fails *open* — the check stops reporting a key it should report.
    """

    def test_a_stock_env_example_has_nothing_unread(self) -> None:
        """The guard for a key added to `.env.example` and nowhere else.

        A new key that is neither a `Settings` field nor in the allowlist is
        either a typo in the example file or a setting nothing implements. Both
        are worth failing a build over, and neither is visible by reading.
        """
        example = Path(__file__).resolve().parents[2] / ".env.example"
        lines = check_env.env_file_lines(example)
        assert lines, "no keys parsed from .env.example — the parser or the path is wrong"
        assert check_env.unread_keys(lines) == []

    def test_no_settings_field_is_hiding_in_the_allowlist(self) -> None:
        """The inverse, and the one that matters more.

        A `Settings` field listed in `READ_ELSEWHERE` is a field the platform
        reads and the check has been told to ignore — so a typo in it would go
        unreported, which is the exact failure this whole class exists for.
        """
        overlap = known_env_vars() & set(check_env.READ_ELSEWHERE)
        assert overlap == set(), f"these are real Settings fields: {sorted(overlap)}"

    def test_every_allowlisted_key_says_what_reads_it(self) -> None:
        """A bare list becomes a dumping ground; a list of reasons does not."""
        for key, why in check_env.READ_ELSEWHERE.items():
            assert why.strip(), f"{key} has no explanation"
            assert "read by" in why, f"{key} does not say what reads it: {why!r}"


class TestTheDatabasePasswordSurvivesTheTrip:
    """`ATP_DB_PASSWORD` reaches Postgres by two paths that must agree.

    Compose interpolates it into `POSTGRES_PASSWORD`, which initdb stores
    verbatim, and into a `DATABASE_URL` that SQLAlchemy parses as a URL. A
    character that means something to either path arrives as two different
    strings, and the only symptom is `password authentication failed for user
    "atp"` on every request the API serves — with a `.env` that reads correctly
    and a password that is correct.

    `Settings` never sees this value, so `config_problems()` cannot reach it.
    These cases were verified against the real `docker compose config` output
    and SQLAlchemy's own parser, not reasoned about.
    """

    def test_a_hex_password_is_fine(self) -> None:
        """What `openssl rand -hex 24` produces, which is what we recommend."""
        assert check_env.db_password_problem("a1b2c3d4e5f6") is None

    def test_empty_is_not_a_problem(self) -> None:
        """`make up` runs the base file, which hardcodes atp/atp. Reporting an
        empty value would fire on every developer's machine."""
        assert check_env.db_password_problem("") is None

    def test_a_percent_escape_is_decoded_before_postgres_sees_it(self) -> None:
        """The silent one, and the one that produces the error verbatim.

        Compose passes `x%3Ay` through to both sides unchanged, so initdb
        stores it literally — and then SQLAlchemy percent-decodes it to `x:y`
        on the way out. The two never match and nothing says why.
        """
        problem = check_env.db_password_problem("x%3Ay")
        assert problem is not None
        assert "%" in problem

    def test_an_at_sign_is_read_as_the_host(self) -> None:
        """Different symptom, same cause: `@` ends the credentials in a URL, so
        the rest of the password becomes part of the hostname and the failure
        is an unresolvable host rather than a refused password."""
        problem = check_env.db_password_problem("p@ssw0rd")
        assert problem is not None
        assert "@" in problem

    def test_a_dollar_is_a_variable_reference_to_compose(self) -> None:
        """Compose substitutes `$ret` away in *both* places, so the containers
        still agree with each other — and `.env`'s own DATABASE_URL, which
        pydantic reads without interpolating, does not. `make migrate` is then
        what fails, against a database the stack is happily using."""
        problem = check_env.db_password_problem("sec$ret")
        assert problem is not None
        assert "$" in problem

    def test_a_bare_percent_is_not_an_escape(self) -> None:
        """Only `%` + two hex digits decodes. Reporting every `%` would refuse
        passwords that work, which is how a check gets bypassed."""
        assert check_env.db_password_problem("pa%ss") is None

    def test_the_reason_never_carries_the_password(self) -> None:
        """§1.6. The reason names the character class so it is actionable; the
        value is withheld by the caller and must not leak through here."""
        secret = "sup3rs3cret%3Avalue"
        problem = check_env.db_password_problem(secret)
        assert problem is not None
        assert secret not in problem

    def test_a_host_side_url_disagreeing_with_the_deploy_password_is_reported(self) -> None:
        """`make migrate`, `seed` and `backfill` read `.env`'s DATABASE_URL, not
        the one compose builds. On a deployed host the two must carry the same
        password or the host-side tools fail against the database the
        containers are using (.env.example, 'datastores').

        `halt.py` used to be named here and in the reason string itself, and it
        never read this url — it reaches Redis and the venue, which is what lets
        docs/RUNBOOK.md promise it keeps working while Postgres is refusing
        everyone. `make migrate` is the tool that genuinely belongs in the list
        and was, until `infra/alembic/env.py` started reading `Settings`, the one
        member of it that did not read this line at all."""
        found = check_env.db_credential_problems(
            {
                "ATP_DB_PASSWORD": "deadbeef",
                "DATABASE_URL": "postgresql+asyncpg://atp:atp@localhost:5432/atp",
            },
            {},
        )
        assert [key for key, _ in found] == ["DATABASE_URL"]

    def test_a_matching_pair_is_silent(self) -> None:
        found = check_env.db_credential_problems(
            {
                "ATP_DB_PASSWORD": "deadbeef",
                "DATABASE_URL": "postgresql+asyncpg://atp:deadbeef@localhost:5432/atp",
            },
            {},
        )
        assert found == []

    def test_the_stock_developer_env_is_silent(self) -> None:
        """No ATP_DB_PASSWORD means `make up` against the base file's atp/atp,
        where the stock DATABASE_URL is correct as written. Reporting it there
        would be noise on every developer's machine."""
        found = check_env.db_credential_problems(
            {"DATABASE_URL": "postgresql+asyncpg://atp:atp@localhost:5432/atp"}, {}
        )
        assert found == []

    def test_a_stock_env_example_reports_no_credential_problem(self) -> None:
        """The template ships ATP_DB_PASSWORD empty and DATABASE_URL on atp/atp.
        A fresh copy must come out clean or the check teaches people to skip it."""
        example = Path(__file__).resolve().parents[2] / ".env.example"
        values = {k: v for k, v in dotenv_values(example).items() if v is not None}
        assert check_env.db_credential_problems(values, {}) == []


#: A DSN shaped like the one `.env` ships, with a password that must never be
#: echoed by anything below (CLAUDE.md §1.6).
_SECRET = "sup3r-s3cret-db-password"
_DSN = f"postgresql+asyncpg://atp:{_SECRET}@127.0.0.1:5432/atp"


class TestTheDatabaseThatRefusesACorrectFile:
    """The dominant cause in the field, and the one `.env` cannot explain.

    `POSTGRES_PASSWORD` is read by initdb and never again, so a volume that
    already existed keeps whatever password it was created with. Set or rotate
    `ATP_DB_PASSWORD` against one and every container sends a new password to a
    database that still wants the old — with a file that is correct, internally
    consistent, and passes every static check in this module. The command used
    to answer "every value loads" to that, and the operator went looking at code.

    What is pinned here is the *gate*, because that is what makes the answer
    worth printing: the question reaches the database only when nothing in the
    file could account for a refusal, so a refusal that survives it means the
    volume rather than a second telling of a fault already named.
    """

    def test_a_clean_file_is_worth_asking_about(self) -> None:
        assert check_env.should_ask_the_database(
            offline=False, problems=[], credentials=[], dsn=_DSN
        )

    def test_a_file_that_already_explains_a_refusal_is_not(self) -> None:
        """A `%` escape names the character and the line. Asking through it adds
        "the database said no" — true, vaguer, and printed last."""
        assert not check_env.should_ask_the_database(
            offline=False,
            problems=[],
            credentials=[("ATP_DB_PASSWORD", "contains a `%` ...")],
            dsn=_DSN,
        )

    def test_a_configuration_that_will_not_load_is_not(self) -> None:
        problem = ConfigProblem(env_var="RISK_MAX_POSITION_PCT", reason="nope", value="x")
        assert not check_env.should_ask_the_database(
            offline=False, problems=[problem], credentials=[], dsn=_DSN
        )

    def test_offline_never_asks(self) -> None:
        """The header's promise that this needs no database stays available."""
        assert not check_env.should_ask_the_database(
            offline=True, problems=[], credentials=[], dsn=_DSN
        )

    def test_no_url_to_ask_down(self) -> None:
        assert not check_env.should_ask_the_database(
            offline=False, problems=[], credentials=[], dsn=""
        )

    def test_an_unread_key_does_not_silence_it(self) -> None:
        """A misspelled risk limit has no bearing on whether Postgres accepts a
        password — suppressing the probe over one would hide the database fault
        behind a typo. Unread keys are not a parameter here, and that is why."""
        assert check_env.should_ask_the_database(
            offline=False, problems=[], credentials=[], dsn=_DSN
        )


class TestWhatTheFindingTellsAnOperator:
    """The report itself: it has to name the cause and the two ways out."""

    def test_it_names_initdb_as_the_reason_a_correct_file_fails(self) -> None:
        report = "\n".join(check_env.describe_refusal(_DSN, {"DATABASE_URL": 7}))
        assert "initdb" in report
        assert "NEVER AGAIN" in report

    def test_it_offers_the_fix_that_keeps_the_data_first(self) -> None:
        """`down -v` is a deletion, not a reset — on a paper host that is the
        trade history. It must not be the first thing an operator reaches for."""
        report = "\n".join(check_env.describe_refusal(_DSN, {}))
        assert report.index("ALTER USER") < report.index("down -v")
        assert "KEEPS THE DATA" in report
        assert "DESTROYS" in report

    def test_it_points_at_the_line_to_edit(self) -> None:
        report = "\n".join(check_env.describe_refusal(_DSN, {"DATABASE_URL": 42}))
        assert "line 42" in report

    def test_the_report_never_carries_the_password(self) -> None:
        """§1.6. The finding is *about* a credential, so every line of it — the
        target it names included — is a place the value must not appear."""
        assert _SECRET not in "\n".join(check_env.describe_refusal(_DSN, {"DATABASE_URL": 7}))

    def test_the_target_is_named_without_its_password(self) -> None:
        """Printed so a probe aimed at the wrong server is visible — a developer
        with an unrelated Postgres on 5432 gets a true answer about it."""
        named = check_env.where_it_asked(_DSN)
        assert named == "atp@127.0.0.1:5432/atp"
        assert _SECRET not in named

    def test_an_unnameable_url_still_renders(self) -> None:
        assert check_env.where_it_asked("not a url") == "the url in DATABASE_URL"


class TestTheProbeClassifiesWhatTheDriverRaises:
    """Which asyncpg outcome is a refusal and which is merely silence.

    Real driver exceptions rather than stand-ins, as `test_database_unavailable`
    and `test_database_auth_failure` both argue: a fake raising what we believe
    asyncpg raises would agree with a wrong implementation. `asyncpg.connect` is
    the seam, so the classification under test is the one that actually runs.
    """

    def _connect_raising(self, monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> Any:
        async def _boom(**kwargs: object) -> object:
            raise exc

        monkeypatch.setattr(check_env.asyncpg, "connect", _boom)
        return check_env.probe_stored_password(_DSN, timeout=0.1)

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(pg.InvalidPasswordError("no"), id="28P01-wrong-password"),
            pytest.param(
                pg.InvalidAuthorizationSpecificationError("no"), id="28000-bad-authorization"
            ),
        ],
    )
    def test_the_server_answered_and_refused(
        self, monkeypatch: pytest.MonkeyPatch, exc: BaseException
    ) -> None:
        assert self._connect_raising(monkeypatch, exc) is check_env.Probe.REFUSED

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(ConnectionRefusedError(111, "refused"), id="nothing-listening"),
            pytest.param(TimeoutError("timed out"), id="timed-out"),
            pytest.param(OSError("name or service not known"), id="unresolvable-host"),
            pytest.param(pg.InvalidCatalogNameError("nodb"), id="3D000-no-such-database"),
            pytest.param(pg.CannotConnectNowError("starting"), id="57P03-still-starting"),
        ],
    )
    def test_silence_is_never_a_finding(
        self, monkeypatch: pytest.MonkeyPatch, exc: BaseException
    ) -> None:
        """None of these is evidence about a stored password, and reporting one
        as though it were would send an operator to rotate a working password."""
        assert self._connect_raising(monkeypatch, exc) is check_env.Probe.UNREACHABLE

    def test_a_connection_that_opens_is_accepted_and_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[bool] = []

        class _Connection:
            async def close(self) -> None:
                closed.append(True)

        async def _connect(**kwargs: object) -> _Connection:
            return _Connection()

        monkeypatch.setattr(check_env.asyncpg, "connect", _connect)
        assert check_env.probe_stored_password(_DSN, timeout=0.1) is check_env.Probe.ACCEPTED
        assert closed == [True], "the probe left a connection open"

    def test_an_unparseable_url_is_left_to_settings(self) -> None:
        """`Settings` reports it; saying it twice reads as two faults."""
        assert check_env.probe_stored_password("not a url at all") is check_env.Probe.NOT_ASKED
