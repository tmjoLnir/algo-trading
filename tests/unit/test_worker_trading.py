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

**Two fixtures, because there are two configurations.** `settings()` is what the
*process* is — run mode, credentials — and still comes from the environment.
`config()` is what the *trader* is, and is now a `worker_config` row the
dashboard writes rather than ten more environment variables. Every test hands
`decide` both, which is also how the worker calls it.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from pydantic import SecretStr

from atp_core.config import Settings
from atp_core.domain import RunMode, StopType, Timeframe
from atp_core.errors import ConfigError
from atp_core.worker import DEFAULT_WORKER_CONFIG, WorkerConfig
from atp_core.worker.config import parse_strategy_params
from atp_worker import main, trading

SYMBOLS = ("SPY",)

_AMBIENT = (
    "ATP_RUN_MODE",
    "ATP_ALLOW_LIVE_TRADING",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
)


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the ambient configuration out of every test in this file.

    Both routes into `Settings`, not just the environment: it also reads
    `env_file=".env"`, so on a machine that has run `make up` the defaults
    asserted below are read out of that file instead of from the code. CI never
    sees it — a fresh clone has no `.env` — which is what let it stand.
    """
    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


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


def config(**kwargs: object) -> WorkerConfig:
    """A watchlist and nothing else, so each test opts into one more thing.

    Deliberately not "a configuration that trades": the first lock is that
    somebody named a strategy, and a fixture that named one by default would
    make the default-is-not-trading tests below assert against a fixture rather
    than against the code.
    """
    base: dict[str, object] = {"symbols": SYMBOLS}
    base.update(kwargs)
    return WorkerConfig(**base)  # type: ignore[arg-type]


def trading_config(**kwargs: object) -> WorkerConfig:
    """One that would trade: a strategy and a watchlist."""
    return config(strategy="sma_crossover", **kwargs)


class TestTheDefaultIsNotTrading:
    def test_an_unset_strategy_places_no_orders(self) -> None:
        """A worker that starts trading because it was deployed, rather than
        because somebody chose to, is the accident this prevents."""
        decision = trading.decide(settings(), config())

        assert decision.enabled is False
        assert "no strategy is configured" in decision.reason

    def test_that_is_a_choice_not_a_blocked_intention(self) -> None:
        """Nobody asked, so it is not CRITICAL — the distinction the startup
        log's level is drawn from."""
        assert trading.decide(settings(), config()).blocked is False


class TestPaper:
    def test_naming_a_strategy_is_the_whole_opt_in(self) -> None:
        decision = trading.decide(settings(), trading_config())

        assert decision.enabled is True
        assert "paper money" in decision.reason

    def test_the_third_lock_does_not_apply_to_paper(self) -> None:
        """It guards real money. Requiring it for paper would train operators to
        set it, which is exactly how a lock stops working."""
        decision = trading.decide(settings(), trading_config(allow_live_orders=False))

        assert decision.enabled is True


class TestLive:
    def test_live_needs_a_third_lock(self) -> None:
        """`ATP_RUN_MODE=live` and `ATP_ALLOW_LIVE_TRADING` say the process may
        trade real money; this says an unattended loop may place the orders."""
        decision = trading.decide(live_settings(), trading_config())

        assert decision.enabled is False
        assert "live order placement is not permitted" in decision.reason
        assert decision.blocked is True

    def test_all_three_open_is_the_only_way_to_real_money(self) -> None:
        """A lock that refused everything would pass the test above and be
        found only by an operator who could not turn the platform on."""
        decision = trading.decide(live_settings(), trading_config(allow_live_orders=True))

        assert decision.enabled is True
        assert "REAL MONEY" in decision.reason

    def test_the_third_lock_alone_arms_nothing(self) -> None:
        """Set on its own in paper it must not change anything — otherwise the
        three-lock design has two."""
        decision = trading.decide(settings(), config(allow_live_orders=True))

        assert decision.enabled is False
        assert "no strategy is configured" in decision.reason


class TestThingsThatAreNotLocks:
    def test_a_strategy_without_a_watchlist_is_refused(self) -> None:
        """Not a safety control — the strategy would be deciding on a
        repository that nothing is updating."""
        decision = trading.decide(settings(), WorkerConfig(strategy="sma_crossover"))

        assert decision.enabled is False
        assert "the watchlist is empty" in decision.reason
        assert decision.blocked is True

    def test_a_backtest_run_mode_is_refused(self) -> None:
        """There is no venue to trade against, and the CLI is how a backtest
        is run."""
        decision = trading.decide(settings(ATP_RUN_MODE="backtest"), trading_config())

        assert decision.enabled is False
        assert "run_backtest.py" in decision.reason
        assert decision.blocked is True


