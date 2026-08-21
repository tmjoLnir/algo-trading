"""Where alerts actually go.

Two transports that reach a phone, a fallback, a fan-out, and a factory.

- `NtfyAlertSink` — no account, one HTTP POST, apps on both platforms, and
  self-hostable, which matters when the alternative is telling a public server
  that your trading system just stopped.
- `TelegramAlertSink` — a bot messaging the operator's own chat. Nothing to
  install for anyone who already has Telegram, and delivery is a real service's
  problem rather than a topic nobody authenticates.
- `LoggingAlertSink` — what you get with nothing configured. A real sink rather
  than a no-op, so an unconfigured platform is loud in its logs instead of
  silently un-alerted.
- `FanOutAlertSink` — both at once, when both are configured.

Adding a third is one class and one branch in `build_alert_sink`; Pushover and
a Twilio SMS would each look like the two above and change nothing outside this
module. That is the port doing its job.

**Both transports are addressed by a credential, and neither is guessable-safe.**
A topic on a public ntfy server is a capability: anyone who knows it reads your
alerts and can publish fake ones. A Telegram bot token is worse — it *is* the
bot, and it travels in the URL path. Both live in the SOPS bundle
(docs/DEPLOYMENT.md), and nothing here logs either, including on the failure
paths. The rule in `ports.py` about never putting the book in an alert exists
mostly because of this paragraph.

That last claim used to be a hope rather than a mechanism, and it was false.
Because the credential is *in the URL*, and `httpx.HTTPStatusError` quotes the
URL in its message, `error=str(exc)` printed the topic and the bot token on
every 4xx — which is exactly what a wrong or revoked credential returns. It is
`_describe` that makes the claim true now, and the tests cover the status codes
rather than only the transport error that hid it.

It was false a second time, and worse. `_describe` guards what *this module*
writes; nothing guarded what `httpx` writes about the request underneath it.
httpx logs `HTTP Request: POST <url> "HTTP/1.1 200 OK"` at INFO, so at the
default `ATP_LOG_LEVEL` the bot token went into the log on every **successful**
alert — not the rare 4xx this module's own scrubbing was built for, but every
halt, every all-clear, forever. It survived a test file full of scrub
assertions because those read structlog events via `capture_logs` and this
record is written by a library to the stdlib stream, where nothing was looking.
`logging._silence_url_logging_libraries` is what closes it, and the test that
would have caught it reads the stdlib stream rather than the event list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from atp_core import metrics
from atp_core.alerts.ports import Alert, AlertSink, Severity
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from atp_core.config import Settings

log = get_logger(__name__)

#: ntfy's priority scale. CRITICAL maps to 5 ("urgent"), which on both mobile
#: apps is the level allowed to bypass a silenced phone — that is the whole
#: point of the level, and using anything lower for a trading halt would make
#: the alert arrive exactly when it is already too late to matter.
_NTFY_PRIORITY = {Severity.INFO: "3", Severity.WARNING: "4", Severity.CRITICAL: "5"}

#: Rendered as an icon next to the notification. Recognisable at a glance from a
#: lock screen is the entire requirement.
_NTFY_TAGS = {
    Severity.INFO: "information_source",
    Severity.WARNING: "warning",
    Severity.CRITICAL: "rotating_light",
}

#: Telegram has no priority scale, so the only lever is whether the message
#: makes a sound. INFO is delivered silently — an all-clear at 02:00 is worth
#: having in the history and is not worth waking anybody for; anything that
#: stopped trading rings.
_TELEGRAM_SILENT = {Severity.INFO: True, Severity.WARNING: False, Severity.CRITICAL: False}

#: Prefixed to the message text. Telegram shows no icon of its own, and the
#: first character is what the operator triages on from a notification shade.
_TELEGRAM_PREFIX = {
    Severity.INFO: "\u2139\ufe0f",
    Severity.WARNING: "\u26a0\ufe0f",
    Severity.CRITICAL: "\U0001f6a8",
}

_LOG_LEVEL = {
    Severity.INFO: "info",
    Severity.WARNING: "warning",
    Severity.CRITICAL: "critical",
}


def _describe(exc: Exception, *secrets: str) -> str:
    """What went wrong, in words that carry no credential.

    **`httpx.HTTPStatusError` puts the request URL in its message**, and for
    both transports here the URL *is* the secret — the ntfy topic and the
    Telegram bot token each live in the path. So `str(exc)` on a 401 from a
    revoked token printed that token into the log, on the one path an operator
    is most likely to be reading. That is the failure this function exists for,
    and it went unnoticed because the tests covered a transport error (whose
    message is the socket's, with no URL in it) and Telegram's `ok: false` —
    but never an HTTP status, which is what a wrong credential actually returns.

    Two layers, because one is a judgement and the other is a guarantee. The
    status line is rebuilt from the response rather than taken from httpx's
    prose, which is both safe and more useful in a log than a sentence ending
    in a link to MDN. The scrub then runs over whatever came out regardless of
    exception type, so a future httpx that starts quoting URLs in transport
    errors, or a body echoed back by a proxy, cannot reintroduce this.

    The scrub is unconditional and not length-guarded. A one-character topic
    would mangle the message — and a one-character topic is the case where
    leaking it matters most, so a readable log is the wrong thing to protect.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        reason = f"HTTP {exc.response.status_code} {exc.response.reason_phrase}".strip()
    else:
        reason = str(exc)
    return _scrub(reason, *secrets)


def _scrub(text: str, *secrets: str) -> str:
    """Replace every credential with `***`, wherever in `text` it appears."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


class LoggingAlertSink:
    """Writes the alert to the log and nothing else.

    The default, and deliberately not a no-op. An operator who has configured no
    transport should be able to see in the log exactly what *would* have been
    sent, and grep for `alert.sent` to find out whether the thing they are
    debugging tried to tell them about itself.
    """

    def send(self, alert: Alert) -> None:
        # Counted as a failure, which is the honest label: nothing left this
        # process and no phone rang. An unconfigured platform showing zero
        # under both `sent` and `failed` would read as "nothing needed
        # alerting" — the one conclusion that is never true here.
        metrics.alert_failed("logging")
        getattr(log, _LOG_LEVEL[alert.severity])(
            "alert.logged",
            key=alert.key,
            title=alert.title,
            body=alert.body,
            delivered=False,
            msg=(
                "no alert transport configured — set ALERT_NTFY_TOPIC, or "
                "ALERT_TELEGRAM_TOKEN and ALERT_TELEGRAM_CHAT_ID, to reach a phone"
            ),
            **alert.context,
        )


class NtfyAlertSink:
    """POSTs the alert to an ntfy topic. Never raises, never blocks for long.

    Synchronous `httpx`, because `KillSwitch.engage` is synchronous (see
    `ports.AlertSink`). The call happens *after* the halt is durable in Redis,
    so the worst case is that the caller waits `timeout_seconds` for a push
    service before returning from an action that has already taken effect — a
    halt cannot be delayed into not happening, only its notification can be.

    That is also why this is a plain blocking call rather than a background
    queue. A queue would remove the wait at the cost of a thread, a shutdown
    flush and a class of bug where the process exits with the alert still in it.
    If the wait ever becomes the problem, the queue goes *behind* this class and
    nothing else changes.
    """

    def __init__(
        self,
        base_url: str,
        topic: str,
        *,
        token: str = "",
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not topic:
            raise ValueError("an ntfy sink needs a topic — use LoggingAlertSink instead")
        self._url = f"{base_url.rstrip('/')}/{topic}"
        # Kept apart from the URL so the failure path can scrub it by value —
        # a credential is only removable from a message if you still hold it.
        self._topic = topic
        self._token = token
        self._timeout = timeout_seconds
        self._client = client

    def send(self, alert: Alert) -> None:
        headers = {
            "Title": alert.title,
            "Priority": _NTFY_PRIORITY[alert.severity],
            "Tags": _NTFY_TAGS[alert.severity],
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            client = self._client or httpx.Client(timeout=self._timeout)
            try:
                response = client.post(self._url, content=alert.body.encode(), headers=headers)
            finally:
                # Only ours to close. An injected client belongs to the caller,
                # and closing it here would break the second send through it.
                if self._client is None:
                    client.close()
            response.raise_for_status()
        except Exception as exc:
            # Swallowed on purpose — `AlertSink.send` promises not to raise, and
            # the action this describes has already happened. The URL is not
            # logged: it contains the topic, which is the capability.
            metrics.alert_failed("ntfy")
            log.error(
                "alert.send_failed",
                key=alert.key,
                severity=alert.severity.value,
                error=_describe(exc, self._topic, self._token),
                msg="THE ALERT DID NOT GO OUT — the event it describes still did",
            )
            return

        metrics.alert_sent("ntfy", alert.severity)
        log.info("alert.sent", key=alert.key, severity=alert.severity.value)


class TelegramAlertSink:
    """Sends the alert as a message from a bot to one chat. Never raises.

    Set up with `@BotFather` (`/newbot`) for the token, then message the bot
    once and read the chat id from
    `https://api.telegram.org/bot<TOKEN>/getUpdates` — a bot cannot start a
    conversation, so that first message from the operator is what makes delivery
    possible at all. docs/DEPLOYMENT.md has it as steps.

    Why it is here alongside ntfy rather than instead of it: an operator who
    already lives in Telegram gets alerts without installing anything, and the
    two can run together so that one service having a bad day is not the same as
    having no alerting (`FanOutAlertSink`).

    **`ok: false` arrives with HTTP 200.** Telegram reports most application
    errors — a revoked token, a chat the bot was removed from, a malformed id —
    in the JSON body of a perfectly successful response. Checking the status
    code alone means a bot that was deleted months ago looks like it is still
    delivering every halt, which is the worst failure this class could have:
    silent, and only discovered on the day it mattered.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("a telegram sink needs both a bot token and a chat id")
        # The token sits in the URL path, so this string is a credential in its
        # entirety and is never logged — see the failure path below.
        self._url = f"{base_url.rstrip('/')}/bot{token}/sendMessage"
        # As above: held so the failure path can remove it from a message, not
        # because anything else reads it.
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout_seconds
        self._client = client

    def send(self, alert: Alert) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": f"{_TELEGRAM_PREFIX[alert.severity]} {alert.title}\n\n{alert.body}",
            "disable_notification": _TELEGRAM_SILENT[alert.severity],
        }
        # Deliberately no parse_mode. Telegram would then interpret markup in
        # the body, and the body carries operator-supplied text — `halt.py
        # --detail` — so an unbalanced asterisk in a hurried note would make the
        # message fail to send at exactly the wrong moment.
        try:
            client = self._client or httpx.Client(timeout=self._timeout)
            try:
                response = client.post(self._url, json=payload)
            finally:
                if self._client is None:
                    client.close()
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            metrics.alert_failed("telegram")
            log.error(
                "alert.send_failed",
                transport="telegram",
                key=alert.key,
                severity=alert.severity.value,
                error=_describe(exc, self._token, self._chat_id),
                msg="THE ALERT DID NOT GO OUT — the event it describes still did",
            )
            return

        if not body.get("ok", False):
            # Counted alongside the transport failure above and not separately:
            # the whole point of reading the body is that `ok: false` on an
            # HTTP 200 *is* a failed delivery, and a metric that told them apart
            # would invite somebody to alert on only one of them.
            metrics.alert_failed("telegram")
            # `description` is Telegram's own words about the failure and
            # carries neither the token nor the chat id.
            log.error(
                "alert.send_failed",
                transport="telegram",
                key=alert.key,
                severity=alert.severity.value,
                error=_scrub(
                    str(body.get("description", "telegram reported ok=false")),
                    self._token,
                    self._chat_id,
                ),
                msg="THE ALERT DID NOT GO OUT — the event it describes still did",
            )
            return

        metrics.alert_sent("telegram", alert.severity)
        log.info("alert.sent", transport="telegram", key=alert.key, severity=alert.severity.value)


