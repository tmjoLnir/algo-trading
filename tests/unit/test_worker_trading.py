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
from typing import ClassVar

import pytest
from pydantic import SecretStr

from atp_core.config import Settings
from atp_core.domain import RunMode, StopType, Timeframe
from atp_core.errors import ConfigError
from atp_core.strategy.examples.sma_crossover import SmaCrossover
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


class TestTheStrategyGetsTheSeriesItAskedFor:
    """Day 1 fixed the *ingestor* and the *runner* disagreeing. This is the
    third party neither fix covered: the strategy.

    `runner.timeframe_mismatch asked_for=1d serving=1m` was logged once, at
    13:33, and 385 more evaluations ran in silence. `sma_crossover`'s 20/50
    pair, declared against daily bars, is a 20-day/50-day trend system; served
    minute bars it is a 20-minute/50-minute scalper. That is what took 38 round
    trips that session, and nobody chose it
    (docs/paper-week/day-2-review.md, F8).
    """

    def test_a_strategy_written_for_daily_bars_refuses_a_minute_worker(self) -> None:
        with pytest.raises(ConfigError, match="written for 1d bars"):
            trading.require_matching_timeframe(SmaCrossover(), Timeframe.M1)

    def test_the_refusal_names_both_ways_out(self) -> None:
        """An operator reading this at 08:00 needs to know which of the two
        knobs to turn, and that changing the strategy's is not free."""
        with pytest.raises(ConfigError) as caught:
            trading.require_matching_timeframe(SmaCrossover(), Timeframe.M1)

        message = str(caught.value)
        assert "strategy_params.timeframe" in message
        assert "re-tune its periods" in message

    def test_an_agreeing_pair_starts(self) -> None:
        trading.require_matching_timeframe(SmaCrossover({"timeframe": "1m"}), Timeframe.M1)
        trading.require_matching_timeframe(SmaCrossover(), Timeframe.D1)

    def test_a_strategy_that_declares_nothing_is_indifferent_not_mismatched(self) -> None:
        """The worker's series is then the only answer available, and it is the
        right one. Refusing here would block every strategy that has no opinion."""

        class Silent(SmaCrossover):
            params_schema: ClassVar[dict[str, object]] = {"type": "object", "properties": {}}

        trading.require_matching_timeframe(Silent(), Timeframe.M1)

    def test_it_is_checked_where_the_strategy_is_built(self) -> None:
        """At assembly, before a socket is opened or a bar is read — a warning
        was already the answer and it was not enough."""
        source = inspect.getsource(trading.build_runner)
        assert "require_matching_timeframe(strategy, config.bar_timeframe)" in source

    def test_a_schema_default_counts_as_a_declaration(self) -> None:
        """Reading only the operator's override would make an unconfigured
        strategy look indifferent when it is not — which is the exact reading
        that let day 2 happen."""
        assert SmaCrossover().declared_timeframe is Timeframe.D1
        assert SmaCrossover({"timeframe": "1m"}).declared_timeframe is Timeframe.M1


class TestTheWorkerRefusesASeriesNobodyWrites:
    """Day 5's near miss, as assertions.

    `require_matching_timeframe` settles which series the strategy and the
    worker agree on. It cannot settle whether that series is written at all, and
    for every timeframe but `STREAMED_BAR_TIMEFRAME` it is not: the realtime
    feed carries minute bars, `MarketDataFeed.subscribe` takes no timeframe to
    ask otherwise, and the adapter decodes every streamed bar at that one
    timeframe. A worker configured for `1d` therefore has an ingestor writing
    `1m`, `_refresh_bars` reading `1d`, and no bar ever closing — `on_bar` is
    never called, all session, on a process reporting healthy.

    The pairing is reachable and was nearly recommended: `sma_crossover`
    declares `1d`, #159 measured `1m` as the reason day 4 could not mean
    anything, and the timeframe is a row the dashboard writes (ADR 0023). An
    operator following that advice would have got day 1 back
    (docs/paper-week/day-5-readiness.md, §3.2).
    """

    def test_a_series_the_feed_does_not_write_refuses_to_start(self) -> None:
        with pytest.raises(ConfigError, match="nothing writes them while the market is open"):
            trading.require_deliverable_timeframe(Timeframe.D1)

    def test_the_series_the_feed_writes_starts(self) -> None:
        trading.require_deliverable_timeframe(Timeframe.M1)

    def test_every_coarser_series_is_refused_not_just_daily(self) -> None:
        """The hole is not `1d`-shaped. Any timeframe the decoder does not stamp
        reads a column the ingestor never writes."""
        for timeframe in (Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.D1):
            with pytest.raises(ConfigError):
                trading.require_deliverable_timeframe(timeframe)

    def test_the_refusal_says_what_would_have_happened(self) -> None:
        """ "Refusing to start" is not enough on its own: the operator set this
        row deliberately, on advice, and needs to know the failure it buys is
        silence rather than an error."""
        with pytest.raises(ConfigError) as caught:
            trading.require_deliverable_timeframe(Timeframe.D1)

        message = str(caught.value)
        assert "no bar would ever close" in message
        assert "reporting healthy" in message
        assert Timeframe.M1.value in message, "and which series to set instead"

    def test_it_is_checked_where_the_runner_is_built(self) -> None:
        """At assembly, beside the match check and before it — a series nothing
        writes is a prior question to which series was agreed."""
        source = inspect.getsource(trading.build_runner)
        assert "require_deliverable_timeframe(config.bar_timeframe)" in source
        assert source.index("require_deliverable_timeframe") < source.index(
            "require_matching_timeframe"
        )

    def test_the_mismatch_refusal_stops_offering_a_series_nobody_writes(self) -> None:
        """`require_matching_timeframe`'s own remedy used to end "or set the
        worker's timeframe to '1d'" — which is this guard's failure, arrived at
        by following the advice of the check next door."""
        with pytest.raises(ConfigError) as caught:
            trading.require_matching_timeframe(SmaCrossover(), Timeframe.M1)

        assert "set the worker's timeframe" not in str(caught.value).lower()

    def test_it_still_offers_the_remedy_when_that_remedy_is_deliverable(self) -> None:
        """A strategy declaring `1m` against a `1d` worker is the mirror case,
        and there the second knob is the right one to turn."""
        with pytest.raises(ConfigError) as caught:
            trading.require_matching_timeframe(SmaCrossover({"timeframe": "1m"}), Timeframe.D1)

        assert "or set the worker's timeframe to '1m'" in str(caught.value).lower()
