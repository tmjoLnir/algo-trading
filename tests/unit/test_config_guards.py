"""The live-trading guards. These must never be relaxed to make a test pass.

Every test here was a `pytest.skip("TODO")` until the Alpaca broker landed,
and the reason was a single piece of knowledge nobody had written down: the
four process switches are **aliased** (`run_mode` → `ATP_RUN_MODE`), and
`Settings` does not populate by field name. So `Settings(run_mode="live")`
does not set live mode — `extra="ignore"` drops it and you silently get the
default. Which is why the TODO said "construct Settings(run_mode=live, ...)"
and stopped there.

That mattered more than a skipped test usually does. Rule §1.8 is the guard
standing between `ATP_RUN_MODE=live` and real money, and until now nothing
exercised it.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from atp_core.config import Settings
from atp_core.domain.enums import RunMode

#: The environment variables `Settings` reads that any test here could be
#: fooled by. Cleared per test — see `_settings_read_only_their_inputs`.
_AMBIENT = (
    "ATP_RUN_MODE",
    "ATP_ALLOW_LIVE_TRADING",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
)


@pytest.fixture(autouse=True)
def _settings_read_only_their_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the ambient environment out of every test in this file.

    `Settings` reads the environment, which is correct — and it makes a test
    asking "what does `Settings` do when nothing is set" unanswerable unless
    the environment is controlled, because the honest answer becomes "whatever
    the machine exported". `conftest.pytest_configure` sets
    `ATP_RUN_MODE=backtest` for the session and CI sets it in the job env too,
    while a developer box may well have `paper` exported.

    That is not hypothetical: two tests here asserted the default was `paper`,
    passed on a machine with `ATP_RUN_MODE=paper` exported, and failed in CI
    where it is `backtest`. Both were reading the environment and reporting it
    as the code's default.

    Clearing rather than pinning is the point — the field defaults are what is
    under test. This does **not** weaken conftest's live-trading guard: that
    runs once at session start, before any fixture here, and refuses the whole
    session if `ATP_RUN_MODE=live` was exported.
    """
    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)


def test_live_mode_requires_second_flag() -> None:
    """`ATP_RUN_MODE=live` alone must raise (rule §1.8). One flag is one typo
    away from trading a half-finished strategy with real capital."""
    with pytest.raises(ValueError, match="ATP_ALLOW_LIVE_TRADING"):
        Settings(
            ATP_RUN_MODE="live",
            ATP_ALLOW_LIVE_TRADING=False,
            alpaca_api_key=SecretStr("k"),
            alpaca_api_secret=SecretStr("s"),
        )


def test_both_locks_open_is_the_only_way_to_live() -> None:
    """The other direction: with both flags set, live mode is reachable.

    A guard that refused everything would also pass the test above, and would
    be discovered only by an operator who could not turn the platform on.
    """
    settings = Settings(
        ATP_RUN_MODE="live",
        ATP_ALLOW_LIVE_TRADING=True,
        alpaca_api_key=SecretStr("k"),
        alpaca_api_secret=SecretStr("s"),
    )

    assert settings.run_mode is RunMode.LIVE
    assert settings.is_live is True


def test_allow_live_trading_alone_does_not_arm_anything() -> None:
    """The flag is a permission, not a mode. Set on its own it must leave the
    platform in paper — otherwise the two-lock design has one lock."""
    settings = Settings(
        ATP_ALLOW_LIVE_TRADING=True,
        alpaca_api_key=SecretStr("k"),
        alpaca_api_secret=SecretStr("s"),
    )

    assert settings.run_mode is RunMode.PAPER
    assert settings.is_live is False


def test_default_run_mode_is_paper() -> None:
    """Nothing set, nothing risked. The default must never be live."""
    settings = Settings(alpaca_api_key=SecretStr("k"), alpaca_api_secret=SecretStr("s"))

    assert settings.run_mode is RunMode.PAPER
    assert settings.is_live is False


def test_the_environment_still_selects_the_mode() -> None:
    """The flip side of the fixture above, stated so nobody "fixes" the
    clearing by pinning a mode instead.

    Reading `ATP_RUN_MODE` from the environment is the whole deployment
    mechanism — it is how compose, CI and the runbook choose a mode. The
    fixture removes it as a hidden *input* to the other tests; it must not be
    read as the platform ignoring it.
    """
    settings = Settings(
        ATP_RUN_MODE="paper", alpaca_api_key=SecretStr("k"), alpaca_api_secret=SecretStr("s")
    )
    assert settings.run_mode is RunMode.PAPER

    settings = Settings(ATP_RUN_MODE="backtest")
    assert settings.run_mode is RunMode.BACKTEST


def test_a_non_backtest_mode_demands_credentials() -> None:
    """Paper still talks to a real venue, so it still needs a key.

    The autouse fixture has already cleared the ambient credentials, which is
    load-bearing here: a machine with real Alpaca keys exported — this repo's
    own CI, which runs the live-feed checks — would otherwise satisfy the
    guard from the environment and pass this test without exercising it.
    """
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        Settings(ATP_RUN_MODE="paper")


def test_paper_and_live_use_different_urls() -> None:
    """Requirement #5 is one host swap, not a branch in the engine."""
    paper = Settings(
        ATP_RUN_MODE="paper", alpaca_api_key=SecretStr("k"), alpaca_api_secret=SecretStr("s")
    )
    live = Settings(
        ATP_RUN_MODE="live",
        ATP_ALLOW_LIVE_TRADING=True,
        alpaca_api_key=SecretStr("k"),
        alpaca_api_secret=SecretStr("s"),
    )

    assert paper.broker_base_url == "https://paper-api.alpaca.markets"
    assert live.broker_base_url == "https://api.alpaca.markets"
    assert paper.broker_base_url != live.broker_base_url


def test_a_backtest_needs_no_credentials() -> None:
    """It never opens a socket, so demanding a key would make the one mode
    that cannot lose money the hardest one to run."""
    settings = Settings(ATP_RUN_MODE="backtest")

    assert settings.run_mode is RunMode.BACKTEST


def test_redacted_settings_hide_secrets() -> None:
    """No credential may reach a log sink (rule §1.6)."""
    settings = Settings(
        alpaca_api_key=SecretStr("super-secret-key"),
        alpaca_api_secret=SecretStr("super-secret-value"),
        api_secret_key=SecretStr("jwt-signing-key"),
    )

    redacted = settings.redacted()

    assert redacted["alpaca_api_key"] == "***"
    assert redacted["alpaca_api_secret"] == "***"
    assert redacted["api_secret_key"] == "***"
    # Not just the named fields: nothing anywhere in the dump may carry a
    # secret's value. A field added later without a `SecretStr` type would
    # otherwise leak silently, and this is the assertion that catches it.
    assert "super-secret-key" not in str(redacted)
    assert "super-secret-value" not in str(redacted)
    assert "jwt-signing-key" not in str(redacted)


def test_repr_does_not_leak_a_secret() -> None:
    """`redacted()` is only used by code that remembers to call it. A stack
    trace or a debugger printing the object calls neither."""
    settings = Settings(
        alpaca_api_key=SecretStr("super-secret-key"),
        alpaca_api_secret=SecretStr("super-secret-value"),
    )

    assert "super-secret-key" not in repr(settings)
    assert "super-secret-value" not in repr(settings)
