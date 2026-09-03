"""Operational metrics and correlation ids.

Two properties are worth more than the rest of this file put together, because
both are ways for monitoring to be confidently wrong rather than absent.

**A metric must agree with the thing it counts.** So the instrumentation tests
below drive the real object — the real kill switch, the real risk engine, the
real router — and read the counter afterwards, rather than calling the recording
function directly. A test that called `metrics.halt_engaged()` and then asserted
the counter moved would pass forever after somebody deleted the call from
`engage`.

**A correlation id must not be able to forge a log line.** `sanitise_correlation_id`
is the only thing between an inbound header and every log line a request writes,
so its refusals are tested by what they would produce, not by the regex.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from atp_core import metrics
from atp_core.alerts import LoggingAlertSink, NtfyAlertSink, Severity
from atp_core.logging import (
    correlation_id,
    current_correlation_id,
    new_correlation_id,
    sanitise_correlation_id,
)
from atp_core.metrics import get_registry
from atp_core.risk.killswitch import HaltReason, HaltScope, RedisKillSwitch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    """Every test starts from zero.

    Counters cannot be decremented, so isolation means a new registry rather
    than a reset. Autouse because a test that forgot it would not fail — it
    would pass on its own and fail in whatever order CI happened to pick.
    """
    metrics.reset_for_tests()


@pytest.fixture
def configured_logging(
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """Configure real structlog JSON logging; return a reader for what it wrote.

    The configuration is process-global and is restored afterwards, so this
    cannot change how the rest of the suite logs — which would be an
    order-dependent failure in somebody else's file.
    """
    saved = structlog.get_config()
    from atp_core.logging import configure

    configure(level="INFO", fmt="json")

    def written() -> list[dict[str, Any]]:
        return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    try:
        yield written
    finally:
        structlog.configure(**saved)


def value(name: str, **labels: str) -> float:
    """One sample from the live registry, or 0.0 if the series does not exist.

    Zero rather than None for an absent series, because that is what a
    Prometheus query would return for it and it keeps the assertions readable.
    """
    sample = get_registry().get_sample_value(name, labels or None)
    return 0.0 if sample is None else sample


class FakeRedis:
    """Just enough Redis for the kill switch. Mirrors `test_kill_switch`."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store.get(k) for k in keys]

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    def scan_iter(self, match: str) -> list[str]:
        return [k for k in self.store if k.startswith(match.rstrip("*"))]

    def publish(self, channel: str, message: str) -> int:
        return 0


class TestTheRegistryItself:
    def test_render_produces_prometheus_text(self) -> None:
        metrics.halt_engaged(HaltScope.GLOBAL, HaltReason.DATA_FEED_LOST)
        body = metrics.render().decode()

        assert "# TYPE atp_halts_engaged_total counter" in body
        assert 'atp_halts_engaged_total{reason="data_feed_lost",scope="global"} 1.0' in body

    def test_render_appends_extra_registries(self) -> None:
        """The API's scrape-time halt state arrives this way."""
        from prometheus_client import CollectorRegistry, Gauge

        extra = CollectorRegistry()
        Gauge("atp_test_extra", "help", registry=extra).set(7)

        body = metrics.render(extra).decode()

        assert "atp_test_extra 7.0" in body
        assert "atp_halts_engaged_total" in body, "the core registry was dropped"

    def test_reset_discards_recorded_values(self) -> None:
        metrics.halt_engaged(HaltScope.GLOBAL, HaltReason.MANUAL)
        assert value("atp_halts_engaged_total", scope="global", reason="manual") == 1

        metrics.reset_for_tests()

        assert value("atp_halts_engaged_total", scope="global", reason="manual") == 0

    def test_build_info_carries_its_payload_in_labels(self) -> None:
        metrics.build_info("0.1.0", "paper")

        assert value("atp_build_info", version="0.1.0", run_mode="paper") == 1

    def test_risk_denial_records_the_rule_and_an_approval_does_not(self) -> None:
        metrics.risk_checked("denied", rule="max_position_size")
        metrics.risk_checked("approved")

        assert value("atp_risk_checks_total", outcome="denied") == 1
        assert value("atp_risk_checks_total", outcome="approved") == 1
        assert value("atp_risk_denials_total", rule="max_position_size") == 1


