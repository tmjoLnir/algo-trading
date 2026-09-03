"""What a worker publishes about itself, and what survives a release boundary.

`encode_running`/`decode_running` are the wire form of `RunningWorkerConfig` —
the report a worker writes to Redis at start so the settings screen can say
whether what is saved is what is running. Two things make it worth its own
module:

- **The risk ceilings are in it.** The worker builds its `RiskEngine` once, at
  start, so a ceiling saved since is not the ceiling refusing that worker's
  orders. The screen can only state that difference if the worker publishes what
  it booted with, which means these eight have to survive the round trip
  exactly.
- **A blob written by the previous release has no `risk` key.** Redis keeps this
  for seven days, so during any deploy there is a window where the API decodes a
  payload the current worker did not write. `WorkerStatusStore.get` is documented
  to return None rather than raise — a dashboard that refused to render the
  settings form because a status blob was a release behind would take away the
  one screen that could fix it — so the decoder must not throw over the missing
  key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from atp_core.persistence.worker_status import decode_running, encode_running
from atp_core.risk.limits import DEFAULT_RISK_LIMITS, RiskLimits
from atp_core.worker import RunningWorkerConfig, WorkerConfig

STARTED = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)

TIGHTENED = RiskLimits(
    max_position_pct=Decimal("0.04"),
    max_gross_exposure_pct=Decimal("0.80"),
    max_daily_loss_pct=Decimal("0.015"),
    max_orders_per_minute=5,
    max_open_positions=6,
    max_quote_age_seconds=10,
    default_stop_loss_pct=Decimal("0.011"),
    default_take_profit_pct=Decimal("0.033"),
)


def running(**overrides: object) -> RunningWorkerConfig:
    config = WorkerConfig(symbols=("SPY",), strategy="sma_crossover", **overrides)  # type: ignore[arg-type]
    return RunningWorkerConfig(
        config=config, revision=9, started_at=STARTED, trading=True, reason="trading SPY"
    )


class TestTheCeilingsSurviveTheRoundTrip:
    def test_every_ceiling_comes_back_identical(self) -> None:
        decoded = decode_running(encode_running(running(risk=TIGHTENED)))
        assert decoded.config.risk == TIGHTENED

    def test_a_fraction_crosses_as_a_string(self) -> None:
        """Never as a JSON float. These are multiplied by equity to produce the
        number an order is refused against, and `0.1` through a float is not the
        ceiling that was typed (rule §1.1)."""
        payload = encode_running(running(risk=TIGHTENED))
        assert payload["config"]["risk"]["max_position_pct"] == "0.04"
        assert isinstance(payload["config"]["risk"]["max_daily_loss_pct"], str)

    def test_a_count_crosses_as_an_integer(self) -> None:
        payload = encode_running(running(risk=TIGHTENED))
        assert payload["config"]["risk"]["max_open_positions"] == 6

    def test_the_rest_of_the_report_is_unchanged(self) -> None:
        decoded = decode_running(encode_running(running(risk=TIGHTENED)))
        assert (decoded.revision, decoded.started_at, decoded.trading) == (9, STARTED, True)


class TestABlobFromThePreviousRelease:
    """The deploy window. Seven days of TTL means this is not hypothetical."""

    def test_a_missing_risk_key_decodes_to_the_defaults(self) -> None:
        """Which is honest, not a guess: a worker from before the ceilings moved
        was running the `.env.example` values, and those are the defaults."""
        payload = encode_running(running(risk=TIGHTENED))
        del payload["config"]["risk"]

        assert decode_running(payload).config.risk == DEFAULT_RISK_LIMITS

    @pytest.mark.parametrize("junk", [None, "risk", 7, []])
    def test_a_risk_key_that_is_not_an_object_is_treated_the_same(self, junk: object) -> None:
        """Anything that is not a mapping is a payload this code did not write,
        and the rule for those is the same: render the form rather than 500."""
        payload = encode_running(running())
        payload["config"]["risk"] = junk

        assert decode_running(payload).config.risk == DEFAULT_RISK_LIMITS

    def test_a_ceiling_a_release_has_since_forbidden_still_raises(self) -> None:
        """The one direction that must *not* be swallowed.

        A published blob whose values no longer satisfy `RiskLimits` is a worker
        running limits this platform has decided are unsafe. `get` catches it
        and reports no worker, which is the truthful thing to render — silently
        substituting the defaults would draw a screen claiming ceilings that
        process is not enforcing.
        """
        payload = encode_running(running())
        payload["config"]["risk"]["max_open_positions"] = 0

        with pytest.raises(Exception, match="max_open_positions"):
            decode_running(payload)
