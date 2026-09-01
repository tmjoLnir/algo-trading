"""The paper-run preflight — what it refuses, and what it deliberately does not.

Every check here restates a precondition docs/FIRST_PAPER_RUN.md already
states. What is new is that they are checked *before* the week rather than
discovered during it, and the properties worth pinning follow from that:

1. **The expensive failures are the quiet ones.** Short warmup history and a
   size the position cap refuses both produce a worker that runs perfectly and
   never fills anything — which docs/FIRST_PAPER_RUN.md says is
   indistinguishable from a strategy that correctly did not signal. Both are
   decidable in advance; both are FAIL, not WARN.
2. **A skip is not a pass.** "We did not look" and "we looked and it was fine"
   must never render the same, because the run that mixes them up is the one
   started against a stack that was not up.
3. **The locks are not re-derived.** There is one answer to "does this worker
   trade" and it is `trading.decide`.
4. **No credential is ever rendered**, in a detail line or a fix line
   (CLAUDE.md §1.6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from atp_core.config import RiskLimits, Settings
from atp_core.risk.killswitch import HaltReason, HaltRecord, HaltScope
from atp_core.worker import WorkerConfig
from atp_worker import preflight, trading
from atp_worker.preflight import Check, Preflight, Status

SECRET = "sk-do-not-print-me-0123456789"


def settings(**overrides: object) -> Settings:
    """Paper settings. `ATP_*` fields go in by alias — this model does not
    populate by field name, so `run_mode=...` is silently dropped
    (`test_worker_trading.py` says the same thing, and it is worth repeating
    because a preflight test that thought it was checking `live` and was
    actually checking `paper` would pass while proving nothing)."""
    base: dict[str, object] = {
        "ATP_RUN_MODE": "paper",
        "alpaca_api_key": SECRET,
        "alpaca_api_secret": SECRET,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def config(**overrides: object) -> WorkerConfig:
    """A worker configuration that would trade, so each test changes one thing.

    The trading parameters are no longer part of `Settings` — they are the
    `worker_config` row the dashboard writes — so the two fixtures are separate
    and the checks take whichever they actually depend on. That separation is
    the point rather than an inconvenience: `check_run_mode` cannot accidentally
    be influenced by a stop multiplier now, because it is not handed one.
    """
    base: dict[str, object] = {"strategy": "sma_crossover", "symbols": ("SPY",)}
    base.update(overrides)
    return WorkerConfig(**base)  # type: ignore[arg-type]


def a_halt(scope: HaltScope = HaltScope.GLOBAL, target: str | None = None) -> HaltRecord:
    return HaltRecord(
        scope=scope,
        reason=HaltReason.MANUAL,
        engaged_by="an operator",
        engaged_at=datetime(2026, 8, 21, tzinfo=UTC),
        target=target,
    )


class TestTheConfigurationItself:
    def test_paper_passes_and_live_is_refused(self) -> None:
        """The one place this module refuses a configuration that would work.
        docs/FIRST_PAPER_RUN.md opens by saying nothing in it should be run
        against a live account; shrugging at that would make the exception
        routine."""
        assert preflight.check_run_mode(settings()).status is Status.PASS

        live = preflight.check_run_mode(settings(ATP_RUN_MODE="live", ATP_ALLOW_LIVE_TRADING=True))
        assert live.status is Status.FAIL
        assert "paper" in live.fix

    def test_backtest_mode_has_no_venue(self) -> None:
        check = preflight.check_run_mode(settings(ATP_RUN_MODE="backtest"))
        assert check.status is Status.FAIL

    def test_a_missing_key_is_a_failure_and_the_key_is_never_printed(self) -> None:
        """§1.6. The check that reads a credential is the likeliest place in
        this codebase to leak one, so both directions are asserted."""
        missing = preflight.check_credentials(settings(alpaca_api_key=""))
        assert missing.status is Status.FAIL
        assert SECRET not in _rendered(missing)

        present = preflight.check_credentials(settings())
        assert present.status is Status.PASS
        assert SECRET not in _rendered(present)
        # What it reports instead is the thing that actually goes wrong.
        assert "paper-api.alpaca.markets" in present.detail

    def test_the_locks_come_from_decide_rather_than_a_second_copy(self) -> None:
        """A preflight that passed while the worker declined would be the exact
        bug this module exists to stop an operator spending a week on."""
        decision = trading.decide(settings(), config(strategy=""))
        check = preflight.check_locks(decision)
        assert check.status is Status.FAIL
        assert check.detail == decision.reason

    def test_a_strategy_that_rejects_its_params_fails_here_not_at_0931(self) -> None:
        check = preflight.check_strategy(
            config(strategy_params={"fast_period": 30, "slow_period": 10})
        )
        assert check.status is Status.FAIL
        # The strategy's own words, so the operator knows which pair.
        assert "fast_period" in check.detail

    def test_an_unknown_strategy_names_the_registered_ones(self) -> None:
        check = preflight.check_strategy(config(strategy="nope"))
        assert check.status is Status.FAIL
        assert "sma_crossover" in check.fix

    def test_a_good_strategy_reports_the_warmup_the_history_check_will_use(self) -> None:
        check = preflight.check_strategy(config())
        assert check.status is Status.PASS
        assert "warmup_bars=" in check.detail

    def test_a_time_stop_warns_that_layer_5_is_not_exercised(self) -> None:
        """A real stop type that places no level. The run is valid and it does
        not demonstrate the thing SAFETY.md's layer 5 is about, which is worth
        knowing before rather than after."""
        check = preflight.check_stop_config(config(stop_type="time"))
        assert check.status is Status.WARN
        assert "layer 5" in check.detail

    def test_alerting_is_a_warning_and_never_a_failure(self) -> None:
        """Alerting is explicitly not one of SAFETY.md's layers — every layer
        acts on its own — so a run without a transport is still a valid run."""
        quiet = preflight.check_alert_transport(settings())
        assert quiet.status is Status.WARN
        assert "check_alerts.py" in quiet.fix

        wired = preflight.check_alert_transport(settings(alert_ntfy_topic="a-topic"))
        assert wired.status is Status.PASS


class TestTheExpensiveFailures:
    """The two that cost a week and present as silence."""

    def test_short_history_fails_rather_than_warns(self) -> None:
        check = preflight.check_warmup("SPY", required=51, stored=30, newest=None)
        assert check.status is Status.FAIL
        # Says what it would produce, not just that it is short — the reason
        # this is a FAIL is that the failure mode is unreadable, not that it
        # is severe.
        assert "silence" in check.detail
        assert "backfill_bars.py" in check.fix

    def test_no_history_at_all_names_the_backfill_command(self) -> None:
        check = preflight.check_warmup("SPY", required=51, stored=0, newest=None)
        assert check.status is Status.FAIL
        assert "--symbols SPY" in check.fix

    def test_enough_history_passes_and_reports_how_stale_it_is(self) -> None:
        check = preflight.check_warmup(
            "SPY", required=51, stored=200, newest=datetime(2026, 8, 20, tzinfo=UTC)
        )
        assert check.status is Status.PASS
        assert "2026-08-20" in check.detail

    def test_a_size_the_position_cap_refuses_fails_before_the_week(self) -> None:
        """The interaction docs/RISK.md's own recommendation produces: 1% of
        equity against a 2xATR stop asks for far more than a 10% position cap
        allows, so `max_position_size` refuses every entry and the week looks
        silent."""
        check = preflight.check_sizing_is_reachable(
            config(sizing_method="risk_pct", sizing_value=Decimal("0.01")),
            RiskLimits(),
            equity=Decimal(100_000),
            price=Decimal("96.76"),
            stop_price=Decimal("93.49"),
        )
        assert check.status is Status.FAIL
        assert "max_position_size" in check.detail
        # And it says what would fit, rather than leaving the arithmetic to
        # somebody at 09:29.
        assert "sizing value" in check.fix

    def test_a_size_that_fits_passes_and_shows_the_headroom(self) -> None:
        check = preflight.check_sizing_is_reachable(
            config(sizing_method="risk_pct", sizing_value=Decimal("0.003")),
            RiskLimits(),
            equity=Decimal(100_000),
            price=Decimal("96.76"),
            stop_price=Decimal("93.49"),
        )
        assert check.status is Status.PASS
        assert "under the 10% cap" in check.detail

    def test_risk_pct_with_no_derivable_stop_is_the_refusal_it_will_produce(self) -> None:
        """`position_size` refuses to invent a stop, and this reports that
        refusal now rather than as a week of `SIZING` rejections."""
        check = preflight.check_sizing_is_reachable(
            config(sizing_method="risk_pct"),
            RiskLimits(),
            equity=Decimal(100_000),
            price=Decimal(100),
            stop_price=None,
        )
        assert check.status is Status.FAIL
        assert "fixed_qty" in check.fix

    def test_the_predicted_quantity_is_position_sizes_own(self) -> None:
        """Not re-derived here. A prediction computed differently from the
        router's arithmetic is a prediction about a different platform."""
        from atp_core.risk.rules import position_size

        expected = position_size(
            "risk_pct",
            Decimal(100_000),
            Decimal(100),
            stop_price=Decimal(95),
            risk_pct=Decimal("0.001"),
        )
        check = preflight.check_sizing_is_reachable(
            config(sizing_method="risk_pct", sizing_value=Decimal("0.001")),
            RiskLimits(),
            equity=Decimal(100_000),
            price=Decimal(100),
            stop_price=Decimal(95),
        )
        assert f"{expected} shares" in check.detail


