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

import io
import json
import logging
from typing import TYPE_CHECKING

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
from atp_core.logging import configure

if TYPE_CHECKING:
    from collections.abc import Callable


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


# The alert credentials a host may have configured are removed from the whole
# test session by `tests/conftest.py`, so a bare `Settings()` below means the
# documented defaults rather than whatever this machine is running.


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

    @pytest.mark.parametrize("failure", ["transport", 401, 403, 429, 500])
    def test_a_failure_never_logs_the_topic(self, failure: object) -> None:
        """The topic is the capability: on a public server it is all that stands
        between the alerts and anyone who has it. A failure path that helpfully
        printed the URL would put it in every log aggregator we own.

        **The status codes are the point of the parametrisation.** This test
        used to cover the transport failure alone, whose message is the socket's
        and carries no URL — so it passed while `str(HTTPStatusError)`, which
        quotes the request URL in full, printed the topic on every 4xx. A wrong
        or revoked credential returns exactly a 4xx, so the one uncovered case
        was the one that happens in practice.

        Asserted over the captured event *dicts* rather than rendered text —
        `caplog` sees nothing here, because structlog does not route through the
        stdlib logger in this configuration, so a `not in caplog.text` assertion
        would pass against an empty string and prove nothing.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if failure == "transport":
                raise httpx.ConnectError("boom")
            return httpx.Response(int(failure))  # type: ignore[arg-type]

        with capture_logs() as events:
            NtfyAlertSink(
                "https://ntfy.sh", "s3cret-topic", token="s3cret-token", client=_client(handler)
            ).send(_alert())

        assert any(e["event"] == "alert.send_failed" for e in events)
        assert "s3cret-topic" not in str(events)
        assert "s3cret-token" not in str(events)

    def test_a_failure_still_says_what_went_wrong(self) -> None:
        """Scrubbing must not turn the log into silence.

        The failure path is what an operator reads when no alert arrived, and
        "something failed" would send them to the vendor's status page with
        nothing to check. The status code survives; only the credential does not.
        """
        with capture_logs() as events:
            NtfyAlertSink(
                "https://ntfy.sh",
                "s3cret-topic",
                client=_client(lambda r: httpx.Response(401)),
            ).send(_alert())

        assert events[0]["error"] == "HTTP 401 Unauthorized"

    def test_the_topic_is_scrubbed_whatever_the_transport_raises(self) -> None:
        """The guarantee, held independently of which exception type appears.

        The status-code branch is a judgement about one library's message
        format; this is the layer under it. An httpx that starts quoting URLs
        in transport errors, or a proxy echoing the request line back in one,
        must not reintroduce the leak — so the scrub runs over every message,
        and this pins that rather than the branch.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("failed to POST https://ntfy.sh/s3cret-topic")

        with capture_logs() as events:
            NtfyAlertSink("https://ntfy.sh", "s3cret-topic", client=_client(handler)).send(_alert())

        assert "s3cret-topic" not in str(events)
        assert "***" in events[0]["error"], "scrubbed, not dropped"

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
            # Realistic credentials rather than "t" and "c". The description is
            # scrubbed of them before it is logged, and a one-character token
            # matches inside ordinary English — `test_a_short_credential_is_
            # still_scrubbed` below pins that deliberately. Here it would only
            # obscure what this test is actually about.
            TelegramAlertSink("77777:AA-tok", "chat-9876", client=_client(handler)).send(_alert())

        failures = [e for e in events if e["event"] == "alert.send_failed"]
        assert len(failures) == 1
        assert failures[0]["error"] == "chat not found"

    def test_a_short_credential_is_still_scrubbed(self) -> None:
        """The trade-off, pinned so it stays a decision rather than a surprise.

        Scrubbing by value is unconditional, so a one-character credential
        matches inside ordinary words and mangles the message. That is
        deliberate and must not be "fixed" with a minimum-length guard: a short
        credential is a *guessable* one, so it is the case where leaking it
        matters most. A mangled log line is the cheaper failure.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "description": "chat not found"})

        with capture_logs() as events:
            TelegramAlertSink("t", "c", client=_client(handler)).send(_alert())

        assert events[0]["error"] == "***ha*** no*** found"

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

    @pytest.mark.parametrize("failure", ["transport", "ok_false", 401, 403, 404, 500])
    def test_a_failure_never_logs_the_token(self, failure: object) -> None:
        """The token is in the URL path, so it *is* the bot: anyone holding it
        reads the chat and posts as you. No failure path may print it.

        **The status codes are why this test was rewritten.** It covered the
        transport error and `ok: false` alone — the two whose messages carry no
        URL — and passed while `str(HTTPStatusError)` printed the whole
        `.../bot<TOKEN>/sendMessage` on any 4xx. A revoked token answers 401 and
        a wrong one 404, so the uncovered case was the likeliest one, on the
        path an operator reads precisely because no alert arrived.
        """
        secret = "77777:AA-super-secret-bot-token"

        def handler(request: httpx.Request) -> httpx.Response:
            if failure == "transport":
                raise httpx.ConnectError("boom")
            if failure == "ok_false":
                return httpx.Response(200, json={"ok": False, "description": "unauthorized"})
            return httpx.Response(int(failure))  # type: ignore[arg-type]

        with capture_logs() as events:
            TelegramAlertSink(secret, "chat-9876", client=_client(handler)).send(_alert())

        assert any(e["event"] == "alert.send_failed" for e in events)
        assert "super-secret-bot-token" not in str(events)
        assert "chat-9876" not in str(events)

    def test_a_failure_still_says_what_went_wrong(self) -> None:
        """401 is the answer a revoked token gives, and naming it is the whole
        diagnosis. Scrubbing the credential must not scrub the reason."""
        with capture_logs() as events:
            TelegramAlertSink(
                "77777:AA-super-secret-bot-token",
                "chat-9876",
                client=_client(lambda r: httpx.Response(401)),
            ).send(_alert())

        assert events[0]["error"] == "HTTP 401 Unauthorized"

    def test_a_description_quoting_the_credential_is_scrubbed(self) -> None:
        """Telegram's own words are echoed into the log on `ok: false`, and they
        are the one string in this path we do not author. A description that
        quoted the chat id back — which the API does for some errors — would
        publish it through a field nothing else checks."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"ok": False, "description": "chat not found: chat-9876"}
            )

        with capture_logs() as events:
            TelegramAlertSink("77777:AA-tok", "chat-9876", client=_client(handler)).send(_alert())

        assert "chat-9876" not in str(events)
        assert "chat not found" in events[0]["error"], "the diagnosis survives"

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


