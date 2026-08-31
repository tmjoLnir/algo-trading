# 22. The dashboard refreshes when asked, not on a clock

**Status:** Accepted · 2026-08-31

Amends requirement #7, which read "auto-refreshed every 5 minutes". The
requirement is now "review live trades selected by preset rules, refreshed on
demand". Nothing else about #7 changes.

## Context

The dashboard polled `/dashboard/live` every five minutes, and `/positions` and
the equity curve alongside it. The cadence came from the server so it could be
changed in one place, hidden tabs were excluded, and the age of the reading was
displayed so nobody mistook a four-minute-old number for a live one.

The operator does not want it. They read the screen when they choose to and
would rather reload than have a timer decide.

**The load argument for polling does not survive the hosting decision.**
[ADR 0021](0021-the-paper-host-is-the-operators-own-mac.md) put the stack on a
Mac under a desk serving one or two tabs. `/dashboard/live` is one Redis `GET`
of a document the worker already assembled, and `AccountView` omits
`buying_power` specifically so that no dashboard read costs a broker call. One
request per five minutes was never the cost that justified the machinery, and on
this host it is indistinguishable from zero. So the cadence was not buying
anything the operator wanted, and removing it costs nothing on the axis it was
defended on.

**What the cadence was quietly buying was something else, and it is the whole
of this decision.** Every age on the screen was computed during render as
`Date.now() - dataUpdatedAt`. That is only correct if something re-renders, and
the poll was that something. The number advanced as a side effect of fetching.
Remove the poll naively and "Updated 8s ago" freezes at 8s for as long as the
tab is open, every staleness threshold becomes unreachable, and the screen
reassures its reader most confidently at exactly the moment it has stopped
knowing anything.

The same trap sits one level deeper. `book_age_seconds` — how far behind the
*worker* was — is computed by the server and frozen the instant it arrives. A
tab that reads a healthy book and then sits through a four-hour laptop sleep has
a twenty-second number describing a four-hour-old outage. On this host that is
not a hypothetical: `docs/LOCAL_HOSTING.md` §1 calls sleep the one thing that
decides whether a paper week is possible, and a tab left open overnight was one
of the ways it showed up.

## Decision

**No query refreshes on a fixed interval.** The reader re-reads with the
browser's reload or the button on the indicator.

Three things remain automatic, and each is a change in the world rather than a
clock:

1. **`refetchOnWindowFocus`.** Returning to the tab re-reads. Load-bearing now
   rather than a convenience.
2. **`staleTime: 0`** on the live reads, so moving between tabs in the app
   re-reads rather than replaying a cache. It previously sat just under the poll
   interval because the poll drove refreshes; with no poll it would be the only
   thing between a navigation and a stale screen.
3. **A fill or a halt on the WebSocket re-reads the dashboard.** A halt must
   reach the banner without waiting to be asked — a screen whose job is to
   interrupt somebody cannot require them to consult it first. This is also what
   keeps `docs/ROADMAP.md` Phase 5's *Verifiable:* clause about halting without a
   reload true.

**Both ages advance on their own clock.** A one-second ticker
(`useSecondsSince`) lives in the components that render an age, not in the pages,
so one caption does not redraw a position table every second.

**The book's age is displayed as its age now, not its age when read** — the
server's number plus the time since the read. That is a lower bound rather than
an estimate: the worker may have stopped publishing after we read, never before.
It is what keeps the staleness warnings reachable on a screen nothing refreshes.

**`DASHBOARD_REFRESH_SECONDS` becomes `DASHBOARD_STALE_AFTER_SECONDS`**, and the
response field `refresh_seconds` becomes `stale_after_seconds`. It stopped being
a cadence and became the age past which the screen warns. It stays server-side:
"too old to act on" is a judgement about the platform, and a browser constant
would keep reassuring a reader after the operator had decided otherwise.

## Consequences

**The screen is honest about being manual.** The indicator says so in words,
both ages count up continuously, and the thresholds still fire. A reader who
believes a screen is live will not reload it, so the sentence is part of the
safety story rather than a caption.

**An expired session is discovered later.** It used to surface when the poll
came back 401 while nobody was watching; now it surfaces on the next read.
Later, but at the only moment it changes what anyone does.

**Two diagnostics get weaker, and one is a real loss.** `docs/RUNBOOK.md`
distinguished a dropped Redis bridge from a dead platform by "the dashboard
still refreshes every 5 minutes but nothing moves in between". That tell is
gone; the entry now sends the reader to reload and compare. And a tab left open
overnight no longer reports a sleep by itself — the derived book age means it
still reports one *when somebody looks*, which is weaker than it was and
stronger than a frozen screen. `pmset -g log` and `scripts/status.py` remain the
checks that decide it.

**Execution safety is untouched.** The watchdog still halts, the kill switch
still fails closed, positions keep their broker-side stops
(`docs/SAFETY.md` layer 5). This changes what a human sees, not what the
platform does when nobody is looking.

**Backtests keep their polling.** `useBacktests` refetches every few seconds
while a run is queued or running, and stops entirely once all are terminal. A
run changes state within seconds, then never again, with nobody necessarily
watching at the moment it does. That is the same principle applied to a
different axis: do not ask a question whose answer cannot have changed.

## Alternatives considered

**Keep the poll, make it configurable — `DASHBOARD_REFRESH_SECONDS=0` to
disable.** Requirement #7 would have stayed literally true and the operator
could opt out per deployment. Rejected because it keeps two code paths and two
freshness stories alive to serve one operator on one host, and the harder half
of this work — the ticker and the derived book age — is needed either way the
moment anybody sets it to zero. A setting nobody else will use is not worth the
branch.

**Drop the poll and change nothing else.** The smallest diff, and the one that
looks like what was asked for. Rejected: it produces a screen whose every
staleness guard is decorative, which is worse than the screen before the change
and worse than one with no ages at all — those at least do not claim to be
current.

**Also drop `refetchOnWindowFocus` and the fill/halt re-read**, for a screen
that truly only ever changes when asked. Rejected on the halt: the banner exists
to interrupt somebody, and one that waits to be consulted is not a banner.
Window focus went with it because with nothing else automatic it is the only
thing that refreshes a screen somebody walked back to.