class TestTheRestOfTheLocalState:
    def test_a_halt_at_any_scope_fails(self) -> None:
        """A symbol-scoped halt left over from an earlier incident is the one
        that would go unnoticed: the worker starts, the loop runs, and one name
        never trades. On a one-symbol watchlist that is the whole run."""
        check = preflight.check_not_halted([a_halt(HaltScope.SYMBOL, "SPY")])
        assert check.status is Status.FAIL
        assert "SPY" in check.detail
        assert "halt.py clear" in check.fix

    def test_nothing_halted_passes(self) -> None:
        assert preflight.check_not_halted([]).status is Status.PASS

    def test_a_missing_quote_warns_because_premarket_is_the_normal_case(self) -> None:
        check = preflight.check_quote_freshness("SPY", age_seconds=None, budget=30)
        assert check.status is Status.WARN

    def test_a_stale_quote_names_the_rule_that_will_refuse_on_it(self) -> None:
        check = preflight.check_quote_freshness("SPY", age_seconds=120, budget=30)
        assert check.status is Status.WARN
        assert "StaleDataRule" in check.detail

    def test_a_fresh_quote_passes(self) -> None:
        assert (
            preflight.check_quote_freshness("SPY", age_seconds=2, budget=30).status is Status.PASS
        )


class TestTheAccount:
    def test_a_blocked_account_fails_with_nothing_here_able_to_fix_it(self) -> None:
        """A restricted account accepts a submit and refuses it, so the reason
        for a silent week lives at Alpaca rather than in any log here."""
        check = preflight.check_account(
            trading_blocked=True,
            is_pattern_day_trader=False,
            equity=Decimal(100_000),
            buying_power=Decimal(200_000),
            is_paper_host=True,
        )
        assert check.status is Status.FAIL
        assert "Alpaca" in check.fix

    def test_reading_a_live_host_is_refused_even_when_the_account_is_healthy(self) -> None:
        check = preflight.check_account(
            trading_blocked=False,
            is_pattern_day_trader=False,
            equity=Decimal(100_000),
            buying_power=Decimal(200_000),
            is_paper_host=False,
        )
        assert check.status is Status.FAIL

    def test_pdt_under_the_floor_warns(self) -> None:
        check = preflight.check_account(
            trading_blocked=False,
            is_pattern_day_trader=True,
            equity=Decimal(10_000),
            buying_power=Decimal(20_000),
            is_paper_host=True,
        )
        assert check.status is Status.WARN
        assert "PDT" in check.detail

    def test_a_healthy_paper_account_passes(self) -> None:
        check = preflight.check_account(
            trading_blocked=False,
            is_pattern_day_trader=False,
            equity=Decimal(100_000),
            buying_power=Decimal(200_000),
            is_paper_host=True,
        )
        assert check.status is Status.PASS


