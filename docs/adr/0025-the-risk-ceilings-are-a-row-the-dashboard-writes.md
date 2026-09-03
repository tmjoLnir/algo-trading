# 25. The account-wide risk ceilings are a row the dashboard writes

**Status:** Accepted · 2026-09-03

## Context

ADR 0023 moved the ten settings that decide *what a worker trades* out of `.env`
and onto a `worker_config` row. It left behind the eight that decide *what this
platform will let itself risk*:

```
RISK_MAX_POSITION_PCT        RISK_MAX_ORDERS_PER_MINUTE
RISK_MAX_GROSS_EXPOSURE_PCT  RISK_MAX_OPEN_POSITIONS
RISK_MAX_DAILY_LOSS_PCT      RISK_MAX_QUOTE_AGE_SECONDS
RISK_DEFAULT_STOP_LOSS_PCT   RISK_DEFAULT_TAKE_PROFIT_PCT
```

Every argument ADR 0023 made applies to these, and one of them applies harder.

- **Reach.** Tightening a position limit after a bad week needed shell access to
  the host, an editor and a restart of two processes.
- **Provenance.** `.env` carries no author and no timestamp. The audit log
  records who halted trading and who flattened the book, and could not see who
  had set the ceiling that decided how large every one of those positions could
  get.
- **Explanation.** The API returns the refusals these ceilings cause — a
  rejection reason on the orders screen naming `max_gross_exposure` — and could
  not read the number that caused it to say what it was.

The harder one is **where the right value is learned**. A database URL is known
before the platform runs. A position limit is not: it is tuned against a book,
in response to what trading actually did, and `.env` is the one place in this
system a book cannot be seen from. Leaving the ceilings there put the decision
furthest from its evidence.

Splitting them from the worker settings also had a cost that only became clear
once ADR 0023 landed: the settings screen showed a stop multiplier an operator
could edit, directly above a position limit they could not, with nothing on the
page explaining why the two behaved differently.

## Decision

**The eight ceilings join the `worker_config` row, saved by the same
`PUT /api/v1/worker/config` request, and are edited in a risk section on the
same screen — now the dashboard's Config tab.**

Four consequences are worth reviewing.

**One row, one save, one revision.** They are nested as `WorkerConfig.risk`
rather than given a table of their own. An operator who widens a stop and lifts
a ceiling in one sitting made one decision, and a single row means one revision,
one audit entry, and one "your worker is running something older than this"
comparison covering all of it. A second table would have needed a second
revision counter and a screen able to explain two of them.

**The rules live in the value object.** `RiskLimits.__post_init__` refuses a
ceiling out of range, and it is the *only* place those rules exist — the API
gets them by constructing the object, and so does the worker when it loads the
row. A ceiling the dashboard accepted and the worker rejected would save cleanly
and then kill the process at its next start, which is the worst of the three
available behaviours. The bounds are typo guards rather than opinions (a
position limit above 100% of the account, leverage above Reg-T's 4×, a stop of a
whole entry price — which is the level zero), with one rule *between* values: a
position limit may not exceed the gross limit, because the tighter one wins and
the operator would otherwise believe they had a limit they do not have.

**Two processes pick up an edit at different moments, and the screen says so.**
The worker builds its `RiskEngine` once at start, so a saved ceiling binds it
only at its next restart — the same restart semantics ADR 0023 established, with
the same revision comparison rendering it. The API builds a router per request,
so a manual order placed from the dashboard is measured against the row as
saved, immediately. Before this change neither half moved until both processes
restarted; the API half is therefore strictly more responsive than it was, which
is an improvement provided nobody assumes the two agree. The settings screen
carries both numbers, and the note under the save button states the difference.

**They do not ask for the password.** `allow_live_orders` does, because it grants
a new capability to an unattended loop (ADR 0009). These bound orders that are
*already* permitted, and a step-up in front of them would make *tightening* a
limit harder than leaving it alone — the direction `/halt` deliberately never
takes. What makes a loosening answerable instead is the audit row, which carries
the field, both numbers and the operator's name.

## Consequences

**`GET /risk/limits` now needs Postgres.** It was previously the one route that
survived every store being down, which was the argument for it existing
separately from `/risk/status`. It still degrades usefully — `/status` needs
Redis for the book *and* Postgres for the day anchor, while this needs one row —
so the failure that takes the book away still leaves an operator able to ask
what the ceilings are. What is gone is surviving a Postgres outage, and nothing
can honestly answer this question during one: the ceilings are a row, and
serving the defaults instead would state numbers nobody set as though somebody
had.

**A backtest reads them at run time.** `scripts/run_backtest.py` and the queue
worker load the saved row rather than a settings field, so a run queued this
morning is judged by the limits the platform was carrying this morning. The
alternative — resolving them once at process start — would judge a run against
whatever the long-lived queue worker booted with days ago.

**An existing `.env` keeps working and is told it is being ignored.** `Settings`
is `extra="ignore"`, so a leftover `RISK_*` line loads and does nothing.
`make check-env` reports each as moved rather than as a typo, naming the tab and
the section, for the reason ADR 0023's ten are reported that way: eighteen lines
saying "nothing reads it" would send an operator looking for a misspelling that
is not there.

**The migration backfills the shipped defaults, not the operator's values.** It
cannot read their file. Anyone who had tuned a ceiling types it into the
dashboard once; the removed block in `.env.example` lists the defaults so the
comparison can be made without digging through git history.

**The tab is renamed from Worker to Config.** The screen stopped being about the
worker when the ceilings landed on it: they bind a manual order typed into this
dashboard while no worker is running at all. Every operator-facing pointer at
"the Worker tab" — in the runbook, the preflight fixes, the worker's own startup
hints — moved with it. ADR 0023's text is left as written, being the record of a
decision taken when that was the name.

## Alternatives considered

**A separate `risk_limits` table.** Cleaner on paper, and rejected because the
screen and the audit trail are what an operator actually uses: two rows means two
revisions, two "pending restart" states and two audit entries for one decision.

**Making them live for the worker too, by re-reading the row each evaluation.**
Rejected for the reason ADR 0023 gives for the rest of the row: limits that
changed underneath a half-finished evaluation are a class of bug that only
appears under load, and a risk chain is the last place to introduce it. The
restart is explicit, and the screen says when one is owed.

**Leaving them in `.env` as "the values too dangerous to put behind a form".**
This is the argument that keeps `ATP_RUN_MODE` and `ATP_ALLOW_LIVE_TRADING`
there, and it does not transfer. Those two decide whether real money can move at
all; a ceiling only ever decides how much of an already-authorised activity is
allowed, and the operator who can already choose the strategy and the position
size can already decide that. What the ceilings needed was not distance from the
operator — it was a record of who moved them.
