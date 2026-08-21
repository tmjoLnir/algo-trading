"""The locks in front of placing orders, and the wiring behind them.

`trading.decide` is the whole reason this module exists separately: "does this
configuration place orders?" is a question that should have one answer in one
function, and it guards real money. Every branch of it is tested here, in both
directions — a lock that fails to block is an obvious bug, but a lock that
blocks a configuration an operator deliberately set is how somebody ends up
disabling the lock.

The environment is cleared per test. `Settings` reads it, and a machine with
`ATP_RUN_MODE` exported — CI, or a developer box — would otherwise answer these
questions from the ambient environment instead of from the code.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import SecretStr

from atp_core.config import Settings
from atp_core.domain import RunMode, StopType
from atp_core.errors import ConfigError
from atp_worker import trading

SYMBOLS = ["SPY"]

_AMBIENT = (
    "ATP_RUN_MODE",
    "ATP_ALLOW_LIVE_TRADING",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
    "WORKER_STRATEGY",
    "WORKER_ALLOW_LIVE_ORDERS",
)


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)


def settings(**kwargs: object) -> Settings:
    """Paper settings. `ATP_*` fields are passed by alias — this model does not
    populate by field name, so `run_mode=...` would be silently dropped."""
    base: dict[str, object] = {
        "ATP_RUN_MODE": "paper",
        "alpaca_api_key": SecretStr("k"),
        "alpaca_api_secret": SecretStr("s"),
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


def live_settings(**kwargs: object) -> Settings:
    return settings(ATP_RUN_MODE="live", ATP_ALLOW_LIVE_TRADING=True, **kwargs)


class TestTheDefaultIsNotTrading:
    def test_an_unset_strategy_places_no_orders(self) -> None:
        """A worker that starts trading because it was deployed, rather than
        because somebody chose to, is the accident this prevents."""
        decision = trading.decide(settings(), SYMBOLS)

        assert decision.enabled is False
        assert "WORKER_STRATEGY is unset" in decision.reason

    def test_that_is_a_choice_not_a_blocked_intention(self) -> None:
        """Nobody asked, so it is not CRITICAL — the distinction the startup
        log's level is drawn from."""
        assert trading.decide(settings(), SYMBOLS).blocked is False


class TestPaper:
    def test_naming_a_strategy_is_the_whole_opt_in(self) -> None:
        decision = trading.decide(settings(worker_strategy="sma_crossover"), SYMBOLS)

        assert decision.enabled is True
        assert "paper money" in decision.reason

    def test_the_third_lock_does_not_apply_to_paper(self) -> None:
        """It guards real money. Requiring it for paper would train operators to
        set it, which is exactly how a lock stops working."""
        decision = trading.decide(
            settings(worker_strategy="sma_crossover", worker_allow_live_orders=False), SYMBOLS
        )

        assert decision.enabled is True


class TestLive:
    def test_live_needs_a_third_lock(self) -> None:
        """`ATP_RUN_MODE=live` and `ATP_ALLOW_LIVE_TRADING` say the process may
        trade real money; this says an unattended loop may place the orders."""
        decision = trading.decide(live_settings(worker_strategy="sma_crossover"), SYMBOLS)

        assert decision.enabled is False
        assert "WORKER_ALLOW_LIVE_ORDERS" in decision.reason
        assert decision.blocked is True

    def test_all_three_open_is_the_only_way_to_real_money(self) -> None:
        """A lock that refused everything would pass the test above and be
        found only by an operator who could not turn the platform on."""
        decision = trading.decide(
            live_settings(worker_strategy="sma_crossover", worker_allow_live_orders=True), SYMBOLS
        )

        assert decision.enabled is True
        assert "REAL MONEY" in decision.reason

    def test_the_third_lock_alone_arms_nothing(self) -> None:
        """Set on its own in paper it must not change anything — otherwise the
        three-lock design has two."""
        decision = trading.decide(settings(worker_allow_live_orders=True), SYMBOLS)

        assert decision.enabled is False
        assert "WORKER_STRATEGY is unset" in decision.reason


class TestThingsThatAreNotLocks:
    def test_a_strategy_without_a_watchlist_is_refused(self) -> None:
        """Not a safety control — the strategy would be deciding on a
        repository that nothing is updating."""
        decision = trading.decide(settings(worker_strategy="sma_crossover"), [])

        assert decision.enabled is False
        assert "WORKER_SYMBOLS is empty" in decision.reason
        assert decision.blocked is True

    def test_a_backtest_run_mode_is_refused(self) -> None:
        """There is no venue to trade against, and the CLI is how a backtest
        is run."""
        decision = trading.decide(
            Settings(ATP_RUN_MODE="backtest", worker_strategy="sma_crossover"), SYMBOLS
        )

        assert decision.enabled is False
        assert "run_backtest.py" in decision.reason
        assert decision.blocked is True