class FanOutAlertSink:
    """Every configured transport gets the alert, and one failing does not stop
    the rest.

    The reason this exists rather than the factory picking a winner: an operator
    who has configured two transports has said they want two, and the usual
    reason for wanting two is that a single push service being down should not
    be the same as having no alerting. Quietly using one of them would be a
    surprise discovered during an incident.

    Failures are isolated per sink even though `AlertSink` says implementations
    must not raise, for the reason `_send_alert` in `risk.killswitch` gives:
    the contract is with code that might not honour it, and here the cost of
    being wrong is the second transport never being tried.
    """

    def __init__(self, sinks: Sequence[AlertSink]) -> None:
        if not sinks:
            raise ValueError("a fan-out sink needs at least one sink to fan out to")
        self._sinks = tuple(sinks)

    def send(self, alert: Alert) -> None:
        for sink in self._sinks:
            try:
                sink.send(alert)
            except Exception as exc:
                log.error(
                    "alert.sink_raised",
                    sink=type(sink).__name__,
                    key=alert.key,
                    error=str(exc),
                    msg="continuing to the other transports",
                )


def build_alert_sink(settings: Settings, *, client: httpx.Client | None = None) -> AlertSink:
    """The sink this deployment's configuration asks for.

    One place that decides, so the worker, the API and `scripts/halt.py` cannot
    drift into alerting differently. Nothing configured is not an error:
    alerting is opt-in, and the unconfigured state is the logging sink saying so
    on every alert rather than a process that refuses to start.

    Configuring both transports sends to both rather than picking one. Two
    configured transports is a request for two, and the usual reason to want
    two is that neither service is owed that much trust.
    """
    sinks: list[AlertSink] = []
    if settings.alert_ntfy_topic:
        sinks.append(
            NtfyAlertSink(
                settings.alert_ntfy_base_url,
                settings.alert_ntfy_topic.get_secret_value(),
                token=settings.alert_ntfy_token.get_secret_value(),
                timeout_seconds=settings.alert_timeout_seconds,
                client=client,
            )
        )
    if settings.alert_telegram_token and settings.alert_telegram_chat_id:
        sinks.append(
            TelegramAlertSink(
                settings.alert_telegram_token.get_secret_value(),
                settings.alert_telegram_chat_id,
                base_url=settings.alert_telegram_base_url,
                timeout_seconds=settings.alert_timeout_seconds,
                client=client,
            )
        )

    if not sinks:
        return LoggingAlertSink()
    if len(sinks) == 1:
        return sinks[0]
    return FanOutAlertSink(sinks)
