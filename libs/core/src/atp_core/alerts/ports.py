"""Alerting ports — getting a human's attention away from the screen.

`docs/SAFETY.md`'s go-live checklist has one line about this: *"Alerts reach a
human on a phone, not just a log file."* Everything this platform does when
something breaks — halting, refusing orders, writing `CRITICAL` — assumes
somebody eventually looks. Between 09:30 and 16:00 nobody is looking at a log
file.

**An alert is a notification, never a mechanism.** By the time one is sent the
thing it describes has already happened and is already durable: the halt is in
Redis, the risk checks are already reading it. So a failed alert must never
fail, delay or undo the action that produced it — the same rule `_announce` in
`risk.killswitch` follows for the dashboard, and the same rule ADR 0010 gives
the audit trail. A platform that refused to stop trading because a push service
was down would have its failure modes exactly inverted.

**Alerts carry the fact, not the book.** A body says *what* happened and *why*
— "trading halted, data_feed_lost" — and never a balance, a position, a P&L or
a fill price. Two reasons. The transport is a third party (and on a public ntfy
server, a guessable topic is the only thing in front of it), and a phone
notification renders on a lock screen in a coffee shop. What the numbers are is
a question for the dashboard, which is behind authentication and a VPN. The
alert's job is to make somebody go and look.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Severity(StrEnum):
    """How hard to push, which is a decision about someone's evening.

    Deliberately three, not five. Every level that does not change what the
    recipient does is a level that trains them to ignore the one below it.
    """

    #: Something happened that a human should know about, at their convenience.
    #: Trading resumed; a halt was cleared. No phone should light up at 03:00.
    INFO = "info"
    #: Something is wrong and will need attention, but nothing is unsafe right
    #: now. Reserved rather than used today — every automated halt is CRITICAL.
    WARNING = "warning"
    #: Trading has stopped, or something that guards money has failed. This is
    #: the level that is allowed to override a silent phone.
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Alert:
    """One thing worth waking somebody for.

    `key` exists so a transport can collapse repeats — most push services take
    some form of tag or dedup id. It is not the deduplication *decision*: that
    belongs to whatever detected the event, because only it knows whether this
    is the same outage or a new one. `risk.killswitch` gets that for free, since
    an already-engaged halt returns early and never reaches an alert at all.
    """

    severity: Severity
    #: One line, read on a lock screen. Front-load the subject: "Trading halted"
    #: beats "The ATP platform has halted trading".
    title: str
    #: A few lines at most, and no numbers from the book (see the module
    #: docstring). What happened, why, and what the operator should look at.
    body: str
    #: Stable across repeats of the same condition — `halt.global.data_feed_lost`
    #: rather than one id per occurrence.
    key: str
    #: Anything the transport can render as structured context. Values are
    #: rendered into a notification, so the same rule applies: no book numbers.
    context: dict[str, str] = field(default_factory=dict)


class AlertSink(Protocol):
    """Somewhere an alert goes. One method, deliberately.

    Synchronous, because the thing that most needs to alert is
    `KillSwitch.engage`, which is synchronous and is the most latency-sensitive
    call in the platform. An async sink would mean either an event loop inside
    the halt path or a halt that cannot alert.

    **Implementations must not raise.** Not "should" — a sink that raises breaks
    the promise in this module's docstring, and the caller is entitled to treat
    `send` as infallible. Swallow, log, and return.
    """

    def send(self, alert: Alert) -> None:
        """Deliver, or fail silently having logged. Never raise, never block long."""
        ...