class TestTheVerdict:
    def test_only_a_failure_stops_the_run(self) -> None:
        report = Preflight(
            [
                Check("a", Status.PASS, ""),
                Check("b", Status.WARN, ""),
                Check("c", Status.SKIP, ""),
            ]
        )
        assert report.ready
        assert report.exit_code() == 0

    def test_a_skip_is_reported_separately_from_a_pass(self) -> None:
        """ "We did not look" and "we looked and it was fine" are the two things
        an operator must never confuse. A skip does not fail the command — the
        local-only run is the first one anybody does — so it has to be visible
        instead."""
        report = Preflight([Check("a", Status.PASS, ""), Check("b", Status.SKIP, "")])
        assert report.exit_code() == 0
        assert [c.name for c in report.skipped] == ["b"]

    def test_any_failure_is_a_non_zero_exit(self) -> None:
        report = Preflight([Check("a", Status.PASS, ""), Check("b", Status.FAIL, "")])
        assert not report.ready
        assert report.exit_code() == 1


@pytest.mark.parametrize(
    "check",
    [
        preflight.check_run_mode(settings(ATP_RUN_MODE="live", ATP_ALLOW_LIVE_TRADING=True)),
        preflight.check_credentials(settings()),
        preflight.check_credentials(settings(alpaca_api_key="")),
        preflight.check_strategy(config()),
        preflight.check_alert_transport(settings(alert_ntfy_topic=SECRET)),
    ],
)
def test_no_check_ever_renders_a_credential(check: Check) -> None:
    """§1.6, as a property over every check that touches one rather than as a
    line in whichever test happened to think of it. The ntfy topic is included
    because config.py's own docstring says the topic *is* a credential."""
    assert SECRET not in _rendered(check)


def _rendered(check: Check) -> str:
    return f"{check.name} {check.detail} {check.fix} {check.source}"