class TestTheKillSwitchIsCounted:
    """The counter and the phone notification must agree, since both hang off
    `engage` for the same reason (ADR 0012)."""

    def test_engaging_counts_the_halt(self) -> None:
        switch = RedisKillSwitch(FakeRedis())  # type: ignore[arg-type]

        switch.engage(HaltScope.GLOBAL, HaltReason.DATA_FEED_LOST, engaged_by="monitor")

        assert value("atp_halts_engaged_total", scope="global", reason="data_feed_lost") == 1

    def test_re_engaging_an_active_halt_counts_once(self) -> None:
        """The property that matters, and the one a naive counter gets wrong.

        `StalenessMonitor` re-engages every five seconds for the length of an
        outage. A counter incremented on entry to `engage` would turn a
        twenty-minute feed loss into 240 halts on the graph and make the rate
        meaningless — the same mistake that would have sent 240 notifications.
        The Redis state is the deduplication for both.
        """
        switch = RedisKillSwitch(FakeRedis())  # type: ignore[arg-type]

        for _ in range(12):
            switch.engage(HaltScope.GLOBAL, HaltReason.DATA_FEED_LOST, engaged_by="monitor")

        assert value("atp_halts_engaged_total", scope="global", reason="data_feed_lost") == 1

    def test_clearing_counts_only_when_something_was_engaged(self) -> None:
        switch = RedisKillSwitch(FakeRedis())  # type: ignore[arg-type]

        switch.clear(HaltScope.GLOBAL, cleared_by="jo")
        assert value("atp_halts_cleared_total", scope="global") == 0, (
            "clearing a halt that was not engaged is not an event"
        )

        switch.engage(HaltScope.GLOBAL, HaltReason.MANUAL, engaged_by="jo")
        switch.clear(HaltScope.GLOBAL, cleared_by="jo")
        assert value("atp_halts_cleared_total", scope="global") == 1


class TestTheRiskEngineIsCounted:
    def test_a_denial_records_the_rule_that_refused(self) -> None:
        from atp_core.risk.engine import RiskDecision, RiskEngine
        from atp_core.risk.limits import RiskLimits

        class AlwaysDenies:
            @property
            def name(self) -> str:
                return "always_denies"

            def check(self, order: Any, portfolio: Any, limits: Any) -> RiskDecision:
                return RiskDecision.deny("always_denies", "no")

        engine = RiskEngine(RiskLimits(), rules=[AlwaysDenies()])
        engine.validate(object(), object())  # type: ignore[arg-type]

        assert value("atp_risk_checks_total", outcome="denied") == 1
        assert value("atp_risk_denials_total", rule="always_denies") == 1

    def test_an_approval_is_counted_as_approved(self) -> None:
        from atp_core.risk.engine import RiskEngine
        from atp_core.risk.limits import RiskLimits

        RiskEngine(RiskLimits(), rules=[]).validate(object(), object())  # type: ignore[arg-type]

        assert value("atp_risk_checks_total", outcome="approved") == 1

    def test_a_shrink_is_counted_apart_from_an_approval(self) -> None:
        """A rule that cut the order in half approved it and also changed it,
        and an operator watching sizes shrink wants to see that separately."""
        from atp_core.domain import Order, OrderType, Side
        from atp_core.risk.engine import RiskDecision, RiskEngine
        from atp_core.risk.limits import RiskLimits

        class Shrinks:
            @property
            def name(self) -> str:
                return "shrinks"

            def check(self, order: Any, portfolio: Any, limits: Any) -> RiskDecision:
                return RiskDecision.shrink("shrinks", "too big", Decimal("5"))

        order = Order(
            symbol="SPY",
            side=Side.BUY,
            qty=Decimal("10"),
            order_type=OrderType.MARKET,
            strategy_id="s",
        )
        RiskEngine(RiskLimits(), rules=[Shrinks()]).validate(order, object())  # type: ignore[arg-type]

        assert value("atp_risk_checks_total", outcome="shrunk") == 1
        assert value("atp_risk_checks_total", outcome="approved") == 0


class TestTheAlertSinksAreCounted:
    """ "Did the halt notification go out" is not answerable from the halt
    counters — a halt is counted whether or not anybody was told."""

    def test_a_delivered_alert_is_counted_against_its_transport(self) -> None:
        import httpx

        from atp_core.alerts import Alert

        sink = NtfyAlertSink(
            "https://ntfy.example",
            "topic",
            client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        )
        sink.send(
            Alert(severity=Severity.CRITICAL, title="t", body="b", key="halt.global.all.manual")
        )

        assert value("atp_alerts_sent_total", transport="ntfy", severity="critical") == 1
        assert value("atp_alerts_failed_total", transport="ntfy") == 0

    def test_a_refused_alert_is_counted_as_failed(self) -> None:
        import httpx

        from atp_core.alerts import Alert

        sink = NtfyAlertSink(
            "https://ntfy.example",
            "topic",
            client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))),
        )
        sink.send(
            Alert(severity=Severity.CRITICAL, title="t", body="b", key="halt.global.all.manual")
        )

        assert value("atp_alerts_failed_total", transport="ntfy") == 1
        assert value("atp_alerts_sent_total", transport="ntfy", severity="critical") == 0

    def test_the_logging_sink_counts_as_a_failure_to_reach_anybody(self) -> None:
        """An unconfigured platform reads as zero-sent-zero-failed otherwise,
        which is indistinguishable from a platform with nothing to report."""
        from atp_core.alerts import Alert

        LoggingAlertSink().send(
            Alert(severity=Severity.CRITICAL, title="t", body="b", key="halt.global.all.manual")
        )

        assert value("atp_alerts_failed_total", transport="logging") == 1


