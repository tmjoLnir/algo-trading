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

import json

import httpx
import pytest
from structlog.testing import capture_logs

from atp_core.alerts import (
    Alert,
    FanOutAlertSink,
    LoggingAlertSink,
    NtfyAlertSink,
    Severity,
    TelegramAlertSink,
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

    def test_a_failure_never_logs_the_topic(self) -> None:
        """The topic is the capability: on a public server it is all that stands
        between the alerts and anyone who has it. A failure path that helpfully
        printed the URL would put it in every log aggregator we own.

        Asserted over the captured event *dicts* rather than rendered text —
        `caplog` sees nothing here, because structlog does not route through the
        stdlib logger this configuration, so a `not in caplog.text` assertion
        would pass against an empty string and prove nothing.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        with capture_logs() as events:
            NtfyAlertSink("https://x", "s3cret-topic", client=_client(handler)).send(_alert())

        assert any(e["event"] == "alert.send_failed" for e in events)
        assert "s3cret-topic" not in str(events)

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

    def test_telegram_alone_gives_the_telegram_sink(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            ALERT_TELEGRAM_TOKEN="123:AAF", ALERT_TELEGRAM_CHAT_ID="9876"
        )
        assert isinstance(build_alert_sink(settings), TelegramAlertSink)

    @pytest.mark.parametrize(
        "kwargs", [{"ALERT_TELEGRAM_TOKEN": "123:AAF"}, {"ALERT_TELEGRAM_CHAT_ID": "9876"}]
    )
    def test_half_a_telegram_configuration_is_ignored_rather_than_fatal(
        self, kwargs: dict[str, str]
    ) -> None:
        """A token with no chat id cannot deliver anything. Refusing to start
        over it would make a notification a dependency of trading; the logging
        sink says on every alert that nothing is configured."""
        assert isinstance(build_alert_sink(Settings(**kwargs)), LoggingAlertSink)  # type: ignore[arg-type]

    def test_both_configured_sends_to_both(self) -> None:
        """Two configured transports is a request for two, and the usual reason
        to want two is that neither service is owed that much trust. Picking a
        winner would be a surprise discovered during an incident."""
        settings = Settings(  # type: ignore[call-arg]
            ALERT_NTFY_TOPIC="atp-abc123",
            ALERT_TELEGRAM_TOKEN="123:AAF",
            ALERT_TELEGRAM_CHAT_ID="9876",
        )
        sink = build_alert_sink(settings)
        assert isinstance(sink, FanOutAlertSink)


class TestTelegramAlertSink:
    def test_posts_the_message_to_the_chat(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        TelegramAlertSink("123:AAF", "9876", client=_client(handler)).send(_alert())

        assert len(seen) == 1
        assert str(seen[0].url) == "https://api.telegram.org/bot123:AAF/sendMessage"
        payload = json.loads(seen[0].content)
        assert payload["chat_id"] == "9876"
        assert "Trading halted: data_feed_lost" in payload["text"]
        assert "global halted by staleness-monitor." in payload["text"]

    def test_info_is_delivered_silently_and_critical_is_not(self) -> None:
        """An all-clear at 02:00 belongs in the history, not on a ringing phone.
        Telegram has no priority scale, so this is the only lever there is."""
        seen: list[bool] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["disable_notification"])
            return httpx.Response(200, json={"ok": True})

        sink = TelegramAlertSink("t", "c", client=_client(handler))
        sink.send(_alert(Severity.INFO))
        sink.send(_alert(Severity.CRITICAL))
        assert seen == [True, False]

    def test_ok_false_inside_a_200_is_a_failure(self) -> None:
        """The failure this class exists to catch. Telegram reports a revoked
        token, a deleted chat and a malformed id in the JSON body of a perfectly
        successful HTTP response — so a sink that trusted the status code would
        report a bot deleted months ago as delivering every halt.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "description": "chat not found"})

        with capture_logs() as events:
            TelegramAlertSink("t", "c", client=_client(handler)).send(_alert())

        failures = [e for e in events if e["event"] == "alert.send_failed"]
        assert len(failures) == 1
        assert failures[0]["error"] == "chat not found"

    def test_a_successful_send_is_not_logged_as_a_failure(self) -> None:
        with capture_logs() as events:
            TelegramAlertSink(
                "t", "c", client=_client(lambda r: httpx.Response(200, json={"ok": True}))
            ).send(_alert())
        assert [e["event"] for e in events] == ["alert.sent"]

    @pytest.mark.parametrize("status", [401, 403, 429, 500])
    def test_an_error_response_does_not_raise(self, status: int) -> None:
        TelegramAlertSink(
            "t", "c", client=_client(lambda r: httpx.Response(status, json={"ok": False}))
        ).send(_alert())

    def test_a_transport_failure_does_not_raise(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        TelegramAlertSink("t", "c", client=_client(handler)).send(_alert())

    def test_a_body_that_is_not_json_does_not_raise(self) -> None:
        """A proxy or a captive portal answers 200 with HTML. `.json()` raises,
        and this runs immediately after a halt."""
        TelegramAlertSink(
            "t", "c", client=_client(lambda r: httpx.Response(200, text="<html>hi</html>"))
        ).send(_alert())

    @pytest.mark.parametrize("failure", ["transport", "ok_false"])
    def test_a_failure_never_logs_the_token(self, failure: str) -> None:
        """The token is in the URL path, so it *is* the bot: anyone holding it
        reads the chat and posts as you. Neither failure path may print it."""
        secret = "77777:AA-super-secret-bot-token"

        def handler(request: httpx.Request) -> httpx.Response:
            if failure == "transport":
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"ok": False, "description": "unauthorized"})

        with capture_logs() as events:
            TelegramAlertSink(secret, "chat-9876", client=_client(handler)).send(_alert())

        assert any(e["event"] == "alert.send_failed" for e in events)
        assert "super-secret-bot-token" not in str(events)
        assert "chat-9876" not in str(events)

    @pytest.mark.parametrize(("token", "chat"), [("", "c"), ("t", ""), ("", "")])
    def test_refuses_half_a_configuration(self, token: str, chat: str) -> None:
        """Half-configured is the state that would post to a URL that is not the
        API, or address nobody. Fail at construction."""
        with pytest.raises(ValueError, match="token and a chat id"):
            TelegramAlertSink(token, chat)

    def test_an_injected_client_is_left_open(self) -> None:
        client = _client(lambda r: httpx.Response(200, json={"ok": True}))
        sink = TelegramAlertSink("t", "c", client=client)
        sink.send(_alert())
        sink.send(_alert())
        assert not client.is_closed


class TestFanOutAlertSink:
    def test_every_sink_gets_the_alert(self) -> None:
        first, second = _Recorder(), _Recorder()
        FanOutAlertSink([first, second]).send(_alert())
        assert len(first.sent) == 1
        assert len(second.sent) == 1

    def test_one_sink_raising_does_not_stop_the_others(self) -> None:
        """The whole reason to configure two transports is that one being down
        should not be the same as having no alerting. A fan-out that gave up on
        the first exception would defeat the point of itself."""

        class Exploding:
            def send(self, alert: Alert) -> None:
                raise RuntimeError("down")

        survivor = _Recorder()
        FanOutAlertSink([Exploding(), survivor]).send(_alert())
        assert len(survivor.sent) == 1

    def test_refuses_an_empty_fan_out(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            FanOutAlertSink([])


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)