class TestTheCredentialNeverReachesTheStdlibLog:
    """The success path, which is where the credential actually leaked.

    Every other scrub test in this file asserts against `capture_logs`, which
    collects the events *this codebase* emits. That is the wrong instrument for
    this bug and is why it survived: `httpx` logs `HTTP Request: POST <url>` at
    INFO through the standard library, and for both transports here the URL is
    the credential. So these read the stdlib stream instead, and they fail
    against a `configure()` that does not silence the library.

    Driven through `MockTransport` — httpx logs the request either way, and the
    point is the log record, not the endpoint (CLAUDE.md §1.7).
    """

    @staticmethod
    def _stdlib_log_of(send: Callable[[], None]) -> str:
        """Whatever `send` causes to be written to the stdlib root logger."""
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.NOTSET)
        root = logging.getLogger()
        root.addHandler(handler)
        # NOTSET on the root would let a parent level suppress the record before
        # the handler sees it, which would make this test pass by not looking.
        previous = root.level
        root.setLevel(logging.DEBUG)
        try:
            send()
        finally:
            root.setLevel(previous)
            root.removeHandler(handler)
        return stream.getvalue()

    def test_httpx_does_not_log_the_telegram_token(self) -> None:
        """The bot token travels in the URL path, so httpx's request line *is*
        the credential. This is the one that was leaking on every halt."""
        configure(level="DEBUG", fmt="json")
        token = "111111111:AAFnotarealtokennotarealtokennotareal"
        sink = TelegramAlertSink(
            token,
            "12345678",
            client=_client(lambda request: httpx.Response(200, json={"ok": True})),
        )
        written = self._stdlib_log_of(lambda: sink.send(_alert()))
        assert token not in written
        assert "AAFsecret" not in written

    def test_httpx_does_not_log_the_ntfy_topic(self) -> None:
        """Same vector, other transport: the topic is a capability in both
        directions — reading the alerts and forging one that says all clear."""
        configure(level="DEBUG", fmt="json")
        topic = "atp-e6f1c0de5b7a4d9f8c3b2a1e0d9c8b7a"
        sink = NtfyAlertSink(
            "https://ntfy.sh",
            topic,
            client=_client(lambda request: httpx.Response(200)),
        )
        written = self._stdlib_log_of(lambda: sink.send(_alert()))
        assert topic not in written

    def test_a_debug_log_level_does_not_reintroduce_it(self) -> None:
        """DEBUG is what an operator raises the level to mid-incident, which is
        also when they are most likely to be pasting output into a chat. The
        silence has to be set on the logger itself rather than inherited, or it
        is only ever as strong as whatever the root happens to be.

        Asserting on `.level` and not `getEffectiveLevel()` on purpose: the
        effective level walks up to the root, and under pytest the root sits at
        WARNING anyway, so that version of this test passed with the fix
        removed entirely.
        """
        configure(level="DEBUG", fmt="json")
        assert logging.getLogger("httpx").level >= logging.WARNING

    def test_the_alert_is_still_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Silencing httpx must not cost the operator the record that the alert
        went out — `alert.sent` is what they grep for, and it carries the key
        and the severity that httpx's line never did.

        Read off stdout rather than through `capture_logs`, because those are
        two different streams: `configure` leaves structlog on its own
        `PrintLogger`, so what an operator actually sees is this, and the
        event list the other tests assert on would not show whether the record
        reached anywhere at all.
        """
        token = "111111111:AAFnotarealtokennotarealtokennotareal"
        configure(level="DEBUG", fmt="json")
        sink = TelegramAlertSink(
            token,
            "12345678",
            client=_client(lambda request: httpx.Response(200, json={"ok": True})),
        )
        sink.send(_alert())
        written = capsys.readouterr().out
        assert "alert.sent" in written
        assert "halt.global.all.data_feed_lost" in written
        assert token not in written


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)