class TestSanitisingAnInboundCorrelationId:
    def test_a_plain_id_is_kept(self) -> None:
        assert sanitise_correlation_id("abc-123_XY.z:9") == "abc-123_XY.z:9"

    @pytest.mark.parametrize(
        "hostile",
        [
            # The one that matters: under the console renderer this would end
            # the line and write a second one, inventing a log entry.
            "id\n2026-01-01 [critical] risk.killswitch.engaged",
            "id\r\nfake",
            "id with spaces",
            "id\ttab",
            "\x00nul",
            "\x1b[31mansi",
            "x" * 65,
            "",
            None,
        ],
    )
    def test_anything_that_could_forge_a_line_is_replaced(self, hostile: str | None) -> None:
        result = sanitise_correlation_id(hostile)

        assert result != hostile
        # Replaced, never dropped: a request with no id is one nobody can trace.
        assert result
        assert sanitise_correlation_id(result) == result, "the replacement is itself safe"

    def test_a_replacement_never_contains_a_line_break(self) -> None:
        for _ in range(200):
            assert "\n" not in new_correlation_id()
            assert "\r" not in new_correlation_id()


class TestBindingACorrelationId:
    def test_the_id_lands_on_every_event_underneath(
        self, configured_logging: Callable[[], list[dict[str, Any]]]
    ) -> None:
        """Driven through the real configured chain, not `capture_logs`.

        `capture_logs` replaces the processor list with its own single
        processor, so `merge_contextvars` never runs under it and the id would
        be absent whether or not the chain has it — a test that could only fail
        for the wrong reason. This one calls `atp_core.logging.configure` and
        reads what was actually written, which is the only way to notice
        somebody removing `merge_contextvars` from the chain.
        """
        from atp_core.logging import get_logger

        log = get_logger("test.correlated")
        with correlation_id("abc123"):
            log.info("something.happened")
        log.info("outside.the.block")

        first, second = configured_logging()
        assert first["event"] == "something.happened"
        assert first["correlation_id"] == "abc123"
        assert "correlation_id" not in second, "the id outlived its block"

    def test_nesting_restores_the_outer_id_rather_than_clearing_it(self) -> None:
        """A scheduled job inside a request must not leave the request
        unlabelled on its way out."""
        with correlation_id("outer"):
            with correlation_id("inner"):
                assert current_correlation_id() == "inner"
            assert current_correlation_id() == "outer"

    def test_nothing_is_bound_outside_a_block(self) -> None:
        with correlation_id("scoped"):
            pass

        assert current_correlation_id() is None

    def test_an_omitted_value_generates_one(self) -> None:
        with correlation_id() as bound:
            assert bound
            assert current_correlation_id() == bound


class TestTheStreamIsCounted:
    async def test_a_quote_moves_the_counter_and_the_last_tick_gauge(self) -> None:
        """The gauge is a timestamp rather than an age, so that an ingestor
        which stopped does not go on reporting a small, steady, healthy
        number."""
        from atp_core.domain import Quote

        ingestor = _ingestor()
        at = datetime(2026, 8, 19, 13, 30, tzinfo=UTC)

        await ingestor._handle_quote(Quote(symbol="SPY", bid=Decimal("1"), ask=Decimal("2"), ts=at))

        assert value("atp_stream_messages_total", kind="quote") == 1
        assert value("atp_stream_last_tick_timestamp_seconds", symbol="SPY") == at.timestamp()

    async def test_a_bar_is_counted_separately_from_a_quote(self) -> None:
        from atp_core.domain import Bar, Timeframe

        ingestor = _ingestor()
        await ingestor._handle_bar(
            Bar(
                symbol="SPY",
                ts=datetime(2026, 8, 19, 13, 30, tzinfo=UTC),
                open=Decimal("1"),
                high=Decimal("2"),
                low=Decimal("1"),
                close=Decimal("2"),
                volume=Decimal("10"),
                timeframe=Timeframe.M1,
            )
        )

        assert value("atp_stream_messages_total", kind="bar") == 1
        assert value("atp_stream_messages_total", kind="quote") == 0


def _ingestor() -> Any:
    """A `StreamIngestor` whose ports all do nothing.

    Built here rather than reusing `test_stream_ingestor`'s fixtures, because
    what is under test is the counting and it should not fail when that file's
    fakes are refactored.
    """
    from atp_core.data.stream import StreamIngestor

    class Nothing:
        async def set_quote(self, quote: Any) -> None: ...
        async def upsert_bars(self, bars: Any) -> None: ...
        async def publish(self, channel: str, message: Any) -> None: ...

    return StreamIngestor(
        feed=Nothing(),  # type: ignore[arg-type]
        quote_cache=Nothing(),  # type: ignore[arg-type]
        bar_repo=Nothing(),  # type: ignore[arg-type]
        provider=Nothing(),  # type: ignore[arg-type]
        publisher=Nothing(),
    )
