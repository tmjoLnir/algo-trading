"""Redis pub/sub channel names — one registry, several producers, one consumer.

Four processes write to these and exactly one reads them: `apps/api/src/atp_api/
ws.py` forwards whatever arrives to the browsers it is holding sockets for. That
asymmetry is what makes a shared registry worth having. A producer that invents
its own channel string is not a compile error and not a test failure — it is a
message nobody receives, and the symptom is a dashboard that silently stops
updating one card while the rest keeps working.

This is the third application of ADR 0006's reasoning: one definition with
several callers, because the alternative is not two definitions that disagree
loudly but two that disagree quietly.

Nothing here is a durable record. Redis pub/sub has no persistence and no
delivery guarantee, so a subscriber that is down misses everything sent while it
was away, and the dashboard catches up on its next read — which is the
authoritative path, and since ADR 0022 happens when the reader asks rather than
on a timer (docs/DASHBOARD.md, `persistence/events.py`). Anything that must not
be lost is written to the database or to a key *first* and announced here
second.
"""

from __future__ import annotations

#: Top-of-book updates, published by `data.stream.StreamIngestor` per tick.
CHANNEL_QUOTES = "atp:md:quotes"

#: Completed bars, published by the ingestor after the bar is stored.
CHANNEL_BARS = "atp:md:bars"

#: Order-lifecycle events — a fill, most importantly. Published by the strategy
#: runner *after* the fill is booked and the position is protected, never
#: before: a dashboard that learns of a fill earlier than the book does would
#: show a position nothing is yet managing.
CHANNEL_ORDERS = "atp:exec:orders"

#: What a strategy decided and why, published whether or not it became an order.
#: A refused signal is the interesting one — a strategy blocked by a risk rule
#: on every bar looks identical, from the outside, to a strategy with no ideas.
CHANNEL_SIGNALS = "atp:exec:signals"

#: Kill-switch transitions, published by `risk.killswitch.RedisKillSwitch` as it
#: engages and clears. Subscribed unconditionally by every dashboard socket:
#: `ws.py` fans halts out to clients that asked for nothing, because a trading
#: halt is not something to opt into.
CHANNEL_HALTS = "atp:risk:halts"

#: Every channel the API's WebSocket bridge subscribes to. Kept here rather than
#: in the bridge so that adding a producer and forgetting the consumer is one
#: edit rather than two.
DASHBOARD_CHANNELS = (CHANNEL_QUOTES, CHANNEL_BARS, CHANNEL_ORDERS, CHANNEL_SIGNALS, CHANNEL_HALTS)
