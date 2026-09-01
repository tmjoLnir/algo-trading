# 23. The worker's trading configuration is a row the dashboard writes

**Status:** Accepted · 2026-09-01

## Context

Ten settings decided what a worker traded: the watchlist, the strategy and its
parameters, how orders were sized, the protective stop and its period, the feed
watchdog, and whether an unattended loop might place live orders. All ten were
environment variables read once by `Settings` at import.

They are the only settings in this platform that an operator changes while it is
*running*. Every other one — the database URL, the broker credentials, the run
mode, the metrics listener — is a property of a deployment, and changing it is a
deploy. These are decisions about trading, revised in response to what trading
did, and an environment variable was the wrong home for such a thing in three
separate ways:

- **Reach.** Widening a stop or adding a symbol required shell access to the
  host, an editor and a restart. The dashboard could show a book it had no way
  to influence.
- **Provenance.** `.env` records no author and no timestamp. After a bad week,
  "who moved risk-per-trade to 2%, and when" was answerable only by asking
  people. The audit log — which records who halted trading, who cancelled an
  order and who flattened the book — could not see the setting that decided how
  large every one of those positions was.
- **Explanation.** The API cannot read another process's environment. So every
  screen that wanted to explain why nothing was trading had to say "check
  `WORKER_STRATEGY`" and hope, which on this platform is the *expected* first
  experience: an unset strategy is the default and a silent worker is the
  correct behaviour.

## Decision

**The ten move to a single `worker_config` row, written only by
`PUT /api/v1/worker/config` and edited on the dashboard's Worker tab.**
`WORKER_METRICS_PORT` and `WORKER_METRICS_ADDR` stay in the environment: a
listener's address is what the process *is*, fixed by its container.

Four things follow from that, and each is the part of the decision worth
reviewing.

**The row is read once, at worker start, and the worker publishes what it read.**
A running worker does not watch the table. Rebuilding a strategy, a stop manager
and a market-data subscription underneath a half-finished evaluation, while
holding positions, is not a thing to do quietly — and the subset of fields that
*could* be applied live is not the subset an operator thinks in. So an edit takes
effect at the next start, and the worker publishes its loaded configuration and
revision to Redis ([ADR 0007](0007-the-worker-publishes-the-book.md)'s pattern,
applied to a second thing the worker is the only authority on). The API serves
the saved row and the running report side by side, and the screen states the
difference. A settings page that showed only what it can edit would report a stop
multiplier no process is using.

**Validation lives in the value object, not at either end.** `WorkerConfig`
refuses a bad configuration at construction, and both the API and the worker get
those rules by building one. The alternative — a check in the handler — produces
the specific failure this is written to avoid: a value that saves cleanly and
then kills the worker at its next restart, discovered by an operator who has just
been told the save worked.

**The revision is allocated by the database.** A save upserts and takes
`revision + 1` from the row's own committed value in the same statement, so two
concurrent saves serialise into two distinct revisions. It increments on every
save, including one that changed nothing: "somebody looked at this and pressed
save" is a fact worth keeping, and a counter that only moved on a diff would make
the restart notice depend on what changed rather than on when.

**The third live lock moves too, and arming it costs a password.**
`allow_live_orders` is `docs/SAFETY.md` layer 2a — locks 1 and 2 say this process
may trade real money, this says this unattended loop may place the orders.
Putting it in the database puts it within reach of anything holding a session
cookie, which is a real widening and is accepted with three conditions:

1. a read-only session cannot reach it — refused by the scope rule before the
   handler runs;
2. arming it requires the operator's password *with the request*, the same
   `require_step_up` that guards `/risk/resume` and `/risk/flatten-all`
   ([ADR 0009](0009-authorisation-is-about-the-act.md): a cookie proves somebody
   signed in this morning, not that anybody is at the keyboard now);
3. the change is written to the audit log with its before and after, and logged
   at `CRITICAL`.

Turning it **off** asks for nothing. That asymmetry is not an oversight — it is
the same one `/risk/halt` has, and it is what makes the widening safe: a control
that made stopping harder would be worse than no control.

`ATP_RUN_MODE` and `ATP_ALLOW_LIVE_TRADING` deliberately do not move. Those decide
whether this process may trade real money at all, and putting them behind a web
form would be the whole live ratchet behind one request. `scripts/manage_secrets.py`
still refuses `WORKER_ALLOW_LIVE_ORDERS` in a secrets bundle even though nothing
reads it any more, so a dead key cannot sit in a bundle looking like it works.

## Consequences

- The migration inserts **no row**. An empty table means the defaults, which are
  no watchlist and no strategy — the same posture an unset `WORKER_STRATEGY` had,
  for the same reason. A freshly deployed host trades nothing until somebody says
  what to trade.
- **An existing deployment must copy its values across by hand.** A migration
  cannot read an operator's `.env`. Old keys left in the file are ignored;
  `make check-env` lists them as keys nothing reads.
- The worker now depends on the database *to start deciding whether to trade*. A
  read failure raises rather than falling back to the defaults: silently ignoring
  a configured watchlist and strategy because Postgres blinked is the same class
  of mistake as adopting the broker's book over our own.
- `scripts/preflight.py`, `status.py` and `paper_report.py` read the row rather
  than the environment, which is what keeps them checking the configuration a
  worker would actually boot on.
- One more table to back up, and it is a small one. It carries no secret.

## Alternatives considered

**Leave them in `.env` and show them read-only on the dashboard.** Honest, and it
solves the explanation problem while solving neither of the others. Rejected
because the operator's request was to *change* them there, and a screen that
displays a setting next to the words "edit this on the host" is a worse answer
than no screen.

**Hot-reload the row on a cadence.** Attractive for the four fields that could
take effect without rebuilding anything (sizing, stop, watchdog, the live lock)
and impossible for the two that matter most (strategy, watchlist). A form where
half the fields apply immediately and half wait for a restart is harder to reason
about than one where none do, particularly at the moment somebody is changing
position sizing mid-session. Revisit it if the restart proves to be the thing
that hurts.

**Have the worker restart itself when the revision changes.** Would make a save
take effect on its own, and would mean that pressing Save during market hours
tears down the trading loop, re-adopts the book and re-warms every strategy. That
is a lot to hang off a form submission. The dashboard says a restart is needed
instead, and the operator decides when.

**Put the row in Redis rather than Postgres.** It is what the kill switch does
and it would have been less work. Rejected because this is the record of what
somebody chose: it must survive a flushed cache, and it carries an author and a
revision a post-mortem will want. Redis holds the *report* of what a worker
loaded, which is a fact about a live process and is exactly the thing that should
disappear.

**Keep `WORKER_ALLOW_LIVE_ORDERS` in the environment while moving the other
nine.** The strictest reading of CLAUDE.md §1.8, and it was seriously considered:
`scripts/manage_secrets.py` refuses that key from a bundle precisely so that it
has to be set per-host by hand. Rejected because it would leave one setting in a
file and nine on a screen, which is the arrangement most likely to have somebody
edit the file and believe they have changed something. The step-up, the scope
check and the audit row are what buy that back, and they make arming it a more
deliberate act than editing a file over SSH ever was.
