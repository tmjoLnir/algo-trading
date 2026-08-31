"""Every metric this platform exports, declared in one file.

Two rules hold this module together, and both are about a failure mode that
metrics are unusually good at producing: numbers that look authoritative and are
quietly wrong.

**One declaration site.** A metric created next to the code that increments it
cannot be inventoried. Nobody can answer "what does this platform export" without
grepping, a rename lands as a silent gap in a dashboard, and two subsystems
counting the same event under two names is not a test failure. Everything is
declared here, so the answer to that question is `cat registry.py`.

**Callers get functions, never the instruments.** `halt_engaged(scope, reason)`
rather than an exported `Counter` with a `.labels()` call at each site. A
mistyped label value is not an error in `prometheus_client` — it is a brand new
time series that reads as zero forever, and the graph it belongs on is the one
somebody is staring at during an incident. Behind a typed function, `mypy
--strict` refuses it at the call site instead. That is the same argument
`channels.py` makes for naming Redis channels in one place, and ADR 0006's for
one definition with several callers.

Metrics are module-global mutable state, which is unlike the rest of `core` and
is deliberate: they are the same shape of concern as `atp_core.logging`, which
is also a module-global every subsystem reaches for without being handed one.
Threading a registry through every constructor in the platform would be the
alternative, and it would buy nothing — there is one process-wide answer to "how
many orders has this worker submitted".

Nothing here does I/O. `prometheus_client`'s registry is pure in-process state;
the parts of that package which open a socket are never imported (CLAUDE.md
§1.3). Serving the text is `apps/api` and `apps/worker`'s job.

Naming follows Prometheus convention rather than this codebase's: `atp_` prefix,
base units in the name, counters ending `_total`. The convention is not ours to
choose — it is what every query, dashboard and alerting rule written against
these assumes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

if TYPE_CHECKING:
    from atp_core.alerts.ports import Severity
    from atp_core.risk.killswitch import HaltReason, HaltScope

#: Buckets for broker round-trip latency, in seconds. Not the client default,
#: which tops out at 10s and spends half its resolution below 100ms. A broker
#: submit that takes 25ms and one that takes 90ms are the same event to an
#: operator; 2s and 9s are not, and the interesting question at the top end is
#: "how close are we to the timeout", so the tail is where the buckets are.
_BROKER_BUCKETS: Final = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)

#: Buckets for handling one HTTP request. Since ADR 0022 the dashboard reads only
#: when asked, so a human is waiting on the other end of every one of these —
#: anything past a couple of seconds is a single "too slow" bucket rather than a
#: distribution.
_API_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class _Instruments:
    """Every instrument, bound to one registry.

    A class rather than module-level objects so `reset_for_tests` can build a
    second set against a fresh registry. `prometheus_client` has no way to reset
    a counter — by design, since a counter that can go down is not a counter —
    and a test suite that shares one registry across tests gets order-dependent
    assertions.
    """

    def __init__(self, registry: CollectorRegistry) -> None:
        self.build_info = Gauge(
            "atp_build_info",
            "Always 1. The labels are the payload: which version is running, and in which mode.",
            ["version", "run_mode"],
            registry=registry,
        )

        # ─── risk and the kill switch ────────────────────────────────────
        # `reason` is `HaltReason`, seven values, closed. `scope` is three.
        # Both are enums rather than free text, which is what makes them safe
        # to use as labels at all.
        self.halts_engaged = Counter(
            "atp_halts_engaged_total",
            "Halts engaged, by scope and reason. A rising rate is an incident.",
            ["scope", "reason"],
            registry=registry,
        )
        self.halts_cleared = Counter(
            "atp_halts_cleared_total",
            "Halts cleared by a human. Every one of these is somebody's decision.",
            ["scope"],
            registry=registry,
        )
        # Deliberately no `atp_halts_active` gauge here. Whether trading is
        # halted lives in Redis and is read by several processes; a copy
        # maintained by whichever process happened to call `engage` would drift
        # the moment another one did. `apps/api` exports that gauge from an
        # authoritative read at scrape time — see its `routers/metrics.py`.
        self.risk_checks = Counter(
            "atp_risk_checks_total",
            "Orders through RiskEngine.validate, by what it decided.",
            ["outcome"],
            registry=registry,
        )
        self.risk_denials = Counter(
            "atp_risk_denials_total",
            "Denials by the rule that refused. Bounded by the rule chain.",
            ["rule"],
            registry=registry,
        )

        # ─── order flow ──────────────────────────────────────────────────
        self.orders_submitted = Counter(
            "atp_orders_submitted_total",
            "Orders the venue acknowledged as working.",
            ["side", "order_type"],
            registry=registry,
        )
        self.orders_rejected = Counter(
            "atp_orders_rejected_total",
            "Orders that did not reach a venue, by where in the path they stopped.",
            ["stage"],
            registry=registry,
        )
        self.order_submit_seconds = Histogram(
            "atp_order_submit_seconds",
            "Time in BrokerPort.submit_order, whatever its outcome.",
            ["broker"],
            buckets=_BROKER_BUCKETS,
            registry=registry,
        )

        # ─── market data ─────────────────────────────────────────────────
        self.stream_messages = Counter(
            "atp_stream_messages_total",
            "Messages taken off the realtime feed, by kind.",
            ["kind"],
            registry=registry,
        )
        self.stream_reconnects = Counter(
            "atp_stream_reconnects_total",
            "Feed reconnects. Each one is a gap that had to be backfilled.",
            registry=registry,
        )
        self.stream_gap_bars = Counter(
            "atp_stream_gap_bars_backfilled_total",
            "Bars fetched over REST to close a reconnect gap.",
            registry=registry,
        )
        # A timestamp, not an age. An age gauge is only correct at the moment it
        # is set, so a process that stops updating it reports a constant, small,
        # reassuring number forever — which is precisely the outage it exists to
        # show. Prometheus subtracts this from `time()` at query time, so a
        # frozen exporter produces a rising age on its own.
        self.stream_last_tick = Gauge(
            "atp_stream_last_tick_timestamp_seconds",
            "When each symbol last printed, as a unix timestamp.",
            ["symbol"],
            registry=registry,
        )

        # ─── the strategy loop ───────────────────────────────────────────
        # `main.py`'s own worry, made observable: "a runner erroring every tick
        # is not trading, but it looks alive to a health check". A liveness
        # probe cannot tell those apart and this pair can — a flat `succeeded`
        # rate is a loop that has stopped working while the process stays up.
        self.strategy_evaluations = Counter(
            "atp_strategy_evaluations_total",
            "Passes of the strategy loop, by how each one ended.",
            ["strategy", "outcome"],
            registry=registry,
        )
        self.strategy_evaluation_seconds = Histogram(
            "atp_strategy_evaluation_seconds",
            "Time for one pass of the strategy loop.",
            ["strategy"],
            buckets=_API_BUCKETS,
            registry=registry,
        )

        # ─── alerting ────────────────────────────────────────────────────
        # "Did the halt notification actually go out" is the question this pair
        # exists for, and it is not answerable from the halt counters: a halt is
        # counted whether or not anybody was told about it.
        self.alerts_sent = Counter(
            "atp_alerts_sent_total",
            "Alerts a transport accepted.",
            ["transport", "severity"],
            registry=registry,
        )
        self.alerts_failed = Counter(
            "atp_alerts_failed_total",
            "Alerts that did not go out. The event they describe still happened.",
            ["transport"],
            registry=registry,
        )

        # ─── the API ─────────────────────────────────────────────────────
        # `route` is the *template* — /api/v1/positions/{symbol} — never the
        # concrete path. Labelling by concrete path gives one time series per
        # symbol ever requested, which is unbounded and is the standard way
        # people take down their own monitoring.
        self.api_requests = Counter(
            "atp_api_requests_total",
            "HTTP requests served, by route template and status.",
            ["method", "route", "status"],
            registry=registry,
        )
        self.api_request_seconds = Histogram(
            "atp_api_request_seconds",
            "Time to serve one request, by route template.",
            ["method", "route"],
            buckets=_API_BUCKETS,
            registry=registry,
        )


_registry = CollectorRegistry()
_m = _Instruments(_registry)


def get_registry() -> CollectorRegistry:
    """The registry everything in this process records into.

    A function rather than an exported object because `reset_for_tests` rebinds
    it, and a module that captured the object at import time would go on
    rendering the registry the suite has already thrown away.
    """
    return _registry


def reset_for_tests() -> None:
    """Throw away every recorded value. Tests only.

    Counters cannot be decremented, so isolating one test from the next means
    replacing the registry rather than zeroing it. Named for what it is so that
    nobody reaches for it in application code and quietly resets a production
    counter mid-session — every graph would show a drop nothing explains.
    """
    global _registry, _m
    _registry = CollectorRegistry()
    _m = _Instruments(_registry)


def build_info(version: str, run_mode: str) -> None:
    """Record which build is running. Called once, at startup."""
    _m.build_info.labels(version=version, run_mode=run_mode).set(1)


def halt_engaged(scope: HaltScope, reason: HaltReason) -> None:
    """A halt took effect. Recorded from `KillSwitch.engage` and nowhere else.

    Only a halt that actually changed the state is counted — re-engaging an
    active halt returns early and is not a second incident, which is the same
    reasoning that stops it sending a second phone notification (ADR 0012).
    """
    _m.halts_engaged.labels(scope=scope.value, reason=reason.value).inc()


def halt_cleared(scope: HaltScope) -> None:
    """Trading resumed. Somebody decided that; there is no automatic clear."""
    _m.halts_cleared.labels(scope=scope.value).inc()


def risk_checked(outcome: str, rule: str = "") -> None:
    """One trip through the risk engine.

    `outcome` is `approved`, `shrunk` or `denied`; `rule` names the rule that
    denied and is empty otherwise. Two counters rather than one with a `rule`
    label on every outcome, because "how many orders were approved" should not
    require summing across every rule that did not object to them.
    """
    _m.risk_checks.labels(outcome=outcome).inc()
    if rule:
        _m.risk_denials.labels(rule=rule).inc()


def order_submitted(side: str, order_type: str) -> None:
    """The venue has this order working. Not "the call returned"."""
    _m.orders_submitted.labels(side=side, order_type=order_type).inc()


def order_rejected(stage: str) -> None:
    """An order stopped short of working, at `stage`.

    The stages are the places the single submit path can end: `risk` (a rule
    refused it), `broker` (the venue refused it), `acknowledgement` (the venue
    answered and its answer was no) and `indeterminate` (the connection failed
    and we do not know). The last one is the one worth alerting on.
    """
    _m.orders_rejected.labels(stage=stage).inc()


def order_submit_seconds(broker: str, seconds: float) -> None:
    """How long the venue took, whatever it said."""
    _m.order_submit_seconds.labels(broker=broker).observe(seconds)


def stream_message(kind: str) -> None:
    """One quote or one bar off the feed."""
    _m.stream_messages.labels(kind=kind).inc()


def stream_reconnected() -> None:
    """The feed dropped and came back. The gap still has to be closed."""
    _m.stream_reconnects.inc()


def stream_gap_bars(count: int) -> None:
    """Bars pulled over REST to close a reconnect gap."""
    _m.stream_gap_bars.inc(count)


def stream_last_tick(symbol: str, epoch_seconds: float) -> None:
    """When `symbol` last printed. Bounded by the watchlist, which is small."""
    _m.stream_last_tick.labels(symbol=symbol).set(epoch_seconds)


def strategy_evaluated(strategy: str, outcome: str, seconds: float) -> None:
    """One pass of the strategy loop. `outcome` is `succeeded` or `failed`.

    `strategy` is a label because a platform running two of them needs to know
    which one stopped, and the strategy set is configuration rather than input —
    `WORKER_STRATEGY` names one. It is not the symbol, which would multiply this
    by the watchlist for no question anybody asks.
    """
    _m.strategy_evaluations.labels(strategy=strategy, outcome=outcome).inc()
    _m.strategy_evaluation_seconds.labels(strategy=strategy).observe(seconds)


def alert_sent(transport: str, severity: Severity) -> None:
    """A transport accepted the alert. Not proof a phone rang."""
    _m.alerts_sent.labels(transport=transport, severity=severity.value).inc()


def alert_failed(transport: str) -> None:
    """The alert did not go out. Whatever it described still happened."""
    _m.alerts_failed.labels(transport=transport).inc()


def api_request(method: str, route: str, status: int, seconds: float) -> None:
    """One served HTTP request. `route` must be the template, not the path."""
    _m.api_requests.labels(method=method, route=route, status=str(status)).inc()
    _m.api_request_seconds.labels(method=method, route=route).observe(seconds)
