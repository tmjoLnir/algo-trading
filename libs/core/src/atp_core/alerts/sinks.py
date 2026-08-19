"""Where alerts actually go.

Two implementations and a factory. `NtfyAlertSink` is the one that reaches a
phone; `LoggingAlertSink` is what you get when nothing is configured, and is a
real sink rather than a no-op so that an unconfigured platform is loud in its
logs instead of silently un-alerted.

**ntfy first, and the port is why that is a small decision.** It needs no
account, the client is one HTTP POST, it has iOS and Android apps, and it can
be self-hosted — which matters here, because the alternative to self-hosting is
trusting a public server with the knowledge that your trading system just
stopped. Pushover, Telegram and a Twilio SMS are each one class implementing
`AlertSink`, and none of them would change a line outside this module.

**A topic on a public ntfy server is a capability, not a name.** Anyone who
knows it can read your alerts and publish fake ones. So: generate a long random
topic, keep it in the SOPS bundle with the other secrets (docs/DEPLOYMENT.md),
and prefer a self-hosted server with a token if the alerts matter. The rule in
`ports.py` about never putting the book in an alert exists mostly because of
this paragraph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from atp_core.alerts.ports import Alert, AlertSink, Severity
from atp_core.logging import get_logger

if TYPE_CHECKING:
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

_LOG_LEVEL = {
    Severity.INFO: "info",
    Severity.WARNING: "warning",
    Severity.CRITICAL: "critical",
}


class LoggingAlertSink:
    """Writes the alert to the log and nothing else.

    The default, and deliberately not a no-op. An operator who has not set
    `ALERT_NTFY_TOPIC` should be able to see in the log exactly what *would*
    have been sent, and grep for `alert.sent` to find out whether the thing they
    are debugging tried to tell them about itself.
    """

    def send(self, alert: Alert) -> None:
        getattr(log, _LOG_LEVEL[alert.severity])(
            "alert.logged",
            key=alert.key,
            title=alert.title,
            body=alert.body,
            delivered=False,
            msg="no alert transport configured — set ALERT_NTFY_TOPIC to reach a phone",
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
            log.error(
                "alert.send_failed",
                key=alert.key,
                severity=alert.severity.value,
                error=str(exc),
                msg="THE ALERT DID NOT GO OUT — the event it describes still did",
            )
            return

        log.info("alert.sent", key=alert.key, severity=alert.severity.value)


def build_alert_sink(settings: Settings, *, client: httpx.Client | None = None) -> AlertSink:
    """The sink this deployment's configuration asks for.

    One place that decides, so the worker, the API and `scripts/halt.py` cannot
    drift into alerting differently. An empty topic is not an error: alerting is
    opt-in, and the unconfigured state is the logging sink saying so on every
    alert rather than a process that refuses to start.
    """
    if not settings.alert_ntfy_topic:
        return LoggingAlertSink()
    return NtfyAlertSink(
        settings.alert_ntfy_base_url,
        settings.alert_ntfy_topic,
        token=settings.alert_ntfy_token,
        timeout_seconds=settings.alert_timeout_seconds,
        client=client,
    )
