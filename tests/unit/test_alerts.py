"""Alert sinks.

The property under test throughout is the one in `alerts.ports`: **a sink must
not raise**. Everything that alerts here does so immediately after halting
trading, and a push service having a bad day must not turn a successful halt
into an exception on the way out of `engage`.

The HTTP tests drive a real `httpx.Client` over `MockTransport` rather than a
hand-written double, because what is being checked is the request that would go
on the wire — the URL, the headers ntfy reads, the body — and a double that
agreed with us about those would prove nothing.
"""

from __future__ import annotations

import httpx
import pytest

from atp_core.alerts import (
    Alert,
    LoggingAlertSink,
    NtfyAlertSink,
    Severity,
    build_alert_sink,
)
from atp_core.config import Settings


def _alert(severity: Severity = Severity.CRITICAL) -> Alert:
    return Alert(
        severity=severity,
        title="Trading halted: data_feed_lost",
        body="global halted by staleness-monitor.",
        key="halt.global.all.data_feed_lost",
        context={"reason": "data_feed_lost"},
    )


def _client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


class TestLoggingAlertSink:
    def test_does_not_raise(self) -> None:
        LoggingAlertSink().send(_alert())

    def test_handles_every_severity(self) -> None:
        """It maps severity onto a log level by lookup; a missing one would be a
        KeyError at exactly the moment something is already going wrong."""
        for severity in Severity:
            LoggingAlertSink().send(_alert(severity))


class TestNtfyAlertSink:
    def test_posts_the_alert_to_the_topic(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        NtfyAlertSink("https://ntfy.sh", "atp-abc123", client=_client(handler)).send(_alert())

        assert len(seen) == 1
        assert str(seen[0].url) == "https://ntfy.sh/atp-abc123"
        assert seen[0].headers["Title"] == "Trading halted: data_feed_lost"
        assert seen[0].headers["Priority"] == "5"  # urgent — bypasses a silent phone
        assert seen[0].content == b"global halted by staleness-monitor."

    def test_a_trailing_slash_on_the_base_url_does_not_double_up(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        NtfyAlertSink("https://ntfy.example.com/", "t", client=_client(handler)).send(_alert())
        assert str(seen[0].url) == "https://ntfy.example.com/t"

    def test_severity_sets_the_priority(self) -> None:
        """INFO must not arrive at the same urgency as a halt, or the urgency
        stops meaning anything and the halt gets swiped away with the rest."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["Priority"])
            return httpx.Response(200)

        sink = NtfyAlertSink("https://ntfy.sh", "t", client=_client(handler))
        for severity in (Severity.INFO, Severity.WARNING, Severity.CRITICAL):
            sink.send(_alert(severity))
        assert seen == ["3", "4", "5"]

    def test_a_token_is_sent_as_a_bearer(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        NtfyAlertSink("https://x", "t", token="tk_1", client=_client(handler)).send(_alert())
        assert seen[0].headers["Authorization"] == "Bearer tk_1"

    def test_no_token_means_no_auth_header(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        NtfyAlertSink("https://x", "t", client=_client(handler)).send(_alert())
        assert "Authorization" not in seen[0].headers

    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
    def test_an_error_response_does_not_raise(self, status: int) -> None:
        """The halt already happened. Whatever ntfy thinks of us cannot be
        allowed to propagate out of the call that stopped trading."""
        sink = NtfyAlertSink(
            "https://x", "t", client=_client(lambda request: httpx.Response(status))
        )
        sink.send(_alert())

    def test_a_transport_failure_does_not_raise(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        NtfyAlertSink("https://x", "t", client=_client(handler)).send(_alert())

    def test_a_failure_never_logs_the_topic(self, caplog: pytest.LogCaptureFixture) -> None:
        """The topic is the capability: on a public server it is all that stands
        between the alerts and anyone who has it. A failure path that helpfully
        printed the URL would put it in every log aggregator we own."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        with caplog.at_level("ERROR"):
            NtfyAlertSink("https://x", "s3cret-topic", client=_client(handler)).send(_alert())

        assert "s3cret-topic" not in caplog.text

    def test_an_injected_client_is_left_open(self) -> None:
        """It belongs to the caller. Closing it here would work exactly once and
        then break every alert after the first."""
        client = _client(lambda request: httpx.Response(200))
        sink = NtfyAlertSink("https://x", "t", client=client)
        sink.send(_alert())
        sink.send(_alert())
        assert not client.is_closed

    def test_refuses_an_empty_topic(self) -> None:
        """Posting to a base URL with no topic is a request to nowhere that
        succeeds. Fail at construction instead."""
        with pytest.raises(ValueError, match="topic"):
            NtfyAlertSink("https://ntfy.sh", "")


class TestBuildAlertSink:
    def test_unconfigured_gives_the_logging_sink(self) -> None:
        """Alerting is opt-in. A platform that would not start without a push
        service configured has made a notification into a dependency."""
        assert isinstance(build_alert_sink(Settings()), LoggingAlertSink)

    def test_a_topic_gives_the_ntfy_sink(self) -> None:
        settings = Settings(ALERT_NTFY_TOPIC="atp-abc123")  # type: ignore[call-arg]
        assert isinstance(build_alert_sink(settings), NtfyAlertSink)