class TestStrategyParams:
    def test_empty_means_the_strategys_own_defaults(self) -> None:
        assert trading.strategy_params(settings()) is None

    def test_json_is_parsed(self) -> None:
        parsed = trading.strategy_params(settings(worker_strategy_params='{"fast": 20}'))

        assert parsed == {"fast": 20}

    def test_malformed_json_raises_rather_than_falling_back(self) -> None:
        """Falling back to defaults would run a strategy on parameters the
        operator does not think it has — the quietest way to trade the wrong
        thing."""
        with pytest.raises(ConfigError, match="not valid JSON"):
            trading.strategy_params(settings(worker_strategy_params="{fast: 20}"))

    def test_a_json_scalar_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must be a JSON object"):
            trading.strategy_params(settings(worker_strategy_params="20"))


class TestStopConfig:
    def test_an_atr_stop_gets_a_multiplier_and_no_value(self) -> None:
        """The two families read the same setting differently, and giving each
        its own variable would let an operator set the one their type
        ignores."""
        config = trading.resolve_stop_config(
            settings(worker_stop_type="atr", worker_stop_multiplier=Decimal("3"))
        )

        assert config.stop_type is StopType.ATR
        assert config.multiplier == Decimal("3")
        assert config.value is None

    def test_a_fixed_pct_stop_gets_a_value_and_no_multiplier(self) -> None:
        config = trading.resolve_stop_config(
            settings(worker_stop_type="fixed_pct", worker_stop_multiplier=Decimal("0.02"))
        )

        assert config.stop_type is StopType.FIXED_PCT
        assert config.value == Decimal("0.02")
        assert config.multiplier is None

    def test_the_default_is_atr(self) -> None:
        """docs/RISK.md prefers it over a fixed percentage, which is too tight
        on a volatile name and too loose on a dull one."""
        assert trading.resolve_stop_config(settings()).stop_type is StopType.ATR


class TestDefaults:
    def test_the_default_sizing_is_risk_pct_at_one_percent(self) -> None:
        """docs/RISK.md's default pair: size so hitting the stop loses 1% of
        equity."""
        config = settings()

        assert config.worker_sizing_method == "risk_pct"
        assert config.worker_sizing_value == Decimal("0.01")

    def test_live_orders_are_off_by_default(self) -> None:
        assert settings().worker_allow_live_orders is False

    def test_the_default_run_mode_still_is_not_live(self) -> None:
        assert (
            Settings(alpaca_api_key=SecretStr("k"), alpaca_api_secret=SecretStr("s")).run_mode
            is RunMode.PAPER
        )


class TestAVenueThatIsNotConfigured:
    """A worker told to trade against Alpaca with no key to reach it.

    This used to be unreachable as a *decision*, because `Settings` refused to
    validate at all without a key and the worker died at import — a crash loop
    rather than a worker saying what was wrong. It is now an ordinary blocked
    intention, which is the same shape as every other lock in this file.
    """

    @staticmethod
    def _uncredentialled(**kwargs: object) -> Settings:
        base: dict[str, object] = {"ATP_RUN_MODE": "paper", "worker_strategy": "sma_crossover"}
        base.update(kwargs)
        return Settings(**base)  # type: ignore[arg-type]

    def test_a_strategy_without_a_key_does_not_trade(self) -> None:
        decision = trading.decide(self._uncredentialled(), SYMBOLS)

        assert decision.enabled is False
        assert "ALPACA_API_KEY" in decision.reason

    def test_it_is_a_blocked_intention_not_a_choice(self) -> None:
        """WORKER_STRATEGY is set, so somebody meant this to trade. That is the
        distinction `blocked` carries, and it is what gets it logged loudly
        rather than as a note about an unconfigured worker."""
        assert trading.decide(self._uncredentialled(), SYMBOLS).blocked is True

    def test_backtest_mode_is_not_blocked_by_a_missing_key(self) -> None:
        """It is blocked for having no venue, which is a different sentence and
        must not be replaced by the credential one."""
        decision = trading.decide(self._uncredentialled(ATP_RUN_MODE="backtest"), SYMBOLS)

        assert decision.enabled is False
        assert "ALPACA_API_KEY" not in decision.reason
