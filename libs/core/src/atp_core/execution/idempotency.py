"""Deterministic order identity — what makes a retry safe (CLAUDE.md §1.4).

`client_order_id` is the idempotency key a venue deduplicates on, and the rule
is that the same *intent* must produce the same key however many times it is
reconstructed: from the signal, from a row in the orders table, from a fresh
process after a crash.

`Order`'s default factory mints a random one. That is stable only while the same
Python object is retried — rebuild the order after a timeout and it mints a
fresh key, which is precisely the duplicate-position scenario the rule exists to
prevent (docs/RISK_IMPLEMENTATION_NOTES.md item 4). So the key is derived from
the decision rather than from the object:

    strategy · symbol · side · purpose · the instant the decision was made

**Quantity is deliberately not in the key.** A risk rule may shrink an order on
its way through the chain — `RiskEngine.validate` mutates `order.qty` — and a
key that moved with the quantity would let the shrunk order through as a second,
different order at the venue. That would turn the one control able to *reduce*
an order into the one control able to duplicate it.

**`purpose` is in the key, and it has to be.** Side is not enough to tell two
orders apart: a strategy exiting a long and the same strategy opening a short on
the same bar both produce a SELL, from one strategy, for one symbol, at one
instant. Without a discriminator those are one key, the venue returns the first
order for the second submit, and a reversal silently half-executes — the leg
that did not trade being the one the strategy is now relying on.

Protective children are keyed differently again, on the range of the entry they
cover rather than on a decision instant. See `protective_client_order_id`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from atp_core.domain import Side

#: Kept from the random default this replaces, so an id is recognisably ours in
#: a broker's dashboard and in a log line.
PREFIX = "atp-"

#: 96 bits of digest. Alpaca caps `client_order_id` at 128 characters, so length
#: is not the constraint — collision resistance is, and 96 bits is far past the
#: point where two distinct intents in one account's lifetime could collide.
DIGEST_CHARS = 24

#: ASCII unit separator. Joining on a character that cannot occur in any field is
#: what stops ("AB", "C") and ("A", "BC") hashing to the same key.
_SEP = "\x1f"

#: The `purpose` vocabulary. Two orders can share a strategy, a symbol, a side
#: and a decision instant without being the same order; this is what separates
#: them. `OrderRequest.purpose` defaults to the literal `"entry"` rather than to
#: `ENTRY`, because `domain/` imports nothing from its siblings (CLAUDE.md §2).
ENTRY = "entry"
EXIT = "exit"
FLATTEN = "flatten"
MANUAL = "manual"
STOP_LOSS = "stop_loss"


def _digest(*parts: str) -> str:
    payload = _SEP.join(parts).encode("utf-8")
    return PREFIX + hashlib.sha256(payload).hexdigest()[:DIGEST_CHARS]


def client_order_id(
    *,
    symbol: str,
    side: Side,
    decided_at: datetime,
    strategy_id: str | None = None,
    purpose: str = "entry",
) -> str:
    """The idempotency key for one decision to trade.

    `decided_at` is the instant the decision was made — a signal's `ts`, the bar
    that triggered it, the moment a human clicked submit. Never `Clock.now()` at
    submission time: that is different on every retry, which is the whole defect
    this function exists to fix.

    Two callers that genuinely mean the same thing collapse to one key, and that
    is the feature rather than a limitation. A human double-clicking "buy SPY"
    within the same microsecond has expressed one intent; the venue rejecting
    the second as a duplicate is the correct outcome.

    `purpose` separates orders that share a decision instant but are not the
    same order — an entry and the protective stop placed against it, or a
    manual order and a strategy's order for the same name at the same moment.
    """
    if decided_at.tzinfo is None:
        raise ValueError(
            f"client_order_id needs a tz-aware decided_at (CLAUDE.md §1.2), got {decided_at!r}"
        )
    if not symbol:
        raise ValueError("client_order_id needs a symbol")
    if not purpose:
        raise ValueError("client_order_id needs a purpose")

    # Normalised, not merely accepted. The same instant written in two zones is
    # one instant, and `symbol` is an uppercase ticker by convention — a key
    # that differed on "spy" vs "SPY" would hand the venue two orders for what
    # the strategy meant as one.
    instant = decided_at.astimezone(UTC).isoformat()
    return _digest(strategy_id or "", symbol.upper(), side.value, purpose, instant)


def protective_client_order_id(
    parent_client_order_id: str, purpose: str, covered_from: Decimal, covered_to: Decimal
) -> str:
    """The key for a protective child of an already-keyed entry.

    Derived from the parent, so it is as reproducible as the parent is, and from
    the *range* of the parent's filled quantity this child covers —
    `(covered_from, covered_to]`, both measured cumulatively from the start of
    the entry.

    A range rather than a quantity, and that is not fussiness. An entry for 200
    that fills 100 and then 100 — the ordinary case — has two protective orders
    to place, and keying on the increment gives both of them `100`: one key, so
    the venue returns the first order for the second submit and the second
    tranche is left naked while the router books it as protected. Keying on the
    cumulative total instead breaks on the other case, a child the risk chain
    shrank, where the top-up covers the same total again. The range distinguishes
    both: `(0, 100]` then `(100, 200]` for the partials, `(0, 100]` then
    `(60, 100]` after a shrink to 60.

    Quantity in the key here is the opposite of the rule for entries above, and
    the asymmetry is real: an entry's quantity is an *output* — the sizer
    proposes it and a risk rule may cut it — whereas a protective order is
    defined by which exposure it exists to cover. A retry of a child that was
    refused re-derives the identical range, so it is one order to the venue
    rather than a second stop.

    Both bounds are normalised through `Decimal.normalize`, so `100` and
    `100.00` — one quantity written two ways — do not become two stops against
    one position.
    """
    if not parent_client_order_id:
        raise ValueError("a protective order needs its parent's client_order_id")
    if not purpose:
        raise ValueError("a protective order needs a purpose")
    if covered_from < 0:
        raise ValueError(f"a covered range starts at or after zero, got {covered_from}")
    if covered_to <= covered_from:
        raise ValueError(
            f"a protective order must cover something, got the empty range "
            f"({covered_from}, {covered_to}]"
        )
    return _digest(
        parent_client_order_id,
        purpose,
        str(covered_from.normalize()),
        str(covered_to.normalize()),
    )
