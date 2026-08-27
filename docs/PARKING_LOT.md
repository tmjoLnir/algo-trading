# Parking lot

Known defects in things that are **already built**, deliberately deferred, with
enough context to pick each one up cold.

## How this file is maintained

This is not a third roadmap. `docs/ROADMAP.md` records what has and has not been
built; its *Later* section lists features nobody has started, and *Explicitly out
of scope* lists features this platform will not have. Neither has a place for
"this shipped, it is wrong in a known way, and we chose not to fix it yet" —
which is what lands here.

An entry earns its place by being **discovered and diagnosed**, not merely
suspected. If nobody has established that the defect is real, it is not parked;
it is a hunch, and it belongs in an issue. Each entry states what is wrong, how
it was found, what it costs while it stays, and what fixing it would take —
because the whole value of parking something is that the next person does not
have to rediscover it.

Conventions:

- **An entry leaves when the work lands**, deleted in the same diff as the fix,
  the way a roadmap box is ticked in the same diff as the work (`CLAUDE.md` §6).
  An entry describing a defect that no longer exists is worse than no entry.
- **An entry that turns out not to be a defect leaves too**, with the correction
  written into the commit message rather than left as a tombstone here.
- **Parking is a decision, not a backlog.** Anything here was deferred for a
  stated reason. If the reason has expired, say so in the entry rather than
  leaving the original justification standing.

---

## Nothing is parked

Every entry this file held has been cleared:

| Was | Landed in |
|---|---|
| A backtest's money never reaches the dashboard | `backtest_runs.totals`, ADR 0019 |
| The backtest equity curve is stamped a day late | ADR 0018, migration `c5e9a03b1f47` |
| mypy does not check the tests | `mypy libs apps tests`, in `make typecheck` and CI |

An empty parking lot is a statement, not an absence: it says that nothing
shipped is known to be wrong and deferred. It is not a claim that nothing is
wrong — an undiagnosed defect is not parked, it is undiscovered, and the rules
above say where each of those belongs.
