"""`scripts/manage_secrets.py`, as far as it can be tested without a key or a bundle.

What is tested here is everything the wrapper itself decides: which keys may
not be in a bundle, how plaintext reaches the disk, and — the one that would be
a security bug rather than an inconvenience — that a failure never quotes the
plaintext it was handling.

What is deliberately not tested is `sops` and `age`. They own the cryptography,
they have their own test suites, and a fake that agreed with us about AES-GCM
would prove nothing. Every test here injects a runner or monkeypatches
`run_sops`, so the suite needs neither binary installed; the round trip through
the real ones is an operator step in docs/DEPLOYMENT.md.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> Any:
    """Import a script by path — `scripts/` is a set of entry points, not a
    package. Same approach as `test_operator_scripts.py`, for the same reason."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


secrets = _load("manage_secrets")


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["sops"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestParseDotenv:
    def test_reads_pairs_and_skips_furniture(self) -> None:
        values = secrets.parse_dotenv(
            "# a comment\n\nALPACA_API_KEY=abc\n  API_USER = operator \nnot a pair\n"
        )
        assert values == {"ALPACA_API_KEY": "abc", "API_USER": "operator"}

    def test_keeps_equals_signs_inside_a_value(self) -> None:
        """A bcrypt hash and a DSN both contain characters that look like syntax.
        Splitting on every `=` would silently truncate a password."""
        values = secrets.parse_dotenv("DATABASE_URL=postgresql://u:p==w@h:5432/db\n")
        assert values["DATABASE_URL"] == "postgresql://u:p==w@h:5432/db"

    def test_strips_an_export_prefix(self) -> None:
        assert secrets.parse_dotenv("export API_USER=operator\n") == {"API_USER": "operator"}

    def test_an_empty_value_is_read_as_present_and_empty(self) -> None:
        """Not skipped: `policy_failures` has to be able to see it. SOPS leaves
        an empty value unencrypted, so it is neither a secret nor protected."""
        assert secrets.parse_dotenv("API_SECRET_KEY=\n") == {"API_SECRET_KEY": ""}


class TestPolicy:
    @pytest.mark.parametrize(
        "key", ["ATP_RUN_MODE", "ATP_ALLOW_LIVE_TRADING", "WORKER_ALLOW_LIVE_ORDERS"]
    )
    def test_a_run_mode_lock_is_refused(self, key: str) -> None:
        """docs/SAFETY.md layers 1-2. A bundle is copied between hosts and
        restored from backups; none of that may switch on live trading."""
        failures = secrets.policy_failures({key: "true"})
        assert len(failures) == 1
        assert key in failures[0]

    def test_an_ordinary_bundle_passes(self) -> None:
        assert secrets.policy_failures({"ALPACA_API_KEY": "abc", "API_USER": "operator"}) == []

    def test_an_empty_value_is_refused(self) -> None:
        failures = secrets.policy_failures({"API_SECRET_KEY": ""})
        assert len(failures) == 1
        assert "API_SECRET_KEY" in failures[0]

    def test_a_failure_never_quotes_the_value(self) -> None:
        """The whole point of the tool. A refusal is printed to a terminal, a CI
        log and sometimes an issue; naming the key is the useful half and
        printing the secret is the dangerous one."""
        failures = secrets.policy_failures({"ATP_ALLOW_LIVE_TRADING": "hunter2-do-not-print-me"})
        assert failures
        assert "hunter2-do-not-print-me" not in "\n".join(failures)

    def test_missing_expected_keys_are_reported_but_are_not_failures(self) -> None:
        """Which of these a host needs depends on its run mode, so an absent one
        is a question rather than an error."""
        assert secrets.policy_failures({}) == []
        assert "ALPACA_API_KEY" in secrets.missing_expected({})


class TestRunSops:
    def test_a_failure_reports_stderr(self) -> None:
        with pytest.raises(secrets.SecretsError, match="config file is invalid"):
            secrets.run_sops(
                ["--decrypt"], runner=lambda *a, **k: _completed(1, stderr="config file is invalid")
            )

    def test_a_failure_never_reports_stdout(self) -> None:
        """On the decrypt path stdout IS the plaintext. An error handler that
        included "the output" would put every credential into a traceback, a CI
        log and a bug report — so this is a security assertion, not tidiness."""
        plaintext = "ALPACA_API_SECRET=the-actual-secret"
        with pytest.raises(secrets.SecretsError) as caught:
            secrets.run_sops(
                ["--decrypt"],
                runner=lambda *a, **k: _completed(1, stdout=plaintext, stderr="boom"),
            )
        assert "the-actual-secret" not in str(caught.value)

    def test_a_missing_key_gets_an_explanation(self) -> None:
        with pytest.raises(secrets.SecretsError, match="SOPS_AGE_KEY_FILE"):
            secrets.run_sops(
                ["--decrypt"],
                runner=lambda *a, **k: _completed(
                    128, stderr="Failed to get the data key required to decrypt the SOPS file."
                ),
            )

    def test_stderr_alone_is_not_a_failure(self) -> None:
        """sops writes an upstream-version-check warning to stderr wherever that
        request is blocked. The exit code is what says whether it worked."""
        out = secrets.run_sops(
            ["--decrypt"],
            runner=lambda *a, **k: _completed(0, stdout="A=1", stderr="[warning] ..."),
        )
        assert out == "A=1"


class TestWritePrivate:
    def test_writes_owner_only(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / ".env"
        secrets.write_private(target, "A=1\n")
        assert target.read_text() == "A=1\n"
        assert target.stat().st_mode & 0o777 == 0o600

    def test_replaces_atomically_leaving_no_temp_behind(self, tmp_path: Path) -> None:
        target = tmp_path / ".env"
        secrets.write_private(target, "A=1\n")
        secrets.write_private(target, "A=2\n")
        assert target.read_text() == "A=2\n"
        assert list(tmp_path.iterdir()) == [target]

    def test_a_failed_write_leaves_the_previous_file_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-written .env starts the stack with half its credentials, which
        fails in ways that look like anything but a truncated file."""
        target = tmp_path / ".env"
        secrets.write_private(target, "OLD=1\n")

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(secrets.os, "fsync", boom)
        with pytest.raises(OSError, match="disk full"):
            secrets.write_private(target, "NEW=1\n")

        assert target.read_text() == "OLD=1\n"
        assert list(tmp_path.iterdir()) == [target]


class TestInstall:
    def test_refuses_a_bundle_carrying_a_live_lock_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The check that matters most: `import` refuses these, but a bundle can
        acquire one later through `sops` directly or a hand edit, and install is
        the last place to notice before it reaches a running host."""
        bundle = tmp_path / "paper.sops.env"
        bundle.write_text("encrypted")
        target = tmp_path / ".env"
        target.write_text("PRE_EXISTING=keep\n")

        monkeypatch.setattr(secrets, "require_bundle", lambda env: bundle)
        monkeypatch.setattr(
            secrets, "run_sops", lambda *a, **k: "ALPACA_API_KEY=x\nWORKER_ALLOW_LIVE_ORDERS=true\n"
        )

        args = secrets.parse_args(["install", "--env", "paper", "--to", str(target)])
        assert args.func(args) == 1
        assert target.read_text() == "PRE_EXISTING=keep\n"
        assert "WORKER_ALLOW_LIVE_ORDERS" in capsys.readouterr().err

    def test_writes_the_bundle_when_it_is_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = tmp_path / "paper.sops.env"
        bundle.write_text("encrypted")
        target = tmp_path / ".env"

        monkeypatch.setattr(secrets, "require_bundle", lambda env: bundle)
        monkeypatch.setattr(secrets, "run_sops", lambda *a, **k: "ALPACA_API_KEY=x\n")

        args = secrets.parse_args(["install", "--env", "paper", "--to", str(target)])
        assert args.func(args) == 0
        assert target.read_text() == "ALPACA_API_KEY=x\n"
        assert target.stat().st_mode & 0o777 == 0o600


class TestBundlePath:
    def test_names_a_bundle_per_environment(self) -> None:
        assert secrets.bundle_path("paper").name == "paper.sops.env"

    @pytest.mark.parametrize("name", ["", "../etc/passwd", ".hidden", "a/b"])
    def test_refuses_a_name_that_would_escape_the_bundle_directory(self, name: str) -> None:
        with pytest.raises(secrets.SecretsError):
            secrets.bundle_path(name)


class TestArguments:
    def test_every_bundle_command_demands_an_environment(self) -> None:
        """Paper and live are separate hosts with separate keys (ADR 0011).
        A default would eventually install one host's secrets on the other."""
        for command in ("import", "edit", "check", "install"):
            with pytest.raises(SystemExit):
                secrets.parse_args([command])
