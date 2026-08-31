"""What the test session itself guarantees about ambient configuration.

Every other file here asks what the platform does. This one asks what the
harness does, because the harness is an input to all of them: `Settings` reads
the process environment *and* `env_file=".env"`, so unless both are taken away
at the start of the session, "what does this do when nothing is configured" is
answered by whatever the machine running the tests happens to have.

That is not a theoretical worry — it is the failure that produced this file.
Six tests asserting on the unconfigured case (four in `test_alerts.py`, one in
`test_operator_scripts.py`, one in `test_preflight.py`) failed on an operator's
machine and passed in CI, because a real `ALERT_TELEGRAM_*` pair in their `.env`
reached a bare `Settings()`. `conftest.pytest_configure` closes both routes;
what follows is the test that would have caught it being open, and which fails
if either half is ever removed.

The failure is worth pinning rather than fixing once, for a reason the git
history makes plain: two files had already met it and detached the file
themselves (`test_config_guards.py`, `test_worker_trading.py`), and a dozen API
fixtures pass `_env_file=None` against it. A defence that has to be remembered
per file is one the next file forgets.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from pydantic_settings import BaseSettings

from atp_core import config
from atp_core.alerts import LoggingAlertSink, build_alert_sink
from atp_core.config import _ENV_MODELS, RiskLimits, Settings

if TYPE_CHECKING:
    from pathlib import Path

#: A `.env` shaped like an operator's: alerting wired up, and one ordinary
#: value per settings model so the assertions below are not all about alerting.
_A_HOSTS_DOTENV = """\
ALERT_NTFY_TOPIC=atp-a-real-topic
ALERT_TELEGRAM_TOKEN=123456:AAF-a-real-token
ALERT_TELEGRAM_CHAT_ID=9876
ENGINE_TICK_INTERVAL_SECONDS=17
RISK_MAX_POSITION_PCT=0.99
"""


@pytest.fixture
def a_dotenv_in_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A populated `.env` where `Settings` looks for one.

    In `tmp_path` and reached by `chdir`, never by writing into the repository:
    `env_file=".env"` is relative to the working directory, so a test that wrote
    one where it is resolved from would be overwriting the real file of the
    operator this whole module exists to protect.
    """
    (tmp_path / ".env").write_text(_A_HOSTS_DOTENV)
    monkeypatch.chdir(tmp_path)


@pytest.mark.usefixtures("a_dotenv_in_the_working_directory")
def test_a_dotenv_does_not_configure_alerting() -> None:
    """The regression, in the shape it actually arrived in.

    A transport configured in a file the test never mentioned makes the sink a
    real one, and the four `TestBuildAlertSink` cases asserting on the
    unconfigured platform fail describing the host rather than the code. Worse
    than the red build: a suite that then sent through that sink would ring
    somebody's actual phone, which CLAUDE.md §1.7 forbids.
    """
    assert isinstance(build_alert_sink(Settings()), LoggingAlertSink)


@pytest.mark.usefixtures("a_dotenv_in_the_working_directory")
def test_a_dotenv_reaches_no_setting_at_all() -> None:
    """Alerting is where it was noticed, not the extent of it.

    Both models read a `.env`, and any test asserting a default is wrong by the
    same mechanism — which is how two `test_config_guards.py` cases came to read
    `ATP_RUN_MODE` out of the file written by `make up`.
    """
    assert Settings().engine_tick_interval_seconds == 60
    assert RiskLimits().max_position_pct == Decimal("0.10")


@pytest.mark.parametrize("model", _ENV_MODELS, ids=lambda m: m.__name__)
def test_every_settings_model_is_detached_from_the_dotenv(
    model: type[BaseSettings],
) -> None:
    """`conftest._detach_dotenv` ran, and covered this model.

    The two tests above prove the outcome for the working directory they were
    given; this one says the mechanism is in place for every model, so removing
    it fails here — where the docstring explains it — rather than in six
    unrelated files on one unlucky machine.
    """
    assert model.model_config.get("env_file") is None


def test_every_settings_model_is_registered() -> None:
    """`_ENV_MODELS` is complete, so everything derived from it is too.

    The detach above walks that list, and so do `known_env_names()` and
    `config_problems()` — the env doctor's whole view of what an operator may
    set. A model missing from it is not merely undetached in tests: it is a
    model `make check-env` silently stops validating.
    """
    defined = {
        value
        for value in vars(config).values()
        if isinstance(value, type) and issubclass(value, BaseSettings) and value is not BaseSettings
    }
    assert defined == set(_ENV_MODELS), (
        "a settings model is missing from atp_core.config._ENV_MODELS — "
        "add it there, or the env doctor and the test harness both skip it"
    )
