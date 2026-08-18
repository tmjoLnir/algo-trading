"""What the platform says about itself while it is running — requirement #7.

One snapshot, built by the worker at the end of every evaluation and served
verbatim by the API. The package exists because the *producer* and the
*consumer* are different processes: the worker holds the live book and the API
holds the browser, and the thing they agree on has to live somewhere neither of
them owns.

Why a snapshot at all, rather than the API assembling the screen itself: it
could. It can reach the order table, the position snapshots, the quote cache
and the kill switch. But it would be recomputing equity from its own reads at
its own instant, and the worker is simultaneously computing equity from its own
book at a different one. Two answers to "what is my equity" is precisely the
failure the single aggregate endpoint exists to prevent (docs/DASHBOARD.md, "One
aggregate endpoint, not six parallel requests"). So the book is computed once,
where it is authoritative, and everything downstream serves that.

What is deliberately *not* in here: the run mode, whether the market is open,
and the active halts. The API answers those from its own configuration, its own
calendar and the kill switch directly — because each of them must still be
correct when the worker is dead, and a halt banner sourced from a snapshot
published by a process that has stopped publishing would say "not halted"
exactly when it matters most.
"""

from atp_core.dashboard.ports import SnapshotStore
from atp_core.dashboard.snapshot import (
    AccountSummary,
    LiveSnapshot,
    OrderSummary,
    PositionSummary,
    SignalSummary,
    build_snapshot,
    decode_snapshot,
    encode_snapshot,
)

__all__ = [
    "AccountSummary",
    "LiveSnapshot",
    "OrderSummary",
    "PositionSummary",
    "SignalSummary",
    "SnapshotStore",
    "build_snapshot",
    "decode_snapshot",
    "encode_snapshot",
]
