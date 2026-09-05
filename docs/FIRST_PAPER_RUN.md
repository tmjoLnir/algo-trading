# The first paper run

A procedure, not an incident — `RUNBOOK.md` is for when something is already
wrong. This is the deliberate act that Phase 4's *Verifiable:* line asks for:

> a strategy trades the paper account for a week and reconciles clean

Everything in Phase 4 is built and every test passes against fakes. **Nothing in
it has met Alpaca.** That is the gap this closes, and the reason to expect
surprises rather than a clean first attempt: #34 is the standing precedent —
market-data fixtures written from the vendor's documentation disagreed with the
real wire in three ways at once, and a parser that rejected every live quote
still passed 648 unit tests.

---

## Read this part first: how to stop

`RUNBOOK.md` says *"HALT. Dashboard, top right."* **That now exists** — the
button is wired to `POST /api/v1/risk/halt` (#70) and clearing it to
`/risk/resume` (#75). Both are covered by unit tests against a fake switch and
neither has engaged a halt in a real Redis from a real browser, which is one of
the things this run is the first chance to watch.

So there are two paths to the same switch and you should know both, because the
dashboard depends on the API being up and the CLI does not. Before placing a
single order, decide which you will reach for:

**1. Engage the kill switch** — stops new orders across every process, leaves
positions and their broker-side stops alone. This is the halt the runbook means.
From the dashboard, or from the terminal, which works when the API does not:

```bash
uv run python scripts/halt.py engage --by "<your name>" --detail "why"
uv run python scripts/halt.py status          # exits 2 when halted
uv run python scripts/halt.py clear --by "<your name>"
```

`--by` is required on both, because "who stopped trading" and "who decided it
was safe to resume" are the two questions asked afterwards. Engaging is
idempotent and keeps the *original* record, so a second call reports the first
one rather than overwriting who stopped it and when.

Narrow it with `--scope strategy --target <id>` or `--scope symbol --target SPY`
when only one thing is misbehaving. Plain `engage` stops everything, which is
the right reflex.

**2. Stop the worker** — `docker compose stop worker`. SIGTERM is an ordinary
shutdown: it does *not* halt, and it deliberately leaves positions open with
their broker-side stops intact, because liquidating on every restart would turn
a deploy into a taxable event.

**3. Flatten** — `POST /api/v1/risk/flatten-all`, with the confirmation phrase
and the step-up password. It cancels resting orders and closes every position at
the venue. Halt first: this does not halt, and the runner can re-enter within a
tick. Halting is not flattening — halting stops new risk, flattening realises
P&L into whatever the market is offering. The dashboard still has no control for
it, so this is a `curl` (docs/RUNBOOK.md 'Emergency flatten' has the command)
and Alpaca's own web UI remains the path that works when this platform does not.

Layer 8 of `SAFETY.md` applies here and is outside this codebase: **set position
and loss limits in Alpaca's own controls too.** They are the only limits that
still hold when this platform is the thing that is broken.

---

## Preconditions

- Paper credentials in `.env` (`ALPACA_API_KEY` / `ALPACA_API_SECRET`).
  Paper and live use **separate key pairs** — a live key against the paper
  endpoint fails auth, which is a useful accident.
- `ATP_RUN_MODE=paper`. Not `live`. Nothing in this document should be run
  against a live account.
- Bars stored for the symbol you intend to trade, enough to cover the
  strategy's `warmup_bars`:
  ```bash
  uv run python scripts/backfill_bars.py --symbols SPY --start 2021-08-17 --verify
  ```
- A backtest of the same strategy on the same symbol, so you have something to
  compare against. A paper run with nothing to compare it to answers a question
  nobody asked.
  ```bash
  uv run python scripts/run_backtest.py --strategy sma_crossover --symbols SPY
  ```

### Then check them all at once

```bash
make preflight            # or: uv run python scripts/preflight.py
```

Eleven checks in about two seconds, each one a precondition stated above or an
entry from "the things most likely to break first" below. It exits non-zero on
anything that would stop the week producing an answer, and prints the command or
the setting that fixes it.

**Why a script and not just this list.** The input this demonstration needs and
cannot re-run is calendar time. Almost everything on the list presents the same
way when it is missed — the worker comes up, runs its loop, and never fills
anything — and *"a week of no signals is not a week of correct trading"* is the
caveat at the bottom of this page. A week that ends in silence you cannot
attribute is a week spent.

Two of the checks are worth naming because they are the ones that produce that
silence, and neither is visible from a log line until it is too late:

- **Warmup history.** `sma_crossover` needs 51 bars before it will decide
  anything. Thirty stored bars is not an error, it is five days of nothing.
- **A size the position cap refuses.** `risk_pct` at 1% against a 2×ATR stop
  asks for roughly 30% of a $100k account on a ~$97 name, and
  the `max_position_pct` ceiling caps a position at 10% — so `max_position_size`
  refuses every entry. Both numbers are right; they measure different things.
  The preflight prices the first entry through `position_size` — the same call
  the router makes — and says which value would fit.

Run it again with the stack up and the credentials in place: without them the
venue and database checks report `--` (not checked) rather than passing, because
"we did not look" and "we looked and it was fine" are the two things worth never
confusing.

---

## Stage 1 — up, ingesting, and deliberately not trading

Choose no strategy on the dashboard's **Config** tab — which is where these
settings live now, rather than in `.env`. Nothing has been saved on a fresh
install, so this stage needs no setup at all: it proves the data path before
anything can place an order.

```bash
make up
docker compose logs -f worker
```

Expect, in order:

| Event | Means |
|---|---|
| `worker.starting` `run_mode=paper` | not live — check this every time |
| `worker.not_trading` | `no strategy is configured` — the opt-in is working |
| `worker.ready` `trading=False` `halted=False` | the locks held, and nothing is halted |
| `data.stream.started` | the market-data socket is up |

`worker.ready` reports `halted` because it did not, and a worker restarted into
a standing halt announced *"trading sma_crossover with paper money"* three times
while nothing could reach the venue (docs/paper-week/day-1-review.md, F4). If it
reports `halted=True` there is a `worker.ready_while_halted` CRITICAL beside it
naming every scope and the command that clears them. Nothing was ever at risk —
every order passes `KillSwitchRule`, which reads Redis per order and fails
closed — but this is the line an operator reads at 09:45 and believes.

Then confirm data is actually moving — the feed being *connected* and the feed
*delivering* are different observations:

```bash
uv run python scripts/status.py --no-broker
```

It prints halts, quote freshness against the saved `max_quote_age_seconds` — the
same budget `StaleDataRule` refuses orders on, read from the `worker_config` row
the Config tab writes — and the latest stored bar per symbol. `--no-broker`
keeps it to local state, so it needs no credentials.

**Do not proceed** until quotes are landing in Redis and bars are landing in the
hypertable. A strategy started on a stale cache will be refused by
`StaleDataRule` and you will spend the session debugging the wrong layer.

---

## Stage 2 — one strategy, one symbol, smallest size

On the dashboard's **Config** tab, set:

| Field | Value |
|---|---|
| Watchlist | `SPY` |
| Strategy | `sma_crossover` |
| Position sizing | Fixed quantity |
| Sizing value | `1` — one share |
| Protective stop | ATR multiple |
| Stop multiple | `2` |

Then press **Save configuration**. The row is written immediately and the screen
says so; the worker is still running the previous one until it restarts, which
is what the banner at the top of that tab is for. The save is recorded in the
audit log against whoever made it.

`fixed_qty` at one share on purpose. The default is `risk_pct`, which is the
right way to size a real strategy and the wrong way to learn whether the wiring
works — it makes every quantity a function of equity, volatility and the derived
stop, so a surprising number gives you three things to check instead of one.
Switch to `risk_pct` once fills are landing.

```bash
docker compose restart worker
docker compose logs -f worker
```

Expect:

| Event | Means |
|---|---|
| `worker.trading` | the opt-in took; the message names the venue |
| `worker.adopted_broker_state` | **the boot adoption** — see the warning below |
| `execution.reconcile.clean` | our book matches Alpaca's |
| `runner.warmed_up` `bars=N` | history loaded; if `N` is small, check `runner.warmup_short_history` |
| `broker.alpaca.trade_updates_connected` | the account stream authenticated |

`broker.alpaca.trade_updates_connected` is the one to watch hardest. That
handshake is **not** the market-data one — different action, nested credentials,
different key names — and it has never run against a real account. If it never
appears, the stream is where to look, not the strategy.

At any point, `uv run python scripts/status.py` shows the same local view plus
the venue's: account, positions, and working orders. It is read-only and safe
to run during an incident. It cannot show the *runner's* book — that lives in
the worker's memory with no persistence behind it — which is exactly the gap
the order and position repositories close.

### Then wait

`sma_crossover` on daily bars signals rarely. Do not interpret silence as
breakage: `runner.evaluations` climbing with no orders is the expected state
most of the time. Check `worker.ready`'s `responsibilities` list includes
`strategy_runner` and `trade_updates`, and let it run.

---

## What to watch, and what each thing means

**Good:**

- `order.risk_denied` — the chain refusing something. Normal, and the reason
  names the rule. A first run that denies everything usually means sizing:
  `risk_pct` with no derivable stop, or a stale quote.
- `execution.trade_update.filled` — a real print, with the venue's execution id.
- `order.protective_stop_placed` — layer 5 holding.

**Stop and investigate:**

| Event | Why it matters |
|---|---|
| `runner.position_unprotected` | you own something with no stop. `SAFETY.md`'s go-live condition is that this never happens. |
| `execution.reconcile.mismatch` | the book diverged; trading has halted. `RUNBOOK.md` 'Reconciliation mismatch'. |
| `runner.fill_for_unknown_order` | a fill arrived for an order the runner does not know — expected only if something else is trading the account. |
| `runner.evaluation_failed` | three in a row halts the strategy. |
| `worker.book_diverged_after_reconnect` | a fill landed while the stream was down. This is the case the trade-updates reconnect exists for. |
| `BrokerError: unrecognised Alpaca ...` | the wire disagrees with the docs. **Expected at least once** — capture the payload verbatim, that is the finding. |

---

## The things most likely to break first

Ranked by how much of this has actually met a venue, which is none of it:

1. **The trade-updates handshake.** Every frame was shaped from Alpaca's
   documentation. #34 is the precedent for that not being enough.
2. **A status or event name we do not map.** Both maps refuse rather than guess,
   so this surfaces as a loud `BrokerError` naming the string — which is the
   design working, not a regression.
3. **`risk_pct` sizing.** It needs a stop to measure against; the runner derives
   one from ATR when the signal carries none, and if warmup loaded too little
   history there is no ATR to derive from.
4. **Warmup history.** `runner.warmup_short_history` warns per symbol;
   `LiveContext.history` raises for whoever actually needs the missing bars.

---

## Two things this run cannot prove

State them when recording the result, because a tick that overstates what was
shown is worse than no tick.

**Reconciliation across a restart proves something now, but only after the
first one.** The repositories landed, so a worker with a stored book restarts
from *ours* and lets the broker disagree — which is a real check, because the
two views were formed independently. The caveat that remains is narrower: on a
**first** boot, or against a fresh database, there is no stored book, so the
worker adopts the broker's wholesale and that reconciliation is still clean by
construction. Watch which one you got — `worker.restored_book` means the check
was real, `worker.adopted_broker_state` means it was not.

**A week of no signals is not a week of correct trading.** If `sma_crossover`
never crosses, the run demonstrates ingestion, warmup, reconciliation and the
locks — and nothing about fills, stops or P&L. Say which of the four clauses
were actually exercised.

---

## Recording the result

Phase 4's line has four clauses. For the roadmap, say which held and how you
know:

- [ ] a strategy traded the paper account — *how many orders, how many fills?*
- [ ] for a week — *calendar days, and how many sessions it was actually up*
- [ ] and reconciles clean — *clean across a restart, or only within one run?*
- [ ] with stops on every position — *any `runner.position_unprotected`?*

Paste the numbers rather than the conclusion. `ROADMAP.md`'s existing entries
are written that way on purpose: the Phase 1 and Phase 2 lines quote the actual
output, so a later reader can disagree with the interpretation without re-running
anything.

```bash
make paper-report                                   # what the record can answer
uv run python scripts/paper_report.py --logs worker.log --markdown
```

It reads the orders, the refusals and the equity history the worker wrote, and
answers each clause with the counts behind it. `--markdown` emits the block to
paste into `ROADMAP.md`.

**Two of the four clauses have no store behind them, and the report says so
rather than filling them in.** Reconciliation reports `execution.reconcile.clean`
and an unprotected position reports `runner.position_unprotected`; both are log
lines, and there is no table, no audit row and no metric a query can reach. The
audit log is the wrong home for them too — that record exists to attribute an
action to a *person* (ADR 0008) and a reconciliation has no actor.

So without `--logs` those clauses render as `[?]` with the exact grep beside
them, and the command exits non-zero. `[?]` is not `[ ]`: it means *unshown*, not
*shown false*, and it must not be ticked either way. Pass `--logs <file>` and it
counts the markers itself and answers all four.

One more thing the report will not do for you: a clause it marks `[x]` because
nothing filled is marked `[?]` instead. Nothing was ever owned, so SAFETY.md's
layer 5 was never asked to hold, and a green tick there would be the emptiest
kind.