class TestStrategyParams:
    """Parsing what an operator typed into the parameters box.

    Moved to `atp_core.worker.config` with the setting itself: the text arrives
    from a textarea now rather than from an environment variable, and the API
    has to be able to refuse it before it is stored. The rule is unchanged and
    is the reason the function exists — a typo must be refused rather than
    quietly falling back to the strategy's defaults.
    """

    def test_empty_means_the_strategys_own_defaults(self) -> None:
        assert parse_strategy_params("") == {}
        assert parse_strategy_params("   ") == {}

    def test_json_is_parsed(self) -> None:
        assert parse_strategy_params('{"fast": 20}') == {"fast": 20}

    def test_malformed_json_raises_rather_than_falling_back(self) -> None:
        """Falling back to defaults would run a strategy on parameters the
        operator does not think it has — the quietest way to trade the wrong
        thing."""
        with pytest.raises(ConfigError, match="not valid JSON"):
            parse_strategy_params("{fast: 20}")

    def test_a_json_scalar_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must be a JSON object"):
            parse_strategy_params("20")


class TestStopConfig:
    def test_an_atr_stop_gets_a_multiplier_and_no_value(self) -> None:
        """The two families read the same field differently, and giving each
        its own would let an operator fill in the one their type ignores."""
        stop = trading.resolve_stop_config(config(stop_type="atr", stop_multiplier=Decimal("3")))

        assert stop.stop_type is StopType.ATR
        assert stop.multiplier == Decimal("3")
        assert stop.value is None

    def test_a_fixed_pct_stop_gets_a_value_and_no_multiplier(self) -> None:
        stop = trading.resolve_stop_config(
            config(stop_type="fixed_pct", stop_multiplier=Decimal("0.02"))
        )

        assert stop.stop_type is StopType.FIXED_PCT
        assert stop.value == Decimal("0.02")
        assert stop.multiplier is None

    def test_the_default_is_atr(self) -> None:
        """docs/RISK.md prefers it over a fixed percentage, which is too tight
        on a volatile name and too loose on a dull one."""
        assert trading.resolve_stop_config(config()).stop_type is StopType.ATR


class TestDefaults:
    """The defaults a worker runs on when nothing has been saved.

    These are asserted against `DEFAULT_WORKER_CONFIG` rather than against a
    `Settings` instance now, and they are the same values the environment
    variables carried. An install that upgrades and saves nothing trades exactly
    what it traded before — which is nothing, because the two fields that decide
    that were empty then too.
    """

    def test_the_default_sizing_is_risk_pct_at_one_percent(self) -> None:
        """docs/RISK.md's default pair: size so hitting the stop loses 1% of
        equity."""
        assert DEFAULT_WORKER_CONFIG.sizing_method == "risk_pct"
        assert DEFAULT_WORKER_CONFIG.sizing_value == Decimal("0.01")

    def test_live_orders_are_off_by_default(self) -> None:
        assert DEFAULT_WORKER_CONFIG.allow_live_orders is False

    def test_nothing_is_traded_by_default(self) -> None:
        """The whole of lock 1, and the reason a fresh install is inert."""
        assert DEFAULT_WORKER_CONFIG.strategy == ""
        assert DEFAULT_WORKER_CONFIG.symbols == ()
        assert DEFAULT_WORKER_CONFIG.trades is False

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
        base: dict[str, object] = {"ATP_RUN_MODE": "paper"}
        base.update(kwargs)
        return Settings(**base)  # type: ignore[arg-type]

    def test_a_strategy_without_a_key_does_not_trade(self) -> None:
        decision = trading.decide(self._uncredentialled(), trading_config())

        assert decision.enabled is False
        assert "ALPACA_API_KEY" in decision.reason

    def test_it_is_a_blocked_intention_not_a_choice(self) -> None:
        """A strategy is configured, so somebody meant this to trade. That is
        the distinction `blocked` carries, and it is what gets it logged loudly
        rather than as a note about an unconfigured worker."""
        assert trading.decide(self._uncredentialled(), trading_config()).blocked is True

    def test_backtest_mode_is_not_blocked_by_a_missing_key(self) -> None:
        """It is blocked for having no venue, which is a different sentence and
        must not be replaced by the credential one."""
        decision = trading.decide(self._uncredentialled(ATP_RUN_MODE="backtest"), trading_config())

        assert decision.enabled is False
        assert "ALPACA_API_KEY" not in decision.reason


class TestTheSeriesBothEndsRead:
    """`build_runner` takes the timeframe off the row, and so does the ingestor.

    The two used to be set independently — the runner hard-coded `Timeframe.D1`
    and the ingestor took its own `1m` default — and because the bar repository
    filters strictly on the column, the disagreement produced no error at all.
    The runner asked for a series nothing was writing and was handed nothing,
    for ten hours (docs/paper-week/day-1-review.md).
    """

    def test_the_runner_is_built_for_the_configured_series(self) -> None:
        assert config(timeframe="1m").bar_timeframe is Timeframe.M1
        assert config(timeframe="1d").bar_timeframe is Timeframe.D1

    def test_it_is_no_longer_hard_coded(self) -> None:
        """The specific regression: a literal here is what made the row's value
        irrelevant, so a reader changing it back should fail this."""
        source = inspect.getsource(trading.build_runner)
        assert "timeframe=config.bar_timeframe" in source
        assert "Timeframe.D1" not in source

    def test_the_worker_gives_the_ingestor_the_same_value(self) -> None:
        """One property feeding both call sites is the whole mechanism. If
        `main` ever stops passing it, the ingestor silently reverts to its own
        default and the disagreement is expressible again."""
        source = inspect.getsource(main.run)
        assert "bar_timeframe=config.bar_timeframe" in source
