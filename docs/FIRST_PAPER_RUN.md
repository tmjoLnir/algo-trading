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

`RUNBOOK.md` says *"HALT. Dashboard, top right."* **That does not exist yet.**
The dashboard is Phase 5 and `POST /api/v1/risk/halt` still raises
`NotImplementedError`. Before placing a single order, know which of these you
will use:

**1. Engage the kill switch** — stops new orders across every process, leaves
positions and their broker-side stops alone. This is the halt the runbook means.

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

**3. Flatten** — there is no operator path for this yet
(`/api/v1/risk/flatten-all` is also a stub). Use Alpaca's own web UI. Halting is
not flattening: halting stops new risk, flattening realises P&L.

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

---

## Stage 1 — up, ingesting, and deliberately not trading

Leave `WORKER_STRATEGY` unset. This stage proves the data path before anything
can place an order.

```bash
make up
docker compose logs -f worker
```

Expect, in order:

| Event | Means |
|---|---|
| `worker.starting` `run_mode=paper` | not live — check this every time |
| `worker.not_trading` | `WORKER_STRATEGY is unset` — the opt-in is working |
| `worker.ready` `trading=False` | the locks held |
| `data.stream.started` | the market-data socket is up |

Then confirm data is actually moving — the feed being *connected* and the feed
*delivering* are different observations:

```bash
uv run python scripts/status.py --no-broker
```

It prints halts, quote freshness against `RISK_MAX_QUOTE_AGE_SECONDS` — the
same budget `StaleDataRule` refuses orders on — and the latest stored bar per
symbol. `--no-broker` keeps it to local state, so it needs no credentials.

**Do not proceed** until quotes are landing in Redis and bars are landing in the
hypertable. A strategy started on a stale cache will be refused by
`StaleDataRule` and you will spend the session debugging the wrong layer.

---

## Stage 2 — one strategy, one symbol, smallest size

Set in `.env`:

```bash
WORKER_SYMBOLS=SPY
WORKER_STRATEGY=sma_crossover
WORKER_SIZING_METHOD=fixed_qty     # not risk_pct, for the first run
WORKER_SIZING_VALUE=1              # one share
WORKER_STOP_TYPE=atr
WORKER_STOP_MULTIPLIER=2
```

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
