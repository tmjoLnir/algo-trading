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

import ast
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings

from atp_core import config
from atp_core.alerts import LoggingAlertSink, build_alert_sink
from atp_core.config import _ENV_MODELS, Settings

#: A `.env` shaped like an operator's: alerting wired up, and two ordinary
#: values so the assertions below are not all about alerting.
_A_HOSTS_DOTENV = """\
ALERT_NTFY_TOPIC=atp-a-real-topic
ALERT_TELEGRAM_TOKEN=123456:AAF-a-real-token
ALERT_TELEGRAM_CHAT_ID=9876
ENGINE_TICK_INTERVAL_SECONDS=17
DASHBOARD_STALE_AFTER_SECONDS=999
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

    `Settings` reads a `.env`, and any test asserting a default is wrong by the
    same mechanism — which is how two `test_config_guards.py` cases came to read
    `ATP_RUN_MODE` out of the file written by `make up`.

    Two fields rather than one, and neither of them aliased, because the failure
    is per-model rather than per-field: one assertion would pass for a model
    that had been detached for an unrelated reason.
    """
    assert Settings().engine_tick_interval_seconds == 60
    assert Settings().dashboard_stale_after_seconds == 300


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


def test_no_test_hides_inside_a_class_pytest_does_not_collect() -> None:
    """A `test_*` method on a class whose name pytest ignores is a test that
    silently does not run.

    This file exists because a defence that has to be remembered per file is one
    the next file forgets, and this is the second instance of that shape. It has
    happened here: a test double inserted into the middle of `TestRiskRules`
    adopted the twenty-two methods below it, `pytest` collected 111 where it had
    collected 126, and **every gate stayed green** — `ruff`, `mypy` and the
    suite itself all pass over tests that are no longer being run. Nothing in
    `make check` reads the collected count, so nothing else can catch it.

    Walked with `ast` rather than by importing, so a module with a collection-
    time side effect cannot hide from it and a syntax error is a loud failure
    rather than a skip. Names, not `pytest`'s own collection: the point is to
    catch the file where the two disagree.

    **Three shapes, because the first version of this guard missed two of them**
    — including the one it was written for. `pytest` never descends into a class
    it will not collect, so a `Test*` class *nested* inside an ordinary one is
    as invisible as a `test_` method is, and the original check skipped any
    class whose own name began with `Test` without looking at what enclosed it.
    A `Test*` class with an `__init__` is the third: `pytest` refuses it with a
    `PytestCollectionWarning` that nothing in `make check` turns into a failure.
    All three were found by an adversarial review of the guard itself, and each
    is verified here by construction rather than by argument.
    """
    tests_root = Path(__file__).resolve().parent.parent
    orphans: list[str] = []

    def walk(node: ast.AST, path: Path, enclosing: tuple[str, ...], collected: bool) -> None:
        """Descend the way `pytest` does, carrying whether it can still reach us.

        `collected` is False the moment any enclosing class is one `pytest`
        skips: nothing beneath such a class is ever reached, however it is
        named. That is the whole correction — the flag is inherited, not
        recomputed from the node's own name.
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                takeable = collected and _is_collectable(child)
                walk(child, path, (*enclosing, child.name), takeable)
            elif (
                isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                and child.name.startswith("test_")
                and enclosing
                and not collected
            ):
                orphans.append(
                    f"{path.relative_to(tests_root)}::{'::'.join(enclosing)}::{child.name}"
                )

    for path in sorted(tests_root.rglob("*.py")):
        walk(ast.parse(path.read_text(), filename=str(path)), path, (), True)

    assert orphans == [], (
        "these test methods live under a class pytest will not collect, so they "
        "never run — rename the class to Test*, give it no __init__, or move the "
        "method out:\n  " + "\n  ".join(orphans)
    )


def _is_collectable(node: ast.ClassDef) -> bool:
    """Whether `pytest` would collect tests from this class at all.

    Two refusals, and neither is loud enough to catch on its own: a name that
    does not start with `Test` is skipped silently, and an `__init__` is skipped
    with a `PytestCollectionWarning` that `make check` does not turn into a
    failure.
    """
    if not node.name.startswith("Test"):
        return False
    return not any(
        isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name == "__init__"
        for item in node.body
    )
