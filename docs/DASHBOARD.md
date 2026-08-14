# Dashboard

Requirement #7: review live trades selected by preset rules, auto-refreshed
every 5 minutes.

## The refresh model

Two paths, deliberately:

| | Cadence | Role |
|---|---|---|
| Poll `/dashboard/live` | 5 min | authoritative, consistent snapshot |
| WebSocket | live | ticks, fills, halts between polls |

**One aggregate endpoint, not six parallel requests.** Six fetches produce a
screen assembled from six different instants; on a fast-moving position, a P&L
computed from one snapshot and a price from another simply disagree, and the
reader cannot tell which to trust. One query, one `as_of`.

**The WebSocket is an enhancement, never the source of truth.** A dropped socket
degrades the dashboard to 5-minute freshness rather than leaving it stale
forever. That is why the poll exists even though push is "better".

Implemented in `useLiveDashboard.ts`:

- interval comes from the server's `refresh_seconds` — one place to change it
- no polling in a hidden tab
- refetch on window focus, so alt-tabbing back does not show 5-minute-old data
- `ageSeconds` is displayed, and warns visibly past 1.5× the interval

## Layout priority

Top to bottom, in the order a person needs it:

1. **Run mode banner** — paper or live. Loud, permanent, not dismissible.
2. **Halt banner** — if trading is stopped, nothing else matters first.
3. **Account summary** — equity, day P&L, exposure, leverage.
4. **Equity curve.**
5. **Open positions** — with distance-to-stop.
6. **Signal feed** — what the rules decided and *why*.
7. **Working orders.**
8. **Kill switch** — always visible, never behind a menu.

Positions before signals: what you are exposed to matters more than what the
system is thinking about.

## Rules for this UI

- **Never show a price without its age.** Grey out anything stale.
- **Never render a monetary value from a float.** The API sends Decimals as
  strings; parse with a decimal library for arithmetic. `parseFloat` on a
  balance is a bug.
- **Colour is not the only signal.** Red/green needs a sign or an arrow too.
- **Show `reason` on every signal.** "Why is this trade on?" must be answerable
  without opening a log.
- **Distance-to-stop, not just the stop price.** How close a position is to
  being closed, without arithmetic in the reader's head.
- **Errors keep the last good data on screen**, clearly labelled stale. A blank
  dashboard during an API blip is worse — the user still needs to see what they
  hold.
- **Destructive actions confirm.** Except the kill switch, which must not.

## Conventions

TanStack Query owns all server state — no `useEffect` fetch loops. API types are
generated (`make gen-types`), never hand-maintained.
